"""Device preflight and read-only idle confirmation; no AI CMA allocation."""

def require_pynq_device():
    """Reject an incomplete runtime environment before consuming idle handoff."""
    from pynq.pl_server import Device
    devices=Device.devices
    if not devices:
        raise RuntimeError('PYNQ device unavailable; launch with env XILINX_XRT=/usr before Python')
    return [device.name for device in devices]

def read_idle_kernel(camera):
    if camera.overlay is None:raise RuntimeError('No loaded overlay to inspect')
    entry=camera.overlay.ip_dict.get('conv_accel_0')
    if not entry or int(entry['phys_addr'])!=0x43c00000 or int(entry['addr_range'])!=65536:
        raise RuntimeError('Unexpected kernel register layout')
    control=int(camera._make_mmio(entry).read(0))
    if control&0x85!=4:raise RuntimeError('Unused kernel is not idle/start-clear/auto-restart-clear')
    return control
