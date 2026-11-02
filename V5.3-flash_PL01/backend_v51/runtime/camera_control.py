"""Safe PYNQ control for the AX7020 V3.3 native 720P camera overlay.

The CPU only owns control registers and three physically contiguous DMA
buffers.  It never reads, fills, flushes, or invalidates complete frames.
PYNQ imports are lazy so this module remains importable on a desktop PC.
"""

from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET


WIDTH = 1280
HEIGHT = 720
BYTES_PER_PIXEL = 2
STRIDE = 2560
FRAME_BYTES = 1843200
FRAME_STORES = 3
HP0_LAST_ADDRESS = 0x3FFFFFFF
DEFAULT_BITSTREAM_NAME = "camera_v25.bit"

CONTROL = 0x00
APPLY = 0x04
STATUS = 0x08
ACTIVE = 0x0C
IDENTITY = 0x10
SEQUENCE = 0x14
EXPECTED_ID = 0x56323543

PL_STATUS = 0x00
PL_COUNTS = 0x08
CAPTURE_DATA = 0x00
CAPTURE_DIRECTION = 0x04

MM2S_CONTROL = 0x00
MM2S_STATUS = 0x04
S2MM_CONTROL = 0x30
S2MM_STATUS = 0x34
MM2S_ADDRESS_BLOCK = 0x50
S2MM_ADDRESS_BLOCK = 0xA0
VDMA_VSIZE = 0x00
VDMA_HSIZE = 0x04
VDMA_STRIDE = 0x08
VDMA_START_ADDRESS = 0x0C
VDMA_FRAMESTORE = 0x18
VDMA_RUN = 0x01
VDMA_CIRCULAR = 0x02
VDMA_RESET = 0x04
VDMA_GENLOCK = 0x08
VDMA_INTERNAL_GENLOCK = 0x80
VDMA_FRAME_THRESHOLD_1 = 1 << 16
VDMA_CONTROL_VALUE = VDMA_FRAME_THRESHOLD_1 | VDMA_INTERNAL_GENLOCK | VDMA_GENLOCK | VDMA_CIRCULAR
VDMA_HALTED = 0x01
VDMA_ERROR_MASK = 0xFF0
VDMA_FRAMECOUNT = 0x1000

PS_REF_MHZ = 33.333333
SLCR_BASE = 0xF8000000
SLCR_RANGE = 0x1000
SLCR_LOCK = 0x004
SLCR_UNLOCK = 0x008
SLCR_LOCKSTA = 0x00C
SLCR_LOCK_KEY = 0x767B
SLCR_UNLOCK_KEY = 0xDF0D
ARM_PLL_CTRL = 0x100
DDR_PLL_CTRL = 0x104
IO_PLL_CTRL = 0x108
PLL_STATUS = 0x10C
FCLK_CTRL = (0x170, 0x180)
FCLK_SOURCE_MASK = 0x3 << 4
FPGA_RST_CTRL = 0x240
AFI0_BASE = 0xF8008000
AFI_RANGE = 0x1000

PLL_SOURCES = {
    0: ("IO PLL", IO_PLL_CTRL, 2),
    1: ("IO PLL", IO_PLL_CTRL, 2),
    2: ("ARM PLL", ARM_PLL_CTRL, 0),
    3: ("DDR PLL", DDR_PLL_CTRL, 1),
}

EXPECTED_IP = {
    "video_status_gpio": (0x41200000, 0x10000, "xilinx.com:ip:axi_gpio:2.0"),
    "video_control_gpio": (0x41210000, 0x10000, "xilinx.com:ip:axi_gpio:2.0"),
    "camera_control_0": (0x41220000, 0x10000, "xilinx.com:module_ref:camera_control_axi_v33:1.0"),
    "axi_vdma_0": (0x43000000, 0x10000, "xilinx.com:ip:axi_vdma:6.3"),
}

