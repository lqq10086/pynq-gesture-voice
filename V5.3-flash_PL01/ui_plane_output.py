"""Independent PL UI plane; background pixels never enter this driver.

ABI source: pl_video/hardware/bd/ui_bd.tcl and ui_present_monitor.v.
VDMA programming follows AMD PG020; a current read pointer is NOT presentation.

Tags 1/2/3 identify DMA pages, not software revisions. Reusing a page is safe
only after a complete different active page has passed the ordered video path.
Submission verifies that active page before writing and after flushing. A fence
then requires a forward completion count, target tag and current DMA page.
"""
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import numpy as np
from town_creator.ui_plane import update_ui_plane

CONTROL,STATUS,PARK=0,4,0x28
VSIZE,HSIZE,STRIDE,ADDRESS=0x50,0x54,0x58,0x5c
ERROR_BITS=0x770


class UIPlaneFence:
    def __init__(self,owner,page,sequence,revision,before_count):
        self.owner,self.page,self.sequence,self.revision=owner,page,sequence,revision
        # sequence is the legacy field name for the fixed per-page wire tag.
        self.before_count=before_count;self.started=time.monotonic();self.complete=False

    def presented(self):
        if self.complete:return True
        self.owner._poll(self)
        return self.complete


class UIPlaneOutput:
    def __init__(self,camera):
        self.buffers=[];self.regs=None;self.closed=False;self.pending=None
        self.active=0;self.sequence=0;self.last_revision=-1
        self.submissions=self.presentations=0;self.samples=[]
        self._motion_rates={}
        self._packed_packets=[None]*3
        self.width,self.height=1280,720;self.timeout_s=3.
        self._validate_layout(camera)
        ip=camera.overlay.ip_dict
        self.regs=camera._make_mmio(ip['ui_vdma'])
        self.status0=camera._make_mmio(ip['ui_status_gpio0'])
        self.status1=camera._make_mmio(ip['ui_status_gpio1'])
        if self.regs.read(CONTROL)&1 or not self.regs.read(STATUS)&1:
            raise RuntimeError('UI VDMA must start halted after overlay download')
        # Newly enabled HP2 must agree with the 64-bit overlay. Read only.
        afi=camera._mmio_factory(0xf800a000,0x1000)
        if afi.read(0)&1 or afi.read(0x14)&1:
            raise RuntimeError('Live HP2 is 32-bit, expected 64-bit; do not modify PS DDR')
        if self.read_status()['identity']!=0x45:
            raise RuntimeError('PL UI ABI identity mismatch')
        for _ in range(3):
            buffer=camera._allocator(shape=(self.height,self.width,8),dtype='u1',cacheable=False)
            self.buffers.append(buffer)
        from region_executor import check_global_addresses
        check_global_addresses([camera,self])
        self.buffers[0][:]=0;self.buffers[0][:,:,3:6]=255;self.buffers[0].flush()
        self.regs.write(CONTROL,1<<16)
        self.regs.write(0x18,3)
        self.regs.write(HSIZE,self.width*8);self.regs.write(STRIDE,self.width*8)
        for i,buffer in enumerate(self.buffers):
            self.regs.write(ADDRESS+4*i,int(buffer.device_address))
            if self.regs.read(ADDRESS+4*i)!=int(buffer.device_address):
                raise RuntimeError('UI DMA address readback mismatch')
        self.regs.write(PARK,0)
        self.regs.write(CONTROL,(1<<16)|1)
        self.regs.write(VSIZE,self.height)

    @staticmethod
    def _validate_layout(camera):
        for name,base,kind in (('ui_vdma',0x43010000,'axi_vdma:6.3'),
                               ('ui_status_gpio0',0x41290000,'axi_gpio:2.0'),
                               ('ui_status_gpio1',0x412a0000,'axi_gpio:2.0')):
            entry=camera.overlay.ip_dict.get(name,{})
            if (int(entry.get('phys_addr',-1))!=base or int(entry.get('addr_range',0))!=0x10000
                    or entry.get('type')!='xilinx.com:ip:'+kind):
                raise RuntimeError('Wrong UI overlay mapping: '+name)
        root=ET.parse(Path(camera.bit_path).with_suffix('.hwh')).getroot()
        module=next((m for m in root.findall('.//MODULE') if m.get('INSTANCE')=='ui_vdma'),None)
        if module is None:raise RuntimeError('HWH missing independent UI VDMA')
        params={p.get('NAME'):p.get('VALUE') for p in module.findall('./PARAMETERS/PARAMETER')}
        expected={'C_INCLUDE_SG':'0','C_INCLUDE_S2MM':'0','C_INCLUDE_MM2S':'1',
                  'C_NUM_FSTORES':'3','C_M_AXIS_MM2S_TDATA_WIDTH':'64',
                  'C_M_AXI_MM2S_DATA_WIDTH':'64','C_M_AXI_MM2S_ADDR_WIDTH':'32'}
        if any(params.get(k)!=v for k,v in expected.items()):
            raise RuntimeError('HWH differs from UI coefficient-plane ABI')

    def read_status(self):
        # Each bundle is coherent in hardware. Read its generation/count twice
        # around the other words so a concurrent status refresh cannot fabricate
        # a presentation from mismatched snapshots.
        for _ in range(8):
            a=int(self.status0.read(0));count=int(self.status0.read(8))
            video=int(self.status1.read(0));rejected=int(self.status1.read(8))
            if a==int(self.status0.read(0)) and count==int(self.status0.read(8)):
                return dict(identity=a>>24,presented_tag=a&65535,mirror_error=bool(a&0x10000),
                            presented_count=count,video_count=video,rejected_frames=rejected)
        raise RuntimeError('UI status did not stabilize')

    def _poll(self,fence):
        if self.closed:raise RuntimeError('UI driver closed before presentation')
        error=int(self.regs.read(STATUS))&ERROR_BITS
        if error:raise RuntimeError('UI VDMA error 0x%x; pages retained'%error)
        status=self.read_status()
        if status['mirror_error']:raise RuntimeError('PL mirror stream synchronization failed')
        current=(int(self.regs.read(PARK))>>16)&31
        advanced=(status['presented_count']-fence.before_count)&0xffffffff
        if (status['presented_tag']==fence.sequence and
                0<advanced<0x80000000 and current==fence.page):
            fence.complete=True;self.active=fence.page;self.pending=None;self.presentations+=1
            for sample in reversed(self.samples):
                if sample['revision']==fence.revision:
                    at=time.monotonic()
                    sample['fence_wait_ms']=(at-fence.started)*1000
                    sample['presented_at']=at
                    if sample.get('native_motion') and sample.get('geometry_pose') is not None:
                        if not hasattr(self,'_motion_rates'):self._motion_rates={}
                        name=sample.get('preset_id') or 'unknown'
                        rate=self._motion_rates.get(name)
                        if rate is None:
                            rate=dict(first_at=at,last_at=at,frames=0,last_pose=None,intervals_ms=[])
                            self._motion_rates[name]=rate
                        if tuple(sample['geometry_pose'])!=rate['last_pose']:
                            if rate['frames']:
                                rate['intervals_ms'].append((at-rate['last_at'])*1000.)
                                rate['intervals_ms']=rate['intervals_ms'][-512:]
                            rate['frames']+=1;rate['last_at']=at
                            rate['last_pose']=tuple(sample['geometry_pose'])
                    break
        elif time.monotonic()-fence.started>self.timeout_s:
            raise TimeoutError('UI frame not completely presented; active/pending DMA pages retained')

    def _submission_anchor(self):
        """Prove the target is inactive and old target-tag ACKs are past us."""
        error=int(self.regs.read(STATUS))&ERROR_BITS
        if error:raise RuntimeError('UI VDMA error 0x%x; pages retained'%error)
        status=self.read_status()
        if status['mirror_error']:raise RuntimeError('PL mirror stream synchronization failed')
        park=int(self.regs.read(PARK))
        if (park&31)!=self.active or ((park>>16)&31)!=self.active:
            raise RuntimeError('UI DMA page differs from confirmed active page; pages retained')
        # Startup page 0 contains transparent pixels with tag 0. It has no
        # application revision and is the only exception to a completed tag.
        expected=0 if self.last_revision<0 else self.active+1
        if status['presented_tag']!=expected:
            raise RuntimeError('UI completed tag differs from confirmed active page; pages retained')
        return status['presented_count']

    def submit_ui_layer(self,prepared):
        if self.closed:raise RuntimeError('UI driver is closed')
        if self.pending is not None:
            if not self.pending.presented():return None
        if prepared.revision<=self.last_revision:return None
        if (prepared.width,prepared.height)!=(self.width,self.height):raise ValueError('UI dimensions changed')
        self._submission_anchor()
        page=(self.active+1)%len(self.buffers)
        sequence=page+1
        started=time.monotonic()
        previous=self._packed_packets[page]
        self._packed_packets[page]=None
        writes=update_ui_plane(prepared,sequence,self.buffers[page],previous,
                               previous_sequence=sequence if previous is not None else None)
        packed=time.monotonic()
        first,last=writes.get('dirty_first_row',0),writes.get('dirty_last_row',self.height)
        if not 0<=first<=last<=self.height:
            raise ValueError('UI writer returned an invalid cache flush range')
        if first<last:
            # A full-row PynqBuffer view is contiguous; its flush preserves the
            # BO offset/virtual address and never touches an active DMA page.
            if first==0 and last==self.height:self.buffers[page].flush()
            else:self.buffers[page][first:last].flush()
        flushed=time.monotonic()
        before=self._submission_anchor()
        self._packed_packets[page]=prepared
        fence=UIPlaneFence(self,page,sequence,prepared.revision,before)
        # Retain ownership before the first uncertain MMIO publication.
        self.pending=fence
        self.regs.write(PARK,(int(self.regs.read(PARK))&~31)|page)
        self.sequence=sequence;self.last_revision=prepared.revision;self.submissions+=1
        self.samples.append(dict(pack_ms=(packed-started)*1000,flush_ms=(flushed-packed)*1000,
                                 sync_requested_bytes=(last-first)*self.width*8,
                                 ui_bo_cacheable=False,
                                 native_motion=bool(getattr(prepared,'native_opaque',False)),
                                 geometry_pose=getattr(prepared,'geometry_pose',None),
                                 preset_id=getattr(prepared,'preset_id',None),
                                 revision=prepared.revision,sequence=sequence,transport_tag=sequence,page=page,**writes))
        self.samples=self.samples[-512:]
        return fence

    def metrics(self):
        motion_rates={name:dict(frames=rate['frames'],
            duration_s=rate['last_at']-rate['first_at'],
            presented_unique_geometry_hz=(rate['frames']-1)/(rate['last_at']-rate['first_at'])
                if rate['last_at']>rate['first_at'] else 0.,intervals_ms=list(rate['intervals_ms']))
            for name,rate in self._motion_rates.items()}
        return dict(mode='PL_BACKGROUND_WITH_INDEPENDENT_UI',submitted=self.submissions,
                    presented=self.presentations,active_page=self.active,
                    pending_revision=self.pending.revision if self.pending else None,
                    last_submitted_revision=self.last_revision,last_transport_tag=self.sequence,
                    tag_mode='fixed_per_dma_page',page_tags=[1,2,3],
                    cache_flush_scope='uncached UI BO; XRT synchronization retained',
                    ui_bo_cacheable=False,
                    motion_presentations=motion_rates,
                    motion_rate_window='first to latest unique geometry; gaps are included',
                    status=self.read_status(),upload_samples=list(self.samples),
                    acknowledgement='complete HDMI frame after different-page anchor; forward count, fixed page tag and current DMA page')

    def close(self):
        if self.closed:return True
        if self.regs is not None:
            self.regs.write(CONTROL,int(self.regs.read(CONTROL))&~1)
            deadline=time.monotonic()+self.timeout_s
            while not int(self.regs.read(STATUS))&1:
                if time.monotonic()>=deadline:raise TimeoutError('UI DMA halt unconfirmed; buffers retained')
                time.sleep(.001)
        for buffer in self.buffers:buffer.freebuffer()
        self.buffers.clear();self.closed=True
        self._packed_packets.clear()
        return True
