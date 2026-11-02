"""Explicit owned-diagnostic cache bootstrap; production requires real proof."""
import hashlib
import inspect
import json
import os
import time
import uuid
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
from pl_runner import digest


def range_sync_candidate(buffer,byte_offset,byte_count,direction):
    if direction not in ('flush','invalidate'):raise ValueError('unknown range sync direction')
    view=buffer.view(np.uint8).reshape(-1)[byte_offset:byte_offset+byte_count]
    if (view.nbytes!=byte_count or view.ctypes.data!=buffer.ctypes.data+byte_offset or
            int(view.device_address)!=int(buffer.device_address)+byte_offset or int(view.offset)!=int(buffer.offset)+byte_offset):
        raise RuntimeError('PynqBuffer sliced offset/nbytes contract differs; no full-buffer fallback')
    getattr(view,direction)()


def installed_source_identity():
    import pynq.buffer
    from pynq import Device
    buffer=Path(inspect.getsourcefile(pynq.buffer.PynqBuffer)).resolve()
    device=Path(inspect.getsourcefile(type(Device.active_device).flush)).resolve()
    if Path(inspect.getsourcefile(type(Device.active_device).invalidate)).resolve()!=device:raise RuntimeError('flush/invalidate implementations need an expanded explicit source qualification')
    return dict(buffer_source_file=str(buffer),buffer_source_sha256=digest(buffer),device_source_file=str(device),device_source_sha256=digest(device))


def load_verified_sync(root,hardware):
    root=Path(root);path=root/'reports/pl01_cache_proof.json'
    if not path.is_file():raise ValueError('real PL01 cache proof missing; production NOT_QUALIFIED')
    proof=json.loads(path.read_text())
    if proof.get('status')!='CACHE_RANGE_DMA_VERIFIED' or proof.get('executed_on_target') is not True:raise ValueError('host assertions cannot replace target dirty-range DMA proof')
    for key in ('bitstream_sha256','hwh_sha256'):
        if proof.get(key)!=hardware[key]:raise ValueError('cache proof new hardware identity differs')
    if proof.get('driver_sha256')!=digest(root/'board/pl_runner.py'):raise ValueError('cache proof driver changed')
    actual=installed_source_identity()
    if any(proof.get(k)!=v for k,v in actual.items()):raise ValueError('installed PYNQ buffer/device source differs')
    if len(proof.get('cases',[]))!=3 or any(not c.get('passed') or not c.get('guard_unchanged') or not c.get('dirty_input_partial_flush') or not c.get('partial_output_invalidate') for c in proof['cases']):raise ValueError('dirty-range probe cases incomplete')
    from board.static_qualification import local
    raw=local(root,proof['probe_report_file'])
    if digest(raw)!=proof.get('probe_report_sha256'):raise ValueError('cache raw measurement report changed')
    return range_sync_candidate,proof


