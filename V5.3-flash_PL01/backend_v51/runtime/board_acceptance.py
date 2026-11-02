"""Qualified full-graph board acceptance; exact numerics precede warmed timing."""
import argparse,hashlib,json,os,platform,signal,sys,threading,time,traceback
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
INPUTS=ROOT/'reports/board_inputs'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write_json(path,value):
    path=Path(path);temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)
def mismatch(actual,expected):
    if actual.shape!=expected.shape or actual.dtype!=expected.dtype:raise ValueError('Tensor shape/dtype mismatch')
    error=actual.astype(np.int64)-expected.astype(np.int64)
    return dict(mismatch_count=int(np.count_nonzero(error)),max_abs=int(np.abs(error).max()))
def tensor(executor,name,shape):
    buffer=executor.tensors[name];buffer.invalidate();return np.array(buffer[:int(np.prod(shape))],copy=True).reshape(shape)
def qinput(item):
    path=INPUTS/item['input_file']
    if sha(path)!=item['input_sha256']:raise ValueError('Frozen integer input changed')
    return np.load(path,allow_pickle=False)
def copy_input(executor,name,value):
    buffer=executor.tensors[name]
    if value.dtype!=buffer.dtype:raise ValueError('Fixture dtype differs')
    buffer[:value.size]=value.ravel();buffer.flush()
def hashes(executor,expected,head_only=False):
    rows=[];heads={b['name'] for b in executor.boundaries}
    for item in expected:
        if head_only and item['tensor'] not in heads:continue
        actual=tensor(executor,item['tensor'],item['shape']);digest=hashlib.sha256(actual.tobytes()).hexdigest()
        rows.append(dict(index=item['index'],tensor=item['tensor'],dtype=str(actual.dtype),expected_sha256=item['sha256'],actual_sha256=digest,byte_exact=digest==item['sha256']))
    if len(rows)!=(3 if head_only else 85) or not all(r['byte_exact'] for r in rows):raise RuntimeError('PL differs from complete frozen integer oracle: '+json.dumps(rows))
    return rows

def validate_inputs(inputs,package):
    operations=package['operations'];records=inputs['records'];fixtures=inputs['fixtures']['records']
    if len(operations)!=85 or len(package['descriptors'])!=87:raise ValueError('Expected complete 85-op/87-invocation graph')
    if len(records)!=25 or [r['index'] for r in records]!=list(range(25)):raise ValueError('Expected frozen four-class validation25')
    if inputs.get('classes')!=package['manifest']['classes']:raise ValueError('Oracle class identity mismatch')
    labels=[sorted({int(b[0]) for b in r['ground_truth']}) for r in records]
    if any(labels.count([c])!=5 for c in range(4)) or labels.count([])!=5:raise ValueError('Expected five independent inputs per class plus five negatives')
    calibration=json.loads(package['blobs']['calibration_manifest.json'])
    if {r['source_sha256'] for r in records}&{r['sha256'] for r in calibration['samples']}:raise ValueError('Calibration/acceptance overlap')
    expected={o['output_tensor']:(o['index'],o['output_shape'][1:],'int16' if o['activation_bits']==16 else 'int8') for o in operations}
    for row in records:
        actual={m['tensor']:(m['index'],m['shape'],m['dtype']) for m in row['expected_tensors']}
        if len(row['expected_tensors'])!=85 or actual!=expected:raise ValueError('Oracle must cover every unique graph tensor')
    if len(fixtures)!=11 or len({r['operation']['index'] for r in fixtures})!=11:raise ValueError('Expected 11 unique representative fixtures')
    for fixture in fixtures:
        operation=fixture['operation']
        if operation!=operations[operation['index']]:raise ValueError('Representative operation differs from qualified package')

