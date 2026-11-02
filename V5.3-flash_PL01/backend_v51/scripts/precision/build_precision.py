"""384 graph packing with the preserved V2.9.8 integer arithmetic rules.

All weights/scales are generated from this model; no historical parameter reuse.
"""
import copy, hashlib, json, re, sys
from pathlib import Path
import cv2, numpy as np, onnx, onnxruntime as ort
from onnx import helper,numpy_helper
ROOT=Path(__file__).resolve().parents[2]
PKG=ROOT/'model_package'
sys.path.insert(0,str(ROOT/'scripts/reference'))
from integer_reference import execute,quant,LUT16,LUT8


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))

def write(p,v):Path(p).parent.mkdir(parents=True,exist_ok=True);Path(p).write_text(json.dumps(v,indent=2,ensure_ascii=False),encoding='utf-8')

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def sess(m):
    o=ort.SessionOptions();o.intra_op_num_threads=2
    return ort.InferenceSession(m if isinstance(m,bytes) else str(m),sess_options=o,providers=['CPUExecutionProvider'])

def model_index(name):return int(re.search(r'model\.(\d+)',name)[1])

def width(v):return int(v).bit_length()+1

def iou(a,b):
    a=np.asarray(a);b=np.asarray(b);inter=np.maximum(0,np.minimum(a[2:],b[2:])-np.maximum(a[:2],b[:2])).prod()
    return float(inter/(np.maximum(0,a[2:]-a[:2]).prod()+np.maximum(0,b[2:]-b[:2]).prod()-inter+1e-9))

def graph(model_path=None):
    source=Path(model_path) if model_path is not None else PKG/'reference.onnx'

    m=onnx.shape_inference.infer_shapes(onnx.load(source))
    shapes={v.name:[d.dim_value for d in v.type.tensor_type.shape.dim] for v in list(m.graph.input)+list(m.graph.value_info)+list(m.graph.output)}
    init={v.name:numpy_helper.to_array(v) for v in m.graph.initializer};ops=[]
    for n in m.graph.node:
        attrs={a.name:helper.get_attribute_value(a) for a in n.attribute}
        if n.op_type=='Conv':
            w=init[n.input[1]];linear=model_index(n.name)==24
            output=n.output[0] if linear else n.name.replace('/conv/Conv','/act/Mul_output_0')
            assert output in shapes
            ops.append(dict(index=len(ops),name=n.name,node_name=n.name,op_type='ConvLinear' if linear else 'ConvSiLU',sources=[n.input[0]],source_tensor=n.input[0],output_tensor=output,weight_tensor=n.input[1],bias_tensor=n.input[2],Cin=w.shape[1],Cout=w.shape[0],kernel=w.shape[2],stride=attrs['strides'][0],padding=attrs['pads'][0],H=shapes[output][2],W=shapes[output][3]))
        elif n.op_type in ['Add','Concat','MaxPool','Resize'] and model_index(n.name)<24:
            ops.append(dict(index=len(ops),name=n.name,op_type=n.op_type,sources=list(n.input) if n.op_type in ['Add','Concat'] else [n.input[0]],output_tensor=n.output[0],shape=shapes[n.output[0]]))
    counts={k:sum(o['op_type']==k for o in ops) for k in ['ConvSiLU','ConvLinear','Add','Concat','MaxPool','Resize']}
    assert counts==dict(ConvSiLU=57,ConvLinear=3,Add=7,Concat=13,MaxPool=3,Resize=2),counts
    return m,shapes,init,ops

