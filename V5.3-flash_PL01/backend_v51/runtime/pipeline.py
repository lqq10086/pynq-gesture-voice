"""Full integer graph in shared PL core, independent PS Detect decode and NMS."""
import time
import hashlib
from pathlib import Path
import cv2
import numpy as np
try:
    from .inference import preprocess,postprocess
    from .frame_reader import rgb565_to_bgr
    from .direct_preprocess import DirectPreprocessor
    from .package_contract import validate_package,freeze_metadata
    from .qualification import verify_runtime
    from .region_executor import RegionExecutor,check_global_addresses,HARDWARE_IP
except ImportError:
    from inference import preprocess,postprocess
    from frame_reader import rgb565_to_bgr
    from direct_preprocess import DirectPreprocessor
    from package_contract import validate_package,freeze_metadata
    from qualification import verify_runtime
    from region_executor import RegionExecutor,check_global_addresses,HARDWARE_IP

class IndependentDecoder:
    """PRECHECK exact retained decoder with actual ONNX-derived anchors/strides."""
    def __init__(self,detect):
        if detect['channels_per_anchor']!=7:raise ValueError('Two-class decoder requires7 values per anchor')
        self.strides=detect['strides'];self.buffers=[]
        self.output=np.empty((1,9072,7),np.float32)
        for h,anchors in zip((48,24,12),detect['anchors_pixels']):
            grid=np.stack(np.meshgrid(np.arange(h),np.arange(h)),axis=-1).astype(np.float32)[None,None]-.5
            self.buffers.append((np.empty((1,3,h,h,7),np.float32),grid,np.asarray(anchors,np.float32)[None,:,None,None]))

    def decode(self,heads):
        if len(heads)!=3:raise ValueError('Expected three raw linear heads')
        offset=0
        for raw,stride,(work,grid,anchors),h in zip(heads,self.strides,self.buffers,(48,24,12)):
            if raw.shape!=(1,21,h,h) or raw.dtype!=np.float32:raise ValueError('Unexpected raw head shape/dtype')
            y=raw.reshape(1,3,7,h,h).transpose(0,1,3,4,2)
            np.negative(y,out=work);np.exp(work,out=work);np.add(work,1,out=work);np.reciprocal(work,out=work)
            np.multiply(work[...,:2],2,out=work[...,:2]);np.add(work[...,:2],grid,out=work[...,:2]);np.multiply(work[...,:2],stride,out=work[...,:2])
            np.multiply(work[...,2:4],2,out=work[...,2:4]);np.square(work[...,2:4],out=work[...,2:4]);np.multiply(work[...,2:4],anchors,out=work[...,2:4])
            count=3*h*h;self.output[:,offset:offset+count]=work.reshape(1,count,7);offset+=count
        return self.output

DETECTION_PROFILES={'baseline':None,'palm_balanced':(.25,.30)}

