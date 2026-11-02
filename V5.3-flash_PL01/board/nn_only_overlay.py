"""Owned full-bit load for saved-frame NN tests; never arms camera/UI VDMA."""
from pathlib import Path


def load_nn_only_overlay(camera,bitfile):
    from camera_control import _validate_hwh,CAPTURE_DATA,CAPTURE_DIRECTION
    bit=Path(bitfile).resolve();_validate_hwh(bit.with_suffix('.hwh'))
    camera._dependencies()
    camera.overlay=camera._overlay_factory(str(bit),download=False)
    camera.bit_path=bit
    camera._prepare_overlay_clock_sources();camera.overlay.download()
    loaded=getattr(camera.overlay,'is_loaded',False)
    if not (loaded() if callable(loaded) else loaded):raise RuntimeError('NN-only overlay download not confirmed')
    descriptions=camera._validate_overlay_ip_dict(camera.overlay.ip_dict)
    camera.capture_mmio=camera._make_mmio(descriptions['video_control_gpio'])
    camera.vdma_mmio=camera._make_mmio(descriptions['axi_vdma_0'])
    camera._check_clocks()
    camera.capture_mmio.write(CAPTURE_DATA,0);camera.capture_mmio.write(CAPTURE_DIRECTION,0)
    camera._halt_channels()
    # The UI engine must remain halted too; do not allocate a UI plane.
    from ui_plane_output import UIPlaneOutput,CONTROL,STATUS
    UIPlaneOutput._validate_layout(camera)
    camera.pl01_ui_mmio=camera._make_mmio(camera.overlay.ip_dict['ui_vdma'])
    if int(camera.pl01_ui_mmio.read(CONTROL))&1 or not int(camera.pl01_ui_mmio.read(STATUS))&1:
        raise RuntimeError('NN-only diagnostic requires halted UI VDMA; no resets or video startup')
    camera.video_running=False
    return dict(scope='NN_ONLY_SAVED_FRAME_DIAGNOSTICS',sensor_required=False,video_armed=False,
        video_validation='NOT_RUN',camera_buffers_allocated=len(camera.buffers),bitfile=str(bit))
