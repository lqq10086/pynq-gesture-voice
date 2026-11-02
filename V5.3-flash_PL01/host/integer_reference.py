"""W8/A16 integer oracle for INTEGER_CONTRACT.md; no old-model parameters."""
import math
import numpy as np


def exponent_for(maximum,bits):
    maximum=float(maximum)
    if not math.isfinite(maximum) or maximum<0:raise ValueError('finite nonnegative maximum required')
    return math.ceil(math.log2(maximum/((1<<(bits-1))-1))) if maximum else 0


def quantize(value,exponent,bits=16):
    value=np.asarray(value,np.float64)
    if not np.isfinite(value).all():raise ValueError('nonfinite quantization input')
    return np.clip(np.rint(np.ldexp(value,-int(exponent))),-(1<<(bits-1)),(1<<(bits-1))-1).astype(np.int16 if bits==16 else np.int8)


def requantize(values,shift,relu=False):
    values=np.asarray(values,np.int64)
    shifts=np.asarray(shift,np.int64)
    if shifts.ndim==1 and values.ndim>=2:
        shifts=shifts.reshape((1,-1)+(1,)*(values.ndim-2))
    shifts=np.broadcast_to(shifts,values.shape)
    result=np.empty_like(values)
    for s in np.unique(shifts):
        mask=shifts==s;v=values[mask];s=int(s)
        if s<0:
            k=-s
            if k>=64:q=np.zeros_like(v)
            else:
                negative=v<0
                a=np.where(negative,-(v+1),v).astype(np.uint64)+negative.astype(np.uint64)
                base=a>>np.uint64(k);remainder=a&np.uint64((1<<k)-1);half=np.uint64(1<<(k-1))
                q=(base+((remainder>half)|((remainder==half)&((base&1)!=0)))).astype(np.int64)
                q=np.where(negative,-q,q)
        elif s>0:
            if s>=16:q=np.where(v>0,32767,np.where(v<0,-32768,0))
            else:q=np.clip(v,-32768,32767)<<s
        else:q=v
        result[mask]=np.clip(q,0 if relu else -32768,32767)
    return result.astype(np.int16)


def conv_i16_w8(x,w,bias,shift,*,stride,pad,groups,relu):
    """Exact NCHW convolution; OIHW weights, including grouped/depthwise."""
    x=np.asarray(x);w=np.asarray(w);bias=np.asarray(bias,np.int64)
    if x.dtype!=np.int16 or w.dtype!=np.int8 or x.ndim!=4 or w.ndim!=4:raise ValueError('int16 NCHW and int8 OIHW required')
    n,ci,h,width=x.shape;co,cg,kh,kw=w.shape
    if ci%groups or co%groups or cg!=ci//groups:raise ValueError('incompatible convolution groups')
    if bias.shape!=(co,):raise ValueError('bias shape differs')
    maximum=int(np.abs(x.astype(np.int64)).max(initial=0))
    bounds=[maximum*int(z)+abs(int(b)) for z,b in zip(np.abs(w.astype(np.int64)).sum(axis=(1,2,3)),bias)]
    if max(bounds,default=0)>=(1<<62):raise OverflowError('integer accumulation bound exceeded')
    padded=np.pad(x.astype(np.int64),((0,0),(0,0),(pad,pad),(pad,pad)))
    patches=np.lib.stride_tricks.sliding_window_view(padded,(kh,kw),axis=(2,3))[:,:,::stride,::stride]
    outputs=[];per=co//groups
    for group in range(groups):
        a=patches[:,group*cg:(group+1)*cg]
        b=w[group*per:(group+1)*per].astype(np.int64)
        outputs.append(np.einsum('nchwkl,ockl->nohw',a,b,optimize=True))
    acc=np.concatenate(outputs,axis=1)+bias[None,:,None,None]
    return requantize(acc,shift,relu=relu)
