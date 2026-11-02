"""Validate external full-graph parameters before any CMA allocation or MMIO."""
import hashlib
import json
import math
from pathlib import Path
import numpy as np

HARDWARE_ABI='v32-384-chw-w8-mixed-a8a16-q16-q22-q24'
ARCHITECTURE_ID='4b38cd9a94933efecd158ed8b08bcedea52db7248c26df24137256b2a7425f08'
MODEL_SHA256='4cdad59edc6b4f9c0e93e61a488c53b65127347fbe0c34e1465e28c89bb989f4'
# Exact two-class ONNX plus fresh model-specific calibration and integer oracle.
# Manifest claims are not trust; board numeric qualification is a separate gate.
APPROVED_MANIFEST_SHA256='ebecd934138182f28c6ca9d92181831eaaf4d2e1d21e02279c787d808f519748'

class FrozenDict(dict):
    def _blocked(self,*args,**kwargs):raise TypeError('Validated metadata is read-only; reconstruct the instance')
    __setitem__=__delitem__=clear=pop=popitem=setdefault=update=__ior__=_blocked
    def __deepcopy__(self,memo):
        import copy
        return {copy.deepcopy(k,memo):copy.deepcopy(v,memo) for k,v in self.items()}

class FrozenList(list):
    def _blocked(self,*args,**kwargs):raise TypeError('Validated metadata is read-only; reconstruct the instance')
    __setitem__=__delitem__=append=clear=extend=insert=pop=remove=reverse=sort=__iadd__=__imul__=_blocked
    def __deepcopy__(self,memo):
        import copy
        return [copy.deepcopy(v,memo) for v in self]

def freeze_metadata(value):
    if isinstance(value,dict):return FrozenDict({k:freeze_metadata(v) for k,v in value.items()})
    if isinstance(value,list):return FrozenList(freeze_metadata(v) for v in value)
    return value
REQUIRED=('layer_config.json','tensor_scales.json','weights_padded.bin','bias_int64.bin','requant_int32.bin','reference.onnx','silu_q16.bin','silu_q8.bin')
def require(condition,message):
    if not condition:raise ValueError(message)
def integer(value,name,minimum=0,maximum=2**31-1):
    require(type(value) is int and minimum<=value<=maximum,'Invalid '+name);return value
def positive(value):
    require(type(value) in (int,float) and math.isfinite(value) and value>0,'Scale must be finite positive');return value
def dimensions(value):
    require(isinstance(value,list) and len(value)==4 and value[0]==1,'Expected batch1 NCHW')
    for d in value:integer(d,'dimension',1,512)
    return tuple(value[1:])
def bitwidth(value):require(value in (8,16),'Only A8/A16 supported');return value

def validate_package(folder):
    try:return _validate(Path(folder).resolve())
    except (KeyError,TypeError,IndexError,OSError,json.JSONDecodeError) as error:
        raise ValueError('Malformed full-graph package: '+str(error)) from error
validate_region_package=validate_package