def numeric(executor,inputs,check_stop):
    from scripts.reference.integer_reference import execute
    records=[]
    guards=[]
    for head in executor.boundaries:
        count=int(np.prod(head['shape']))
        guarded=executor._alloc(count+64,np.int16)
        guarded.fill(23130);guarded.flush()
        executor.tensors[head['name']]=guarded
        guards.append((head['name'],count,guarded))
    def verify_tails():
        result=[]
        for name,count,buffer in guards:
            buffer.invalidate()
            intact=bool(np.all(buffer[count:]==23130))
            result.append(dict(tensor=name,logical_elements=count,guard_elements=int(buffer.size-count),tail_intact=intact))
            if not intact:raise RuntimeError('27-channel head wrote past logical tensor: '+name)
        return result
    for entry in inputs['fixtures']['records']:
        check_stop();op=entry['operation'];index=op['index'];folder=INPUTS/'fixtures'/('op%02d'%index)
        for name,item in entry['files'].items():
            if sha(folder/name)!=item['sha256']:raise ValueError('Fixture identity drift')
        for j,name in enumerate(op['sources']):
            value=np.fromfile(folder/('input%d.bin'%j),np.int8 if op['input_bits'][j]==8 else np.int16).reshape(op['input_shapes'][j][1:]);copy_input(executor,name,value)
        for i,d in enumerate(executor.descriptors):
            if d['graph_index']==index:executor._invoke(i)
        actual=tensor(executor,op['output_tensor'],op['output_shape'][1:]);expected=np.fromfile(folder/'expected.bin',actual.dtype).reshape(actual.shape)
        error=mismatch(actual,expected);record=dict(index=index,op_type=op['op_type'],**error);records.append(record)
        if error['mismatch_count']:raise RuntimeError('PL fixture failed: '+json.dumps(record))
        verify_tails()
    check_stop();sample=next(r for r in inputs['records'] if r['index']==0);_,profile=executor.run({'images':qinput(sample)},copy_outputs=False,collect_phase_cycles=True)
    full=hashes(executor,sample['expected_tensors'])
    op=next(r['operation'] for r in inputs['fixtures']['records'] if r['operation']['index']==84);folder=INPUTS/'fixtures/op84'
    x=np.fromfile(folder/'input0.bin',np.int8 if op['input_bits'][0]==8 else np.int16).reshape(op['input_shapes'][0][1:]);copy_input(executor,op['sources'][0],x)
    params={'w84':np.fromfile(folder/'w.bin',np.int8).reshape(op['Cout'],op['Cin'],1,1),'b84':np.fromfile(folder/'b.bin',np.int64),'m84':np.fromfile(folder/'m.bin',np.int32)}
    original=params['b84'].copy();changed=original.copy();changed[0]+=1<<18
    reference,_=execute([op],params,{op['sources'][0]:x});params['b84']=changed;mutref,_=execute([op],params,{op['sources'][0]:x})
    changed_count=int(np.count_nonzero(reference[op['output_tensor']]!=mutref[op['output_tensor']]))
    if not changed_count:raise RuntimeError('Mutation has no reference effect')
    position=op['bias_offset']//8;i=next(i for i,d in enumerate(executor.descriptors) if d['graph_index']==84)
    executor.wait_idle();executor.bias[position:position+op['Cout']]=changed;executor.bias.flush()
    try:
        executor._invoke(i);muterr=mismatch(tensor(executor,op['output_tensor'],op['output_shape'][1:]),mutref[op['output_tensor']])
    finally:
        executor.wait_idle();executor.bias[position:position+op['Cout']]=original;executor.bias.flush()
    executor._invoke(i);resterr=mismatch(tensor(executor,op['output_tensor'],op['output_shape'][1:]),reference[op['output_tensor']])
    if muterr['mismatch_count'] or resterr['mismatch_count']:raise RuntimeError('Mutation/restoration mismatch')
    return dict(status='NUMERIC_PASS',representative_fixtures=records,full_graph_sample0_tensors=full,production_phase_profile=profile,head_tail_guards=verify_tails(),
                mutation=dict(index=84,bias_channel=0,bias_delta=1<<18,reference_output_changes=changed_count,mutated=muterr,restored=resterr,overlay_reloads=0,executor_recreations=0),
                tensor_comparison='Exact logical CHW byte SHA256; representative fixtures also compare each value')

def iou(a,b):
    a=np.asarray(a);b=np.asarray(b);inter=np.maximum(0,np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2])).prod()
    return float(inter/(sum(np.maximum(0,v[2:]-v[:2]).prod() for v in (a,b))-inter+1e-7))
