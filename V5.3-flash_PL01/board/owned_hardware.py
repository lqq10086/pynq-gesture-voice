"""Read-only new-core guard available before PLRunner/cache initialization."""
import json
import math
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from pl_runner import verify_overlay_mapping,digest


def verify_hp1_readonly(camera,core_mmio,mmio_factory,register_map):
    """UG1019 reset gate, SLCR-derived clock, core ACK, then UG585 HP1 width.

    No reset, clock, AFI or DDR configuration writes are permitted here.
    """
    slcr=mmio_factory(0xf8000000,0x1000)
    reset=int(slcr.read(0x240))
    if reset!=0:raise RuntimeError('FPGA_RST_CTRL must be entirely zero before HP1 reads; no reset/PS writes')
    clocks=camera._check_clocks();fclk=[float(x['mhz']) for x in clocks['fclk'] if x['index']==0]
    if len(fclk)!=1 or not math.isfinite(fclk[0]) or abs(fclk[0]-100.)>.001:raise RuntimeError('readback FCLK0 must be 100 MHz before HP1 reads')
    control=int(core_mmio.read(register_map['control_offset']))
    if not control&register_map['idle_mask'] or control&(register_map['start_mask']|register_map['auto_restart_mask']):raise RuntimeError('new core ACK/idle confirmation required before HP1 reads')
    afi1=mmio_factory(0xf8009000,0x1000)
    read_control=int(afi1.read(0x00));write_control=int(afi1.read(0x14))
    if read_control&1 or write_control&1:raise RuntimeError('HP1 RD/WR 32BitEn must both be zero (64-bit); never modify PS/DDR width')
    return dict(scope='READ_ONLY_HP1_PREFLIGHT',fpga_rst_ctrl=reset,fclk0_mhz=fclk[0],core_ack_ap_ctrl=control,
        hp1_rdchan_ctrl=read_control,hp1_wrchan_ctrl=write_control,hp1_bits=64,ps_writes=0,
        qualification='NOT_A_BOARD_NUMERIC_OR_CACHE_PASS')


class OwnedPLHardware:
    def __init__(self,camera,identity,owner_check,mmio_factory,shutdown=False):
        self.camera=camera;self.identity=dict(identity);self.owner_check=owner_check
        self.shutdown_owner_check=getattr(camera,'pl01_shutdown_owner_check',None)
        self.register_map=json.loads((Path(__file__).parent/'register_map.json').read_text())
        if shutdown:self.verify_shutdown()
        else:self.verify_active()
        probe=SimpleNamespace(base_addr=identity['ip_base_addr'],length=identity['ip_addr_range'])
        verify_overlay_mapping(camera.overlay,probe,identity,self.register_map['minimum_mmio_bytes'])
        phase=camera.overlay.ip_dict.get(identity['phase_instance'],{})
        if (phase.get('type')!=identity['phase_type'] or int(phase.get('phys_addr',-1))!=identity['phase_base_addr'] or
                int(phase.get('addr_range',0))!=identity['phase_addr_range'] or identity['phase_type']!='xilinx.com:ip:axi_gpio:2.0'):
            raise RuntimeError('new phase GPIO metadata differs')
        self.mmio=mmio_factory(identity['ip_base_addr'],identity['ip_addr_range'])
        self.phase_mmio=mmio_factory(identity['phase_base_addr'],identity['phase_addr_range'])
        self.hp1_preflight=verify_hp1_readonly(camera,self.mmio,mmio_factory,self.register_map)

    def verify_active(self):
        if not self.owner_check():raise RuntimeError('new-core access requires the held ACTIVE owner')
        return True

    def verify_shutdown(self):
        if self.shutdown_owner_check is None or not self.shutdown_owner_check():raise RuntimeError('shutdown requires the original locked owner and stopped worker')
        return True

    def verify_shutdown_owner(self):return self.verify_shutdown()

    def check_idle(self,shutdown=False):
        if shutdown:self.verify_shutdown()
        else:self.verify_active()
        verify_overlay_mapping(self.camera.overlay,self.mmio,self.identity,self.register_map['minimum_mmio_bytes'])
        control=int(self.mmio.read(self.register_map['control_offset']));phase=int(self.phase_mmio.read(0))
        if (control&self.register_map['idle_mask']==0 or control&(self.register_map['start_mask']|self.register_map['auto_restart_mask']) or phase!=0):
            raise RuntimeError('Fastest must be AP_IDLE/start0/auto0/phase0; buffers and owner retained')
        return dict(state='PRESENT_IDLE',instance=self.identity['ip_instance'],ap_ctrl=control,phase=phase,bitstream_sha256=self.identity['bitstream_sha256'],hwh_sha256=self.identity['hwh_sha256'])

    def runner_proof(self,root,native_library):
        root=Path(root)
        return dict(self.identity,**self.register_map,status='PL01_LOADED_OVERLAY_QUALIFIED',
            driver_sha256=digest(root/'board/pl_runner.py'),native_library_sha256=digest(native_library),
            physical_plan_sha256=digest(root/'reports/physical_bundle_2402/plan/plan.json'),package_manifest_sha256=digest(root/'models/quant_2020/manifest.json'))


def ensure_core_guard(camera,shutdown=False):
    guard=getattr(camera,'pl01_core_guard',None)
    if guard is not None:return guard
    identity=getattr(camera,'pl01_static_identity',None);owner_check=getattr(camera,'pl01_owner_check',None)
    if identity is None or owner_check is None:raise RuntimeError('partial PL01 startup lacks static/owner guard; retain owner')
    guard=OwnedPLHardware(camera,identity,owner_check,camera._mmio_factory,shutdown=shutdown);camera.pl01_core_guard=guard
    return guard
