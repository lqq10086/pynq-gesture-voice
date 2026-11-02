"""Benchmark-only V296 FP32/41-op hybrid behavior on the same single IP1.9.

This does not represent an original IP1.8 timing measurement. The caller owns
camera lifecycle; close releases only adapter CMA after the shared core is idle.
"""
import hashlib,importlib.util,json,time
from pathlib import Path
import cv2,numpy as np
try:
    from .inference import preprocess,postprocess
    from .frame_reader import rgb565_to_bgr
    from .region_executor import RegionExecutor,register_values,check_global_addresses,HARDWARE_IP
except ImportError:
    from inference import preprocess,postprocess
    from frame_reader import rgb565_to_bgr
    from region_executor import RegionExecutor,register_values,check_global_addresses,HARDWARE_IP

def validate_baseline(folder):
    folder=Path(folder).resolve();manifest=json.loads((folder/'compat_manifest.json').read_text())
    if manifest.get('production') is not False or manifest.get('scope')!='BENCHMARK_ONLY_V296_NUMERICS_ON_SINGLE_V297_OVERLAY':raise ValueError('Not a benchmark-only baseline adapter')
    if manifest.get('source_model_sha256')!='d48ae064614a4c51ff7e9f62669ab2b112537426c31cd992aa820e467b54e655':raise ValueError('Baseline model drift')
    for name,identity in manifest['files'].items():
        if '/' in name or '\\' in name or ':' in name or name in ('','.','..'):raise ValueError('Unsafe baseline path')
        path=folder/name
        if path.resolve().parent!=folder:raise ValueError('Baseline symlink escape')
        data=path.read_bytes()
        if len(data)!=identity['bytes'] or hashlib.sha256(data).hexdigest()!=identity['sha256']:raise ValueError('Baseline adapter hash mismatch '+name)
    spec=importlib.util.spec_from_file_location('_v296_benchmark_validator',folder/'baseline_validator.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    p=module.validate_region_package(folder);ds=json.loads((folder/'compat_descriptors.json').read_text());wb=(folder/'compat_weights_padded.bin').read_bytes();bias=np.fromfile(folder/'compat_bias_int64.bin','<i8');mult=np.fromfile(folder/'compat_requant_int32.bin','<i4')
    if len(ds)!=41 or manifest['counts']!={'ConvSiLU':30,'Add':7,'Concat':4}:raise ValueError('Baseline operation drift')
    originalw=np.frombuffer(p['blobs']['weights_int8.bin'],np.int8);originalb=np.frombuffer(p['blobs']['bias_int32.bin'],'<i4');originalm=np.frombuffer(p['blobs']['requant.bin'],'<i4')
    for o,d in zip(p['config'],ds):
        for k in ['index','op_type','Cin','Cout','H','W','output_tensor','output_scale']:
            if o[k]!=d[k]:raise ValueError('Baseline descriptor drift '+k)
        sources=[o['source_tensor']] if o['op_type']=='ConvSiLU' else o['sources']
        if d['sources']!=sources or d['input_bits']!=[8]*len(sources) or d['activation_bits']!=8:raise ValueError('Baseline activation drift')
        if d['dataflow_mode']!=0:raise ValueError('Baseline scheduling comparison changed')
        if o['op_type']=='ConvSiLU':
            for k in ['kernel','stride','padding','input_scale','output_multiplier']:
                if d[k]!=o[k]:raise ValueError('Baseline Conv scalar drift '+k)
            if d['silu_fraction_bits']!=8 or register_values(d)[17]!=2:raise ValueError('Baseline must use legacyQ8 SiLU')
            co,ci,k=o['Cout'],o['Cin'],o['kernel'];count=ci*k*k;pitch=(count+7)//8*8;off=d['weight_padded_offset'];raw=np.frombuffer(wb[off:off+co*pitch],np.uint8).reshape(co,pitch)
            w=raw[:,:count].copy().view(np.int8).reshape(co,ci,k,k);ref=originalw[o['weight_offset']:o['weight_offset']+w.size].reshape(w.shape)
            if not np.array_equal(w,ref) or np.any(raw[:,count:]):raise ValueError('Baseline weight drift')
            if not np.array_equal(bias[d['bias_offset']//8:d['bias_offset']//8+co],originalb[o['bias_offset']//4:o['bias_offset']//4+co]):raise ValueError('Baseline bias drift')
            if not np.array_equal(mult[d['quant_parameter_offset']//4:d['quant_parameter_offset']//4+co],originalm[o['quant_parameter_offset']//4:o['quant_parameter_offset']//4+co]):raise ValueError('Baseline requant drift')
        elif d['multipliers']!=o['multipliers'] or d['kernel']!=(0 if o['op_type']=='Add' else 2):raise ValueError('Baseline branch scalar drift')
    p.update(manifest=p['manifest'],compat_manifest=manifest,operations=p['config'],descriptors=ds,tensor_bits={n:8 for n in p['tensor_shapes']},tensor_scales=p['meta']['scales'])
    p['blobs']=dict(p['blobs'],**{'weights_padded.bin':wb,'bias_int64.bin':bias.tobytes(),'requant_int32.bin':mult.tobytes()})
    return p

class BaselineExecutor(RegionExecutor):
    def __init__(self,folder,mmio_factory=None,allocator=None,base_address=0x43c00000):
        p=validate_baseline(folder)
        if mmio_factory is None or allocator is None:
            from pynq import MMIO,allocate
            mmio_factory=mmio_factory or MMIO;allocator=allocator or allocate
        self.buffers=[];self.allocator=allocator;self.poisoned=False;self.regs=mmio_factory(base_address,0x10000);self.wait_idle()
        self.package_dir=Path(folder);self.package=p;self.descriptors=p['descriptors'];self.boundaries=p['boundaries'];self.input_shapes=p['inputs'];self.input_scales=p['input_scales']
        self.inputs=p['manifest']['subgraph_inputs']
        try:
            self.tensors={n:self._alloc(int(np.prod(s)),np.int8) for n,s in p['tensor_shapes'].items()}
            self.weight=self._alloc(len(p['blobs']['weights_padded.bin']),np.uint8);self.bias=self._alloc(len(p['blobs']['bias_int64.bin'])//8,np.int64);self.requant=self._alloc(len(p['blobs']['requant_int32.bin'])//4,np.int32);self.acc=self._alloc(p['scratch_count'],np.int64)
            check_global_addresses([self]);self._copy_parameters(p)
        except Exception:
            while self.buffers:self.buffers.pop().close()
            raise
    def load_package(self,folder):raise RuntimeError('Benchmark baseline adapter cannot switch production packages')

class BaselinePipeline:
    def __init__(self,package_dir,mode='hybrid'):
        if mode not in ('hybrid','ps_only'):raise ValueError('Unknown baseline mode')
        start=time.monotonic();self.folder=Path(package_dir);self.mode=mode;self.executor=None;self.package=validate_baseline(self.folder);self.config=json.loads(self.package['blobs']['config.json']);self.executor_load_ms=None
        cv2.setNumThreads(self.config['threads'])
        def load(name):
            net=cv2.dnn.readNetFromONNX(str(self.folder/name));net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV);net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU);return net
        if mode=='ps_only':self.full=load('reference.onnx')
        else:self.prefix=load('prefix.onnx');self.suffix=load('suffix.onnx')
        self.load_ms=(time.monotonic()-start)*1000
    def attach(self,camera):
        if self.mode=='ps_only':return
        entry=camera.overlay.ip_dict.get('conv_accel_0')
        if not entry or entry['type']!=HARDWARE_IP or int(entry['phys_addr'])!=0x43c00000:raise RuntimeError('Same-overlay baseline requires qualified singleIP1.9')
        start=time.monotonic();self.executor=BaselineExecutor(self.folder);self.executor.bind_phase_counters(camera.overlay);check_global_addresses([camera,self.executor])
        self.executor.bind_phase_counters(camera.overlay,camera._mmio_factory)
        self.qwork={n:np.empty(s,np.float32) for n,s in self.executor.input_shapes.items()};self.qinput={n:np.empty(s,np.int8) for n,s in self.executor.input_shapes.items()};self.dwork={b['name']:np.empty(b['shape'],np.float32) for b in self.executor.boundaries};self.executor_load_ms=(time.monotonic()-start)*1000
    def forward_tensor(self,tensor,diagnostic=False):
        start=time.monotonic()
        if self.mode=='ps_only':
            self.full.setInput(tensor);raw=self.full.forward();return raw,dict(network_total_ms=(time.monotonic()-start)*1000,all_network_mode='baseline_ps_only')
        if self.executor is None:raise RuntimeError('Baseline executor not attached')
        self.prefix.setInput(tensor);values=self.prefix.forward(list(self.executor.input_shapes));prefix_end=time.monotonic()
        for name,value in zip(self.executor.input_shapes,values):
            np.divide(value[0],self.executor.input_scales[name],out=self.qwork[name]);np.rint(self.qwork[name],out=self.qwork[name]);np.clip(self.qwork[name],-127,127,out=self.qwork[name]);self.qinput[name][:]=self.qwork[name]
        quantized=time.monotonic();outputs,pl=self.executor.run(self.qinput,diagnostic=diagnostic);pl_end=time.monotonic()
        for b in self.executor.boundaries:np.multiply(outputs[b['name']],b['scale'],out=self.dwork[b['name']][0],dtype=np.float32)
        for name,value in self.dwork.items():self.suffix.setInput(value,name)
        suffix_start=time.monotonic();raw=self.suffix.forward();end=time.monotonic()
        return raw,dict(all_network_mode='baseline_V296_numerics_single_V297_core',network_total_ms=(end-start)*1000,ps_prefix_ms=(prefix_end-start)*1000,input_quantize_ms=(quantized-prefix_end)*1000,pl_region_e2e_ms=(pl_end-quantized)*1000,pl_input_copy_flush_ms=pl['input_copy_flush_ms'],pl_invoke_ms=pl['invoke_ms'],pl_output_invalidate_copy_ms=pl['output_invalidate_copy_ms'],output_dequantize_suffix_set_ms=(suffix_start-pl_end)*1000,ps_suffix_ms=(end-suffix_start)*1000,pl_phase_cycles=pl['phase_cycles'],pl_core_invocations=pl['core_invocations'])
    def infer(self,frame,diagnostic=False):
        start=time.monotonic();capture=0.
        if isinstance(frame,dict):
            capture=float(frame.get('copy_ms',0))+float(frame.get('copy_guard_ms',0));image=rgb565_to_bgr(frame['raw565']) if frame.get('raw565') is not None else frame['bgr']
        else:image=frame
        tensor,g=preprocess(image,224,self.config['inference_mirror']);pre_end=time.monotonic();raw,times=self.forward_tensor(tensor,diagnostic);net_end=time.monotonic()
        if raw.shape!=(1,3087,8) or not np.isfinite(raw).all():raise RuntimeError('Invalid baseline output')
        detections=postprocess(raw,g,.60,.45,1000,self.config['classes']);scores=(raw[0,:,4:5]*raw[0,:,5:]).max(0).tolist();end=time.monotonic()
        return dict(times,detections=detections,geometry=g,preprocess_ms=(pre_end-start)*1000,postprocess_ms=(end-net_end)*1000,ai_compute_total_ms=(end-start)*1000,ai_total_ms=(end-start)*1000+capture,ai_capture_copy_guard_ms=capture,ai_total_includes_capture_copy=True,prescreen_max_scores=scores,inference_start=start,inference_end=end,input_path='RGB565_TO_FULL_BGR_REFERENCE_PREPROCESS')
    def identity(self):
        return dict(version='V2.9.6-reference-on-V2.9.7-overlay',mode=self.mode,benchmark_only=True,original_V296_bitstream_timing=False,pl_convolution_count=30 if self.mode=='hybrid' else 0,hardware_ip=HARDWARE_IP,model_sha256=self.package['manifest']['source_onnx_sha256'],quantization_id=self.package['manifest']['quantization_id'],network_load_ms=self.load_ms,executor_load_ms=self.executor_load_ms)
    def close(self):
        if self.executor is not None:self.executor.close();self.executor=None