def parameters(ops,init,cal,end=8,input_bits=16):
    maxima=cal['maximum'];pct=cal['percentile'];input_name='images';ibits=input_bits
    info={input_name:dict(bits=ibits,scale=max(maxima[input_name],1e-12)/((1<<(ibits-1))-1))};params={};outops=[]
    for item in ops:
        o=copy.deepcopy(item);i=o['index'];kind=o['op_type'];mi=model_index(o['name'])
        bits=16 if mi<=end and mi>=1 else 8
        if kind=='ConvLinear':bits=16
        # stem output must feed high precision model1 input without discarding its values.
        if mi==0:bits=16
        if kind in ['MaxPool','Resize']:bits=info[o['sources'][0]]['bits']
        qmax=(1<<(bits-1))-1;name=o['output_tensor'];ss=[info[n]['scale'] for n in o['sources']]
        so=max((maxima if bits==16 else pct)[name]/qmax,1e-12)
        frac=16 if bits==16 else 8
        if kind=='ConvSiLU' and frac==8:so=min(so,32767/(256*127))
        if kind in ['ConvSiLU','ConvLinear']:
            w=init[o['weight_tensor']];b=init[o['bias_tensor']]
            wmax=127
            sw=np.maximum(np.max(np.abs(w),axis=(1,2,3))/wmax,1e-12)
            wq=np.clip(np.rint(w/sw[:,None,None,None]),-wmax,wmax).astype(np.int8)
            bq=np.rint(b/(ss[0]*sw)).astype(np.int64)
            if kind=='ConvLinear':mult=np.rint(ss[0]*sw/so*(1<<24)).astype(np.int64);om=0
            else:mult=np.rint(ss[0]*sw*(1<<frac)*(1<<24)).astype(np.int64);om=int(np.rint((1<<24)/((1<<frac)*so)))
            assert mult.min()>0 and mult.max()<2**31 and 0<=om<2**31
            inputmax=(1<<(info[o['sources'][0]]['bits']-1))-1
            weightmax=32767 if wq.dtype==np.int16 else 127
            bound=inputmax*weightmax*o['Cin']*o['kernel']**2+int(np.abs(bq).max())
            prod=bound*int(mult.max())+(1<<23)
            assert prod<2**63,(o['name'],prod)
            accbits=max(34,width(bound));productbits=width(prod)
            zbound=(prod>>24)+1
            outputbound=(32767 if frac==8 else max(zbound,int(np.abs(LUT16).max())))*om if kind=='ConvSiLU' else prod
            assert outputbound<2**63,(o['name'],'outputrequantoverflow')
            o.update(weight_dtype=str(wq.dtype),weight_bits=16 if wq.dtype==np.int16 else 8,weight_scale=sw.tolist(),bias_dtype='int64',input_scale=ss[0],output_scale=so,output_multiplier=om,silu_fraction_bits=frac,accumulator_width=accbits,accumulator_bound=bound,requant_product_width=productbits,requant_multiplier_max=int(mult.max()),activation_mode='LINEAR' if kind=='ConvLinear' else 'SiLU')
            o.update(bias_bound=int(np.abs(bq).max()),bias_required_width=width(np.abs(bq).max()),multiplier_required_width=width(mult.max()),output_requant_bound=outputbound,output_requant_width=width(outputbound),silu_lut_interpolation_product_width=25 if frac==16 else 0)
            for key,a in [('w',wq),('b',bq),('m',mult.astype('<i4'))]:params[f'{key}{i:02d}']=a
        elif kind in ['Add','Concat']:
            if kind=='Concat':so=max(ss)
            ms=[int(np.rint(s/so*(1<<22))) for s in ss]
            assert all(0<m<=((1<<23)-1 if kind=='Add' else 1<<22) for m in ms),('branchmultiplierexceedsfixedhardwarecapacity',o['name'],ms)
            o.update(multipliers=ms,branch_intermediate_width=width(sum(((1<<(info[n]['bits']-1))-1)*m for n,m in zip(o['sources'],ms))))
        else:so=ss[0]
        o.update(activation_bits=bits,input_bits=[info[n]['bits'] for n in o['sources']],input_scales=ss,output_scale=so,layout='CHW',input_shapes=[list(init[n].shape) if n in init else [] for n in o['sources']])
        info[name]=dict(bits=bits,scale=so);outops.append(o)
    return outops,params,info

