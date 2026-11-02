"""Opt-in diagnostic run adapter. Original _invoke and release remain intact."""
import ctypes
import copy
import hashlib
import math
import mmap
from pathlib import Path
import sys
import threading
import time

import numpy as np

EXECUTOR_SHA256='93cc6db8bc2a2c7030ad8565a8a2adacad7ef48222d2704d21fee8a97b51ddb2'
PHASE_NAMES=('load','conv','activation_store','total','alignment_or_add','weight_load')


def _load_library(path):
    library=ctypes.CDLL(str(Path(path).resolve()))
    library.batch_scheduler_abi.argtypes=[];library.batch_scheduler_abi.restype=ctypes.c_uint32
    if library.batch_scheduler_abi()!=1:raise RuntimeError('Unexpected batch scheduler ABI')
    library.run=library.batch_scheduler_run
    library.run.argtypes=[ctypes.c_void_p]*8+[ctypes.c_size_t,ctypes.c_double,
        ctypes.c_uint32,ctypes.c_uint32]+[ctypes.c_void_p]*3
    library.run.restype=ctypes.c_int
    return library


def _region_module(executor):
    module=sys.modules[type(executor).__module__]
    if type(executor) is not getattr(module,'RegionExecutor',None):
        raise RuntimeError('Expected existing RegionExecutor')
    if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()!=EXECUTOR_SHA256:
        raise RuntimeError('RegionExecutor changed; batch candidate requires review')
    return module


def _mapped_array(mmio,address):
    from pynq import MMIO
    if (type(mmio) is not MMIO or not mmio.device.has_capability('MEMORY_MAPPED')
            or getattr(mmio.read,'__func__',None) is not MMIO.read
            or getattr(mmio.write,'__func__',None) is not MMIO.write_mm):
        raise RuntimeError('Only original PYNQ MEMORY_MAPPED MMIO is supported')
    array=mmio.array
    if (type(array) is not np.ndarray or array.dtype!=np.dtype('<u4')
            or array.ndim!=1 or not array.flags.c_contiguous or not array.flags.writeable
            or not array.flags.aligned or mmio.base_addr!=address or mmio.length!=0x10000
            or array.nbytes!=mmio.length):raise RuntimeError('Unexpected existing MMIO mapping')
    backing=array.base
    if isinstance(backing,memoryview):backing=backing.obj
    if not isinstance(backing,mmap.mmap):raise RuntimeError('MMIO must retain original mmap backing')
    return array


