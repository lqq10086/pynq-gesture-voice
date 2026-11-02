"""Inventory and compile the exact frozen Fastest graph, including shuffle views."""
from collections import Counter,defaultdict
import copy
import hashlib
import json
from pathlib import Path
import onnx
from onnx import helper,numpy_helper
import numpy as np
from integer_reference import exponent_for,quantize

MODEL=Path('D:/Desktop/YOLO-FastestV2/exports/gesture_best.onnx')
MODEL_SHA='e210ef35bdd9ea8e6f622cda6eba92ef5ef4b2d80371bdc204d39914d23bb394'


def json_value(value):
    if isinstance(value,bytes):return value.decode('ascii')
    if isinstance(value,np.ndarray):return value.tolist()
    return value


def inventory(path=MODEL):
    path=Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=MODEL_SHA:raise ValueError('exact trained Fastest model required')
    model=onnx.shape_inference.infer_shapes(onnx.load(str(path)))
    onnx.checker.check_model(model)
    shapes={x.name:[d.dim_value for d in x.type.tensor_type.shape.dim] for x in list(model.graph.input)+list(model.graph.value_info)+list(model.graph.output)}
    initializers={x.name:numpy_helper.to_array(x) for x in model.graph.initializer}
    uses=defaultdict(list)
    for node in model.graph.node:
        for name in node.input:uses[name].append(node)
    convs=[];nodes=[]
    for node in model.graph.node:
        attrs={a.name:json_value(helper.get_attribute_value(a)) for a in node.attribute}
        if node.op_type=='Constant':attrs={'value':numpy_helper.to_array(helper.get_attribute_value(node.attribute[0])).tolist(),'dtype':str(numpy_helper.to_array(helper.get_attribute_value(node.attribute[0])).dtype)}
        row=dict(name=node.name,kind=node.op_type,inputs=list(node.input),outputs=list(node.output),attrs=attrs,
                 input_shapes=[shapes.get(x,[]) for x in node.input],output_shape=shapes.get(node.output[0],[]))
        if node.op_type=='Conv':
            w=initializers[node.input[1]];group=attrs.get('group',1)
            consumers=uses[node.output[0]]
            relu=len(consumers)==1 and consumers[0].op_type=='Relu'
            quant_output=consumers[0].output[0] if relu else node.output[0]
            conv=dict(index=len(convs),name=node.name,input=node.input[0],output=node.output[0],quant_output=quant_output,
                weight=node.input[1],bias=node.input[2],ci=int(w.shape[1]*group),co=int(w.shape[0]),
                groups=group,kernel=int(w.shape[2]),stride=attrs['strides'][0],pad=attrs['pads'][0],
                input_shape=shapes[node.input[0]],output_shape=shapes[node.output[0]],relu=relu)
            if attrs.get('dilations',[1,1])!=[1,1] or len(set(attrs['pads']))!=1:raise ValueError('unsupported Conv geometry')
            convs.append(conv);row['conv_index']=conv['index']
        nodes.append(row)
    counts=dict(Counter(n['kind'] for n in nodes))
    if counts.get('Conv')!=79 or counts.get('Relu')!=46 or counts.get('BatchNormalization',0):raise ValueError('trained topology changed')
    result=dict(model=str(path),model_sha256=MODEL_SHA,opset=11,classes=['open_palm','pinch_point'],
        input_shape=shapes['images'],output_names=[x.name for x in model.graph.output],
        output_shapes=[shapes[x.name] for x in model.graph.output],counts=counts,convolutions=convs,nodes=nodes,
        unique_weight_count=len({x['weight'] for x in convs}),bn='ALREADY_FOLDED_DO_NOT_REFOLD')
    return result,model,initializers