EXPECTED_VDMA_PARAMETERS = {
    "C_ENABLE_VIDPRMTR_READS": "1",
    "C_NUM_FSTORES": "3",
    "C_INCLUDE_INTERNAL_GENLOCK": "1",
    "C_INCLUDE_MM2S": "1",
    "C_MM2S_GENLOCK_MODE": "3",
    "C_INCLUDE_S2MM": "1",
    "C_S2MM_GENLOCK_MODE": "2",
    "C_M_AXI_MM2S_ADDR_WIDTH": "32",
    "C_M_AXI_MM2S_DATA_WIDTH": "64",
    "C_M_AXI_S2MM_ADDR_WIDTH": "32",
    "C_M_AXI_S2MM_DATA_WIDTH": "64",
}


def _integer(value):
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _module_parameters(module):
    return {
        item.get("NAME"): item.get("VALUE")
        for item in module.findall("./PARAMETERS/PARAMETER")
    }


def _pll_output_mhz(source, pll_controls, pll_status):
    try:
        name, offset, lock_bit = PLL_SOURCES[source]
    except KeyError as error:
        raise RuntimeError(f"FCLK source encoding {source!r} is invalid") from error
    value = pll_controls[offset]
    if value & 0x13 or not (pll_status & (1 << lock_bit)):
        raise RuntimeError(f"{name} is bypassed, powered down, reset, or unlocked")
    fbdiv = (value >> 12) & 0x7F
    if not fbdiv:
        raise RuntimeError(f"{name} feedback divider is zero")
    return PS_REF_MHZ * fbdiv