def save_package(folder,ops,params,info,shapes,label,model_path=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    source=Path(model_path) if model_path is not None else PKG/'reference.onnx'
    np.savez(folder/'integer_parameters.npz',**params)
    blobs={k:[] for k in ['weights_int8','bias_int64','requant_int32']};offset={k:0 for k in blobs}
    for o in ops:
        for n in o['sources']:o['input_shapes'][o['sources'].index(n)]=shapes[n]
        o['output_shape']=shapes[o['output_tensor']]
        if o['op_type'].startswith('Conv'):
            for key,blob in [('w','weights_int8'),('b','bias_int64'),('m','requant_int32')]:
                a=params[f'{key}{o["index"]:02d}'];o[dict(w='weight_offset',b='bias_offset',m='quant_parameter_offset')[key]]=offset[blob];blobs[blob].append(a.ravel());offset[blob]+=a.nbytes
    for name,arrays in blobs.items():np.concatenate(arrays).tofile(folder/f'{name}.bin')
    padded=bytearray()
    for o in ops:
        if o['op_type'].startswith('Conv'):
            a=params[f'w{o["index"]:02d}'].reshape(o['Cout'],-1);logical=a.shape[1]*a.dtype.itemsize;stride=(logical+7)//8*8
            o['weight_padded_offset']=len(padded);o['weight_channel_stride_bytes']=stride
            o['weight_layout']='OIHW per-Cout8bytepadded';o['zero_point']=0
            for channel in a:padded.extend(channel.tobytes());padded.extend(bytes(stride-logical))
    (folder/'weights_padded.bin').write_bytes(padded)
    LUT16.astype('<i4').tofile(folder/'silu_q16.bin');LUT8.astype('<i2').tofile(folder/'silu_q8.bin')
    write(folder/'layer_config.json',ops);write(folder/'tensor_scales.json',info)
    write(folder/'model_manifest.json',dict(version='V3.4_WB1_V5_MODELS_20261025',scope=label,model_sha256=sha(source),architecture_id=hashlib.sha256(json.dumps([{k:o.get(k) for k in ['name','op_type','sources','output_tensor','Cin','Cout','kernel','stride','padding','output_shape']} for o in ops],sort_keys=True).encode()).hexdigest(),hardware_abi='v32-384-chw-w8-mixed-a8a16-q16-q22-q24',input_tensor='images',input_shape=shapes['images'],input_bits=info.get('images',info[next(iter(info))])['bits'],input_scale=info.get('images',info[next(iter(info))])['scale'],head_outputs=[dict(name=o['output_tensor'],shape=o['output_shape'],scale=o['output_scale'],bits=o['activation_bits']) for o in ops if o['op_type']=='ConvLinear'],counts={k:sum(o['op_type']==k for o in ops) for k in sorted(set(o['op_type'] for o in ops))},files={p.name:dict(bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(folder.iterdir()) if p.is_file() and p.name!='model_manifest.json'},conf_threshold=.60,nms_iou=.45,classes=['open_palm','pinch_point']))
    manifest=read(folder/'model_manifest.json');model=onnx.load(source)
    constants={n.name:numpy_helper.to_array(helper.get_attribute_value(n.attribute[0])) for n in model.graph.node if n.op_type=='Constant' and 'model.24/' in n.name}
    anchors=[constants[f'/model.24/Constant_{i}'][0,:,0,0,:].tolist() for i in [6,14,22]]
    strides=[float(constants[f'/model.24/Constant_{i}']) for i in [3,11,19]]
    manifest.update(weight_layout='OIHW per-Cout8bytepadded',weight_file='weights_padded.bin',zero_point=0,detect=dict(anchors_pixels=anchors,strides=strides,channels_per_anchor=7,anchors_per_head=3,layout='NCHWreshape[N,3,7,H,W]transpose[N,3,H,W,7]',xy='(sigmoid(rawxy)*2+grid_xy-0.5)*stride',wh='(sigmoid(rawwh)*2)**2*anchor_pixels',scores='sigmoid(objectness)*sigmoid(class)',actual_ONNX_constants=True))
    write(folder/'model_manifest.json',manifest)

def decode_session(model,ops,shapes):
    heads=[o['output_tensor'] for o in ops if o['op_type']=='ConvLinear'];done=set(heads)
    nodes=[]
    for n in model.graph.node:
        if n.op_type=='Constant' or (any(x in done for x in n.input)):
            if n.op_type=='Constant' or model_index(n.name)==24:nodes.append(copy.deepcopy(n));done.update(n.output)
    g=helper.make_graph(nodes,'PSDetectDecode',[helper.make_tensor_value_info(n,onnx.TensorProto.FLOAT,shapes[n]) for n in heads],list(model.graph.output))
    mm=helper.make_model(g,opset_imports=list(model.opset_import));mm.ir_version=model.ir_version
    onnx.checker.check_model(mm);onnx.save(mm,PKG/'decode.onnx');return sess(mm.SerializeToString())
