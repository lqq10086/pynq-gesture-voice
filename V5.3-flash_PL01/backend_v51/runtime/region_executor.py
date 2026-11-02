"""One shared IP3.2 executes the full integer graph; caller owns safe lifecycle."""
from pathlib import Path
import json
import time
import numpy as np
try:
    from .package_contract import validate_package
    from .input_range import validate_symmetric_input
except ImportError:
    from package_contract import validate_package
    from input_range import validate_symmetric_input

POINTER_OFFSETS=(0x18,0x20,0x28,0x30,0x38,0x40)
SCALAR_OFFSETS=tuple(range(0x48,0xf8,8))
HARDWARE_IP='xilinx.com:hls:conv_accel:3.2'
PHASE_GPIO_ADDRESSES=(0x41230000,0x41240000,0x41250000)

def check_global_addresses(executors_or_buffers):
    intervals=[]
    for owner in executors_or_buffers:
        for buffer in owner.buffers if hasattr(owner,'buffers') else [owner]:
            lo=int(buffer.physical_address);hi=lo+int(buffer.nbytes)
            if lo%8 or not 0<=lo<hi<=0x40000000:raise RuntimeError('Unaligned buffer or outside HP1 DDR window')
            if any(lo<old_hi and hi>old_lo for old_lo,old_hi in intervals):raise RuntimeError('Overlapping live CMA buffers')
            intervals.append((lo,hi))
    return intervals

def register_values(desc,diagnostic=False):
    kind=desc['op_type'];conv=kind.startswith('Conv');alignment=desc.get('multipliers',[0,0])
    activation=1 if kind=='ConvLinear' else (0 if desc.get('silu_fraction_bits')==16 else 2)
    return [desc['Cin'],desc['Cout'],desc['H'],desc['W'],desc['kernel'],desc.get('stride',1),desc.get('padding',0),
            (1<<24) if kind=='ConvLinear' else desc.get('output_multiplier',0),int(diagnostic),0,
            alignment[0],alignment[1],22,0,desc['input_bits'][0],desc['activation_bits'],
            desc['input_bits'][1] if len(desc['input_bits'])>1 else desc['input_bits'][0],activation if conv else 0,
            desc.get('dataflow_mode',0),desc['input_height'],desc['input_width'],desc.get('weight_bits',8)]