def probe_cache_ranges(session,allocate,native,*,sync=range_sync_candidate,source_identity=None):
    """Only an explicitly admitted diagnostic owner may run VIEW_COPY bootstrap.

    Returns measurements; never writes a qualified report or frees allocations.
    Failed/unfinished hardware retains every allocation on the session.
    """
    if session.scope!='PL01_OWNED_DIAGNOSTICS_ONLY':raise RuntimeError('cache bootstrap is diagnostic-only')
    core=session.core_guard;core.check_idle();source_identity=source_identity or installed_source_identity()
    cases=[]
    for index,(start,count,out) in enumerate(((5,64,256),(33,68,336),(201,32,464))):
        buffers={}
        for name,shape,dtype in [('workspace',(512,),np.int16),('weights',(1,),np.uint64),('bias',(1,),np.int64),('shift',(1,),np.int32),('maps',(1,),np.uint32),('channel_shifts',(1,),np.int32)]:
            buffer=allocate(shape=shape,dtype=dtype);buffers[name]=buffer;session.retained_probe_buffers.append(buffer)
        workspace=buffers['workspace'];baseline=np.arange(512,dtype=np.int16)+1000;workspace[:]=baseline
        buffers['maps'][:]=start;buffers['shift'][:]=0;buffers['channel_shifts'][:]=0;buffers['weights'][:]=0;buffers['bias'][:]=0
        for buffer in buffers.values():sync(buffer,0,buffer.nbytes,'flush')
        changed=np.random.default_rng(index).integers(-32000,32001,count,dtype=np.int16);workspace[start:start+count]=changed
        sync(workspace,start*2,count*2,'flush')
        addresses={name:int(buffer.device_address) for name,buffer in buffers.items()}
        operation=dict(input_r=addresses['workspace'],weights=addresses['weights'],bias=addresses['bias'],shift=addresses['shift'],output_r=addresses['workspace']+out*2,
            channel_offsets=addresses['maps'],channel_shifts=addresses['channel_shifts'],op=3,ci=1,co=1,ih=1,iw=count,kernel=1,stride=1,pad=0,groups=1,relu=0,use_channel_offsets=1,input_span_elements=512)
        result=native.run(core.mmio.array,[operation],2.)
        if result.code:raise RuntimeError('cache VIEW_COPY failed; owner and probe buffers retained: '+str(result.code))
        core.check_idle();sync(workspace,out*2,count*2,'invalidate')
        sync(workspace,(out-8)*2,16,'invalidate');sync(workspace,(out+count)*2,16,'invalidate')
        exact=np.array_equal(np.asarray(workspace[out:out+count]),changed)
        guards=np.array_equal(np.asarray(workspace[out-8:out]),baseline[out-8:out]) and np.array_equal(np.asarray(workspace[out+count:out+count+8]),baseline[out+count:out+count+8])
        cases.append(dict(index=index,input_offset_bytes=start*2,output_offset_bytes=out*2,nbytes=count*2,
            passed=bool(exact and guards),guard_unchanged=bool(guards),dirty_input_partial_flush=True,partial_output_invalidate=True,
            input_sha256=hashlib.sha256(changed.tobytes()).hexdigest(),output_sha256=hashlib.sha256(np.asarray(workspace[out:out+count]).tobytes()).hexdigest()))
        if not exact or not guards:raise RuntimeError('cache/guard numeric mismatch; diagnostic owner and buffers retained')
    return dict(schema='pl01-cache-range-measurements-v1',status='MEASUREMENTS_MATCH_NOT_PRODUCTION_QUALIFICATION',executed_on_target=session.executed_on_target,
        scope=session.scope,video_scope=getattr(session,'video_scope','NOT_MEASURED'),cases=cases,source_identity=source_identity,hardware=core.identity,board_production_qualified=False)


def publish_cache_proof(root,session,measurements):
    """Explicit root-owned target step, never callable with host-only evidence."""
    root=Path(root)
    if (session.scope!='PL01_OWNED_DIAGNOSTICS_ONLY' or not session.executed_on_target or measurements.get('executed_on_target') is not True
            or not session.owner_check()):raise ValueError('cache proof publication requires the current real diagnostic target owner')
    cases=measurements.get('cases',[])
    if len(cases)!=3 or {x.get('index') for x in cases}!={0,1,2} or any(not x.get('passed') or not x.get('guard_unchanged') for x in cases):raise ValueError('three actual dirty-range cases must pass')
    session.core_guard.check_idle()
    actual=installed_source_identity()
    if measurements['source_identity']!=actual:raise ValueError('PYNQ source identity changed during target probe')
    import board_identity
    if board_identity.verify_diagnostic(root)!=session.admission:raise ValueError('diagnostic source/hardware admission changed')
    proof_path=root/'reports/pl01_cache_proof.json'
    if proof_path.exists():raise FileExistsError('preserve the previous cache proof before requalification')
    raw_name='reports/pl01_cache_measurements_%d_%s.json'%(time.time_ns(),uuid.uuid4().hex[:8]);raw=root/raw_name
    with raw.open('x') as output:json.dump(measurements,output,indent=2)
    refs=[dict(file=raw_name,sha256=digest(raw))]
    for kind in ('buffer','device'):
        source=Path(actual[kind+'_source_file']);name='reports/pl01_cache_sources/'+actual[kind+'_source_sha256']+'/'+kind+'.py';path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and digest(path)!=actual[kind+'_source_sha256']:raise ValueError('saved installed-source snapshot changed')
        if not path.exists():path.write_bytes(source.read_bytes())
        refs.append(dict(file=name,sha256=digest(path)))
    identity=session.core_guard.runner_proof(root,root/'board/build_pl_batch/armv7/libpl_batch.so')
    proof=dict(identity,**actual,status='CACHE_RANGE_DMA_VERIFIED',schema='pl01-cache-proof-v1',executed_on_target=True,
        boot_id=session.boot_id,owner_pid=os.getpid(),cases=cases,probe_report_file=raw_name,probe_report_sha256=digest(raw),raw_references=refs)
    with proof_path.open('x') as output:json.dump(proof,output,indent=2)
    return proof