class MiniPipeline:
    def __init__(self,package_dir,mode='ps_only',*,detection_profile='baseline'):
        if mode not in ('ps_only','hybrid'):raise ValueError('Unknown execution mode')
        if detection_profile not in DETECTION_PROFILES:raise ValueError('Unknown detection profile')
        self.detection_profile=detection_profile
        palm_thresholds=DETECTION_PROFILES[detection_profile]
        obj,score=palm_thresholds or (.45,.60)
        start=time.monotonic();self.folder=Path(package_dir).resolve();self.mode=mode;self.executor=None;self.preprocessor=None
        self.package=validate_package(self.folder);m=self.package['manifest']
        self.qualification=verify_runtime()
        if m.get('test_only_mutation'):raise ValueError('Mutation package is diagnostic only')
        self.config=freeze_metadata(dict(input_shape=m['input_shape'],output_shape=[1,9072,7],confidence=m['conf_threshold'],iou=m['nms_iou'],
                         classes=m['classes'],inference_mirror=True,max_det=1000,threads=2,
                         detection_profile=detection_profile,palm_thresholds=list(palm_thresholds) if palm_thresholds else None,
                         detection_thresholds=dict(objectness_by_class=[obj,.45],
                             score_floor_by_class=[min(obj,.45),.45],
                             class_thresholds=[score,.45],enabled_classes=[0,1],iou=.45,max_det=20)))
        cv2.setNumThreads(2)
        if mode=='ps_only':
            self.full=cv2.dnn.readNetFromONNX(str(self.folder/'reference.onnx'))
            self.full.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV);self.full.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        else:
            try:from .quantized_decode import QuantizedDecoder
            except ImportError:from quantized_decode import QuantizedDecoder
            self.decoder=QuantizedDecoder(self.package)
        sources=['pipeline.py']+(['quantized_decode.py'] if mode=='hybrid' else [])
        self.decoder_identity=dict(implementation='A16_SIGMOID_LUT_FP32_DETECT' if mode=='hybrid' else 'ONNX_DETECT_FP32',
            source_sha256={'runtime/'+name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sources})
        self.load_ms=(time.monotonic()-start)*1000;self.executor_load_ms=None

    def attach_executor(self,executor):
        if self.mode!='hybrid':raise ValueError('Executor belongs to hybrid mode')
        self.qualification=verify_runtime()
        a=self.package['manifest'];b=executor.package['manifest']
        if a!=b:raise ValueError('Pipeline/executor package identities differ')
        self.executor=executor
        self.preprocessor=DirectPreprocessor(a['input_scale'],a['input_bits'],a.get('zero_point',0),True,output=executor.input_view)

    def attach(self,camera):
        if self.mode=='ps_only':return
        self.qualification=verify_runtime(overlay=camera.overlay)
        entry=camera.overlay.ip_dict.get('conv_accel_0')
        if not entry or entry['type']!=HARDWARE_IP or int(entry['phys_addr'])!=0x43c00000:raise RuntimeError('Full graph requires qualified single replacement IP3.2')
        start=time.monotonic();self.executor=RegionExecutor(self.folder,qualified_overlay=camera.overlay)
        check_global_addresses([camera,self.executor]);self.attach_executor(self.executor)
        self.executor_load_ms=(time.monotonic()-start)*1000

    def _quantize(self,tensor):
        if self.executor is None:raise RuntimeError('Hybrid executor not attached')
        self.executor.prepare_input()
        if not hasattr(self,'qwork'):self.qwork=np.empty((1,3,384,384),np.float32)
        m=self.package['manifest'];limit=(1<<(m['input_bits']-1))-1
        np.divide(tensor,np.float32(m['input_scale']),out=self.qwork);np.rint(self.qwork,out=self.qwork);np.clip(self.qwork,-limit,limit,out=self.qwork)
        np.copyto(self.executor.input_view,self.qwork,casting='unsafe');return self.executor.input_view

    def decode_heads(self,heads):
        """Shared production/numeric path; head scale and sigmoid are fused in the LUT."""
        start=time.monotonic()
        output=self.decoder.decode([heads[b['name']] for b in self.package['boundaries']])
        return output,dict(output_dequantize_ms=0.,ps_decode_ms=(time.monotonic()-start)*1000,
                           decoder_implementation=self.decoder_identity['implementation'])

    def forward_tensor(self,tensor,diagnostic=False):
        """Public backend boundary returns an independently owned validated snapshot."""
        return self._forward_tensor(tensor,diagnostic,lambda output,times:(output.copy(),times))

    def _forward_tensor(self,tensor,diagnostic,consume):
        start=time.monotonic()
        if not isinstance(tensor,np.ndarray):raise ValueError('Expected a contiguous input tensor')
        if self.mode=='ps_only' or tensor.dtype==np.float32:
            if (tensor.dtype!=np.float32 or tensor.shape!=(1,3,384,384) or not tensor.flags.c_contiguous
                    or not np.isfinite(tensor).all() or tensor.min()<0 or tensor.max()>1):
                raise ValueError('Expected finite RGB FP32 [1,3,384,384] input in [0,1]')
        if self.mode=='ps_only':
            self.full.setInput(tensor);set_at=time.monotonic();output=self.full.forward();end=time.monotonic()
            if output.dtype!=np.float32 or output.shape!=(1,9072,7) or not np.isfinite(output).all():
                raise ValueError('Invalid external backend decoded output')
            return consume(output,dict(setInput_ms=(set_at-start)*1000,network_forward_ms=(end-set_at)*1000,network_total_ms=(end-start)*1000,all_network_mode=self.mode))
        if self.executor is None:raise RuntimeError('Hybrid executor has not acquired qualified hardware')
        quantized=self._quantize(tensor) if tensor.dtype==np.float32 else tensor
        quant_at=time.monotonic();heads,times=self.executor.run({'images':quantized},diagnostic=diagnostic,copy_outputs=False);pl_end=time.monotonic()
        def decoded(output):
            end=time.monotonic()
            return consume(output,dict(all_network_mode=self.mode,setInput_ms=0.,network_forward_ms=None,network_total_ms=(end-start)*1000,
                input_quantize_ms=(quant_at-start)*1000,pl_region_e2e_ms=(pl_end-quant_at)*1000,
                pl_input_copy_ms=times['input_copy_ms'],pl_input_flush_ms=times['input_flush_ms'],pl_input_copy_flush_ms=times['input_copy_flush_ms'],
                pl_invoke_ms=times['invoke_ms'],pl_output_invalidate_copy_ms=times['output_invalidate_copy_ms'],pl_phase_cycles=times['phase_cycles'],
                pl_core_invocations=times['core_invocations'],pl_graph_operations=times['graph_operations'],
                pl_hardware_cycles_status=times['hardware_cycles_status'],
                pl_input_validation_ms=times.get('input_validation_ms'),pl_config_write_count=times.get('config_write_count'),
                pl_batch_prepare_ms=times.get('batch_prepare_ms'),pl_batch_native_ms=times.get('batch_native_ms'),
                pl_batch_detail_ms=times.get('batch_detail_ms'),pl_batch_schedule_cache_hit=times.get('batch_schedule_cache_hit'),
                pl_phase_counter_semantics=times.get('phase_counter_semantics'),
                output_dequantize_ms=0.,ps_decode_ms=(end-pl_end)*1000,
                decoder_implementation=self.decoder_identity['implementation'],ps_prefix_ms=0.,ps_suffix_ms=0.))
        return self.decoder._decode_and_consume([heads[b['name']] for b in self.package['boundaries']],decoded)

    def infer(self,frame,diagnostic=False,*,collect_scores=False,trace_level=None):
        start=time.monotonic();capture_cost=0.
        if isinstance(frame,dict):
            capture_cost=float(frame.get('copy_ms',0))+float(frame.get('copy_guard_ms',0))
            raw=frame.get('raw565');image=frame.get('bgr')
        else:raw=None;image=frame
        input_path='BGR_REFERENCE_PREPROCESS'
        if raw is not None and self.mode=='hybrid':
            if self.preprocessor is None:raise RuntimeError('Native input buffer unavailable before executor attachment')
            self.executor.prepare_input()
            tensor,geometry=self.preprocessor.run(raw)
            input_path='RGB565_NATIVE_FINAL_INTEGER'
        elif (raw is None and self.mode=='hybrid' and isinstance(image,np.ndarray)
                and image.dtype==np.uint8 and image.shape==(720,1280,3) and image.flags.c_contiguous):
            if self.preprocessor is None:raise RuntimeError('Native input buffer unavailable before executor attachment')
            self.executor.prepare_input()
            tensor,geometry=self.preprocessor.run_bgr(image)
            input_path='BGR_NATIVE_FINAL_INTEGER'
        else:
            if raw is not None:image=rgb565_to_bgr(raw)
            tensor,geometry=preprocess(image,384,self.config['inference_mirror'])
            if self.mode=='hybrid':tensor=self._quantize(tensor)
        preprocessed=time.monotonic()
        def consume(output,times):
            network_end=time.monotonic()
            if output.dtype!=np.float32 or output.shape!=(1,9072,7):raise RuntimeError('Invalid decoded output')
            filter_trace={'level':trace_level} if trace_level else None
            profile=getattr(self,'detection_profile','baseline')
            detections=postprocess(output,geometry,.45,.45,20,self.config['classes'],enabled_classes=(0,1),
                                   class_thresholds=(.60,.45),trace=filter_trace,
                                   palm_thresholds=DETECTION_PROFILES[profile])
            scores=(output[0,:,4:5]*output[0,:,5:]).max(0).tolist() if collect_scores else None;end=time.monotonic()
            result=dict(times,detections=detections,geometry=geometry,detection_profile=profile,preprocess_ms=(preprocessed-start)*1000,postprocess_ms=(end-network_end)*1000,
                ai_compute_total_ms=(end-start)*1000,ai_total_ms=(end-start)*1000+capture_cost,ai_capture_copy_guard_ms=capture_cost,
                ai_total_includes_capture_copy=True,prescreen_max_scores=scores,inference_start=start,inference_end=end,
                input_path=input_path)
            if filter_trace is not None:result['filter_trace']=filter_trace
            return result
        return self._forward_tensor(tensor,diagnostic,consume)

    def identity(self):
        m=self.package['manifest']
        return dict(version='V3.4',mode=self.mode,neural_network_backend='PS_OpenCV_DNN_FP32' if self.mode=='ps_only' else 'PL_FULL_GRAPH_MIXED_INTEGER_PS_DECODE_NMS',
            pl_conv_acceleration=self.mode=='hybrid',pl_convolution_count=60 if self.mode=='hybrid' else 0,opencv=cv2.__version__,threads=cv2.getNumThreads(),
            model_sha256=m['model_sha256'],architecture_id=m['architecture_id'],hardware_abi=m['hardware_abi'],input_bits=m['input_bits'],input_scale=m['input_scale'],decoder=self.decoder_identity,
            network_load_ms=self.load_ms,executor_load_ms=self.executor_load_ms,package=str(self.folder),config=self.config)
