"""Small CPU keyword classifier using the teammate's unmodified ONNX assets."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Union

import numpy as np

from .features import MfccFrontend


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class KwsClassifier:
    """One utterance, one inference; no global OpenCV thread changes.

    OpenCV shares the gesture application's existing OpenCV runtime. An explicitly
    requested ONNX Runtime session is constrained to one thread so it cannot
    create a competing CPU thread pool. This class is used by one worker only.
    Assets and thresholds remain the teammate's originals, checked on startup.
    """

    def __init__(self, asset_dir: Union[str, Path], backend: str = "opencv",
                 threads: int = 1) -> None:
        self.asset_dir = Path(asset_dir).resolve()
        self.config = _read_json(self.asset_dir / "config_en8.json")
        calibration = _read_json(self.asset_dir / "thresholds.json")
        provenance = _read_json(self.asset_dir / "PROVENANCE.json")
        self.model_path = self.asset_dir / "ds_cnn_en8.onnx"
        self.model_sha256 = _sha256(self.model_path)
        expected_sha = str(provenance.get("model_sha256", "")).lower()
        if len(expected_sha) != 64 or self.model_sha256 != expected_sha:
            raise ValueError("keyword model SHA256 differs from voice_assets/PROVENANCE.json")
        # No ONNX parser, Torch, YAML or ONNX Runtime dependency is required to
        # read this metadata; exported config and NPZ were verified against ONNX.
        classes = self.config["classes"]
        self.unknown_label = str(classes["unknown_label"])
        self.silence_label = str(classes["silence_label"])
        self.keywords = [str(label) for label in classes["keywords"]]
        self.labels = self.keywords + [self.unknown_label, self.silence_label]
        if (self.labels != calibration.get("labels") or len(set(self.labels)) != len(self.labels)
                or self.labels != provenance.get("labels")):
            raise ValueError("keyword model and calibrated label order do not agree")
        self.thresholds = {str(key): float(value)
                           for key, value in calibration["thresholds"].items()}
        self.boosts = {str(key): float(value)
                       for key, value in calibration.get("boosts", {}).items()}
        if set(self.thresholds) != set(self.labels):
            raise ValueError("a calibrated threshold is required for every label")
        for rules in (self.thresholds, self.boosts):
            if (not set(rules).issubset(self.labels)
                    or any(not np.isfinite(v) or not 0.0 <= v <= 1.0 for v in rules.values())):
                raise ValueError("invalid calibrated probability thresholds")
        with np.load(self.asset_dir / "norm_stats_en8.npz", allow_pickle=False) as stats:
            mean = np.asarray(stats["mean"], dtype=np.float32)
            std = np.asarray(stats["std"], dtype=np.float32)
        self.frontend = MfccFrontend(self.config, mean, std)
        self.mfcc = self.frontend
        self.sample_rate = self.frontend.sample_rate
        self.num_samples = self.frontend.num_samples
        self.input_shape = self.frontend.input_shape
        if self.sample_rate != 16000 or self.num_samples != 24000 or self.input_shape != (1, 1, 40, 151):
            raise ValueError("supplied keyword model requires 16 kHz, 24000 samples and [1,1,40,151]")
        self.backend_name = str(backend).lower()
        if self.backend_name == "onnxruntime":
            self.backend_name = "ort"
        self.threads = max(1, int(threads))
        self.net = None
        self.session = None
        self._closed = False
        if self.backend_name == "opencv":
            import cv2
            # Buffer loading also works in Windows workspaces with Chinese
            # directory names, which some OpenCV filename overloads cannot read.
            model_bytes = np.frombuffer(self.model_path.read_bytes(), dtype=np.uint8)
            self.net = cv2.dnn.readNetFromONNX(model_bytes)
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            # OpenCV 5's default graph engine already runs on CPU and does not
            # support this target setter. Keep the explicit CPU target for 4.x.
            # This compatibility change does not alter scores or thresholds.
            if int(cv2.__version__.split('.', 1)[0]) < 5:
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        elif self.backend_name == "ort":
            if self.threads != 1:
                raise ValueError("keyword ONNX Runtime backend must use threads=1")
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.session = ort.InferenceSession(str(self.model_path), sess_options=options,
                                                providers=["CPUExecutionProvider"])
            inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
            if (len(inputs) != 1 or len(outputs) != 1
                    or tuple(inputs[0].shape) != self.input_shape
                    or tuple(outputs[0].shape) != (1, len(self.labels))):
                raise ValueError("unexpected keyword ONNX input/output contract")
            self.input_name = inputs[0].name
            self.output_name = outputs[0].name
        else:
            raise ValueError("keyword backend must be opencv or ort")
        self.last_mfcc_ms = 0.0
        self.last_network_ms = 0.0
        self.last_timing = {"mfcc_ms": 0.0, "network_ms": 0.0}
        self.metadata = {"sample_rate": self.sample_rate, "num_samples": self.num_samples,
                         "input_shape": list(self.input_shape), "labels": list(self.labels),
                         "model_sha256": self.model_sha256, "backend": self.backend_name,
                         "fft_backend": self.frontend.fft_backend}

    def features(self, audio: np.ndarray) -> np.ndarray:
        if self._closed:
            raise RuntimeError("keyword classifier has been closed")
        begin = time.perf_counter()
        features = self.frontend.features(audio)
        self.last_mfcc_ms = (time.perf_counter() - begin) * 1000.0
        return features

    def predict(self, audio: np.ndarray) -> Dict[str, Any]:
        """Return raw logits/probabilities and timings without command filtering."""
        features = self.features(audio)
        begin = time.perf_counter()
        if self.backend_name == "opencv":
            self.net.setInput(features)
            raw = self.net.forward()
        else:
            raw = self.session.run([self.output_name], {self.input_name: features})[0]
        self.last_network_ms = (time.perf_counter() - begin) * 1000.0
        self.last_timing = {"mfcc_ms": self.last_mfcc_ms, "network_ms": self.last_network_ms}
        logits = np.asarray(raw, dtype=np.float32).reshape(-1)
        if logits.size != len(self.labels) or not np.isfinite(logits).all():
            raise ValueError("invalid keyword network output; expected {} finite logits".format(len(self.labels)))
        shifted = logits - np.max(logits)
        exp = np.exp(shifted)
        probabilities = np.asarray(exp / np.sum(exp), dtype=np.float32)
        return {"logits": logits.copy(), "probs": probabilities,
                "mfcc_ms": self.last_mfcc_ms, "network_ms": self.last_network_ms}

    def select_probabilities(self, probabilities: np.ndarray) -> Dict[str, Any]:
        """Apply the original per-class thresholds and back boost.

        The source evaluator falls back to argmax if no class passes a threshold.
        We retain that diagnostic label but reject it as an event. In particular,
        this is the model's calibrated decision, before the unchanged live
        session confidence gate. Unknown/silence never issue commands.
        """
        probs = np.asarray(probabilities, dtype=np.float32).reshape(-1)
        if (probs.size != len(self.labels) or not np.isfinite(probs).all()
                or np.any(probs < 0) or np.any(probs > 1)
                or not np.isclose(np.sum(probs), 1.0, atol=1e-4)):
            raise ValueError("expected {} normalized finite keyword probabilities".format(len(self.labels)))
        boosted = [index for index, label in enumerate(self.labels)
                   if label in self.boosts and float(probs[index]) >= self.boosts[label]]
        if boosted:
            selected = max(boosted, key=lambda index: float(probs[index]))
        else:
            order = np.argsort(-probs)
            selected = int(order[0])
            for index in order:
                index = int(index)
                if float(probs[index]) >= self.thresholds[self.labels[index]]:
                    selected = index
                    break
        label = self.labels[selected]
        confidence = float(probs[selected])
        threshold = self.thresholds[label]
        accepted = confidence >= threshold
        return {"label": label, "confidence": confidence, "threshold": threshold,
                "accepted": accepted, "is_target": bool(accepted and label in self.keywords),
                "raw_argmax": self.labels[int(np.argmax(probs))]}

    def classify(self, audio: np.ndarray) -> Dict[str, Any]:
        raw = self.predict(audio)
        result = self.select_probabilities(raw["probs"])
        result.update({"probs": raw["probs"].tolist(), "logits": raw["logits"].tolist(),
                       "mfcc_ms": raw["mfcc_ms"], "network_ms": raw["network_ms"],
                       "backend": self.backend_name})
        return result

    def close(self) -> None:
        """Release network/session references; callers must first stop the worker."""
        self.net = None
        self.session = None
        self._closed = True


__all__ = ["KwsClassifier"]