def compile_package(graph,initializers,maxima):
    graph=copy.deepcopy(graph);exponents={'images':exponent_for(maxima['images'],16)};parameters={};weights={};convs=graph['convolutions']
    for node in graph['nodes']:
        kind=node['kind'];inputs=node['inputs'];output=node['outputs'][0]
        if kind=='Conv':
            op=convs[node['conv_index']];ei=exponents[op['input']];eo=exponent_for(maxima[op['quant_output']],16)
            op.update(input_exponent=ei,output_exponent=eo)
            exponents[output]=eo
        elif kind=='Constant':pass
        elif kind in ('Sigmoid','Softmax'):exponents[output]=None
        elif kind=='Concat':
            scales=[exponents[x] for x in inputs]
            if any(x is None for x in scales):
                if not all(x is None for x in scales):raise ValueError('mixed integer/floating Concat')
                exponents[output]=None
            else:
                eo=max(scales);exponents[output]=eo;node['align_shifts']=[x-eo for x in scales]
                node['output_exponent']=eo
                node['alignment']='EXPLICIT_INTEGER_RNE_COPY' if any(node['align_shifts']) else 'SAME_SCALE_CHANNEL_VIEW'
                elements=sum(int(np.prod(shape)) for shape,shift in zip(node['input_shapes'],node['align_shifts']) if shift)
                node['alignment_read_write_bytes']=elements*4
        elif kind in ('Relu','MaxPool','Resize','Reshape','Transpose','Gather'):
            exponents[output]=exponents[inputs[0]]
        else:raise ValueError('unimplemented graph operation: '+kind)
    # Shared heads must use one weight quantization satisfying every invocation.
    weight_quantization={}
    for op in convs:
        weight_name=op['weight']
        if weight_name in weights:continue
        key='w%03d'%len(weights);weights[weight_name]=key;w=initializers[weight_name].astype(np.float64)
        derived=np.array([exponent_for(x,8) for x in np.abs(w).max(axis=(1,2,3))],np.int32);ew=derived.copy()
        consumers=[x for x in convs if x['weight']==weight_name]
        for channel in range(w.shape[0]):
            for _ in range(256):
                wq=np.clip(np.rint(np.ldexp(w[channel],-int(ew[channel]))),-128,127).astype(np.int8)
                weight_bound=32768*sum(abs(int(x)) for x in wq.ravel())
                valid=True
                for consumer in consumers:
                    b=float(initializers[consumer['bias']][channel]);ei=consumer['input_exponent']
                    floating=np.rint(np.ldexp(b,-(ei+int(ew[channel]))))
                    if not np.isfinite(floating) or abs(float(floating))>2**47-1 or abs(int(floating))+weight_bound>2**47-1:
                        valid=False;break
                if valid:break
                ew[channel]+=1
            else:raise OverflowError('no safe 48-bit weight exponent')
        parameters[key]=np.clip(np.rint(np.ldexp(w,-ew[:,None,None,None])),-128,127).astype(np.int8)
        parameters[key+'_exponent']=ew
        adjustments=[dict(channel=i,weight_derived_exponent=int(a),effective_exponent=int(b),reason='bias plus worst-case MAC must fit signed 48-bit') for i,(a,b) in enumerate(zip(derived,ew)) if a!=b]
        weight_quantization[key]=dict(initializer=weight_name,weight_derived_exponents=derived.tolist(),effective_exponents=ew.tolist(),adjustments=adjustments)
    for op in convs:
        key=weights[op['weight']];ew=parameters[key+'_exponent'];ei=op['input_exponent'];eo=op['output_exponent'];index=op['index']
        original=initializers[op['bias']].astype(np.float64)
        bq=np.rint(np.ldexp(original,-(ei+ew))).astype(np.int64);shift=(ei+ew-eo).astype(np.int32)
        bkey='b%03d'%index;skey='shift%03d'%index;parameters[bkey]=bq;parameters[skey]=shift
        bound=32768*np.abs(parameters[key].astype(np.int64)).sum(axis=(1,2,3))+np.abs(bq)
        required=max(int(x).bit_length()+1 for x in bound)
        if required>48:raise OverflowError('48-bit accumulator insufficient: '+op['name'])
        dequant=np.ldexp(bq.astype(np.float64),ei+ew);adjustments=weight_quantization[key]['adjustments']
        op.update(weight_key=key,bias_key=bkey,shift_key=skey,weight_exponents=ew.tolist(),shift=shift.tolist(),
            accumulator_bound=int(bound.max()),accumulator_required_bits=required,weight_exponent_adjustments=adjustments,
            bias_dequant_max_abs_error=float(np.abs(dequant-original).max()),
            adjusted_channel_bias=[dict(channel=x['channel'],fp32_bias=float(original[x['channel']]),integer_bias=int(bq[x['channel']]),dequantized_bias=float(dequant[x['channel']]),abs_error=float(abs(dequant[x['channel']]-original[x['channel']]))) for x in adjustments])
    graph.update(tensor_exponents=exponents,weight_sharing=weights,weight_quantization=weight_quantization,
        integer_contract='W8-per-output-channel/A16/power-of-two/RNE/saturate-int16/int64-bias',
        parameter_keys=sorted(parameters),ps_ops=['Sigmoid','Softmax','native decode/NMS'],pl_neural_network_executed=False)
    return graph,parameters


def write_package(folder,graph,parameters,calibration):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    np.savez(folder/'parameters.npz',**parameters)
    manifest={}
    for family,dtype in [('weights','i1'),('bias','<i8'),('shift','<i4')]:
        keys=sorted(set(op[dict(weights='weight_key',bias='bias_key',shift='shift_key')[family]] for op in graph['convolutions']))
        offset=0
        with (folder/(family+'.bin')).open('wb') as out:
            for key in keys:
                data=parameters[key].astype(dtype).tobytes();out.write(data)
                manifest[key]=dict(file=family+'.bin',offset_bytes=offset,bytes=len(data),shape=list(parameters[key].shape),dtype=dtype)
                offset+=len(data)
    offset=0
    with (folder/'weights_padded.bin').open('wb') as out:
        for key in sorted(set(op['weight_key'] for op in graph['convolutions'])):
            weights=parameters[key].reshape(parameters[key].shape[0],-1);logical=weights.shape[1];stride=(logical+7)//8*8
            for channel in weights:out.write(channel.tobytes());out.write(bytes(stride-logical))
            manifest[key].update(padded_file='weights_padded.bin',padded_offset_bytes=offset,
                logical_row_bytes=logical,padded_row_stride_bytes=stride,padded_bytes=len(weights)*stride)
            offset+=len(weights)*stride
    for name,value in [('graph.json',graph),('calibration.json',calibration),('binary_layout.json',manifest)]:
        (folder/name).write_text(json.dumps(value,indent=2)+'\n')
    files={p.name:dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(folder.iterdir()) if p.is_file()}
    (folder/'manifest.json').write_text(json.dumps(dict(status='OFFLINE_QUANTIZATION_PACKAGE_NOT_BOARD_QUALIFIED',model_sha256=MODEL_SHA,files=files),indent=2)+'\n')
