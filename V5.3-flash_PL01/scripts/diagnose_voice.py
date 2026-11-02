"""Standalone check of THIS project's actual KwsSession, without video or hand NN.

No threshold, gain, model or VAD changes. JSON is always saved; audio is saved
only with --save-audio. WAV replay is explicitly reported as a replay test.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import wave

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_control.session import KwsSession

WINDOW = "Standalone Voice Diagnostic - SPACE start / Q stop"


def audio_levels(audio):
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not audio.size:
        return dict(samples=0, rms_dbfs=-120., peak=0., near_fullscale_fraction=0.)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return dict(samples=int(audio.size), rms_dbfs=max(-120., 20 * np.log10(max(rms, 1e-12))),
                peak=float(np.max(np.abs(audio))),
                near_fullscale_fraction=float(np.mean(np.abs(audio) >= 32760. / 32768.)))


def plain(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def load_voice_config(path, wav=None, prime_offline=False):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    config = deepcopy(document["voice"])
    if not config.get("enabled", False):
        raise ValueError("Voice is disabled in the selected project config")
    if prime_offline and not wav:
        raise ValueError("--prime-offline is only allowed with --wav")
    if wav:
        config["audio"] = {"kind": "wav", "path": str(Path(wav).resolve()), "realtime": False}
        if prime_offline:
            config["prime_offline"] = True
    return config


def prompt_at(elapsed):
    for start, end, word in ((2., 7., "TWO"), (7., 12., "ON"),
                             (12., 17., "OFF"), (17., 22., "BACK")):
        if start <= elapsed < end:
            return "Say " + word + " once; then keep quiet"
    return "Keep quiet"


class ObservedSource:
    """Observe the same source read results, returning each chunk unchanged."""
    def __init__(self, source, save_audio=False):
        self.wrapped = source
        self.save_audio = save_audio
        self.started_at = None
        self.lock = threading.Lock()
        self.latest = audio_levels([])
        self.rows = []
        self.saved = []

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def start(self):
        self.wrapped.start()
        self.started_at = time.monotonic()

    def read(self, timeout=.05):
        chunk = self.wrapped.read(timeout=timeout)
        if chunk is not None:
            levels = audio_levels(chunk.audio)
            levels.update(sequence=int(chunk.sequence), captured_at=float(chunk.captured_at),
                          elapsed=time.monotonic() - self.started_at)
            with self.lock:
                self.latest = levels
                self.rows.append(levels)
                if self.save_audio:
                    self.saved.append(np.asarray(chunk.audio, np.float32).copy())
        return chunk

    def snapshot(self):
        with self.lock:
            return dict(self.latest)


class ObservedClassifier:
    """Log the exact classifier return value without changing its decision."""
    def __init__(self, classifier, source, save_audio=False):
        self.wrapped, self.source = classifier, source
        self.save_audio = save_audio
        self.rows, self.saved = [], []
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def classify(self, audio):
        result = self.wrapped.classify(audio)
        record = plain(result)
        record.update(audio_levels(audio))
        record["elapsed"] = time.monotonic() - self.source.started_at
        with self.lock:
            record["sequence"] = len(self.rows) + 1
            self.rows.append(record)
            if self.save_audio:
                self.saved.append(np.asarray(audio, np.float32).copy())
        return result

    def snapshot(self):
        with self.lock:
            return deepcopy(self.rows)


def capture_metadata(source):
    result = {"sample_rate": 16000, "channels": 1, "capture_dtype": "PCM16",
              "source_class": type(source.wrapped).__name__}
    stream = getattr(source.wrapped, "_stream", None)
    if stream is not None:
        import sounddevice as sd
        device = stream.device
        if isinstance(device, (list, tuple)):
            device = device[0]
        device_info = dict(sd.query_devices(device, "input"))
        result.update(device_index=int(device), device=device_info,
                      host_api=dict(sd.query_hostapis(device_info["hostapi"])),
                      stream_samplerate=float(stream.samplerate),
                      stream_channels=int(stream.channels), stream_dtype=str(stream.dtype),
                      stream_latency_seconds=float(stream.latency))
    return plain(result)


def draw_status(started, elapsed, duration, session, source, decisions, events):
    frame = np.full((570, 960, 3), (28, 30, 32), np.uint8)
    def text(value, y, color=(220, 222, 225), scale=.65):
        cv2.putText(frame, value, (35, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    color, 1, cv2.LINE_AA)
    text("STANDALONE VOICE - SAME PROJECT MODEL AND CONFIG", 45)
    text("No camera / No gesture model / No application navigation", 80, (160, 166, 174), .55)
    if not started:
        text("Press SPACE to start the microphone", 150, (180, 220, 190), .85)
        text("First keep quiet for 2 seconds. Follow each word prompt.", 210)
        text("Speak once per prompt. The microphone gain is not changed.", 250)
        text("Q: close without capture", 490)
        return frame
    stats = session.stats
    text("%.1f / %.1f sec  | VAD ready: %s | listening: %s" %
         (elapsed, duration, stats["ready"], stats["listening"]), 130)
    text(prompt_at(elapsed), 180, (180, 220, 190), .85)
    levels = source.snapshot()
    clip = levels["near_fullscale_fraction"]
    text("RMS %6.1f dBFS   Peak %.3f   Near full-scale %.2f%%" %
         (levels["rms_dbfs"], levels["peak"], 100 * clip), 235,
         (130, 150, 240) if clip else (220, 222, 225))
    cv2.rectangle(frame, (35, 255), (925, 273), (65, 68, 72), -1)
    cv2.rectangle(frame, (35, 255), (35 + int(890 * min(1., levels["peak"])), 273),
                  (140, 170, 150), -1)
    if decisions:
        latest = decisions[-1]
        text("Classifier: %s  %.3f  | raw: %s" %
             (latest["label"], latest["confidence"], latest.get("raw_argmax", "?")), 325)
    observations = stats.get("observations", [])
    if observations:
        latest = observations[-1]
        text("Session: accepted=%s  reason=%s" %
             (latest["accepted"], latest.get("reason", "unknown")), 365)
    if events:
        text("Last delivered event: %s  %.3f" %
             (events[-1]["label"], events[-1]["confidence"]), 405, (180, 220, 190))
    text("Segments %s / Neural calls %s / Delivered events %s" %
         (stats["segments"], stats["nn_calls"], len(events)), 450)
    text("Q: stop and save diagnostic JSON", 505)
    if stats.get("error"):
        text("ERROR: " + str(stats["error"])[:92], 545, (100, 110, 240), .5)
    return frame


def save_wav(path, parts):
    if not parts:
        return
    audio = np.concatenate(parts)
    pcm = np.clip(np.rint(audio.astype(np.float64) * 32768), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm.tobytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "configs/pc.json"))
    parser.add_argument("--seconds", type=float, default=26.)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--prime-offline", action="store_true")
    parser.add_argument("--save-audio", action="store_true")
    parser.add_argument("--report", "--output", type=Path)
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    config_path = Path(args.config).resolve()
    config = load_voice_config(config_path, args.wav, args.prime_offline)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    target = args.report or ROOT / "logs" / ("voice_standalone_" + stamp + ".json")
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    report = dict(schema=1, project_root=str(ROOT), executable=sys.executable,
                  python_version=sys.version, opencv_version=cv2.__version__,
                  config_path=str(config_path), voice_config=config,
                  timestamp=datetime.now().astimezone().isoformat(),
                  scope="fast WAV replay; not live microphone accuracy or real-time latency" if args.wav
                  else "actual microphone and voice session; no UI navigation or gesture inference",
                  audio_saved=bool(args.save_audio), started=False, ready_seen=False,
                  delivered_events=[], error=None)
    session = source = classifier = None
    started = False
    error = None
    seen_decisions = 0
    try:
        session = KwsSession(config, ROOT)
        source = ObservedSource(session.source, args.save_audio)
        classifier = ObservedClassifier(session.classifier, source, args.save_audio)
        session.source, session.classifier = source, classifier
        report["model"] = plain(classifier.metadata)
        report["model"]["model_path"] = str(classifier.model_path)
        report["used_voice_files"] = {}
        for path in ([ROOT / "voice_control" / (name + ".py") for name in
                      ("session", "audio_source", "classifier", "features", "vad")]
                     + [classifier.asset_dir / name for name in
                        ("ds_cnn_en8.onnx", "config_en8.json", "norm_stats_en8.npz",
                         "thresholds.json", "PROVENANCE.json")]):
            report["used_voice_files"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        print("PROJECT ROOT:", ROOT, flush=True)
        print("PYTHON:", sys.executable, flush=True)
        print("VOICE MODEL:", classifier.model_path, flush=True)
        print("VOICE SHA256:", classifier.model_sha256, flush=True)
        print("LABELS:", ", ".join(classifier.labels), flush=True)
        print("REPORT:", target, flush=True)
        print("Model loaded. Microphone starts only after SPACE (or --autostart/--headless).", flush=True)
        if not args.headless:
            cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        start_requested = bool(args.autostart or args.headless or args.wav)
        while True:
            if start_requested and not started:
                session.start()
                started = report["started"] = True
                report["capture"] = capture_metadata(source)
                print("CAPTURE:", json.dumps(report["capture"], ensure_ascii=True), flush=True)
                print("Capture started. Keep quiet for 2 seconds.", flush=True)
            elapsed = time.monotonic() - source.started_at if started else 0.
            decisions = classifier.snapshot()
            while seen_decisions < len(decisions):
                record = decisions[seen_decisions]
                print("CLASSIFIER %d: %s %.3f raw=%s" %
                      (record["sequence"], record["label"], record["confidence"], record.get("raw_argmax")), flush=True)
                seen_decisions += 1
            if started:
                if session.ready and not report["ready_seen"]:
                    report["ready_seen"] = True
                    report["vad_ready_elapsed"] = elapsed
                    print("VAD READY: this means audio warmup completed, not command recognition.", flush=True)
                while True:
                    event = session.poll()
                    if event is None:
                        break
                    event = plain(event)
                    event["elapsed"] = elapsed
                    report["delivered_events"].append(event)
                    print("DELIVERED EVENT: %s %.3f" % (event["label"], event["confidence"]), flush=True)
                if session.error:
                    raise RuntimeError(session.error)
                if session.eof or elapsed >= args.seconds:
                    break
            if not args.headless:
                cv2.imshow(WINDOW, draw_status(started, elapsed, args.seconds, session,
                                               source, decisions, report["delivered_events"]))
                key = cv2.waitKey(20) & 255
                if key in (ord("q"), ord("Q"), 27):
                    break
                if key == 32:
                    start_requested = True
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
            else:
                time.sleep(.002 if args.wav else .02)
    except KeyboardInterrupt:
        report["stopped_by_keyboard"] = True
    except Exception as exc:
        error = report["error"] = str(exc)
        print("VOICE DIAGNOSTIC ERROR:", error, flush=True)
    finally:
        if session is not None:
            try:
                session.stop()
            except Exception as exc:
                error = report["shutdown_error"] = str(exc)
            report["session"] = plain(session.stats)
        if source is not None:
            report["audio_blocks"] = plain(source.rows)
        if classifier is not None:
            report["classifier_decisions"] = classifier.snapshot()
        if args.save_audio and source is not None:
            audio_dir = target.with_suffix("")
            audio_dir.mkdir(parents=True, exist_ok=True)
            save_wav(audio_dir / "microphone_full.wav", source.saved)
            for index, audio in enumerate(classifier.saved):
                save_wav(audio_dir / ("segment_%02d.wav" % index), [audio])
            report["audio_directory"] = str(audio_dir)
        if not args.headless:
            cv2.destroyAllWindows()
        report["note"] = "A loaded model/listening/ready status does not prove recognition. Inspect classifier_decisions, session observations/reasons, and delivered_events separately. Prompt slots are suggestions, not human-verified labels."
        target.write_text(json.dumps(plain(report), ensure_ascii=False, indent=2), encoding="utf-8")
        print("SAVED:", target, flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
