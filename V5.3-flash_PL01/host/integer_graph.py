"""Exact bounded-integer graph using float64 convolution as a host accelerator.

Every product/partial sum is an integer with sum-of-absolute-values <2^53;
IEEE float64 therefore represents it exactly. RNE shifts remain int64 operations.
This is a host oracle, not PL execution or a latency measurement.
"""
import hashlib
import json
from pathlib import Path
import numpy as np
import torch
import torch.nn.functional as F
from integer_reference import quantize
from fastest_graph import inventory


def torch_requantize(acc,shifts,relu=False):
    result=torch.empty_like(acc,dtype=torch.int64)
    for shift in np.unique(shifts):
        channels=np.flatnonzero(shifts==shift).tolist();v=acc[:,channels];s=int(shift)
        if s<0:
            k=-s
            if k>=63:q=torch.zeros_like(v)
            else:
                a=v.abs();base=a>>k;remainder=a&((1<<k)-1);half=1<<(k-1)
                q=base+((remainder>half)|((remainder==half)&((base&1)!=0))).to(torch.int64)
                q=torch.where(v<0,-q,q)
        elif s>0:
            q=torch.where(v>0,32767,torch.where(v<0,-32768,0)) if s>=16 else v.clamp(-32768,32767)<<s
        else:q=v
        result[:,channels]=q.clamp(0 if relu else -32768,32767)
    return result.to(torch.int16)


def conv_exact_accelerated(x,w,bias,shift,*,stride,pad,groups,relu,device='cpu'):
    bound=max(32768*int(z)+abs(int(b)) for z,b in zip(np.abs(w.astype(np.int64)).sum(axis=(1,2,3)),bias))
    if bound>=2**53:raise OverflowError('float64 cannot prove exact integer accumulation')
    with torch.inference_mode():
        tx=torch.as_tensor(x,device=device).to(torch.float64);tw=torch.as_tensor(w,device=device).to(torch.float64)
        tb=torch.as_tensor(bias,device=device).to(torch.float64)
        acc=F.conv2d(tx,tw,tb,stride=stride,padding=pad,groups=groups)
        if not torch.equal(acc,acc.round()):raise ArithmeticError('noninteger accelerated accumulation')
        return torch_requantize(acc.to(torch.int64),np.asarray(shift),relu).cpu().numpy()


class IntegerGraph:
    def __init__(self,package,device='cpu',threads=2):
        self.package=Path(package);manifest=json.loads((self.package/'manifest.json').read_text())
        for filename,record in manifest['files'].items():
            if hashlib.sha256((self.package/filename).read_bytes()).hexdigest()!=record['sha256']:raise ValueError('quant package SHA mismatch: '+filename)
        self.graph=json.loads((self.package/'graph.json').read_text());self.parameters=dict(np.load(self.package/'parameters.npz'))
        self.device=device;torch.set_num_threads(threads);self.threads=threads
        self.weights={key:torch.as_tensor(value,device=device).to(torch.float64) for key,value in self.parameters.items() if key.startswith('w') and not key.endswith('exponent')}
        self.bias={op['index']:torch.as_tensor(self.parameters[op['bias_key']],device=device).to(torch.float64) for op in self.graph['convolutions']}
        _,_,original=inventory()
        self.float_parameters={k:torch.as_tensor(v.copy(),device=device).to(torch.float32) for k,v in original.items()}
        self.last_telemetry=[]

    def run(self,input_fp32,*,capture=False,floating=False):
        exponents=self.graph['tensor_exponents'];qinput=quantize(input_fp32,exponents['images'])
        tensors={'images':torch.as_tensor(input_fp32 if floating else qinput,device=self.device)};telemetry=[];captured={}
        with torch.inference_mode():
            for node in self.graph['nodes']:
                kind=node['kind'];names=node['inputs'];out=node['outputs'][0];attrs=node['attrs']
                args=[tensors[x] for x in (names[:1] if kind=='Conv' else names) if x]
                if kind=='Constant':value=torch.as_tensor(np.asarray(attrs['value'],dtype=attrs['dtype']),device=self.device)
                elif kind=='Conv':
                    op=self.graph['convolutions'][node['conv_index']]
                    if floating:
                        value=F.conv2d(args[0],self.float_parameters[op['weight']],self.float_parameters[op['bias']],stride=op['stride'],padding=op['pad'],groups=op['groups'])
                        if op['relu']:value=value.clamp_min(0)
                        tensors[out]=value
                        continue
                    if op['accumulator_bound']>=2**53:raise OverflowError('unproved exact float64 accumulator')
                    acc=F.conv2d(args[0].to(torch.float64),self.weights[op['weight_key']],self.bias[op['index']],stride=op['stride'],padding=op['pad'],groups=op['groups'])
                    if capture and not torch.equal(acc,acc.round()):raise ArithmeticError('noninteger accelerated accumulation')
                    value=torch_requantize(acc.to(torch.int64),self.parameters[op['shift_key']],op['relu'])
                    telemetry.append(dict(index=op['index'],tensor=op['quant_output'],minimum=int(value.min()),maximum=int(value.max()),
                        saturation_count=int(((value==-32768)|(value==32767)).sum()),elements=value.numel()))
                    if capture:captured['conv%03d'%op['index']]=value.cpu().numpy()
                elif kind=='Relu':value=args[0].clamp_min(0)
                elif kind=='Concat':
                    if not floating and node.get('align_shifts') is not None:
                        aligned=[]
                        for tensor,shift in zip(args,node['align_shifts']):
                            aligned.append(torch_requantize(tensor.to(torch.int64),np.full(tensor.shape[1],shift,np.int32)))
                        value=torch.cat(aligned,dim=attrs['axis'])
                    else:value=torch.cat(args,dim=attrs['axis'])
                elif kind=='Reshape':
                    shape=[int(x) for x in args[1].cpu().numpy().ravel()]
                    shape=[args[0].shape[i] if x==0 else x for i,x in enumerate(shape)]
                    value=args[0].reshape(shape)
                elif kind=='Transpose':value=args[0].permute(attrs['perm'])
                elif kind=='Gather':
                    index=args[1].to(torch.int64);axis=attrs.get('axis',0)
                    if index.ndim==0:value=args[0].select(axis,int(index))
                    else:value=torch.index_select(args[0],axis,index.ravel())
                elif kind=='MaxPool':value=F.max_pool2d(args[0].to(torch.float64),attrs['kernel_shape'],attrs['strides'],attrs['pads'][0]).to(args[0].dtype)
                elif kind=='Resize':
                    if attrs['mode']!='nearest' or attrs['coordinate_transformation_mode']!='asymmetric' or attrs['nearest_mode']!='floor':raise ValueError('unimplemented Resize semantics')
                    scales=args[-1].cpu().numpy()
                    if not np.array_equal(scales,[1,1,2,2]):raise ValueError('expected nearest2 Resize')
                    value=args[0].repeat_interleave(2,dim=2).repeat_interleave(2,dim=3)
                elif kind in ('Sigmoid','Softmax'):
                    source=args[0].to(torch.float32);exponent=exponents[names[0]]
                    if exponent is not None and not floating:source=source*float(2.**exponent)
                    value=torch.sigmoid(source) if kind=='Sigmoid' else torch.softmax(source,dim=attrs['axis'])
                else:raise ValueError('unimplemented op '+kind)
                tensors[out]=value
            outputs=[tensors[x].to(torch.float32).cpu().numpy() for x in self.graph['output_names']]
        self.last_telemetry=telemetry
        return (outputs,captured) if capture else outputs
