"""Config-driven entry point; custom video and INT8 PL drivers are plugins."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import cv2

from .engine import GestureApplication
from .frame_io import OpenCvSource, FileSink
from .games import GameHub
from .inference import create_runner, load_factory

ROOT = Path(__file__).resolve().parents[1]


def run(config, root=ROOT, output=None, max_frames=0, no_preview=False, report=None):
    io_config = config.get("video", {"kind": "opencv", "source": "0"})
    source = app = sink = inference_runner = None
    frame_count = 0
    summary = dict(board_verified=False, frames=0, modes=[], inference_backend=config["inference"]["backend"])
    started = time.perf_counter()
    try:
        if io_config.get("kind", "opencv") == "opencv":
            source = OpenCvSource(io_config, root)
        elif io_config["kind"] == "bridge":
            source = load_factory(io_config["factory"])(io_config.get("options", {}))
        else:
            raise ValueError("Unknown video kind")
        inference_runner, names = create_runner(config["inference"], root)
        app = GestureApplication(inference_runner, names, **config.get("application", {}))
        destination = output if output is not None else config.get("output")
        if destination:
            if str(destination) == "auto":
                extension = "processed.png" if getattr(source, "image_source", False) else "processed.avi"
                destination = Path("runs") / datetime.now().strftime("%Y%m%d_%H%M%S") / extension
            path = Path(destination)
            if not path.is_absolute():
                path = Path(root) / path
            sink = FileSink(path, getattr(source, "image_source", False),
                            getattr(source, "fps", 30.0), getattr(source, "camera", False))
        preview = config.get("preview", True) and not no_preview
        while not max_frames or frame_count < max_frames:
            frame = source.read_frame()
            if frame is None:
                break
            processed = app.process_frame(frame)
            # A combined PYNQ bridge displays through the configured PL writer.
            if hasattr(source, "write_frame"):
                source.write_frame(processed)
            if sink is not None:
                sink.write_frame(processed)
            frame_count += 1
            if app.hub.mode not in summary["modes"]:
                summary["modes"].append(app.hub.mode)
            if preview:
                cv2.imshow("PYNQ Gesture Apps", processed)
                if not app.handle_key(cv2.waitKey(1) & 0xFF):
                    break
        summary.update(frames=frame_count, elapsed_s=time.perf_counter()-started,
                       last_frame_timings_ms=app.timings, output=str(sink.path) if sink else None)
        if frame_count == 0:
            raise RuntimeError("Input source returned no frames")
    finally:
        # Every resource gets cleanup even if another driver's close fails.
        errors = []
        for resource in (sink, app if app is not None else inference_runner, source):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(str(exc))
        if not no_preview and config.get("preview", True):
            cv2.destroyAllWindows()
        if errors:
            print("Cleanup errors: " + "; ".join(errors))
            if sys.exc_info()[0] is None:
                raise RuntimeError("Driver cleanup failed: " + "; ".join(errors))
    if report:
        report = Path(report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description="Restored gesture UI on PC/PYNQ PS")
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pc_reference.json")
    parser.add_argument("--source", help="Override OpenCV camera/image/video source")
    parser.add_argument("--mode", choices=("home", "menu") + GameHub.modes)
    parser.add_argument("--backend", choices=("opencv", "ort"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.max_frames < 0:
        parser.error("max-frames must be >=0")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.source is not None:
        config["video"] = dict(kind="opencv", source=args.source)
    if args.mode:
        config.setdefault("application", {})["mode"] = args.mode
    if args.backend:
        config["inference"]["backend"] = args.backend
    run(config, output=args.output, max_frames=args.max_frames, no_preview=args.no_preview, report=args.report)
