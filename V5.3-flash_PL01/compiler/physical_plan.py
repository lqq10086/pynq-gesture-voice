"""Compile CHW plane-address views and explicit integer materialization stages."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np


class PlaneView:
    def __init__(self,shape,offsets,exponent,plane_size=None):
        self.shape=tuple(int(x) for x in shape);self.exponent=exponent
        self.plane_size=int(np.prod(self.shape[-2:])) if plane_size is None else int(plane_size)
        self.offsets=np.asarray(offsets,np.uint32).reshape(-1)
        if np.prod(self.shape)!=self.offsets.size*self.plane_size:raise ValueError('plane map does not span shape')
        self.leading=self._leading(self.shape)

    def _leading(self,shape):
        if len(shape)>=4 and np.prod(shape[-2:])==self.plane_size:return tuple(shape[:-2])
        product=1
        for count,value in enumerate(reversed(shape),1):
            product*=value
            if product==self.plane_size:return tuple(shape[:-count])
            if product>self.plane_size:break
        raise ValueError('shape operation splits a spatial plane')

    def reshape(self,shape):
        shape=list(int(x) for x in shape)
        if -1 in shape:
            if shape.count(-1)!=1:raise ValueError('multiple inferred reshape dimensions')
            shape[shape.index(-1)]=int(np.prod(self.shape))//abs(int(np.prod(shape)))
        if np.prod(shape)!=np.prod(self.shape):raise ValueError('reshape changes element count')
        return PlaneView(shape,self.offsets,self.exponent,self.plane_size)

    def transpose(self,perm):
        prefix=len(self.leading);perm=tuple(perm)
        if perm[prefix:]!=tuple(range(prefix,len(self.shape))) or sorted(perm[:prefix])!=list(range(prefix)):raise ValueError('transpose changes spatial pixel order')
        offsets=self.offsets.reshape(self.leading).transpose(perm[:prefix]).reshape(-1)
        return PlaneView(tuple(self.shape[x] for x in perm),offsets,self.exponent,self.plane_size)

    def gather(self,axis,indices):
        axis%=len(self.shape);indices=np.asarray(indices,np.int64)
        if axis>=len(self.leading):raise ValueError('Gather indexes pixels rather than channels')
        offsets=np.take(self.offsets.reshape(self.leading),indices,axis=axis).reshape(-1)
        shape=self.shape[:axis]+indices.shape+self.shape[axis+1:]
        return PlaneView(shape,offsets,self.exponent,self.plane_size)

    def record(self):return dict(shape=list(self.shape),exponent=self.exponent,plane_elements=self.plane_size,channel_offsets_elements=self.offsets.tolist())


def compile_plan(graph,layout):
    cursor=0;allocations=[];operations=[];maps={};shifts={};views={};constants={};ps_tail=[];ps=False
    def allocate(name,shape,exponent):
        nonlocal cursor
        shape=tuple(shape)
        if len(shape)!=4 or shape[0]!=1:raise ValueError('materialized hardware tensors require batch-one NCHW')
        base=(cursor+3)//4*4;elements=int(np.prod(shape));cursor=base+elements
        offsets=base+np.arange(shape[1],dtype=np.uint32)*int(np.prod(shape[2:]))
        allocations.append(dict(tensor=name,offset_elements=base,elements=elements,bytes=elements*2,shape=list(shape)))
        return PlaneView(shape,offsets,exponent)
    def emit(name,kind,source,output,shape,exponent,read_shifts=None,conv=None):
        target=allocate(output,shape,exponent);index=len(operations);mkey='map%03d'%index;skey='channel_shift%03d'%index
        maps[mkey]=source.offsets.copy();shifts[skey]=np.zeros(source.offsets.size,np.int32) if read_shifts is None else np.asarray(read_shifts,np.int32)
        if len(shifts[skey])!=len(maps[mkey]):raise ValueError('channel shift/map length differs')
        op=dict(index=index,name=name,op=kind,input_shape=list(source.shape),output_shape=list(shape),ci=source.shape[1],co=shape[1],
            ih=source.shape[2],iw=source.shape[3],oh=shape[2],ow=shape[3],kernel=1,stride=1,pad=0,groups=1,relu=False,
            use_channel_offsets=1,map_key=mkey,channel_shift_key=skey,output_tensor=output,output_offset_elements=int(target.offsets[0]),
            input_exponent=source.exponent,output_exponent=exponent,
            logical_tensor_read_bytes=int(np.prod(source.shape))*2,logical_tensor_write_bytes=int(np.prod(shape))*2,
            channel_map_read_bytes=source.offsets.size*4,channel_shift_read_bytes=source.offsets.size*4)
        if conv is not None:
            for key in ('kernel','stride','pad','groups','relu','weight_key','bias_key','shift_key','index'):op['conv_index' if key=='index' else key]=conv[key]
            weight=layout[conv['weight_key']];bias=layout[conv['bias_key']];shift=layout[conv['shift_key']]
            op.update(weight_offset_bytes=weight['padded_offset_bytes'],weight_row_stride_bytes=weight['padded_row_stride_bytes'],
                padded_weight_bytes=weight['padded_bytes'],bias_offset_bytes=bias['offset_bytes'],bias_read_bytes=bias['bytes'],
                shift_offset_bytes=shift['offset_bytes'],shift_read_bytes=shift['bytes'])
        operations.append(op);views[output]=target
        return target
    views['images']=allocate('images',graph['input_shape'],graph['tensor_exponents']['images'])
    for node in graph['nodes']:
        kind=node['kind'];names=node['inputs'];out=node['outputs'][0];attrs=node['attrs']
        if kind in ('Sigmoid','Softmax'):ps=True
        if ps:ps_tail.append(node);continue
        if kind=='Constant':constants[out]=np.asarray(attrs['value'],dtype=attrs['dtype'])
        elif kind=='Conv':
            conv=graph['convolutions'][node['conv_index']];source=views[names[0]]
            if source.exponent!=conv['input_exponent']:raise ValueError('physical Conv input exponent differs')
            emit(node['name'],0,source,out,conv['output_shape'],conv['output_exponent'],conv=conv)
        elif kind=='Relu':views[out]=views[names[0]]
        elif kind=='Reshape':
            shape=constants[names[1]].reshape(-1).tolist();shape=[views[names[0]].shape[i] if x==0 else x for i,x in enumerate(shape)]
            views[out]=views[names[0]].reshape(shape)
        elif kind=='Transpose':views[out]=views[names[0]].transpose(attrs['perm'])
        elif kind=='Gather':views[out]=views[names[0]].gather(attrs.get('axis',0),constants[names[1]])
        elif kind=='Concat':
            sources=[views[x] for x in names]
            if attrs['axis']!=1 or any(x.shape[0]!=1 or x.shape[2:]!=sources[0].shape[2:] for x in sources):raise ValueError('expected NCHW channel Concat')
            exponent=max(x.exponent for x in sources);shape=(1,sum(x.shape[1] for x in sources),*sources[0].shape[2:])
            offsets=np.concatenate([x.offsets for x in sources]);combined=PlaneView(shape,offsets,exponent)
            align=[x.exponent-exponent for x in sources]
            if align!=node['align_shifts']:raise ValueError('Concat alignment differs from numerical graph')
            if any(align):
                per_channel=np.concatenate([np.full(x.shape[1],s,np.int32) for x,s in zip(sources,align)])
                emit(node['name'],3,combined,out,shape,exponent,per_channel)
                operations[-1].update(source_tensors=names,source_exponents=[x.exponent for x in sources],alignment_stages_preserved=True)
            else:views[out]=combined
        elif kind=='MaxPool':
            source=views[names[0]];k=attrs['kernel_shape'][0];s=attrs['strides'][0];p=attrs['pads'][0]
            shape=(1,source.shape[1],(source.shape[2]+2*p-k)//s+1,(source.shape[3]+2*p-k)//s+1)
            emit(node['name'],1,source,out,shape,source.exponent);operations[-1].update(kernel=k,stride=s,pad=p)
        elif kind=='Resize':
            source=views[names[0]]
            if attrs['mode']!='nearest' or attrs['nearest_mode']!='floor' or attrs['coordinate_transformation_mode']!='asymmetric' or not np.array_equal(constants[names[-1]],[1,1,2,2]):raise ValueError('unsupported Resize')
            emit(node['name'],2,source,out,(1,source.shape[1],source.shape[2]*2,source.shape[3]*2),source.exponent)
        else:raise ValueError('unexpected integer node '+kind)
    logits={name for node in ps_tail for name in node['inputs'] if name in views}
    if not ps_tail:logits=set(graph['output_names'])
    total=(cursor+3)//4*4
    traffic={key:sum(x.get(key,0) for x in operations) for key in ('logical_tensor_read_bytes','logical_tensor_write_bytes','channel_map_read_bytes','channel_shift_read_bytes','padded_weight_bytes','bias_read_bytes','shift_read_bytes')}
    traffic['scope']='EXACT_LOGICAL_STAGE_PAYLOAD_COUNTS; hardware cache/repeat/burst effects are not measured DDR bus traffic'
    plan=dict(schema='fastest-physical-plan-v1',model_sha256=graph.get('model_sha256'),input=views['images'].record(),
        workspace_elements=total,workspace_bytes=total*2,alignment_elements=4,allocations=allocations,operations=operations,
        operation_counts={str(k):sum(x['op']==k for x in operations) for k in range(4)},operation_count=len(operations),
        tensor_views={k:v.record() for k,v in views.items()},ps_logits={k:views[k].record() for k in sorted(logits)},
        ps_tail_nodes=ps_tail,output_names=graph['output_names'],traffic=traffic,phase_output_bits=3,
        alias_strategy='Reshape/Transpose/Gather and same-e Concat reorder CHW plane-offset arrays only; every nonzero Concat alignment materializes one op3 stage',
        board_executed=False)
    return plan,maps,shifts


def write_plan(package,output):
    package=Path(package);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((package/'manifest.json').read_text())
    for name,record in manifest['files'].items():
        if hashlib.sha256((package/name).read_bytes()).hexdigest()!=record['sha256']:raise ValueError('quant package file changed: '+name)
    graph=json.loads((package/'graph.json').read_text());layout=json.loads((package/'binary_layout.json').read_text())
    plan,maps,shifts=compile_plan(graph,layout)
    for filename,table,dtype in [('channel_offsets.bin',maps,'<u4'),('channel_shifts.bin',shifts,'<i4')]:
        position=0
        with (output/filename).open('wb') as stream:
            for op in plan['operations']:
                key=op['map_key' if filename.startswith('channel_offsets') else 'channel_shift_key'];data=table[key].astype(dtype).tobytes();stream.write(data)
                op['channel_offsets_offset_bytes' if filename.startswith('channel_offsets') else 'channel_shifts_offset_bytes']=position;position+=len(data)
    np.savez(output/'channel_tables.npz',**maps,**shifts)
    plan.update(package_manifest_sha256=hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest(),package=str(package.resolve()),
        source_sha256={'compiler/physical_plan.py':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    (output/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output/'manifest.json').write_text(json.dumps(dict(status='HOST_PLAN_NOT_BOARD_QUALIFIED',files=files),indent=2)+'\n')
    print(json.dumps(dict(operations=plan['operation_count'],counts=plan['operation_counts'],workspace_bytes=plan['workspace_bytes'],traffic=plan['traffic'])),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True);p.add_argument('--output',required=True);args=p.parse_args();write_plan(args.package,args.output)