def compare(reference,actual):
    unused=set(range(len(actual)));misses=[];matches=[]
    for j,old in enumerate(reference):
        pairs=[(iou(old['source_xyxy'],actual[k]['source_xyxy']),k) for k in unused if actual[k]['class_id']==old['class_id']]
        if not pairs or max(pairs)[0]<.5:misses.append(j);continue
        overlap,k=max(pairs);unused.remove(k);matches.append(dict(reference=j,actual=k,iou=overlap,confidence_delta=actual[k]['confidence']-old['confidence']))
    return dict(new_formal_misses=misses,new_formal_extras=sorted(unused),matches=matches)
def proposal_agreement(index,a,b):
    # Letterbox proposals may have zero area after source-image clipping.
    # Check the original nondegenerate AI box with the unchanged IoU threshold.
    return dict(index=index,class_id=a['class_id'],confidence_delta=a['confidence']-b['confidence'],iou=iou(a['ai_xyxy'],b['ai_xyxy']))
def regression(pipeline,inputs,output,check_stop):
    from inference import postprocess,map_box
    rows=[]
    for item in inputs['records']:
        check_stop();decoded,times=pipeline.forward_tensor(qinput(item));evidence=hashes(pipeline.executor,item['expected_tensors'],True)
        detected=postprocess(decoded,item['geometry'],.60,.45,1000,pipeline.config['classes']);proposals=[]
        for cls in range(4):
            scores=decoded[0,:,4]*decoded[0,:,5+cls];j=int(scores.argmax());cx,cy,w,h=decoded[0,j,:4]
            box=[float(cx-w/2),float(cy-h/2),float(cx+w/2),float(cy+h/2)]
            proposals.append(dict(class_id=cls,confidence=float(scores[j]),ai_xyxy=box,source_xyxy=map_box(box,item['geometry'],False),diagnostic_only=True))
        rows.append(dict(index=item['index'],set=item['set'],actual_PL_detections=detected,FP32_reference_detections=item['fp32_detections'],
                         proposals=proposals,comparison=compare(item['fp32_detections'],detected),head_hashes=evidence,timings=times));write_json(output/'regression_raw.json',rows)
    misses=sum(len(r['comparison']['new_formal_misses']) for r in rows)
    extras=sum(len(r['comparison']['new_formal_extras']) for r in rows)
    agreement=[]
    for item,actual in zip(inputs['records'],rows):
        for a,b in zip(actual['proposals'],item['proposals']):
            agreement.append(proposal_agreement(item['index'],a,b))
    integer_exact=all(abs(a['confidence_delta'])<=1e-5 and a['iou']>=.999 for a in agreement)
    # Retain baseline engineering budget (<=1 new miss, zero extras); never call
    # this formal FP32 agreement when a miss remains. All100 proposals are
    # checked, not a special historical frame/class exception.
    accepted=misses<=1 and extras==0 and integer_exact
    return dict(status='ENGINEERING_ACCEPTED_CURRENT_MODEL_LIMIT_RECORDED' if accepted else 'DETECTION_ENGINEERING_FAIL',images=len(rows),
        new_formal_misses=misses,new_formal_extras=extras,missing_indices=[r['index'] for r in rows if r['comparison']['new_formal_misses']],
        integer_proposal_agreement=agreement,integer_agreement=integer_exact,
        formal_detection_regression='FAIL_QUANTIZATION_MISS_OR_EXTRA_REMAINS' if misses or extras else 'FP32_AGREEMENT_PASS',
        engineering_budget=dict(max_added_misses=1,max_added_extras=0,provenance='Unchanged predecessor budget, fresh25 inputs and all100 proposal checks'),
        scope='All3 PL heads exact to new-model oracle; PS decode/NMS agreement. Recorded FP32/quantization limits; OBS and accuracy independent.')

