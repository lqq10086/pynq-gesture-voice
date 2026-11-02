"""Frame-in/frame-out PS application, shared by PC, Jupyter and PYNQ drivers."""
from __future__ import annotations
import time
import cv2

from .core import Detection, InteractionState, SoftwarePL, SoftwarePLCompositor
from .games import GameHub
from .inference import preprocess, decode


class GestureApplication:
    def __init__(self, runner, names=("open_palm", "side_palm", "pinch_point"),
                 mode="menu", already_mirrored=False, inference_stride=2,
                 confidence=0.60, pinch_confidence=0.45, iou=0.45,
                 restore_policy="yolov5"):
        if mode not in ("home", "menu") + GameHub.modes:
            raise ValueError("Unknown application mode")
        if inference_stride < 1:
            raise ValueError("inference_stride must be >=1")
        self.runner, self.names = runner, tuple(names)
        self.already_mirrored, self.inference_stride = already_mirrored, inference_stride
        self.postprocess = dict(names=self.names, confidence=confidence,
                               pinch_confidence=pinch_confidence, iou=iou,
                               restore_policy=restore_policy)
        self.pl, self.compositor = SoftwarePL(), SoftwarePLCompositor()
        self.state, self.hub = InteractionState(), GameHub(mode)
        self.detections = []
        self.frame_index = 0
        self.last_time = None
        self.fps = 0.0
        self.show_edges = False
        self.show_boxes = False
        self.timings = {}
        self.last_tensor = self.last_raw = self.last_geometry = None
        self._shape = None

    def process_frame(self, bgr):
        start = time.perf_counter()
        if self.last_time is not None and start - self.last_time >= 0.001:
            instant = min(240.0, 1 / (start - self.last_time))
            self.fps = instant if self.fps == 0 else self.fps * 0.8 + instant * 0.2
        self.last_time = start
        # Copy even already-mirrored input: no mutation of caller-owned DMA frame.
        mirrored = bgr.copy() if self.already_mirrored else cv2.flip(bgr, 1)
        pl_frame = self.pl.process(mirrored)
        height, width = mirrored.shape[:2]
        if self._shape is not None and self._shape != (height, width):
            self.detections = []
            self.state.reset()
            self.frame_index = 0
            latched = self.hub._latched
            self.hub = GameHub(self.hub.mode)
            self.hub._latched = latched
        self._shape = (height, width)
        infer_start = time.perf_counter()
        two_pinches = sum(d.label == "pinch_point" for d in self.detections) >= 2
        inferred = (self.hub.mode == "palm_bounce" or self.hub.resizing or two_pinches
                    or self.frame_index % self.inference_stride == 0)
        if inferred:
            tensor, geometry = preprocess(pl_frame.image)
            raw = self.runner.infer(tensor)
            self.detections = decode(raw, geometry, **self.postprocess)
            self.last_tensor, self.last_raw, self.last_geometry = tensor, raw, geometry
            previous = self.hub.mode
            interaction = self.detections
            if self.hub.mode == "zoom" and self.state.zoom != 1:
                interaction = [Detection(d.label, d.confidence,
                    (*self.compositor._map(d.box[:2], self.state.zoom, width, height),
                     *self.compositor._map(d.box[2:], self.state.zoom, width, height)))
                    for d in self.detections]
            self.hub.observe(interaction, width, height, image=pl_frame.image)
            if self.hub.mode != previous:
                self.state.reset()
            if self.hub.mode == "zoom":
                if self.hub.resizing or any(d.label == "pinch_point" for d in self.detections):
                    self.state._zoom_start_distance = None
                else:
                    self.state.update_detections(self.detections)
        render_start = time.perf_counter()
        if self.hub.mode != "zoom":
            self.state.zoom = 1.0
            self.state.status = "Pinch to choose; release before the next selection"
        result = self.compositor.render(pl_frame, self.detections, self.state,
                                        self.show_edges, self.show_boxes)
        result = self.hub.render(result, self.fps, self.state.zoom)
        self.hub.draw_pointer(result)
        self.frame_index += 1
        end = time.perf_counter()
        self.timings = dict(inferred=inferred, preprocess_frame_ms=(infer_start-start)*1000,
                            inference_postprocess_interaction_ms=(render_start-infer_start)*1000,
                            render_ms=(end-render_start)*1000, total_ms=(end-start)*1000)
        return result

    def handle_key(self, key):
        """Return False on quit, otherwise handle predecessor keyboard controls."""
        if key in (ord("q"), ord("Q"), 27):
            return False
        if key in (ord("r"), ord("R")):
            self.state.reset() if self.hub.mode == "zoom" else self.hub.reset()
        if key in (ord("m"), ord("M")):
            self.hub.mode, self.hub.game = "menu", None
            self.state.reset()
        if key in (ord("e"), ord("E")):
            self.show_edges = not self.show_edges
        if key in (ord("d"), ord("D")):
            self.show_boxes = not self.show_boxes
        return True

    def close(self):
        if hasattr(self.runner, "close"):
            self.runner.close()