def validate_execution_descriptor(desc):
    """Match the qualified IP3.2 array bounds after any dataflow override."""
    ci,co,ih,iw,h,w=(desc[k] for k in ('Cin','Cout','input_height','input_width','H','W'))
    if not (1<=ci<=512 and 1<=co<=512 and 1<=ih<=384 and 1<=iw<=384
            and 1<=h<=192 and 1<=w<=192 and ci*ih*iw<=589824 and co*h*w<=589824):
        raise ValueError('Descriptor exceeds IP3.2 tensor bounds: '+desc['name'])
    kind=desc['op_type']
    if kind in ('MaxPool','Resize'):
        if ci!=co or iw>28 or h!=(ih if kind=='MaxPool' else 2*ih):
            raise ValueError('Descriptor exceeds pool/resize bounds: '+desc['name'])
    elif kind.startswith('Conv'):
        mode=desc.get('dataflow_mode',0)
        if type(mode) is not int or mode not in (0,1):
            raise ValueError('Invalid dataflow mode: '+desc['name'])
        kernel,stride,pad=desc['kernel'],desc.get('stride',1),desc.get('padding',0)
        if ci*kernel*kernel>1152:
            raise ValueError('Descriptor exceeds coefficient cache: '+desc['name'])
        rowsmax=2 if stride==2 else 4
        if mode==1 and h<=7:rowsmax=h
        ep=8 if desc['input_bits'][0]==8 else 4
        for ty in range(0,h,rowsmax):
            rows=min(h-ty,rowsmax)
            begin=max(ty*stride-pad,0)
            end=min((ty+rows-1)*stride+kernel-pad,ih)
            pitch=(((end-begin)*iw+2*ep-2)//ep)*ep
            if ci*pitch>28672 or rows*w>448:
                raise ValueError('Descriptor exceeds pixel/output tile cache: '+desc['name'])

class RegionExecutor:
    def __init__(self,package_dir,mmio_factory=None,allocator=None,base_address=0x43c00000,dataflow_plan=None,qualified_overlay=None):
        use_native=mmio_factory is None and allocator is None
        package=validate_package(package_dir)
        plan_path=Path(package_dir).parent/'layer_dataflow_plan.json'
        plan=dataflow_plan if dataflow_plan is not None else (json.loads(plan_path.read_text()) if plan_path.exists() else None)
        if qualified_overlay is not None or mmio_factory is None or allocator is None:
            try:from .qualification import verify_runtime,ROOT
            except ImportError:from qualification import verify_runtime,ROOT
            if plan is None:plan=json.loads((ROOT/'layer_dataflow_plan.json').read_text())
            if base_address!=0x43c00000:raise ValueError('Unqualified accelerator base address')
            self.qualification=verify_runtime(overlay=qualified_overlay,execution_plan=plan)
        if plan:
            manifest=package['manifest']
            if (plan.get('architecture_id')!=manifest['architecture_id'] or
                    plan.get('source_onnx_sha256')!=manifest['model_sha256'] or
                    plan.get('hardware_abi')!=manifest['hardware_abi']):
                raise ValueError('Dataflow plan identity differs from the validated package')
            chosen={v['node']:v['dataflow_mode'] for v in plan['layers']}
            conv_names={d['name'] for d in package['descriptors'] if d['op_type'].startswith('Conv')}
            if len(chosen)!=len(plan['layers']) or not set(chosen)<=conv_names:
                raise ValueError('Duplicate or unknown dataflow-plan convolution')
            for d in package['descriptors']:
                if d['op_type'].startswith('Conv') and d['name'] in chosen:
                    d['dataflow_mode']=chosen[d['name']]
        for desc in package['descriptors']:validate_execution_descriptor(desc)
        if mmio_factory is None or allocator is None:
            from pynq import MMIO,allocate
            mmio_factory=mmio_factory or MMIO;allocator=allocator or allocate
        self.buffers=[];self.allocator=allocator;self.poisoned=False
        self._config_cache={};self._config_write_count=0;self._config_write_changed_count=0
        self.timers=None;self._mmio_factory=mmio_factory
        if qualified_overlay is not None:self.bind_phase_counters(qualified_overlay,mmio_factory)
        self.regs=mmio_factory(base_address,0x10000);self.wait_idle()
        self.package_dir=Path(package_dir);self.package=package
        self.descriptors=package['descriptors'];self.boundaries=package['boundaries']
        self.input_shapes=package['inputs'];self.input_scales=package['input_scales']
        self.inputs=[dict(name='images',shape=[1]+list(self.input_shapes['images']),scale=self.input_scales['images'],bits=package['tensor_bits']['images'])]
        try:
            self.tensors={name:self._alloc(int(np.prod(shape)),np.int8 if package['tensor_bits'][name]==8 else np.int16) for name,shape in package['tensor_shapes'].items()}
            self.weight=self._alloc(len(package['blobs']['weights_padded.bin']),np.uint8)
            self.bias=self._alloc(len(package['blobs']['bias_int64.bin'])//8,np.int64)
            self.requant=self._alloc(len(package['blobs']['requant_int32.bin'])//4,np.int32)
            self.acc=self._alloc(package['scratch_count'],np.int64)
            check_global_addresses([self]);self._copy_parameters(package)
            self.input_view=self.tensors['images'][:int(np.prod(self.input_shapes['images']))].reshape((1,)+self.input_shapes['images'])
            if use_native:
                try:from . import native_scheduler
                except ImportError:import native_scheduler
                self._native_scheduler=native_scheduler.install(self,ROOT/'native/libhost_scheduler.so')
                try:from . import native_batch_scheduler
                except ImportError:import native_batch_scheduler
                self._batch_scheduler=native_batch_scheduler.install(self,ROOT/'native/libbatch_scheduler.so')
        except Exception:
            # Construction has not submitted any allocated pointers to the core.
            while self.buffers:
                self.buffers[-1].close();self.buffers.pop()
            raise

    def _alloc(self,count,dtype):
        dtype=np.dtype(dtype);padded=(count*dtype.itemsize+7)//8*8//dtype.itemsize
        buffer=self.allocator(shape=(padded,),dtype=dtype);self.buffers.append(buffer)
        if buffer.dtype!=dtype or buffer.size!=padded or not buffer.flags.c_contiguous:raise RuntimeError('Allocator returned invalid tensor buffer')
        return buffer

    def _copy_parameters(self,package):
        for dst,name,dtype in [(self.weight,'weights_padded.bin',np.uint8),(self.bias,'bias_int64.bin','<i8'),(self.requant,'requant_int32.bin','<i4')]:
            values=np.frombuffer(package['blobs'][name],dtype);dst[:values.size]=values;dst.flush()

    def load_package(self,folder):
        package=validate_package(folder)
        original=self.package
        for key in ('tensor_shapes','tensor_bits'):
            if package[key]!=original[key]:raise ValueError('Changed shapes/dtypes require new executor')
        for filename in ('weights_padded.bin','bias_int64.bin','requant_int32.bin'):
            if len(package['blobs'][filename])!=len(original['blobs'][filename]):raise ValueError('Changed parameter sizes require new executor')
        self.wait_idle();self._config_cache.clear()
        try:self._copy_parameters(package)
        except Exception:self.poisoned=True;raise
        modes=[d['dataflow_mode'] for d in self.descriptors]
        self.package=package;self.package_dir=Path(folder);self.descriptors=package['descriptors'];self.boundaries=package['boundaries'];self.input_scales=package['input_scales']
        self.inputs=[dict(name='images',shape=[1]+list(self.input_shapes['images']),scale=self.input_scales['images'],bits=package['tensor_bits']['images'])]
        for d,mode in zip(self.descriptors,modes):
            d['dataflow_mode']=mode;validate_execution_descriptor(d)

    def _invoke(self,index,diagnostic=False,timeout=20):
        d=self.descriptors[index];conv=d['op_type'].startswith('Conv')
        second=int(self.weight.physical_address)+d['weight_padded_offset'] if conv else int(self.tensors[d['sources'][1] if len(d['sources'])>1 else d['sources'][0]].physical_address)
        pointers=[int(self.tensors[d['sources'][0]].physical_address),second,int(self.bias.physical_address)+d.get('bias_offset',0),
                  int(self.requant.physical_address)+d.get('quant_parameter_offset',0),int(self.acc.physical_address),int(self.tensors[d['output_tensor']].physical_address)]
        cache=getattr(self,'_config_cache',None)
        if cache is None:
            self._config_cache=cache={};self._config_write_count=0;self._config_write_changed_count=0
        writes=0;changed=0
        for offset,value in zip(POINTER_OFFSETS+SCALAR_OFFSETS,pointers+register_values(d,diagnostic)):
            value=int(value);previous=cache.get(offset)
            if previous!=value:
                self.regs.write(offset,value);cache[offset]=value
                writes+=1;changed+=previous is not None
        self._config_write_count+=writes;self._config_write_changed_count+=changed
        self.regs.write(0,1);deadline=time.monotonic()+timeout
        while not self.regs.read(0)&2:
            if time.monotonic()>=deadline:
                self.poisoned=True;raise TimeoutError('PL operation %d timed out; all CMA retained'%index)
            time.sleep(.0001)
        result=self.regs.read(0x10)
        if result:
            self.poisoned=True;raise RuntimeError('PL rejected operation %d with status %d'%(index,result))

    def bind_phase_counters(self,overlay,mmio_factory=None):
        entries=[]
        for index,address in enumerate(PHASE_GPIO_ADDRESSES):
            entry=overlay.ip_dict.get('accel_timer%d'%index)
            if not entry or entry.get('type')!='xilinx.com:ip:axi_gpio:2.0' or entry.get('phys_addr')!=address or entry.get('addr_range')!=0x10000:
                raise RuntimeError('Unqualified phase GPIO metadata: accel_timer%d'%index)
            entries.append(entry)
        factory=mmio_factory or getattr(self,'_mmio_factory',None)
        if factory is None:
            from pynq import MMIO
            factory=MMIO
        self.timers=[factory(entry['phys_addr'],entry['addr_range']) for entry in entries]

    def _phase_cycles(self):
        if not getattr(self,'timers',None):raise RuntimeError('Hardware phase counters not qualified by Overlay metadata')
        a,b,c=self.timers
        return dict(load=int(a.read(0)),conv=int(a.read(8)),activation_store=int(b.read(0)),total=int(b.read(8)),
                    alignment_or_add=int(c.read(0)),weight_load=int(c.read(8)))

    def run(self,inputs,diagnostic=False,collect_phase_cycles=False,copy_outputs=True):
        if not self.buffers or self.poisoned:raise RuntimeError('Closed/failed executor; owned buffers retained until verified idle')
        read_cycles=diagnostic or collect_phase_cycles
        if read_cycles and not getattr(self,'timers',None):raise RuntimeError('Hardware phase counters not qualified by Overlay metadata')
        if not isinstance(inputs,dict) or set(inputs)!=set(self.input_shapes):raise ValueError('Full graph requires exact input names')
        validation_started=time.monotonic()
        for name,dims in self.input_shapes.items():
            value=inputs[name];expected=np.dtype(np.int8 if self.package['tensor_bits'][name]==8 else np.int16)
            if not isinstance(value,np.ndarray) or value.dtype!=expected or value.shape not in (dims,(1,)+dims) or not value.flags.c_contiguous:raise ValueError('Expected contiguous integer NCHW/CHW input '+name)
            validate_symmetric_input(value)
        input_validation_ms=(time.monotonic()-validation_started)*1000
        self.wait_idle();start=time.perf_counter();copy_ms=0.;cache_ms=0.
        for name,value in inputs.items():
            target=self.tensors[name];before=time.perf_counter()
            same=value.ctypes.data==target.ctypes.data
            if not same:target[:value.size]=value.ravel()
            copied=time.perf_counter();target.flush();flushed=time.perf_counter()
            copy_ms+=0 if same else (copied-before)*1000;cache_ms+=(flushed-copied)*1000
        copied_at=time.perf_counter();phases=[]
        config_count_before=self._config_write_count;config_changed_before=self._config_write_changed_count
        for index in range(len(self.descriptors)):
            before=time.perf_counter()
            try:self._invoke(index,diagnostic)
            except Exception:self.poisoned=True;raise
            if read_cycles:
                host_ms=(time.perf_counter()-before)*1000
                instrumented=self.descriptors[index]['op_type'] not in ('MaxPool','Resize')
                phases.append(dict(invocation=index,graph_index=self.descriptors[index]['graph_index'],host_invoke_ms=host_ms,
                    hardware_phase_cycles=self._phase_cycles() if instrumented else None,
                    hardware_cycles_status='READ_QUALIFIED_GPIO_PHASE_COUNTERS' if instrumented else 'NOT_INSTRUMENTED_FOR_THIS_OPERATOR'))
        invoked_at=time.perf_counter();outputs={};invalid_ms=0.;output_copy_ms=0.
        for b in self.boundaries:
            buffer=self.tensors[b['name']];before=time.perf_counter();buffer.invalidate();invalidated=time.perf_counter()
            value=buffer[:int(np.prod(b['shape']))].reshape(b['shape'][1:])
            outputs[b['name']]=np.array(value,copy=True) if copy_outputs else value
            invalid_ms+=(invalidated-before)*1000;output_copy_ms+=(time.perf_counter()-invalidated)*1000
        end=time.perf_counter()
        return outputs,dict(input_copy_ms=copy_ms,input_flush_ms=cache_ms,input_copy_flush_ms=(copied_at-start)*1000,invoke_ms=(invoked_at-copied_at)*1000,
            host_scheduler='NATIVE_MMIO' if getattr(self,'_native_scheduler',None) is self._invoke else 'PYTHON_MMIO',
            output_invalidate_ms=invalid_ms,output_copy_ms=output_copy_ms,output_invalidate_copy_ms=(end-invoked_at)*1000,total_ms=(end-start)*1000,phase_cycles=phases,
            graph_operations=len(self.package['operations']),core_invocations=len(self.descriptors),hardware_cycles_status='PARTIAL_OPERATOR_COVERAGE' if read_cycles else 'NOT_READ',
            input_validation_ms=input_validation_ms,
            config_write_count=self._config_write_count-config_count_before,config_write_changed_count=self._config_write_changed_count-config_changed_before)

    def prepare_input(self):
        if not self.buffers or self.poisoned:raise RuntimeError('Closed/failed executor; input writes forbidden')
        self.wait_idle()
        return self.input_view

    def wait_idle(self,timeout=2.):
        deadline=time.monotonic()+timeout
        while self.regs.read(0)&0x85!=4:
            if time.monotonic()>=deadline:raise TimeoutError('PL idle/start/restart unsafe; CMA buffers remain owned')
            time.sleep(.002)

    def close(self,timeout=2.):
        if not self.buffers:return
        self.wait_idle(timeout)
        while self.buffers:
            self.buffers[-1].close();self.buffers.pop()