def benchmark(models,raw,output,check_stop,health,count):
    rows=[];warmup=[]
    for i in range(5):
        for name,model in models.items():
            check_stop();value=model.infer({'raw565':raw});warmup.append(dict(round=i,mode=name,ai_total_ms=value['ai_total_ms']))
    for i in range(count):
        pairs=list(models.items())
        if i%2:pairs.reverse()
        for name,model in pairs:
            check_stop()
            if not health()['healthy']:raise RuntimeError('Video/VDMA error during benchmark')
            row=dict(model.infer({'raw565':raw}),round=i,mode=name,valid=True);rows.append(row)
            with (output/'performance_raw.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    statistics={}
    for name in models:
        values=np.array([r['ai_total_ms'] for r in rows if r['mode']==name]);statistics[name]=dict(samples=len(values),median_ms=float(np.median(values)),P95_ms=float(np.percentile(values,95)),max_ms=float(values.max()))
    return dict(status='MEASURED',warmup_runs_per_path=5,measured_runs_per_path=count,statistics=statistics,warmup=warmup,
                overlay_loads_during_comparison=0,all_slow_samples_retained=True,
                scope='One loaded Overlay, same fixed preloaded RGB565; complete input generation/quantization/PL/PS/decode/NMS/copies/scheduling; no capture wait')

def baseline_numeric(model):
    folder=INPUTS/'baseline_fixture';manifest=json.loads((folder/'manifest.json').read_text())
    if len(manifest['boundaries'])!=3 or {b['name'] for b in manifest['boundaries']}!={b['name'] for b in model.executor.boundaries}:raise ValueError('Baseline fixture must cover all three unique boundaries')
    if sha(model.folder/'compat_manifest.json')!=manifest['package_sha256']:raise ValueError('Baseline fixture package differs')
    if len(manifest['inputs'])!=len(model.executor.input_shapes) or {b['name'] for b in manifest['inputs']}!=set(model.executor.input_shapes):raise ValueError('Baseline fixture input set differs')
    values={}
    for item in manifest['inputs']:
        if sha(folder/item['file'])!=item['sha256']:raise ValueError('Baseline fixture input drift')
        values[item['name']]=np.load(folder/item['file'],allow_pickle=False)
    actual,times=model.executor.run(values);records=[]
    for item in manifest['boundaries']:
        if sha(folder/item['file'])!=item['sha256']:raise ValueError('Baseline expected boundary drift')
        error=mismatch(actual[item['name']],np.load(folder/item['file'],allow_pickle=False));records.append(dict(name=item['name'],**error))
    if any(r['mismatch_count'] for r in records):raise RuntimeError('Same-overlay baseline numerical mismatch')
    return dict(status='BASELINE_SAME_OVERLAY_NUMERIC_PASS',boundaries=records,timings=times,package_sha256=manifest['package_sha256'])

def run(args):
    sys.path.insert(0,str(ROOT))
    from launch_v297_mini import qualified_pair
    from pipeline import MiniPipeline
    from camera_control import CameraControlSession
    from display_owner import DisplayOwner
    from owner_guard import acquire_owner,mark_quiescent
    from shutdown_owner import shutdown_retaining
    from run_realtime import check_overlay_ip,read_video_health
    from hardware_idle import read_idle_kernel,require_pynq_device
    bit=qualified_pair();hybrid=MiniPipeline(args.package,'hybrid');inputs=json.loads((INPUTS/'manifest.json').read_text())
    if inputs['package_manifest_sha256']!=sha(args.package/'model_manifest.json'):raise ValueError('Oracle package differs')
    validate_inputs(inputs,hybrid.package)
    output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=False)
    identity=dict(package_manifest_sha256=sha(args.package/'model_manifest.json'),model_sha256=hybrid.identity()['model_sha256'],architecture_id=hybrid.identity()['architecture_id'],
                  hardware_abi=hybrid.identity()['hardware_abi'],bitstream_sha256=sha(bit),hwh_sha256=sha(bit.with_suffix('.hwh')))
    camera=CameraControlSession();display=None;owner=None;executor=None;baseline=None;wake=threading.Event();stopping=[False]
    def defer(signum,frame):stopping[0]=True;wake.set()
    handlers={s:signal.signal(s,defer) for s in (signal.SIGTERM,signal.SIGINT)}
    def check_stop():
        if stopping[0]:raise InterruptedError('Normal stop requested between completed operations')
    result=dict(status='STARTING',version='V2.9.8_GESTURE_4CLASS',owner_pid=os.getpid(),machine=platform.machine(),run_started_unix_s=time.time(),identity=identity)
    save=lambda value:write_json(output/'summary.json',value)
    try:
        save(result);result['device_preflight']=require_pynq_device();owner=acquire_owner();check_stop();camera.load_overlay(bit);result['overlay_load_count']=1
        check_overlay_ip(camera);read_idle_kernel(camera);display=DisplayOwner(status_path=None,events_path=None);display.start()
        try:hybrid.attach(camera)
        finally:executor=hybrid.executor
        result['video_before']=read_video_health(camera)
        if not result['video_before']['healthy']:raise RuntimeError('Video/VDMA unhealthy before numerics')
        if getattr(args,'numeric_evidence',None):
            previous=json.loads(args.numeric_evidence.read_text())
            if previous.get('status')!='NUMERIC_PASS' or any(previous.get(k)!=v for k,v in identity.items()):
                raise ValueError('Reused numeric evidence does not match all six identities')
            result['numeric']=previous
            result['numeric_evidence_reuse']=dict(path=str(args.numeric_evidence),sha256=sha(args.numeric_evidence),reason='Only diagnostic coordinate comparison changed; parameters/hardware/oracles unchanged')
        else:
            result['numeric']=numeric(executor,inputs,check_stop)
        write_json(output/'numeric_approval.json',dict(identity,**result['numeric']))
        result['regression']=regression(hybrid,inputs,output,check_stop)
        if result['regression']['status']!='ENGINEERING_ACCEPTED_CURRENT_MODEL_LIMIT_RECORDED':raise RuntimeError('Detection engineering gate failed')
        write_json(output/'detection_approval.json',dict(identity,**result['regression']))
        models={'v298_hybrid':hybrid,'PS_only_reference':MiniPipeline(args.package,'ps_only')}
        if args.baseline:
            from baseline_compat import BaselinePipeline
            baseline=BaselinePipeline(args.baseline);baseline.attach(camera);models['v296_region_on_v297_core']=baseline
            from region_executor import check_global_addresses
            check_global_addresses([camera,executor,baseline.executor])
            result['baseline_numeric']=baseline_numeric(baseline)
        rawitem=inputs['performance_input'];rawpath=INPUTS/rawitem['file']
        if sha(rawpath)!=rawitem['sha256']:raise ValueError('Fixed performance input changed')
        result['performance_input']=rawitem;raw=np.load(rawpath,allow_pickle=False)
        result['performance']=benchmark(models,raw,output,check_stop,lambda:read_video_health(camera),args.samples)
        result['video_after']=read_video_health(camera)
        if not result['video_after']['healthy']:raise RuntimeError('Video/VDMA unhealthy after tests')
        result['CMA']=dict(ai_buffers=len(executor.buffers),ai_bytes=sum(int(b.nbytes) for b in executor.buffers),video_bytes=sum(int(b.nbytes) for b in camera.buffers))
        result['status']='BOARD_ELECTRONIC_NUMERIC_AND_PERFORMANCE_COMPLETE'
    except BaseException:result.update(status='FAILED',error=traceback.format_exc())
    finally:
        if owner is not None:
            if baseline is not None and baseline.executor is not None:
                final_status=result['status']
                while True:
                    try:
                        baseline.executor.close();result['status']=final_status;break
                    except Exception as error:
                        result.update(status='RETAINED_OWNER_RETRY',baseline_close_error=repr(error));save(result);wake.wait(5);wake.clear()
            shutdown_retaining(camera,executor,display,result,save,wake)
            try:result['handoff']=mark_quiescent(camera,executor)
            except BaseException:result.update(status='FAILED',handoff_error=traceback.format_exc())
            finally:owner.close()
        save(result)
        for signum,handler in handlers.items():signal.signal(signum,handler)
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--package',type=Path,default=ROOT/'packages/current');parser.add_argument('--baseline',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--samples',type=int,default=40)
    parser.add_argument('--numeric-evidence',type=Path,help='Resume from an actual same-package same-hardware NUMERIC_PASS')
    args=parser.parse_args()
    if args.samples<40:parser.error('At least 40 warmed samples per path required')
    result=run(args);print(json.dumps({k:result[k] for k in ('status','shutdown_verified') if k in result},indent=2))
    return int(result['status']!='BOARD_ELECTRONIC_NUMERIC_AND_PERFORMANCE_COMPLETE' or result.get('shutdown_verified') is not True)
if __name__=='__main__':raise SystemExit(main())