def _validate(folder):
    manifest_bytes=(folder/'model_manifest.json').read_bytes()
    require(hashlib.sha256(manifest_bytes).hexdigest()==APPROVED_MANIFEST_SHA256,
            'Manifest differs from the approved model/Detect/parameter identity; requalification required')
    manifest=json.loads(manifest_bytes)
    require(manifest['hardware_abi']==HARDWARE_ABI,'Incompatible hardware ABI')
    require(manifest['architecture_id']==ARCHITECTURE_ID,'Unsupported full graph architecture')
    require(manifest['input_tensor']=='images' and manifest['input_shape']==[1,3,384,384],'Fixed384 input required')
    require(manifest['output_shape']==[1,9072,7],'Fixed384 two-class decoded output required')
    require(manifest['model_sha256']==MODEL_SHA256,'Unqualified 384 model identity')
    require(manifest['conf_threshold']==.60 and manifest['nms_iou']==.45,'Formal thresholds changed')
    require(manifest['classes']==['open_palm','pinch_point'],'Class order changed')
    require(manifest.get('zero_point',0)==0,'Symmetric zero-point0 required')
    require(manifest.get('weight_layout')=='OIHW per-Cout8bytepadded' and manifest.get('weight_file')=='weights_padded.bin','Packed weight layout contract missing')
    require(set(REQUIRED)<=set(manifest['files']),'Required file not hashed')
    blobs={}
    for name,info in manifest['files'].items():
        require(isinstance(name,str) and name not in ('','.','..') and not any(c in name for c in '/\\:'),'Unsafe package filename')
        path=folder/name;require(path.resolve().parent==folder,'Package symlink escape')
        data=path.read_bytes()
        require(len(data)==info['bytes'] and hashlib.sha256(data).hexdigest()==info['sha256'],'SHA256 or byte-count mismatch: '+name)
        blobs[name]=data
    require(hashlib.sha256(blobs['reference.onnx']).hexdigest()==manifest['model_sha256'],'Reference model identity mismatch')
    for name,digest in [('silu_q16.bin','7f5cd69251f8cf59200010af8410be806712243e9ae54470831213bbe3327a8e'),('silu_q8.bin','c3a113dc40aabacafbe1060198b262dcb5278cf6027b37987184e8efef58f441')]:
        require(hashlib.sha256(blobs[name]).hexdigest()==digest,'Fixed hardware SiLU table mismatch')
    operations=json.loads(blobs['layer_config.json']);scales=json.loads(blobs['tensor_scales.json'])
    architecture=[{k:o.get(k) for k in ['name','op_type','sources','output_tensor','Cin','Cout','kernel','stride','padding','output_shape']} for o in operations]
    require(hashlib.sha256(json.dumps(architecture,sort_keys=True).encode()).hexdigest()==ARCHITECTURE_ID,'Actual graph structure changed')
    known={'images':dimensions(manifest['input_shape'])};bits={'images':bitwidth(manifest['input_bits'])};values={'images':positive(manifest['input_scale'])}
    require(scales['images']==dict(bits=bits['images'],scale=values['images']),'Input quantization metadata mismatch')
    for name,alignment in [('weights_padded.bin',8),('bias_int64.bin',8),('requant_int32.bin',4)]:
        require(len(blobs[name])>0 and len(blobs[name])%alignment==0,'Parameter byte alignment: '+name)
    bias=np.frombuffer(blobs['bias_int64.bin'],'<i8');mult=np.frombuffer(blobs['requant_int32.bin'],'<i4')
    descriptors=[];counts={};scratch=1
    for index,o in enumerate(operations):
        kind=o['op_type'];counts[kind]=counts.get(kind,0)+1
        require(o['index']==index and kind in ('ConvSiLU','ConvLinear','Add','Concat','MaxPool','Resize'),'Unsupported op/index')
        sources=o['sources'];out=o['output_tensor'];shape=dimensions(o['output_shape'])
        require(out not in known and sources and all(s in known for s in sources),'Disconnected/duplicate graph tensor')
        require(o['input_shapes']==[[1]+list(known[s]) for s in sources],'Connected tensor shape mismatch')
        require(o['input_bits']==[bits[s] for s in sources] and o['input_scales']==[values[s] for s in sources],'Connected quantization mismatch')
        require(o['layout']=='CHW','Unsupported tensor layout')
        obits=bitwidth(o['activation_bits']);oscale=positive(o['output_scale']);co,h,w=shape
        require(h==w and h in (192,96,48,24,12),'Unsupported spatial output')
        require(co*h*w<=589824,'Tensor exceeds qualified core capacity')
        ci,ih,iw=known[sources[0]]
        desc=dict(o,graph_index=index,Cin=ci,Cout=co,H=h,W=w,input_height=ih,input_width=iw,dataflow_mode=int(h<=7 and ci>=128))
        if kind.startswith('Conv'):
            require(len(sources)==1 and o['Cin']==ci and o['Cout']==co and o['H']==h and o['W']==w,'Conv channel mismatch')
            k=integer(o['kernel'],'kernel',1,6);s=integer(o['stride'],'stride',1,2);pad=integer(o['padding'],'pad',0,2)
            require(k in (1,3,6) and pad==(2 if k==6 else k//2) and (ih+2*pad-k)//s+1==h,'Conv geometry mismatch')
            require(ci*k*k<=1152 and co<=512,'Conv core capacity exceeded')
            wbits=bitwidth(o.get('weight_bits',16 if o['weight_dtype']=='int16' else 8))
            require(wbits==8,'Final IP3.2 supports W8 only; changed weight precision needs qualified hardware')
            require(o['weight_dtype']=='int8' and o['bias_dtype']=='int64','Weight/bias precision metadata mismatch')
            pitch=(ci*k*k*(wbits//8)+7)//8*8
            require(o['weight_channel_stride_bytes']==pitch,'Per-output packed weight stride mismatch')
            for key,size,alignment,file in [('weight_padded_offset',co*pitch,8,'weights_padded.bin'),('bias_offset',co*8,8,'bias_int64.bin'),('quant_parameter_offset',co*4,4,'requant_int32.bin')]:
                off=integer(o[key],key);require(off%alignment==0 and off+size<=len(blobs[file]),'Parameter offset/capacity: '+key)
            off=o['weight_padded_offset'];raw=np.frombuffer(blobs['weights_padded.bin'][off:off+co*pitch],np.uint8).reshape(co,pitch)
            active=raw[:,:ci*k*k*(wbits//8)].copy().view(np.int8 if wbits==8 else '<i2')
            require(not np.any(active==-(1<<(wbits-1))),'Weight outside symmetric range')
            require(not np.any(raw[:,ci*k*k*(wbits//8):]),'Packed weight padding must be zero')
            b=bias[o['bias_offset']//8:o['bias_offset']//8+co];m=mult[o['quant_parameter_offset']//4:o['quant_parameter_offset']//4+co]
            require(np.all(m>0),'Positive requant multipliers required')
            sw=np.asarray(o['weight_scale'],np.float64)
            require(sw.shape==(co,) and np.isfinite(sw).all() and np.all(sw>0),'Invalid per-Cout weight scales')
            product32=np.multiply(np.float32(values[sources[0]]),sw.astype(np.float32),dtype=np.float32)
            if kind=='ConvLinear':
                expected_mult=np.rint(values[sources[0]]*sw/oscale*(1<<24))
                expected_mult32=np.rint(np.divide(product32,np.float32(oscale),dtype=np.float32).astype(np.float64)*(1<<24))
            else:
                frac=o['silu_fraction_bits']
                require(frac==(16 if obits==16 else 8),'SiLU fraction precision mismatch')
                expected_mult=np.rint(values[sources[0]]*sw*(1<<frac)*(1<<24))
                expected_mult32=np.rint(product32.astype(np.float64)*(1<<frac)*(1<<24))
                require(o['output_multiplier']==int(np.rint((1<<24)/((1<<frac)*oscale))),'Output requant scale mismatch')
            # Current package deliberately combines preserved float64 baseline
            # scales and new float32 ONNX scales; each entire per-Cout vector must
            # match one exact arithmetic provenance, never a free tolerance.
            require(np.array_equal(m.astype(np.float64),expected_mult) or np.array_equal(m.astype(np.float64),expected_mult32),'Per-Cout requant scale mismatch')
            bound=(((1<<(bits[sources[0]]-1))-1)*((1<<(wbits-1))-1)*ci*k*k+max(abs(int(v)) for v in b))
            require(bound<2**33 and bound*int(m.max())+(1<<23)<2**65,'Accumulator/requant arithmetic bound exceeded')
            require(o['accumulator_bound']>=bound and o['accumulator_width']<=34,'Accumulator contract mismatch')
            if kind=='ConvSiLU':
                om=integer(o['output_multiplier'],'output multiplier',1)
                zbound=(bound*int(m.max())+(1<<23))>>24
                require(max(zbound,524288)*om<2**65,'Activation requant arithmetic could overflow')
            desc['weight_bits']=wbits;desc['kernel']=k
            scratch=max(scratch,co*h*w)
        elif kind in ('Add','Concat'):
            require(len(sources) in ((2,) if kind=='Add' else (2,4)) and all(known[t]==known[sources[0]] for t in sources),'Equal branch shapes required')
            require(shape==(ci if kind=='Add' else ci*len(sources),ih,iw),'Branch output shape mismatch')
            ms=o['multipliers'];require(len(ms)==len(sources),'Branch multipliers missing')
            for m in ms:integer(m,'Q22 branch multiplier',1,(1<<23)-1 if kind=='Add' else 1<<22)
            require(ms==[int(np.rint(values[t]/oscale*(1<<22))) for t in sources],'Q22 branch scale alignment mismatch')
            require(sum(m*((1<<(bits[t]-1))-1) for m,t in zip(ms,sources))<2**65,'Branch arithmetic overflow')
            desc['kernel']=0 if kind=='Add' else 2
        else:
            require(len(sources)==1 and obits==bits[sources[0]] and oscale==values[sources[0]],'Pool/Resize quantization must be preserved')
            require(shape==(ci,ih,iw) if kind=='MaxPool' else shape==(ci,2*ih,2*iw),'Pool/Resize shape mismatch')
            desc.update(kernel=5 if kind=='MaxPool' else 7,stride=1,padding=0)
        known[out]=shape;bits[out]=obits;values[out]=oscale
        require(scales[out]==dict(bits=obits,scale=oscale),'Output quantization metadata mismatch')
        if kind=='Concat' and len(sources)==4:
            temporary=[]
            for j in range(2):
                name='@sppf_pair_%d'%j;temporary.append(name);known[name]=(2*ci,h,w);bits[name]=obits;values[name]=oscale
                descriptors.append(dict(desc,sources=sources[2*j:2*j+2],output_tensor=name,Cout=2*ci,multipliers=desc['multipliers'][2*j:2*j+2],input_bits=desc['input_bits'][2*j:2*j+2]))
            descriptors.append(dict(desc,sources=temporary,Cin=2*ci,input_bits=[obits,obits],multipliers=[1<<22,1<<22]))
        else:descriptors.append(desc)
    require(counts==manifest['counts'] and counts=={'ConvSiLU':57,'ConvLinear':3,'Add':7,'Concat':13,'MaxPool':3,'Resize':2},'Full graph operation count mismatch')
    heads=manifest['head_outputs'];require(len(heads)==3,'Three linear heads required')
    for j,b in enumerate(heads):
        require(dimensions(b['shape'])==known[b['name']]==(21,[48,24,12][j],[48,24,12][j]),'Detect head shape mismatch')
        require(b['bits']==bits[b['name']] and b['scale']==values[b['name']],'Detect head quantization mismatch')
    detect=manifest['detect']
    require(detect['strides']==[8.,16.,32.] and detect['channels_per_anchor']==7 and detect['anchors_per_head']==3 and detect['actual_ONNX_constants'] is True,'Detect decode contract mismatch')
    require(detect['xy']=='(sigmoid(rawxy)*2+grid_xy-0.5)*stride' and detect['wh']=='(sigmoid(rawwh)*2)**2*anchor_pixels' and detect['scores']=='sigmoid(objectness)*sigmoid(class)','Unsupported Detect formulas')
    anchors=np.asarray(detect['anchors_pixels'])
    require(anchors.shape==(3,3,2) and np.isfinite(anchors).all() and np.all(anchors>0),'Invalid Detect anchors')
    require(set(scales)==set(values)-{'@sppf_pair_0','@sppf_pair_1'},'Unexpected/unresolved scale tensors')
    manifest=freeze_metadata(manifest);heads=manifest['head_outputs']
    return dict(manifest=manifest,operations=operations,descriptors=descriptors,blobs=blobs,tensor_shapes=known,tensor_bits=bits,tensor_scales=values,
                inputs={'images':known['images']},input_scales={'images':values['images']},boundaries=heads,scratch_count=scratch)