def _validate_hwh(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise RuntimeError(f"Invalid HWH XML: {path}: {error}") from error

    modules = {item.get("INSTANCE"): item for item in root.findall(".//MODULE")}
    missing = [name for name in EXPECTED_IP if name not in modules]
    if missing:
        raise RuntimeError(f"HWH is missing required IP: {', '.join(missing)}")

    control = modules["camera_control_0"]
    if control.get("VLNV") != EXPECTED_IP["camera_control_0"][2]:
        raise RuntimeError("camera_control_0 metadata type mismatch")

    vdma_parameters = _module_parameters(modules["axi_vdma_0"])
    for name, expected in EXPECTED_VDMA_PARAMETERS.items():
        actual = vdma_parameters.get(name)
        if actual != expected:
            raise RuntimeError(f"HWH {name} mismatch: expected {expected}, found {actual}")

    ps_parameters = _module_parameters(modules["processing_system7_0"])
    for name, expected in (("C_USE_S_AXI_HP0", "1"), ("C_S_AXI_HP0_DATA_WIDTH", "64")):
        actual = ps_parameters.get(name)
        if actual != expected:
            raise RuntimeError(f"HWH {name} mismatch: expected {expected}, found {actual}")

    memory_ranges = root.findall(".//MEMRANGE")
    address_map = {
        item.get("INSTANCE"): (_integer(item.get("BASEVALUE")), _integer(item.get("HIGHVALUE")))
        for item in memory_ranges
        if item.get("INSTANCE") in EXPECTED_IP
    }
    for name, (base, size, _kind) in EXPECTED_IP.items():
        expected_range = (base, base + size - 1)
        if address_map.get(name) != expected_range:
            raise RuntimeError(f"HWH address range mismatch for {name}: expected 0x{base:08X}-0x{expected_range[1]:08X}")

    hp0_ranges = [
        (_integer(item.get("BASEVALUE")), _integer(item.get("HIGHVALUE")))
        for item in memory_ranges
        if item.get("INSTANCE") == "processing_system7_0"
        and item.get("SLAVEBUSINTERFACE") == "S_AXI_HP0"
        and item.get("MASTERBUSINTERFACE") in ("M_AXI_MM2S", "M_AXI_S2MM")
    ]
    if len(hp0_ranges) != 2 or any(item != (0, HP0_LAST_ADDRESS) for item in hp0_ranges):
        raise RuntimeError("HWH HP0 MM2S/S2MM address window must be 0x00000000-0x3FFFFFFF")

    clocks = {
        item.get("NAME"): _integer(item.get("CLKFREQUENCY"))
        for item in modules["processing_system7_0"].findall("./PORTS/PORT")
        if item.get("NAME") in ("FCLK_CLK0", "FCLK_CLK1") and item.get("CLKFREQUENCY")
    }
    if clocks.get("FCLK_CLK0") != 100000000 or abs(clocks.get("FCLK_CLK1", 0) - 142857143) > 100:
        raise RuntimeError(f"HWH clock mismatch: FCLK0={clocks.get('FCLK_CLK0')}, FCLK1={clocks.get('FCLK_CLK1')}")


class CameraControlSession:
    """Own one overlay instance, its live VDMA buffers, and serialized control."""

    def __init__(
        self,
        *,
        overlay_factory=None,
        mmio_factory=None,
        allocator=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
        poll_interval=0.001,
        startup_timeout=5.0,
        command_timeout=45.0,
    ):
        self._overlay_factory = overlay_factory
        self._mmio_factory = mmio_factory
        self._allocator = allocator
        self._sleep = sleep
        self._monotonic = monotonic
        self.poll_interval = poll_interval
        self.startup_timeout = startup_timeout
        self.command_timeout = command_timeout
        self._lock = threading.RLock()
        self.overlay = None
        self.control_mmio = None
        self.status_mmio = None
        self.capture_mmio = None
        self.vdma_mmio = None
        self.buffers = []
        self.video_running = False
        self.bit_path = None

    def _dependencies(self):
        if self._overlay_factory is None or self._mmio_factory is None or self._allocator is None:
            try:
                from pynq import MMIO, Overlay, allocate
            except ImportError as error:
                raise RuntimeError("PYNQ is required to load the overlay; copy this file to the AX7020 PYNQ image") from error
            if self._overlay_factory is None:
                self._overlay_factory = Overlay
            if self._mmio_factory is None:
                self._mmio_factory = MMIO
            if self._allocator is None:
                self._allocator = allocate

    def load_overlay(self, bit_path=None, *, output_factory=None):
        """Load the paired overlay, allocate buffers, and start the existing VDMA."""
        with self._lock:
            if self.vdma_mmio is not None or self.buffers:
                self._stop_video_locked()

            path = Path(bit_path) if bit_path is not None else Path(__file__).resolve().with_name(DEFAULT_BITSTREAM_NAME)
            path = path.resolve()
            hwh_path = path.with_suffix(".hwh")
            if not path.is_file():
                raise FileNotFoundError(f"Required bitstream missing: {path}")
            if not hwh_path.is_file():
                raise FileNotFoundError(f"Paired HWH missing: {hwh_path}")
            if path.suffix.lower() != ".bit" or hwh_path.stem != path.stem:
                raise RuntimeError("Overlay must use a paired .bit/.hwh basename")
            _validate_hwh(hwh_path)
            self._dependencies()

            self.overlay = self._overlay_factory(str(path), download=False)
            self._prepare_overlay_clock_sources()
            self.overlay.download()
            loaded = self.overlay.is_loaded() if callable(getattr(self.overlay, "is_loaded", None)) else bool(getattr(self.overlay, "is_loaded", False))
            if not loaded:
                raise RuntimeError("Overlay download returned but the FPGA is not loaded")
            descriptions = self._validate_overlay_ip_dict(self.overlay.ip_dict)
            self.control_mmio = self._make_mmio(descriptions["camera_control_0"])
            self.status_mmio = self._make_mmio(descriptions["video_status_gpio"])
            self.capture_mmio = self._make_mmio(descriptions["video_control_gpio"])
            self.vdma_mmio = self._make_mmio(descriptions["axi_vdma_0"])

            found_id = self.control_mmio.read(IDENTITY) & 0xFFFFFFFF
            if found_id != EXPECTED_ID:
                raise RuntimeError(f"camera_control_0 ID mismatch: expected 0x{EXPECTED_ID:08X}, found 0x{found_id:08X}")
            self._check_clocks()
            self._wait_camera_ready()
            self.capture_mmio.write(CAPTURE_DATA, 0)
            self.capture_mmio.write(CAPTURE_DIRECTION, 0)
            self._halt_channels()
            self._reset_channels()
            self.bit_path = path

            try:
                self._allocate_buffers()
                self._program_vdma()
                self._start_channel(S2MM_CONTROL, S2MM_STATUS, S2MM_ADDRESS_BLOCK,
                                    genlock=output_factory is None)
                self.vdma_mmio.write(S2MM_STATUS, VDMA_FRAMECOUNT)
                self.capture_mmio.write(CAPTURE_DATA, 1)
                self._wait_s2mm_frames(2)
                if output_factory is None:
                    self._start_channel(MM2S_CONTROL, MM2S_STATUS, MM2S_ADDRESS_BLOCK)
                else:
                    self.hdmi_output = output_factory(self)
                self._wait_video_running()
            except BaseException:
                self._cleanup_failed_start()
                raise

            self.video_running = True
            self.bit_path = path
            return self.get_status()

    def _validate_overlay_ip_dict(self, ip_dict):
        descriptions = {}
        for name, (base, size, expected_type) in EXPECTED_IP.items():
            if name not in ip_dict:
                raise RuntimeError(f"Overlay metadata is missing named IP {name}")
            description = ip_dict[name]
            actual = (_integer(description.get("phys_addr")), _integer(description.get("addr_range")), description.get("type"))
            if actual != (base, size, expected_type):
                raise RuntimeError(f"Overlay metadata mismatch for {name}: found {actual!r}")
            descriptions[name] = description
        return descriptions

    def _make_mmio(self, description):
        return self._mmio_factory(_integer(description["phys_addr"]), _integer(description["addr_range"]))

    def _prepare_overlay_clock_sources(self):
        """Apply the HWH-required IO PLL source before PYNQ writes its divisors."""
        slcr = self._mmio_factory(SLCR_BASE, SLCR_RANGE)
        originals = {offset: slcr.read(offset) & 0xFFFFFFFF for offset in FCLK_CTRL}
        changes = [
            (offset, value, value & ~FCLK_SOURCE_MASK)
            for offset, value in originals.items()
            if ((value >> 4) & 0x3) not in (0, 1)
        ]
        if not changes:
            return

        pll_controls = {IO_PLL_CTRL: slcr.read(IO_PLL_CTRL) & 0xFFFFFFFF}
        _pll_output_mhz(0, pll_controls, slcr.read(PLL_STATUS) & 0xFFFFFFFF)
        was_locked = bool(slcr.read(SLCR_LOCKSTA) & 1)
        attempted = []
        slcr.write(SLCR_UNLOCK, SLCR_UNLOCK_KEY)
        try:
            for offset, original, updated in changes:
                attempted.append((offset, original))
                slcr.write(offset, updated)
                readback = slcr.read(offset) & 0xFFFFFFFF
                if readback != updated:
                    raise RuntimeError(
                        f"FCLK source readback mismatch at SLCR offset 0x{offset:03X}: "
                        f"wrote 0x{updated:08X}, read 0x{readback:08X}"
                    )
        except BaseException:
            for offset, original in reversed(attempted):
                slcr.write(offset, original)
            raise
        finally:
            if was_locked:
                slcr.write(SLCR_LOCK, SLCR_LOCK_KEY)

        if bool(slcr.read(SLCR_LOCKSTA) & 1) != was_locked:
            raise RuntimeError("SLCR lock state was not restored after FCLK source configuration")

    def _check_clocks(self):
        slcr = self._mmio_factory(SLCR_BASE, SLCR_RANGE)
        pll_controls = {
            ARM_PLL_CTRL: slcr.read(ARM_PLL_CTRL) & 0xFFFFFFFF,
            DDR_PLL_CTRL: slcr.read(DDR_PLL_CTRL) & 0xFFFFFFFF,
            IO_PLL_CTRL: slcr.read(IO_PLL_CTRL) & 0xFFFFFFFF,
        }
        pll_status = slcr.read(PLL_STATUS) & 0xFFFFFFFF
        reset = slcr.read(FPGA_RST_CTRL) & 0xFFFFFFFF
        if reset & 1:
            raise RuntimeError("FCLK reset0 is asserted; both overlay clock domains require reset0 released")
        measured = []
        for index, (offset, expected) in enumerate(zip(FCLK_CTRL, (100.0, 142.857143))):
            value = slcr.read(offset) & 0xFFFFFFFF
            source = (value >> 4) & 0x3
            div0 = (value >> 8) & 0x3F
            div1 = (value >> 20) & 0x3F
            if not div0 or not div1:
                raise RuntimeError(f"FCLK{index} divisor is zero")
            actual = _pll_output_mhz(source, pll_controls, pll_status) / (div0 * div1)
            if abs(actual - expected) > 1.0:
                raise RuntimeError(f"FCLK{index} is {actual:.3f} MHz, expected {expected:.3f} MHz; do not run ps7_init or alter PS PLL/DDR")
            measured.append(dict(index=index,mhz=actual,control=value,source=source,div0=div0,div1=div1))
        afi0 = self._mmio_factory(AFI0_BASE, AFI_RANGE)
        if afi0.read(0x00) & 1 or afi0.read(0x14) & 1:
            raise RuntimeError("Live HP0 read/write channels are configured for 32-bit; this overlay requires 64-bit")
        return dict(fclk=measured,source='SLCR register-derived',hp0_bits=64)

    def _deadline(self, timeout):
        return self._monotonic() + timeout

    def _pause_until(self, deadline, message):
        if self._monotonic() >= deadline:
            raise TimeoutError(message)
        self._sleep(self.poll_interval)

    def _raise_live_errors(self, *, include_sensor=False):
        status = self.control_mmio.read(STATUS)
        counts = self.status_mmio.read(PL_COUNTS)
        if status & (1 << 3):
            raise RuntimeError("camera control fatal error; safely stop video, then reload the overlay (no automatic retry)")
        if include_sensor and counts & (1 << 31):
            raise RuntimeError("sensor error reported by live PL telemetry")
        return status

    def _wait_camera_ready(self):
        deadline = self._deadline(self.startup_timeout)
        while True:
            self._raise_live_errors(include_sensor=True)
            pl_status = self.status_mmio.read(PL_STATUS)
            counts = self.status_mmio.read(PL_COUNTS)
            if pl_status & (1 << 2):
                raise RuntimeError("camera configuration error reported by PL")
            if not (pl_status & (1 << 31)) or (counts & 0xFF) != 0x25:
                raise RuntimeError("live status GPIO layout/version marker is incompatible with V2.5")
            if (pl_status & 0x3) == 0x3:
                return
            self._pause_until(deadline, "camera configuration did not complete")

    def _halt_channels(self):
        for control in (MM2S_CONTROL, S2MM_CONTROL):
            self.vdma_mmio.write(control, self.vdma_mmio.read(control) & ~VDMA_RUN)
        deadline = self._deadline(self.startup_timeout)
        while True:
            if all(self.vdma_mmio.read(status) & VDMA_HALTED for status in (MM2S_STATUS, S2MM_STATUS)):
                return
            self._pause_until(deadline, "VDMA channels failed to halt; live DMA buffers remain owned")

    def _reset_channels(self):
        for control in (MM2S_CONTROL, S2MM_CONTROL):
            self.vdma_mmio.write(control, self.vdma_mmio.read(control) | VDMA_RESET)
        deadline = self._deadline(self.startup_timeout)
        while True:
            reset_done = all(not (self.vdma_mmio.read(control) & VDMA_RESET) for control in (MM2S_CONTROL, S2MM_CONTROL))
            halted = all(self.vdma_mmio.read(status) & VDMA_HALTED for status in (MM2S_STATUS, S2MM_STATUS))
            if reset_done and halted:
                return
            self._pause_until(deadline, "VDMA reset did not complete")

    def _allocate_one(self):
        try:
            return self._allocator(shape=(FRAME_BYTES,), dtype="u1", cacheable=False)
        except TypeError as error:
            if "cacheable" not in str(error):
                raise
            return self._allocator(shape=(FRAME_BYTES,), dtype="u1")

    def _allocate_buffers(self):
        self.buffers = []
        intervals = []
        for _index in range(FRAME_STORES):
            buffer = self._allocate_one()
            self.buffers.append(buffer)
            address = _integer(buffer.device_address)
            size = _integer(buffer.size)
            last = address + size - 1
            if size != FRAME_BYTES or address < 0 or last > HP0_LAST_ADDRESS:
                raise RuntimeError(f"DMA buffer interval 0x{address:08X}-0x{last:08X} is outside the HWH HP0 window")
            if any(address <= old_last and last >= old_first for old_first, old_last in intervals):
                raise RuntimeError("CMA allocator returned overlapping DMA buffers")
            intervals.append((address, last))

    def _program_channel(self, control, address_block):
        self.vdma_mmio.write(control, VDMA_CONTROL_VALUE)
        self.vdma_mmio.write(control + VDMA_FRAMESTORE, FRAME_STORES)
        self.vdma_mmio.write(address_block + VDMA_HSIZE, STRIDE)
        self.vdma_mmio.write(address_block + VDMA_STRIDE, STRIDE)
        for index, buffer in enumerate(self.buffers):
            self.vdma_mmio.write(address_block + VDMA_START_ADDRESS + index * 4, _integer(buffer.device_address))
        self.vdma_mmio.write(address_block + VDMA_VSIZE, HEIGHT)

        expected = {
            address_block + VDMA_VSIZE: HEIGHT,
            address_block + VDMA_HSIZE: STRIDE,
            address_block + VDMA_STRIDE: STRIDE,
        }
        expected.update({address_block + VDMA_START_ADDRESS + index * 4: _integer(buffer.device_address) for index, buffer in enumerate(self.buffers)})
        for offset, value in expected.items():
            actual = self.vdma_mmio.read(offset) & 0xFFFFFFFF
            if actual != value:
                raise RuntimeError(f"VDMA readback mismatch at offset 0x{offset:02X}: wrote 0x{value:08X}, read 0x{actual:08X}")

    def _program_vdma(self):
        self._program_channel(MM2S_CONTROL, MM2S_ADDRESS_BLOCK)
        self._program_channel(S2MM_CONTROL, S2MM_ADDRESS_BLOCK)

    def _start_channel(self, control, status, address_block, *, genlock=True):
        control_value = VDMA_CONTROL_VALUE
        if not genlock:
            # Separate output buffers need no cross-channel frame exclusion.
            # Dynamic Genlock otherwise skips capture indices (AMD PG020).
            control_value &= ~(VDMA_GENLOCK | VDMA_INTERNAL_GENLOCK)
        self.vdma_mmio.write(control, control_value | VDMA_RUN)
        self.vdma_mmio.write(address_block + VDMA_VSIZE, HEIGHT)
        deadline = self._deadline(self.startup_timeout)
        while True:
            value = self.vdma_mmio.read(status)
            if value & VDMA_ERROR_MASK:
                raise RuntimeError(f"VDMA channel error 0x{value & VDMA_ERROR_MASK:03X}")
            if not (value & VDMA_HALTED):
                return
            self._pause_until(deadline, "VDMA channel did not start")

    def _wait_s2mm_frames(self, count):
        completed = 0
        deadline = self._deadline(self.startup_timeout)
        while completed < count:
            value = self.vdma_mmio.read(S2MM_STATUS)
            if value & VDMA_ERROR_MASK:
                raise RuntimeError(f"S2MM error while priming: 0x{value & VDMA_ERROR_MASK:03X}")
            if value & VDMA_FRAMECOUNT:
                self.vdma_mmio.write(S2MM_STATUS, VDMA_FRAMECOUNT)
                completed += 1
                continue
            self._raise_live_errors(include_sensor=True)
            self._pause_until(deadline, f"S2MM did not report {count} fresh frame completions")

    def _wait_video_running(self):
        deadline = self._deadline(self.startup_timeout)
        while True:
            self._raise_live_errors(include_sensor=True)
            mm2s = self.vdma_mmio.read(MM2S_STATUS)
            s2mm = self.vdma_mmio.read(S2MM_STATUS)
            if (mm2s | s2mm) & VDMA_ERROR_MASK:
                raise RuntimeError("VDMA error while waiting for video")
            if self.status_mmio.read(PL_STATUS) & (1 << 6):
                return
            self._pause_until(deadline, "PL video-running status did not assert")

    def _release_buffers(self):
        for buffer in self.buffers:
            buffer.freebuffer()
        self.buffers = []

    def _cleanup_failed_start(self):
        if self.capture_mmio is not None:
            self.capture_mmio.write(CAPTURE_DATA, 0)
        if self.vdma_mmio is None:
            self._release_buffers()
            return
        try:
            self._halt_channels()
        except BaseException:
            # DMA may still own the addresses. Keeping strong references is safer.
            raise
        self._release_buffers()

    def _stop_video_locked(self):
        if self.capture_mmio is not None:
            self.capture_mmio.write(CAPTURE_DATA, 0)
        if self.vdma_mmio is not None:
            self._halt_channels()
        self.video_running = False
        self._release_buffers()

    def stop_video(self):
        """Disarm capture, halt both DMA channels, then release owned buffers."""
        with self._lock:
            self._stop_video_locked()

    def halt_video(self):
        """Confirm both channels halted while retaining all camera allocations."""
        with self._lock:
            if self.capture_mmio is not None:
                self.capture_mmio.write(CAPTURE_DATA, 0)
            if self.vdma_mmio is not None:
                self._halt_channels()
            self.video_running = False

    def _require_loaded(self):
        if self.control_mmio is None or self.status_mmio is None:
            raise RuntimeError("Overlay is not loaded; call load_overlay() first")

    def _apply_control(self, value):
        self._require_loaded()
        value &= 0xF
        self._raise_live_errors()
        idle_deadline = self._deadline(self.command_timeout)
        while self.control_mmio.read(STATUS) & 1:
            self._raise_live_errors()
            self._pause_until(idle_deadline, "camera control remained busy before APPLY")
        previous_sequence = self.control_mmio.read(SEQUENCE) & 0xFFFFFFFF
        self.control_mmio.write(CONTROL, value)
        readback = self.control_mmio.read(CONTROL) & 0xF
        if readback != value:
            raise RuntimeError(f"CONTROL readback mismatch: wrote 0x{value:X}, read 0x{readback:X}")
        self.control_mmio.write(APPLY, 1)
        deadline = self._deadline(self.command_timeout)
        while True:
            status = self._raise_live_errors()
            sequence = self.control_mmio.read(SEQUENCE) & 0xFFFFFFFF
            if (sequence - previous_sequence) & 0xFFFFFFFF and status & (1 << 1) and not (status & 1):
                if status & (1 << 2):
                    raise RuntimeError("camera control request error")
                if not (status & (1 << 4)):
                    raise RuntimeError("camera configuration was not done at completion")
                break
            self._pause_until(deadline, "camera control command timed out waiting for sequence completion")

        active = self.control_mmio.read(ACTIVE) & 0xFF
        if active & (1 << 5):
            raise RuntimeError("camera control request error in completed snapshot")
        if active & (1 << 6):
            raise RuntimeError("camera control fatal error in completed snapshot")
        if not (active & (1 << 4)):
            raise RuntimeError("completed snapshot reports camera configuration incomplete")
        source = value & 1
        if (active & 1) != source:
            raise RuntimeError("completed control source does not match requested source")
        if source:
            enabled = (value >> 1) & 1
            mode = (value >> 2) & 3
            expected_mode = (mode & 2) | ((mode & 1) if enabled else 0)
            actual_mode = ((active >> 3) & 1) << 1 | ((active >> 2) & 1)
            if ((active >> 1) & 1) != enabled or actual_mode != expected_mode:
                raise RuntimeError(f"completed AXI mode mismatch: expected {expected_mode}, found {actual_mode}")
        return self.get_status()

    def set_control_source(self, source):
        with self._lock:
            if source not in ("pynq", "switch"):
                raise ValueError("source must be 'pynq' or 'switch'")
            control = self.control_mmio.read(CONTROL) & 0xF if self.control_mmio is not None else 0
            control = (control | 1) if source == "pynq" else (control & ~1)
            return self._apply_control(control)

    def enable_color(self, enabled):
        with self._lock:
            if not isinstance(enabled, bool):
                raise TypeError("enabled must be bool")
            self._require_loaded()
            control = self.control_mmio.read(CONTROL) & 0xF
            control = (control | 2) if enabled else (control & ~2)
            return self._apply_control(control)

    def set_color_mode(self, mode):
        with self._lock:
            if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 3:
                raise ValueError("mode must be an integer from 0 through 3")
            self._require_loaded()
            control = (self.control_mmio.read(CONTROL) & 0x3) | (mode << 2)
            return self._apply_control(control)

    def get_status(self):
        with self._lock:
            self._require_loaded()
            control = self.control_mmio.read(CONTROL) & 0xF
            status = self.control_mmio.read(STATUS) & 0x3F
            active = self.control_mmio.read(ACTIVE) & 0xFF
            pl_status = self.status_mmio.read(PL_STATUS) & 0xFFFFFFFF
            counts = self.status_mmio.read(PL_COUNTS) & 0xFFFFFFFF
            requested_source = "pynq" if control & 1 else "switch"
            completed_source = "pynq" if active & 1 else "switch"
            return {
                "requested": {
                    "source": requested_source,
                    "enabled": bool(control & 2),
                    "mode": (control >> 2) & 3,
                },
                "command": {
                    "busy": bool(status & 1),
                    "completion_valid": bool(status & (1 << 1)),
                    "request_error": bool(status & (1 << 2)),
                    "fatal": bool(status & (1 << 3)),
                    "config_done_at_completion": bool(status & (1 << 4)),
                    "af_error_at_completion": bool(status & (1 << 5)),
                    "sequence": self.control_mmio.read(SEQUENCE) & 0xFFFFFFFF,
                },
                "completed": {
                    "source": completed_source,
                    "enabled": bool(active & (1 << 1)),
                    "pl_active": bool(active & (1 << 2)),
                    "isp_active": bool(active & (1 << 3)),
                    "config_done": bool(active & (1 << 4)),
                    "request_error": bool(active & (1 << 5)),
                    "fatal": bool(active & (1 << 6)),
                    "af_error": bool(active & (1 << 7)),
                },
                "live": {
                    "clock_locked": bool(pl_status & 1),
                    "config_done": bool(pl_status & (1 << 1)),
                    "config_error": bool(pl_status & (1 << 2)),
                    "camera_frame_seen": bool(pl_status & (1 << 3)),
                    "fifo_overflow": bool(pl_status & (1 << 4)),
                    "address_overflow": bool(pl_status & (1 << 5)),
                    "video_running": bool(pl_status & (1 << 6)),
                    "underflow": bool(pl_status & (1 << 7)),
                    "sync_error": bool(pl_status & (1 << 8)),
                    "layout_marker": bool(pl_status & (1 << 31)),
                    "sensor_frame_counter_advisory": (pl_status >> 17) & 0x3FFF,
                    "sensor_error": bool(counts & (1 << 31)),
                    "pending": bool(counts & (1 << 30)),
                    "pl_active": bool(counts & (1 << 29)),
                    "pl_request": bool(counts & (1 << 28)),
                    "isp_actual": bool(counts & (1 << 27)),
                    "pl_actual": bool(counts & (1 << 26)),
                    "af_state": (counts >> 22) & 0xF,
                    "af_error": (counts >> 18) & 0xF,
                    "af_last_read": (counts >> 10) & 0xFF,
                    "sw1_current": bool(counts & (1 << 9)),
                    "sw1_differs_from_boot": bool(counts & (1 << 8)),
                    "version_marker": counts & 0xFF,
                },
            }


_session = CameraControlSession()


def load_overlay(bit_path=None):
    return _session.load_overlay(bit_path)


def set_control_source(source):
    return _session.set_control_source(source)


def enable_color(enabled):
    return _session.enable_color(enabled)


def set_color_mode(mode):
    return _session.set_color_mode(mode)


def get_status():
    return _session.get_status()


def stop_video():
    return _session.stop_video()
