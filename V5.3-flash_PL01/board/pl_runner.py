"""Future PL01 owner-held executor. No PYNQ import, overlay load, or implicit free."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import numpy as np
from register_map import assert_compatible
from pl_batch import NativeBatch,prepare_operations

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'compiler'))
from replay_plan import tail_heads,verify_files
sys.path.insert(0,str(ROOT/'host'))
MODEL_SHA='e210ef35bdd9ea8e6f622cda6eba92ef5ef4b2d80371bdc204d39914d23bb394'

_RETAINED={}


class PLExecutionError(RuntimeError):
    def __init__(self,message,runner,result=None):
        super().__init__(message);self.runner=runner;self.result=result


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def valid_sha(value):return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def verify_overlay_mapping(overlay,mmio,proof,minimum_bytes):
    """Read Python identity metadata and files only; never read MMIO registers."""
    loaded=getattr(overlay,'is_loaded',None)
    if loaded is None or not bool(loaded() if callable(loaded) else loaded):raise ValueError('qualified overlay is not loaded')
    try:
        bit=Path(overlay.bitfile_name).resolve(strict=True);expected=Path(proof['bitfile_name']).resolve(strict=True)
        hwh=Path(proof['hwh_path']).resolve(strict=True)
    except (AttributeError,KeyError,TypeError,OSError) as error:raise ValueError('qualified bit/HWH paths missing') from error
    if bit!=expected or hwh!=bit.with_suffix('.hwh').resolve(strict=True):raise ValueError('actual overlay bitfile/HWH path differs')
    if digest(bit)!=proof['bitstream_sha256'] or digest(hwh)!=proof['hwh_sha256']:raise ValueError('actual overlay bit/HWH SHA differs')
    try:
        instance=proof['ip_instance'];expected_type=proof['ip_type'];ip=overlay.ip_dict[instance]
        base=int(proof['ip_base_addr']);length=int(proof['ip_addr_range'])
        if expected_type.split(':')[-2]!='fastest_accel' or ip['type']!=expected_type:raise ValueError('actual IP type is not the qualified fastest_accel')
        if int(ip['phys_addr'])!=base or int(ip['addr_range'])!=length:raise ValueError('actual overlay IP base/range differs')
        if int(mmio.base_addr)!=base or int(mmio.length)!=length or length<minimum_bytes:raise ValueError('MMIO mapping does not match qualified IP base/range')
    except (AttributeError,KeyError,TypeError,IndexError) as error:raise ValueError('qualified accelerator instance/mapping metadata missing') from error
    return dict(bitfile_name=str(bit),hwh_path=str(hwh),ip_instance=instance,ip_type=expected_type,base_addr=base,length=length)


class PLRunner:
    def __init__(self,overlay,mmio,allocate,owner_check,hardware_proof,*,cache_sync,cache_proof,
                 package=ROOT/'models/quant_2020',plan=ROOT/'reports/physical_bundle_2402/plan',
                 register_map=ROOT/'board/register_map.json',native=None,library=None,shutdown_owner_check=None):
        self.overlay=overlay;self.mmio=mmio;self.owner_check=owner_check;self.cache_sync=cache_sync
        self.shutdown_owner_check=shutdown_owner_check
        self.buffers={};self.released_names=set();self.closed=False;self.released=False;self.failed=False;self.lock=threading.Lock();self.last_result=None;self.release_thread_id=None
        self.register_map=json.loads(Path(register_map).read_text());assert_compatible(self.register_map,hardware_proof)
        if hardware_proof.get('status')!='PL01_LOADED_OVERLAY_QUALIFIED':raise ValueError('new loaded overlay qualification required')
        if hardware_proof.get('driver_sha256')!=digest(__file__):raise ValueError('loaded driver qualification differs')
        if cache_proof.get('status')!='CACHE_RANGE_DMA_VERIFIED':raise ValueError('target source + dirty-range DMA cache proof missing')
        for key in ('bitstream_sha256','hwh_sha256','driver_sha256'):
            if not valid_sha(hardware_proof.get(key)) or hardware_proof[key]!=cache_proof.get(key):raise ValueError('cache proof hardware/driver binding differs: '+key)
        if not all(valid_sha(cache_proof.get(x)) for x in ('buffer_source_sha256','device_source_sha256','probe_report_sha256')):raise ValueError('cache proof source/report SHA missing')
        if not callable(cache_sync) or not callable(owner_check) or not owner_check():raise ValueError('verified owner and cache range adapter required')
        self.overlay_identity=verify_overlay_mapping(overlay,mmio,hardware_proof,self.register_map['minimum_mmio_bytes'])
        self.registers=np.asarray(mmio.array)
        if self.registers.dtype!=np.uint32 or not self.registers.flags.c_contiguous or self.registers.nbytes<self.register_map['minimum_mmio_bytes']:raise ValueError('qualified MMIO array differs')
        if not self.idle():raise ValueError('qualified accelerator must be idle before allocating')
        package=Path(package);plan=Path(plan);package_manifest=json.loads((package/'manifest.json').read_text());verify_files(package,package_manifest)
        verify_files(plan,json.loads((plan/'manifest.json').read_text()));self.plan=json.loads((plan/'plan.json').read_text())
        if self.plan['package_manifest_sha256']!=digest(package/'manifest.json') or hardware_proof.get('package_manifest_sha256')!=digest(package/'manifest.json'):raise ValueError('quant package identity differs')
        if hardware_proof.get('physical_plan_sha256')!=digest(plan/'plan.json') or self.plan['model_sha256']!=MODEL_SHA or package_manifest['model_sha256']!=MODEL_SHA:raise ValueError('physical plan/model identity differs')
        if self.plan['operation_count']!=96 or self.plan['operation_counts']!={'0':79,'1':1,'2':1,'3':15}:raise ValueError('only current frozen 96-op model is supported')
        if native is None:
            if library is None:library=ROOT/'board/build_pl_batch'/('host/pl_batch.dll' if os.name=='nt' else 'armv7/libpl_batch.so')
            native=NativeBatch(library,self.register_map)
        if native.library_sha256!=hardware_proof.get('native_library_sha256'):raise ValueError('qualified native batch library differs')
        self.native=native;self.cache_proof=dict(cache_proof);self.hardware_proof=dict(hardware_proof)
        self.input_shape=tuple(self.plan['input']['shape']);self.input_exponent=self.plan['input']['exponent']
        _RETAINED[id(self)]=self
        try:
            self.buffers['workspace']=allocate(shape=(self.plan['workspace_elements'],),dtype=np.int16)
            self.buffers['workspace'].fill(0)
            self._sync('workspace',0,self.buffers['workspace'].nbytes,'flush')
            for name,file,dtype in [('weights','weights_padded.bin',np.uint64),('bias','bias.bin',np.int64),('shift','shift.bin',np.int32),
                                    ('maps','channel_offsets.bin',np.uint32),('channel_shifts','channel_shifts.bin',np.int32)]:
                source=package/file if name in ('weights','bias','shift') else plan/file;data=source.read_bytes()
                if len(data)%np.dtype(dtype).itemsize:raise ValueError('static payload alignment differs')
                buffer=allocate(shape=(len(data)//np.dtype(dtype).itemsize,),dtype=dtype);self.buffers[name]=buffer
                np.asarray(buffer).view(np.uint8).reshape(-1)[:]=np.frombuffer(data,np.uint8)
                self._sync(name,0,buffer.nbytes,'flush')
            self.operations=self._operations();self.native_operations=prepare_operations(self.operations)
        except BaseException as error:
            self.failed=True;raise PLExecutionError('initialization failed; allocated buffers retained: '+str(error),self) from error

    def _sync(self,name,offset,size,direction):
        buffer=self.buffers[name]
        if offset<0 or size<=0 or offset+size>buffer.nbytes:raise ValueError('cache range outside owned allocation')
        self.cache_sync(buffer,int(offset),int(size),direction)

    def _address(self,name):
        buffer=self.buffers[name];address=getattr(buffer,'device_address',None)
        if address is None:address=getattr(buffer,'physical_address',None)
        if address is None:raise ValueError('physical DMA address missing')
        address=int(address)
        if address<0 or address+buffer.nbytes>2**32:raise ValueError('current generated IP has 32-bit pointers')
        if address%(8 if name in ('workspace','weights','bias') else 4):raise ValueError('DMA base alignment differs')
        return address

    def _operations(self):
        base={name:self._address(name) for name in self.buffers};rows=[]
        for op in self.plan['operations']:
            offset=int(op['output_offset_elements']);count=int(np.prod(op['output_shape']))
            if offset%4 or offset<0 or offset+count>self.plan['workspace_elements']:raise ValueError('physical output span differs')
            row=dict(input_r=base['workspace'],weights=base['weights']+op.get('weight_offset_bytes',0),bias=base['bias']+op.get('bias_offset_bytes',0),
                shift=base['shift']+op.get('shift_offset_bytes',0),output_r=base['workspace']+offset*2,
                channel_offsets=base['maps']+op['channel_offsets_offset_bytes'],channel_shifts=base['channel_shifts']+op['channel_shifts_offset_bytes'],
                input_span_elements=self.plan['workspace_elements'])
            row.update({key:int(op[key]) for key in ('op','ci','co','ih','iw','kernel','stride','pad','groups','relu','use_channel_offsets')})
            for name,arg in self.register_map['arguments'].items():
                if row[name]<0 or row[name]>=2**arg['width_bits']:raise ValueError('argument cannot fit generated register width: '+name)
            rows.append(row)
        return rows

    def idle(self):
        value=int(self.registers[self.register_map['control_offset']//4])
        return bool(value&self.register_map['idle_mask']) and not bool(value&(self.register_map['start_mask']|self.register_map['auto_restart_mask']))

    def infer_heads(self,input_i16,timeout_s=10.):
        if not self.lock.acquire(False):raise PLExecutionError('batch already active; buffers retained',self)
        batch_started=False
        try:
            if self.closed or self.released or self.failed:raise PLExecutionError('runner closed/failed; explicit owner recovery required',self)
            if not self.owner_check():raise PLExecutionError('hardware owner proof no longer current',self)
            values=np.asarray(input_i16)
            if values.dtype!=np.int16 or values.shape!=self.input_shape:raise ValueError('raw signed int16 input shape differs; no implicit preprocessing')
            if not self.idle():raise PLExecutionError('accelerator not idle; buffers retained',self)
            workspace=self.buffers['workspace'];input_elements=values.size
            np.asarray(workspace)[:input_elements]=values.ravel()
            self._sync('workspace',0,input_elements*2,'flush')
            batch_started=True
            result=self.native.run(self.registers,self.native_operations,timeout_s);self.last_result=result
            if result.code:
                self.failed=True;raise PLExecutionError('native batch failed (%d), DMA buffers and owner retained'%result.code,self,result)
            if not self.owner_check() or not self.idle():
                self.failed=True;raise PLExecutionError('post-batch owner/idle proof missing; buffers retained',self,result)
            for view in self.plan['ps_logits'].values():
                offsets=view['channel_offsets_elements'];plane=view['plane_elements'];start=offsets[0]
                if offsets!=[start+i*plane for i in range(len(offsets))]:raise ValueError('noncontiguous PS logit allocation')
                self._sync('workspace',start*2,int(np.prod(view['shape']))*2,'invalidate')
            heads=tail_heads(self.plan,np.asarray(workspace),None)
            return [heads[name] for name in self.plan['output_names']]
        except BaseException:
            # Retain even on KeyboardInterrupt; close never resets or frees.
            if batch_started:self.failed=True
            raise
        finally:self.lock.release()

    def infer(self,input_i16,timeout_s=10.):
        heads=self.infer_heads(input_i16,timeout_s)
        return decode_heads(heads)

    def close(self):
        self.closed=True
        return dict(buffers_retained=not self.released,ap_idle=None,batch_active=self.lock.locked(),failed=self.failed)

    def release(self,*,owner_confirmed=False):
        if self.released:return True
        if not self.closed or not self.lock.acquire(False):return False
        self.release_thread_id=threading.get_ident()
        try:
            check=self.shutdown_owner_check or self.owner_check
            if not owner_confirmed or not check() or not self.idle():return False
            self.closed=True
            for name,buffer in self.buffers.items():
                if name not in self.released_names:buffer.freebuffer();self.released_names.add(name)
            self.released=True;_RETAINED.pop(id(self),None);return True
        finally:self.release_thread_id=None;self.lock.release()


def decode_heads(heads):
    if [list(x.shape) for x in heads]!=[[1,24,24,17],[1,12,12,17]]:raise ValueError('expected two probability NHWC heads')
    anchors=np.array([[12.64,19.39],[37.88,51.48],[55.71,138.31],[126.91,78.23],[131.57,214.55],[279.92,258.87]],np.float32);rows=[]
    for index,head in enumerate(heads):
        _,height,width,_=head.shape;reg=head[...,:12].reshape(1,height,width,3,4).transpose(0,3,1,2,4)
        obj=head[...,12:15].transpose(0,3,1,2)[...,None];classes=np.broadcast_to(head[:,None,...,15:17],(1,3,height,width,2))
        y,x=np.meshgrid(np.arange(height),np.arange(width),indexing='ij');grid=np.stack((x,y),-1).astype(np.float32)[None,None]
        xy=(reg[...,:2]*2-.5+grid)*np.float32(384/height);wh=(reg[...,2:4]*2)**2*anchors[index*3:index*3+3][None,:,None,None]
        rows.append(np.concatenate((xy,wh,obj,classes),-1).reshape(1,-1,7))
    return np.concatenate(rows,1)
