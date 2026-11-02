"""Fill actual bitfile/IP names and lifecycle config before board use.

This is a thin factory, not an AX7020 overlay or camera driver. It requires
real .bit/.hwh files and real readframe/newframe/writeframe APIs. With already
configured VideoIn/VideoOut set configure/start=False. For self-managed VDMA
channels set configure/start=True and *_configure_api='mode_attribute'. For
VideoIn/VideoOut use 'video_in'/'video_out' and explicit *_already_started.
RGB565 requires a custom uint16-word video interface, not PYNQ's byte-packed
16-bit frames. The bridge returns independent BGR uint8 frames to the app.
"""
from gesture_app.pynq_io import PynqVideoBridge


def create(config):
    """Return read_frame/write_frame/close methods on the configured bridge."""
    return PynqVideoBridge(config)
