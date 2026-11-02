"""Experimental FP32 simple-AXI-DMA model adapter, not a universal PL runner.

Requires the real accelerator to accept FP32[1,3,384,384] and emit already
decoded FP32[1,9072,7], with matching stream word order and TLAST. An INT8 or
raw-head accelerator must use the application plugin interface instead.
Input is 1769472 bytes: AXI DMA length width must be >=21 bits. No wait() or
unbounded PYNQ stop() is used; owned simple-channel abort uses its driver MMIO
metadata and a bounded status poll. No GPIO/DDR/control addresses are guessed.
"""
import math
import time

import numpy as np

from .inference import INPUT_SHAPE as MODEL_INPUT_SHAPE, NAMES_2, PREDICTION_COUNT
from .pynq_io import flag, free_buffer, load_overlay, resolve_ip


class PynqDmaRunner:
    INPUT_SHAPE = MODEL_INPUT_SHAPE
    OUTPUT_SHAPE = (1, PREDICTION_COUNT, 5 + len(NAMES_2))
    INPUT_BYTES = math.prod(INPUT_SHAPE) * np.dtype(np.float32).itemsize
    OUTPUT_BYTES = math.prod(OUTPUT_SHAPE) * np.dtype(np.float32).itemsize

    def __init__(self, config, overlay=None, allocator=None):
        self.config = dict(config)
        self.timeout_s = self._positive(config.get("timeout_s", 2.0), "timeout_s")
        self.poll_interval_s = self._positive(config.get("poll_interval_s", .001), "poll_interval_s")
        if config.get("dma_mode", "simple") != "simple":
            raise ValueError("Only simple AXI DMA is supported; use a custom plugin for SG/INT8/raw heads")
        for key, expected in (("input_shape", self.INPUT_SHAPE), ("output_shape", self.OUTPUT_SHAPE)):
            if key in config and tuple(config[key]) != expected:
                raise ValueError(key + " must equal " + str(expected))
        for key in ("input_dtype", "output_dtype"):
            if key in config and np.dtype(config[key]) != np.float32:
                raise ValueError("Only native FP32 is supported; replace this runner via plugin for INT8")
        if not flag(config, "output_decoded", True):
            raise ValueError("This adapter requires an already decoded Detect output; raw heads need a custom plugin")
        internal = overlay is None
        self.owned_dma = flag(config, "owned_dma", internal)
        self.overlay = load_overlay(config) if internal else overlay
        self.dma = resolve_ip(self.overlay, config.get("dma_ip"))
        if bool(getattr(self.dma, "_sg", False)) or bool(getattr(self.dma, "_micro", False)):
            raise ValueError("SG/micro DMA is outside this FP32 simple-DMA contract")
        self.send = getattr(self.dma, "sendchannel", None)
        self.recv = getattr(self.dma, "recvchannel", None)
        for name, channel, required in (("send", self.send, self.INPUT_BYTES), ("recv", self.recv, self.OUTPUT_BYTES)):
            if channel is None or not callable(getattr(channel, "transfer", None)):
                raise TypeError(name + " requires the actual simple DMA transfer interface")
            if not hasattr(channel, "idle") or not hasattr(channel, "error"):
                raise TypeError(name + " must expose idle and error for bounded polling")
            capacities = [getattr(channel, "_max_size", None), getattr(self.dma, "buffer_max_size", None)]
            capacities = [int(v) for v in capacities if isinstance(v, (int, np.integer)) and not isinstance(v, bool) and v > 0]
            if not capacities:
                raise TypeError("Cannot inspect actual DMA transfer capacity (_max_size/buffer_max_size)")
            if min(capacities) < required:
                raise ValueError(name + " DMA capacity is too small: needs " + str(required)
                                 + " bytes. FP32 input is " + str(self.INPUT_BYTES)
                                 + " bytes, requiring at least 21-bit DMA length; use a plugin for other hardware contracts.")
            if self.owned_dma and (not callable(getattr(getattr(channel, "_mmio", None), "write", None))
                                   or not isinstance(getattr(channel, "_offset", None), int)
                                   or isinstance(getattr(channel, "_offset", None), bool)
                                   or getattr(channel, "_offset", -1) < 0
                                   or not hasattr(channel, "running")):
                raise TypeError("Owned abort requires simple PYNQ channel _mmio/_offset/running metadata; no register offsets are guessed")
        if allocator is None:
            from pynq import allocate
            allocator = allocate
        if not callable(allocator):
            raise TypeError("allocator must be pynq.allocate-compatible")
        self.allocator = allocator
        self.control = None
        control_keys = ("control_ip", "ap_start_offset", "ap_start_value")
        if any(key in config for key in control_keys):
            if not all(key in config for key in control_keys):
                raise ValueError("control_ip, ap_start_offset and ap_start_value must all be explicit")
            self.control = resolve_ip(self.overlay, config["control_ip"])
            if not callable(getattr(self.control, "write", None)):
                raise TypeError("Control IP requires write(offset,value)")
            for key in control_keys[1:]:
                value = config[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(key + " must be a nonnegative integer")
            if config["ap_start_offset"] % 4 or config["ap_start_value"] > 0xFFFFFFFF:
                raise ValueError("Control offset must be 4-byte aligned and value must fit uint32")
        self._buffers = []
        self._started_channels = []
        self._poisoned = False
        self._closed = False

    @staticmethod
    def _positive(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(name + " must be positive and finite")
        return float(value)

    def _allocate(self, shape, channel):
        buffer = self.allocator(shape=shape, dtype=np.float32)
        if not callable(getattr(buffer, "freebuffer", None)):
            raise TypeError("DMA allocator must provide PynqBuffer freebuffer()")
        self._buffers.append(buffer)
        if (not isinstance(buffer, np.ndarray) or buffer.shape != shape or buffer.dtype != np.float32
                or not buffer.flags.c_contiguous or buffer.nbytes != math.prod(shape) * 4):
            raise ValueError("Allocator returned the wrong FP32 buffer shape/layout/size")
        if not all(callable(getattr(buffer, n, None)) for n in ("flush", "invalidate", "freebuffer")):
            raise TypeError("DMA allocator must provide PynqBuffer flush/invalidate/freebuffer")
        address = getattr(buffer, "physical_address", None)
        if not isinstance(address, (int, np.integer)) or isinstance(address, bool) or address < 0:
            raise TypeError("DMA buffer lacks a valid physical_address")
        if getattr(channel, "_dre", True) is False:
            alignment = getattr(channel, "_align", None)
            if not isinstance(alignment, int) or alignment <= 0 or address % alignment:
                raise ValueError("DMA buffer is not aligned to the configured channel word width")
        return buffer

    def _release(self):
        for channel in (self.send, self.recv):
            active = getattr(channel, "_active_buffer", None)
            if any(active is b for b in self._buffers):
                channel._active_buffer = None
        while self._buffers:
            free_buffer(self._buffers[0])
            self._buffers.pop(0)
        self._started_channels.clear()

    def _safe(self):
        return all(bool(c.idle) or (hasattr(c, "running") and not bool(c.running))
                   for c in self._started_channels)

    def _abort_owned(self):
        # PYNQ stop() itself busy-spins without a deadline. Use only the simple
        # channel's own metadata for its documented stop register instead.
        channels = self._started_channels or [self.recv, self.send]
        for channel in channels:
            channel._mmio.write(channel._offset, 0)
        deadline = time.monotonic() + self.timeout_s
        while any(bool(c.running) for c in channels):
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned DMA did not halt; live buffers are retained")
            time.sleep(min(self.poll_interval_s, max(0, deadline - time.monotonic())))

    def infer(self, tensor):
        if self._closed or self._poisoned or self._buffers:
            raise RuntimeError("DMA runner is closed or failed; retained transfers require hardware recovery/close before replacement")
        if (not isinstance(tensor, np.ndarray) or tensor.dtype != np.float32
                or tensor.shape != self.INPUT_SHAPE or not tensor.flags.c_contiguous
                or not np.isfinite(tensor).all()):
            raise ValueError("DMA input must be finite contiguous native FP32 " + str(self.INPUT_SHAPE))
        for channel in (self.recv, self.send):
            if bool(channel.error):
                raise RuntimeError("DMA is already in an error state")
            if not bool(channel.idle) and getattr(channel, "_first_transfer", False) is not True:
                raise RuntimeError("DMA is busy; existing external transfers must not be interrupted")
        try:
            incoming = self._allocate(self.INPUT_SHAPE, self.send)
            outgoing = self._allocate(self.OUTPUT_SHAPE, self.recv)
            incoming[:] = tensor
            outgoing.fill(np.nan)  # Detect incomplete/short output, never fake predictions.
            incoming.flush()
            outgoing.flush()
            deadline = time.monotonic() + self.timeout_s
            self._started_channels.append(self.recv)
            self.recv.transfer(outgoing, nbytes=self.OUTPUT_BYTES)
            if self.control is not None:
                self.control.write(self.config["ap_start_offset"], self.config["ap_start_value"])
            self._started_channels.append(self.send)
            self.send.transfer(incoming, nbytes=self.INPUT_BYTES)
            while True:
                if any(bool(c.error) for c in (self.recv, self.send)):
                    raise RuntimeError("DMA transfer error; check stream format, TLAST and memory access")
                if all(bool(c.idle) for c in (self.recv, self.send)):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("FP32 DMA inference timed out; check accelerator/TLAST/"
                                       + str(self.INPUT_BYTES) + "-byte input contract")
                time.sleep(min(self.poll_interval_s, max(0, deadline - time.monotonic())))
            outgoing.invalidate()
            result = np.array(outgoing, dtype=np.float32, order="C", copy=True)
            if not np.isfinite(result).all():
                raise ValueError("DMA output is incomplete/nonfinite, not decoded FP32 " + str(self.OUTPUT_SHAPE))
            self._release()
            return result
        except Exception as exc:
            self._poisoned = True
            try:
                if self._started_channels and self.owned_dma:
                    self._abort_owned()
                if self._safe():
                    self._release()
            except Exception as abort_exc:
                raise RuntimeError("DMA failed and cleanup could not halt safely; buffers retained: " + str(abort_exc)) from exc
            # External busy DMA buffers intentionally remain allocated.
            raise

    def close(self):
        if self._closed:
            return
        if self.owned_dma:
            self._abort_owned()
        if self._buffers:
            if not self._safe():
                raise RuntimeError("External DMA is still busy; not stopped and buffers retained")
            self._release()
        self._closed = True