class BatchScheduler:
    def __init__(self,executor,library,array,timers):
        self.executor=executor;self.library=library;self.array=array;self.timers=timers
        self.module=_region_module(executor);self.original=executor.run
        # Serialize against the current C1 per-op adapter as well as ourselves.
        self.lock=getattr(getattr(executor,'_native_scheduler',None),'lock',threading.Lock())
        self.last_detail=None
        self._schedule_key=None;self._descriptor_snapshot=None
        self._values=np.empty((87,28),np.int64)
        self._instrumented=np.empty(87,np.uint32)
        self._cache_values=np.empty(28,np.int64);self._valid=np.empty(28,np.uint32)
        self._reports=np.empty((87,13),np.uint32);self._timings=np.empty(87,np.float64)
        self._state=np.empty(2,np.int32)

    def _prepare_schedule(self,diagnostic):
        owner=self.executor
        # BO identity matters even when an allocator reuses a physical address.
        # Numeric qualification replaces head BOs with guarded allocations.
        bindings=tuple((name,id(buffer),int(buffer.physical_address))
                       for name,buffer in owner.tensors.items())
        parameters=tuple((id(buffer),int(buffer.physical_address))
                         for buffer in (owner.weight,owner.bias,owner.requant,owner.acc))
        key=(int(diagnostic),id(owner.descriptors),tuple(map(id,owner.descriptors)),bindings,parameters)
        # A private deep snapshot also catches in-place edits to nested lists;
        # descriptor identity alone cannot establish an unchanged register table.
        if key==self._schedule_key and owner.descriptors==self._descriptor_snapshot:return True
        rows=[]
        for d in owner.descriptors:
            second=(int(owner.weight.physical_address)+d['weight_padded_offset'] if d['op_type'].startswith('Conv')
                else int(owner.tensors[d['sources'][1] if len(d['sources'])>1 else d['sources'][0]].physical_address))
            rows.append([int(owner.tensors[d['sources'][0]].physical_address),second,
                int(owner.bias.physical_address)+d.get('bias_offset',0),
                int(owner.requant.physical_address)+d.get('quant_parameter_offset',0),int(owner.acc.physical_address),
                int(owner.tensors[d['output_tensor']].physical_address)]+self.module.register_values(d,diagnostic))
        self._values[:]=rows
        self._instrumented[:]=[d['op_type'] not in ('MaxPool','Resize') for d in owner.descriptors]
        self._descriptor_snapshot=copy.deepcopy(owner.descriptors)
        self._schedule_key=key
        return False

    def invoke_all(self,diagnostic=False,read_cycles=False,timeout=20):
        owner=self.executor
        if not owner.buffers or owner.poisoned:
            raise RuntimeError('Closed/failed executor; owned buffers retained until verified idle')
        if not math.isfinite(timeout) or timeout<=0:raise ValueError('Positive finite per-op timeout required')
        if len(owner.descriptors)!=87:raise ValueError('Batch candidate requires exactly 87 invocations')
        if owner.regs.array is not self.array:raise RuntimeError('MMIO mapping changed')
        if read_cycles and (len(self.timers)!=3 or len(getattr(owner,'timers',[]) or [])!=3
                or any(t.array is not a for t,a in zip(owner.timers,self.timers))):
            raise RuntimeError('Qualified phase mappings changed')
        if not self.lock.acquire(False):raise RuntimeError('Concurrent native invocation refused')
        try:
            self.last_detail=None
            prepare=time.perf_counter();offsets=self.module.POINTER_OFFSETS+self.module.SCALAR_OFFSETS
            cache=getattr(owner,'_config_cache',None)
            if cache is None:
                owner._config_cache=cache={};owner._config_write_count=owner._config_write_changed_count=0
            cache_hit=self._prepare_schedule(diagnostic)
            values=self._values;instrumented=self._instrumented
            cache_values=self._cache_values;valid=self._valid
            # The executor's register cache is authoritative, including explicit
            # clear/replacement after a package load or an external invocation.
            for i,offset in enumerate(offsets):
                cache_values[i]=cache.get(offset,0);valid[i]=offset in cache
            reports=self._reports;timings=self._timings;state=self._state
            reports.fill(0);timings.fill(0);state[:]=[0,-1]
            pointers=[a.ctypes.data for a in self.timers] if read_cycles else [None]*3
            prepared=time.perf_counter()
            try:
                code=self.library.run(self.array.ctypes.data,*pointers,values.ctypes.data,cache_values.ctypes.data,
                    valid.ctypes.data,instrumented.ctypes.data,87,timeout,100000,int(read_cycles),
                    reports.ctypes.data,timings.ctypes.data,state.ctypes.data)
            finally:
                returned=time.perf_counter()
                for offset,value,present in zip(offsets,cache_values,valid):
                    if present:cache[offset]=int(value)
                owner._config_write_count+=int(reports[:,0].sum())
                owner._config_write_changed_count+=int(reports[:,1].sum())
                started_count=int(np.count_nonzero(reports[:,5]==1))
                done_count=int(np.count_nonzero(reports[:,6]==1))
                status_count=int(np.count_nonzero(reports[:,4]))
                rows=[];phases=[]
                if diagnostic or read_cycles:
                    for i in range(87):
                        if not reports[i,5]:continue
                        row=dict(invocation=i,graph_index=owner.descriptors[i]['graph_index'],host_invoke_ms=float(timings[i]),
                            config_write_count=int(reports[i,0]),config_write_changed_count=int(reports[i,1]),
                            polls=int(reports[i,2]),sleeps=int(reports[i,3]),status=int(reports[i,4]),done=bool(reports[i,6]))
                        rows.append(row)
                        if read_cycles and i<int(state[0]):
                            phase_row=dict(invocation=i,graph_index=row['graph_index'],host_invoke_ms=row['host_invoke_ms'],
                                hardware_phase_cycles={k:int(v) for k,v in zip(PHASE_NAMES,reports[i,7:])} if instrumented[i] else None,
                                hardware_cycles_status='READ_QUALIFIED_GPIO_PHASE_COUNTERS' if instrumented[i] else 'NOT_INSTRUMENTED_FOR_THIS_OPERATOR')
                            if owner.descriptors[i]['op_type'].startswith('Conv'):
                                phase_row.update(hardware_phase_cycles={'total':int(reports[i,10])},
                                    hardware_cycles_status='MEASURED_PIPELINE_INTERVAL_TOTAL_ONLY',
                                    phase7_protocol_observed=bool(reports[i,10]>0 and
                                        not any(reports[i,j] for j in (7,8,9,11,12))),
                                    exclusive_stage_cycles_status='NOT_MEASURED',
                                    phase_interval='After capacity preflight, before bias preload, through final pipeline drain')
                            phases.append(phase_row)
                self.last_detail=dict(batch_prepare_ms=(prepared-prepare)*1000,batch_native_ms=(returned-prepared)*1000,
                    batch_schedule_cache_hit=cache_hit,batch_started_count=started_count,batch_done_count=done_count,
                    batch_status_nonzero_count=status_count,batch_polls=int(reports[:,2].sum()),batch_sleeps=int(reports[:,3].sum()),
                    batch_completed=int(state[0]),batch_failed_index=int(state[1]),batch_op_timings=rows,
                    phase_counter_semantics='pipeline_phase7_v1',
                    host_invoke_timing_scope='NATIVE_CONFIG_START_WAIT_STATUS_EXCLUDES_PYTHON_PREPARATION_AND_PHASE_READS')
                self.last_detail['batch_detail_ms']=(time.perf_counter()-returned)*1000
            if code==1:raise TimeoutError('PL operation %d timed out; all CMA retained'%state[1])
            if code==2:
                failed=int(state[1]);status=int(reports[failed,4]) if 0<=failed<87 else None
                raise RuntimeError('PL rejected operation %d with status %s'%(failed,status))
            if code:raise RuntimeError('Native batch error %d at operation %d; all CMA retained'%(code,state[1]))
            if state[0]!=87 or state[1]!=-1 or started_count!=87 or done_count!=87 or status_count:
                raise RuntimeError('Incomplete/failed native batch; all CMA retained')
            return phases,self.last_detail
        except BaseException:
            owner.poisoned=True;raise
        finally:self.lock.release()

    def run(self,inputs,diagnostic=False,collect_phase_cycles=False,copy_outputs=True):
        """C1 run's unchanged I/O lifecycle, replacing only its invocation loop."""
        owner=self.executor
        if not owner.buffers or owner.poisoned:raise RuntimeError('Closed/failed executor; owned buffers retained until verified idle')
        read_cycles=diagnostic or collect_phase_cycles
        if read_cycles and not getattr(owner,'timers',None):raise RuntimeError('Hardware phase counters not qualified by Overlay metadata')
        if not isinstance(inputs,dict) or set(inputs)!=set(owner.input_shapes):raise ValueError('Full graph requires exact input names')
        validation_started=time.monotonic()
        for name,dims in owner.input_shapes.items():
            value=inputs[name];expected=np.dtype(np.int8 if owner.package['tensor_bits'][name]==8 else np.int16)
            if not isinstance(value,np.ndarray) or value.dtype!=expected or value.shape not in (dims,(1,)+dims) or not value.flags.c_contiguous:
                raise ValueError('Expected contiguous integer NCHW/CHW input '+name)
            self.module.validate_symmetric_input(value)
        input_validation_ms=(time.monotonic()-validation_started)*1000
        owner.wait_idle();start=time.perf_counter();copy_ms=0.;cache_ms=0.
        for name,value in inputs.items():
            target=owner.tensors[name];before=time.perf_counter();same=value.ctypes.data==target.ctypes.data
            if not same:target[:value.size]=value.ravel()
            copied=time.perf_counter();target.flush();flushed=time.perf_counter()
            copy_ms+=0 if same else (copied-before)*1000;cache_ms+=(flushed-copied)*1000
        copied_at=time.perf_counter()
        count_before=owner._config_write_count;changed_before=owner._config_write_changed_count
        phases,detail=self.invoke_all(diagnostic,read_cycles)
        invoked_at=time.perf_counter();outputs={};invalid_ms=0.;output_copy_ms=0.
        for boundary in owner.boundaries:
            buffer=owner.tensors[boundary['name']];before=time.perf_counter();buffer.invalidate();invalidated=time.perf_counter()
            value=buffer[:int(np.prod(boundary['shape']))].reshape(boundary['shape'][1:])
            outputs[boundary['name']]=np.array(value,copy=True) if copy_outputs else value
            invalid_ms+=(invalidated-before)*1000;output_copy_ms+=(time.perf_counter()-invalidated)*1000
        end=time.perf_counter()
        return outputs,dict(input_copy_ms=copy_ms,input_flush_ms=cache_ms,input_copy_flush_ms=(copied_at-start)*1000,
            invoke_ms=(invoked_at-copied_at)*1000,host_scheduler='NATIVE_BATCH_MMIO',
            output_invalidate_ms=invalid_ms,output_copy_ms=output_copy_ms,output_invalidate_copy_ms=(end-invoked_at)*1000,
            total_ms=(end-start)*1000,phase_cycles=phases,graph_operations=len(owner.package['operations']),
            core_invocations=len(owner.descriptors),hardware_cycles_status='PARTIAL_OPERATOR_COVERAGE' if read_cycles else 'NOT_READ',
            input_validation_ms=input_validation_ms,config_write_count=owner._config_write_count-count_before,
            config_write_changed_count=owner._config_write_changed_count-changed_before,**detail)

    def restore(self):
        self.executor.wait_idle()
        if getattr(self.executor.run,'__self__',None) is not self:raise RuntimeError('Run method changed after batch installation')
        self.executor.run=self.original


def install(executor,library_path):
    if sys.platform!='linux':raise RuntimeError('Hardware installation requires existing Linux owner')
    module=_region_module(executor)
    if (not getattr(executor,'qualification',None) or not executor.buffers or executor.poisoned
            or getattr(executor.run,'__func__',None) is not module.RegionExecutor.run):
        raise RuntimeError('Expected healthy qualified executor with original run')
    array=_mapped_array(executor.regs,0x43c00000)
    timers=getattr(executor,'timers',None)
    if timers and len(timers)!=3:raise RuntimeError('Expected three qualified phase GPIOs')
    arrays=[_mapped_array(mmio,address) for mmio,address in zip(timers or [],module.PHASE_GPIO_ADDRESSES)]
    library=_load_library(library_path);executor.wait_idle()
    adapter=BatchScheduler(executor,library,array,arrays);executor.run=adapter.run
    return adapter
