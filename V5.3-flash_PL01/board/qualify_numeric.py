"""Formal saved-A16 target qualification; never creates a PASS from host fakes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
from pl_runner import decode_heads
from fastest_numpy import postprocess,restore_boxes

IMAGES=('2402','2493','2410')
_RETAINED=[]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path,value):
    path=Path(path);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n');os.replace(temporary,path)


def required_numeric_files(root=ROOT):
    root=Path(root)
    return [root/'reports/quant_eval_2020/golden'/image/name for image in IMAGES for name in ('integer_tensors.npz','probability_heads.npz','source.json')]


def load_expected(root=ROOT):
    root=Path(root);proof=json.loads((root/'reports/physical_binary_replay/summary.json').read_text())
    if proof.get('status')!='PHYSICAL_PLAN_HOST_REPLAY_PASS':raise ValueError('independent physical oracle proof missing')
    rows={x['image']:x for x in proof['probes']};result={}
    for image in IMAGES:
        folder=root/'reports/quant_eval_2020/golden'/image
        for name,digest in rows[image]['source_artifact_sha256'].items():
            if sha(folder/name)!=digest:raise ValueError('original integer oracle artifact changed: '+image+'/'+name)
        result[image]=dict(integer=dict(np.load(folder/'integer_tensors.npz')),heads=dict(np.load(folder/'probability_heads.npz')),
            source=json.loads((folder/'source.json').read_text()),references=[dict(file=p.relative_to(root).as_posix(),sha256=sha(p)) for p in required_numeric_files(root) if p.parent==folder])
    return result,proof


def fixed_detections(heads,source):
    decoded=decode_heads(heads)
    pred=restore_boxes(postprocess(decoded[0],confidence=.001,iou=.45,max_det=300),(source['height'],source['width'],3))
    if len(pred):
        pred=pred[(pred[:,2]>pred[:,0])&(pred[:,3]>pred[:,1])]
        pred=pred[pred[:,4]>=np.array([.60,.45],np.float32)[pred[:,5].astype(np.int64)]]
    return decoded,pred


def detection_comparison(reference,candidate):
    available=set(range(len(candidate)));pairs=[];misses=[]
    for index,box in enumerate(reference):
        options=[]
        for j in available:
            other=candidate[j]
            if other[5]!=box[5]:continue
            intersection=np.maximum(0,np.minimum(box[2:4],other[2:4])-np.maximum(box[:2],other[:2])).prod()
            union=np.maximum(0,box[2:4]-box[:2]).prod()+np.maximum(0,other[2:4]-other[:2]).prod()-intersection
            options.append((float(intersection/max(float(union),1e-9)),j))
        overlap,j=max(options,default=(0.,-1))
        if overlap<.5:misses.append(index);continue
        available.remove(j);pairs.append(dict(reference_index=index,candidate_index=j,iou=overlap,
            box_max_abs_pixels=float(np.abs(box[:4]-candidate[j,:4]).max()),score_max_abs=float(abs(box[4]-candidate[j,4]))))
    box_error=max((x['box_max_abs_pixels'] for x in pairs),default=0.);score_error=max((x['score_max_abs'] for x in pairs),default=0.)
    return dict(passed=not misses and not available and box_error<.1 and score_error<1e-4,reference_count=len(reference),candidate_count=len(candidate),
        box_max_abs_pixels=box_error,score_max_abs=score_error,unmatched_reference=misses,unmatched_candidate=sorted(available),matches=pairs,
        reference=reference.tolist(),candidate=candidate.tolist())


def compare_workspace(plan,workspace,heads,expected):
    layers=[];by_tensor={}
    for op in plan['operations']:
        if op['op']!=0:continue
        offset=op['output_offset_elements'];count=int(np.prod(op['output_shape']))
        actual=np.asarray(workspace[offset:offset+count]).reshape(op['output_shape'])
        reference=expected['integer']['conv%03d'%op['conv_index']]
        if actual.shape!=reference.shape:raise ValueError('physical Conv output shape differs')
        difference=np.abs(actual.astype(np.int32)-reference.astype(np.int32))
        row=dict(conv_index=op['conv_index'],physical_op=op['index'],elements=count,mismatches=int(np.count_nonzero(difference)),maximum_integer_error=int(difference.max(initial=0)))
        layers.append(row);by_tensor[op['output_tensor']]=row
    raw_heads=[]
    for name,view in plan['ps_logits'].items():
        row=by_tensor[name]
        raw_heads.append(dict(tensor=name,shape=view['shape'],exponent=view['exponent'],mismatches=row['mismatches'],integer_max_abs=row['maximum_integer_error'],
            dequantized_logit_max_abs=float(row['maximum_integer_error']*2.**view['exponent'])))
    reference_heads=[expected['heads'][name] for name in plan['output_names']]
    if len(heads)!=len(reference_heads) or any(a.shape!=b.shape for a,b in zip(heads,reference_heads)):raise ValueError('target probability head count/shape differs')
    if any(not np.isfinite(x).all() for x in heads):raise ValueError('nonfinite target probability heads')
    errors={name:float(np.abs(a-b).max()) for name,a,b in zip(plan['output_names'],heads,reference_heads)}
    actual_decoded,actual_boxes=fixed_detections(heads,expected['source']);reference_decoded,reference_boxes=fixed_detections(reference_heads,expected['source'])
    decoded_box_error=float(np.abs(actual_decoded[...,:4]-reference_decoded[...,:4]).max())
    probability_error=float(np.abs(actual_decoded[...,4:]-reference_decoded[...,4:]).max())
    products=lambda x:x[...,4:5]*x[...,5:7]
    score_error=float(np.abs(products(actual_decoded)-products(reference_decoded)).max())
    fixed=detection_comparison(reference_boxes,actual_boxes)
    count=sum(x['mismatches'] for x in layers);probability_error=max(probability_error,max(errors.values()))
    passed=(len(layers)==79 and {x['conv_index'] for x in layers}==set(range(79)) and len(raw_heads)==6 and not count and
        probability_error<1e-4 and decoded_box_error<.1 and score_error<1e-4 and fixed['passed'])
    return dict(passed=passed,conv_layers=len(layers),integer_mismatches=count,layer_comparisons=layers,six_raw_logit_heads=raw_heads,
        raw_head_dequantized_max_abs=max((x['dequantized_logit_max_abs'] for x in raw_heads),default=0.),probability_heads_max_abs=errors,
        probability_max_abs=probability_error,decoded_box_max_abs_model_pixels=decoded_box_error,score_product_max_abs=score_error,fixed_threshold_boxes=fixed)


def statistics(values):
    return dict(samples=len(values),mean=float(np.mean(values)),median=float(np.median(values)),p95=float(np.percentile(values,95)),minimum=float(min(values)),maximum=float(max(values))) if values else dict(samples=0,status='NOT_MEASURED')


def is_real_target(session):
    return session.executed_on_target is True and os.name!='nt' and platform.machine().lower().startswith(('arm','aarch'))


def run_numeric(session,root,output,*,warmup=5,runs=30):
    root=Path(root).resolve();output=Path(output).resolve()
    if root/'reports' not in output.parents:raise ValueError('raw numeric output must be a fresh reports directory')
    if not is_real_target(session) or not session.owner_check():raise ValueError('real current ARM diagnostic owner required; host fake cannot qualify target')
    expected,oracle=load_expected(root);output.mkdir(parents=True,exist_ok=False)
    _RETAINED.append(session);status=dict(status='TARGET_NUMERIC_RUNNING',scope='SAVED_A16_NN_ONLY_NO_CAMERA_OR_RAW565_PREPROCESS',cases=[],executed_on_target=True,
        boot_id=session.boot_id,owner_pid=os.getpid(),owner_start_ticks=session.owner_start_ticks)
    status_path=output/'run.json';atomic(status_path,status)
    try:
        from board.cache_qualification import probe_cache_ranges,publish_cache_proof,load_verified_sync
        from pl_batch import NativeBatch
        library=root/'board/build_pl_batch/armv7/libpl_batch.so'
        if not (root/'reports/pl01_cache_proof.json').exists():
            from pynq import allocate
            native=NativeBatch(library,session.core_guard.register_map)
            measurements=probe_cache_ranges(session,allocate,native)
            publish_cache_proof(root,session,measurements)
        load_verified_sync(root,session.static)
        if session.runner is None:session.make_runner()
        runner=session.runner;pl=runner.pl;plan=pl.plan
        if plan['operation_count']!=96 or sha(root/'models/quant_2020/manifest.json')!=oracle['package_manifest_sha256']:raise ValueError('target physical plan/package differs from frozen oracle')
        if sha(root/'reports/physical_bundle_2402/plan/plan.json')!=oracle['plan_sha256']:raise ValueError('physical plan differs from independent binary replay')
        bindings=session.core_guard.runner_proof(root,library)
        status.update({k:bindings[k] for k in ('bitstream_sha256','hwh_sha256','package_manifest_sha256','physical_plan_sha256','driver_sha256','native_library_sha256')})
        status['cache_proof_sha256']=sha(root/'reports/pl01_cache_proof.json')
        raw=[]
        for image in IMAGES:
            case_dir=output/image;case_dir.mkdir();qinput=expected[image]['integer']['images']
            begin=time.monotonic();heads=pl.infer_heads(qinput);normal_ms=(time.monotonic()-begin)*1000
            result=pl.last_result
            if result.code or result.completed_ops!=96:raise RuntimeError('native batch did not complete all96 operations')
            session.core_guard.check_idle()
            start=time.monotonic();workspace=pl.buffers['workspace'];pl._sync('workspace',0,workspace.nbytes,'invalidate');invalidate_ms=(time.monotonic()-start)*1000
            start=time.monotonic();snapshot=np.asarray(workspace).copy();copy_ms=(time.monotonic()-start)*1000
            snapshot.astype('<i2').tofile(case_dir/'actual_workspace.bin');np.savez(case_dir/'actual_probability_heads.npz',**dict(zip(plan['output_names'],heads)))
            comparison=compare_workspace(plan,snapshot,heads,expected[image])
            case=dict(comparison,image=image,native_batch_executed=True,native_completed_ops=int(result.completed_ops),
                timing_ms=dict(normal_six_logit_path=normal_ms,native_batch=result.elapsed_ns/1e6,diagnostic_full_workspace_invalidate=invalidate_ms,diagnostic_workspace_copy=copy_ms),
                expected_references=expected[image]['references'],source=expected[image]['source'])
            atomic(case_dir/'case.json',case);status['cases'].append(case);atomic(status_path,status)
            for name in ('actual_workspace.bin','actual_probability_heads.npz','case.json'):
                path=case_dir/name;raw.append(dict(file=path.relative_to(root).as_posix(),sha256=sha(path)))
            if not comparison['passed']:raise RuntimeError('target integer/raw-head/probability/fixed-box mismatch: '+image)
        for index in range(warmup):pl.infer_heads(expected[IMAGES[index%3]]['integer']['images'])
        normal=[];native_times=[]
        for index in range(runs):
            begin=time.monotonic();pl.infer_heads(expected[IMAGES[index%3]]['integer']['images']);normal.append((time.monotonic()-begin)*1000);native_times.append(pl.last_result.elapsed_ns/1e6)
        timing=dict(normal_six_logit_path_ms=statistics(normal),native_batch_ms=statistics(native_times),warmup=warmup,
            scope='SAVED_A16_INPUT_TO_PROBABILITY_HEADS; excludes file decode/static allocation/cache bootstrap/full-workspace diagnostic synchronization/raw565 preprocessing/camera/display',
            board_live_fps_claim=False)
        atomic(output/'stable_timing.json',timing);raw.append(dict(file=(output/'stable_timing.json').relative_to(root).as_posix(),sha256=sha(output/'stable_timing.json')))
        status.update(status='TARGET_NUMERIC_MEASUREMENTS_PASS',stable_timing=timing,qualification_scope='INTEGER_NN_NUMERIC_ONLY_NOT_NEW_ACCURACY_OR_CAMERA_ACCEPTANCE')
        atomic(status_path,status);raw.append(dict(file=status_path.relative_to(root).as_posix(),sha256=sha(status_path)))
        proof=dict(status,schema='pl01-target-numeric-v1',status='PL01_TARGET_INTEGER_NUMERIC_PASS',raw_references=raw,
            expected_oracle_sha256=sha(root/'reports/physical_binary_replay/summary.json'),tool_sha256=sha(__file__))
        final=root/'reports/pl01_board_numeric.json'
        with final.open('x') as target:json.dump(proof,target,indent=2)
        return proof
    except BaseException as error:
        status.update(status='RETAINED_OWNER_RETRY_NOT_QUALIFIED',error=repr(error),buffers_retained=True)
        atomic(status_path,status)
        return status


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--output',type=Path)
    p.add_argument('--run',action='store_true');p.add_argument('--warmup',type=int,default=5);p.add_argument('--runs',type=int,default=30);args=p.parse_args()
    if args.warmup<0 or args.runs<0:p.error('nonnegative timing counts required')
    import board_identity
    try:load_expected(args.root);admission=board_identity.verify_diagnostic(args.root)
    except Exception as error:print(json.dumps(dict(status='BLOCKED_NOT_QUALIFIED',error=str(error))),flush=True);return 2
    if not args.run:print(json.dumps(dict(status='LOCAL_PREREQUISITES_READY_NO_BOARD_OPERATION',scope=admission['scope'])),flush=True);return 0
    if args.output is None:p.error('--run requires a new --output reports directory')
    stop=[False]
    def request_stop(*_):stop[0]=True
    for number in (signal.SIGTERM,signal.SIGINT):signal.signal(number,request_stop)
    from board.diagnostic_session import NNOnlyDiagnosticSession,retained_diagnostic_sessions
    session=None;result={'status':'NOT_QUALIFIED'}
    try:
        session=NNOnlyDiagnosticSession(args.root);result=run_numeric(session,args.root,args.output,warmup=args.warmup,runs=args.runs)
        print(json.dumps(dict(status=result['status'],output=str(args.output))),flush=True)
        if result['status']=='PL01_TARGET_INTEGER_NUMERIC_PASS':stop[0]=True
    except BaseException as error:
        retained=retained_diagnostic_sessions();session=session or (retained[-1] if retained else None)
        print(json.dumps(dict(status='RETAINED_OWNER_RETRY_NOT_QUALIFIED' if session and session.owner else 'BLOCKED_NO_OWNER',error=repr(error))),flush=True)
        if session is None or session.owner is None:return 2
    # On any failure remain alive and retain references. Only an explicit normal
    # stop request attempts verified shutdown; never kill/free/reset on timeout.
    while True:
        if stop[0]:
            try:
                if session.close():print('NN_ONLY_DIAGNOSTIC_QUIESCENT',flush=True);return 0 if result.get('status')=='PL01_TARGET_INTEGER_NUMERIC_PASS' else 2
            except Exception as error:print(json.dumps(dict(status='RETAINED_OWNER_RETRY',shutdown_error=repr(error))),flush=True)
        time.sleep(2)


if __name__=='__main__':raise SystemExit(main())
