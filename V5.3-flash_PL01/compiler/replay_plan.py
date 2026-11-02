"""Independent workspace/map replay; never supplies golden intermediate values."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from integer_reference import conv_i16_w8,requantize


def read_tensor(workspace,offsets,shape,initialized=None):
    _,channels,height,width=shape;plane=height*width
    if len(offsets)!=channels:raise ValueError('channel map length differs')
    rows=[]
    for offset in offsets:
        offset=int(offset)
        if offset<0 or offset+plane>workspace.size:raise ValueError('channel map outside workspace')
        if initialized is not None and not initialized[offset:offset+plane].all():raise ValueError('read before physical producer write')
        rows.append(workspace[offset:offset+plane].reshape(height,width))
    return np.stack(rows)[None]


def tail_heads(plan,workspace,initialized):
    values={}
    for name,view in plan['ps_logits'].items():
        q=read_tensor(workspace,np.asarray(view['channel_offsets_elements']),view['shape'],initialized)
        values[name]=np.ldexp(q.astype(np.float32),view['exponent'])
    for node in plan['ps_tail_nodes']:
        kind=node['kind'];args=[values[x] for x in node['inputs']];attrs=node['attrs']
        if kind=='Sigmoid':
            x=args[0];value=np.empty_like(x);positive=x>=0;value[positive]=1/(1+np.exp(-x[positive]));e=np.exp(x[~positive]);value[~positive]=e/(1+e)
        elif kind=='Softmax':
            x=args[0]-args[0].max(axis=attrs['axis'],keepdims=True);e=np.exp(x);value=e/e.sum(axis=attrs['axis'],keepdims=True)
        elif kind=='Transpose':value=args[0].transpose(attrs['perm'])
        elif kind=='Concat':value=np.concatenate(args,axis=attrs['axis'])
        else:raise ValueError('unexpected PS tail operation '+kind)
        values[node['outputs'][0]]=value
    return {name:values[name] for name in plan['output_names']}


def replay(plan,maps,shifts,parameters,qinput,expected=None):
    if list(qinput.shape)!=plan['input']['shape'] or qinput.dtype!=np.int16:raise ValueError('int16 physical input shape differs')
    workspace=np.empty(plan['workspace_elements'],np.int16);initialized=np.zeros(workspace.size,bool)
    for channel,offset in enumerate(plan['input']['channel_offsets_elements']):
        plane=qinput[0,channel].size;workspace[offset:offset+plane]=qinput[0,channel].ravel();initialized[offset:offset+plane]=True
    integer_outputs={};comparisons=[]
    for op in plan['operations']:
        source=read_tensor(workspace,maps[op['map_key']],op['input_shape'],initialized)
        read_shifts=np.asarray(shifts[op['channel_shift_key']])
        if np.any(read_shifts):source=requantize(source.astype(np.int64),read_shifts)
        kind=op['op']
        if kind==0:
            result=conv_i16_w8(source,parameters[op['weight_key']],parameters[op['bias_key']],parameters[op['shift_key']],
                **{key:op[key] for key in ('stride','pad','groups','relu')})
            if expected is not None:
                golden=expected['conv%03d'%op['conv_index']]
                difference=np.abs(result.astype(np.int32)-golden.astype(np.int32))
                comparisons.append(dict(conv_index=op['conv_index'],physical_op=op['index'],elements=result.size,
                    mismatches=int(np.count_nonzero(difference)),maximum_integer_error=int(difference.max(initial=0))))
        elif kind==1:
            p=op['pad'];k=op['kernel'];s=op['stride'];padded=np.pad(source,((0,0),(0,0),(p,p),(p,p)),constant_values=-32768)
            result=np.lib.stride_tricks.sliding_window_view(padded,(k,k),axis=(2,3))[:,:,::s,::s].max(axis=(-1,-2))
        elif kind==2:result=np.repeat(np.repeat(source,2,axis=2),2,axis=3)
        elif kind==3:result=source
        else:raise ValueError('unknown HLS operation code')
        if list(result.shape)!=op['output_shape'] or result.dtype!=np.int16:raise ValueError('physical operator result contract differs')
        offset=op['output_offset_elements'];size=result.size
        if initialized[offset:offset+size].any():raise ValueError('bump allocation overlaps an existing producer')
        workspace[offset:offset+size]=result.ravel();initialized[offset:offset+size]=True
        integer_outputs[op['output_tensor']]=workspace[offset:offset+size].reshape(result.shape)
    heads=tail_heads(plan,workspace,initialized) if plan['ps_tail_nodes'] else {
        name:read_tensor(workspace,np.asarray(plan['tensor_views'][name]['channel_offsets_elements']),plan['tensor_views'][name]['shape'],initialized) for name in plan['output_names']}
    return dict(workspace=workspace,integer_outputs=integer_outputs,heads=heads,layer_comparisons=comparisons)


def verify_files(folder,manifest):
    for name,record in manifest['files'].items():
        digest=record['sha256'] if isinstance(record,dict) else record
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:raise ValueError('artifact SHA mismatch: '+name)


def read_binary_tables(plan,folder):
    offsets=(Path(folder)/'channel_offsets.bin').read_bytes();shift_data=(Path(folder)/'channel_shifts.bin').read_bytes();maps={};shifts={}
    for op in plan['operations']:
        count=op['ci'];mo=op['channel_offsets_offset_bytes'];so=op['channel_shifts_offset_bytes']
        maps[op['map_key']]=np.frombuffer(offsets,dtype='<u4',count=count,offset=mo).copy()
        shifts[op['channel_shift_key']]=np.frombuffer(shift_data,dtype='<i4',count=count,offset=so).copy()
    return maps,shifts


def read_binary_parameters(plan,package):
    package=Path(package);weights=(package/'weights_padded.bin').read_bytes();bias=(package/'bias.bin').read_bytes();shifts=(package/'shift.bin').read_bytes();result={}
    for op in plan['operations']:
        if op['op']!=0:continue
        logical=(op['ci']//op['groups'])*op['kernel']**2;stride=op['weight_row_stride_bytes'];offset=op['weight_offset_bytes']
        rows=[np.frombuffer(weights,dtype='i1',count=logical,offset=offset+c*stride) for c in range(op['co'])]
        weight=np.stack(rows).reshape(op['co'],op['ci']//op['groups'],op['kernel'],op['kernel'])
        if op['weight_key'] in result and not np.array_equal(result[op['weight_key']],weight):raise ValueError('shared weight binary offsets disagree')
        result[op['weight_key']]=weight
        result[op['bias_key']]=np.frombuffer(bias,dtype='<i8',count=op['co'],offset=op['bias_offset_bytes']).copy()
        result[op['shift_key']]=np.frombuffer(shifts,dtype='<i4',count=op['co'],offset=op['shift_offset_bytes']).copy()
    return result


def run(plan_folder,goldens,output):
    plan_folder=Path(plan_folder);goldens=Path(goldens);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    verify_files(plan_folder,json.loads((plan_folder/'manifest.json').read_text()))
    plan=json.loads((plan_folder/'plan.json').read_text());package=Path(plan['package'])
    if hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest()!=plan['package_manifest_sha256']:raise ValueError('physical plan package binding differs')
    verify_files(package,json.loads((package/'manifest.json').read_text()))
    maps,shifts=read_binary_tables(plan,plan_folder);parameters=read_binary_parameters(plan,package);rows=[];started=time.perf_counter()
    for name in ('2402','2493','2410'):
        folder=goldens/name;expected=dict(np.load(folder/'integer_tensors.npz'));probabilities=dict(np.load(folder/'probability_heads.npz'))
        result=replay(plan,maps,shifts,parameters,expected['images'],expected)
        differences={key:float(np.abs(result['heads'][key]-probabilities[key]).max()) for key in plan['output_names']}
        mismatches=sum(x['mismatches'] for x in result['layer_comparisons'])
        passed=not mismatches and len(result['layer_comparisons'])==sum(x['op']==0 for x in plan['operations']) and max(differences.values())<1e-6
        record=dict(image=name,status='PASS' if passed else 'FAIL',conv_layers=len(result['layer_comparisons']),integer_mismatches=mismatches,
            head_max_abs=differences,layer_comparisons=result['layer_comparisons'],source=json.loads((folder/'source.json').read_text()),
            source_artifact_sha256={f:hashlib.sha256((folder/f).read_bytes()).hexdigest() for f in ('integer_tensors.npz','probability_heads.npz','source.json')})
        rows.append(record);np.savez(output/(name+'_physical_heads.npz'),**result['heads'])
        print(json.dumps(dict(image=name,status=record['status'],conv_layers=record['conv_layers'],integer_mismatches=mismatches,head_max_abs=differences)),flush=True)
    summary=dict(status='PHYSICAL_PLAN_HOST_REPLAY_PASS' if all(x['status']=='PASS' for x in rows) else 'FAIL',board_executed=False,
        scope='Independent NumPy int64 convolution and actual DDR workspace execution; reads packed channel-map/shift/row-padded-weight/bias/shift binaries via physical byte offsets; goldens are comparison-only after each computed Conv',
        package_manifest_sha256=plan['package_manifest_sha256'],plan_sha256=hashlib.sha256((plan_folder/'plan.json').read_bytes()).hexdigest(),
        operation_count=plan['operation_count'],workspace_bytes=plan['workspace_bytes'],traffic=plan['traffic'],probes=rows,elapsed_s=time.perf_counter()-started,
        source_sha256={str(p.name):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(__file__).parent/'physical_plan.py',Path(__file__).resolve().parents[1]/'host/integer_reference.py']})
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(dict(status=summary['status'],elapsed_s=summary['elapsed_s'])),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',required=True);p.add_argument('--goldens',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();run(args.plan,args.goldens,args.output)
