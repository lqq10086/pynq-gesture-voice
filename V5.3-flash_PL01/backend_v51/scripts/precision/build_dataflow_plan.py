"""Capacity-checked estimates for the actual 384 package; not measured cycles."""
import json,math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def ceil(a,b):return (a+b-1)//b
def analyze(c,bits,outbits,flow):
    ci,co,h,k,s,p=c['Cin'],c['Cout'],c['H'],c['kernel'],c['stride'],c['padding'];ih=c['input_shape'][2]
    rows=h if flow=='output_channel_group_weight_stationary' and h<=7 else (2 if s==2 else 4)
    ng=ceil(co,16);nt=ceil(h,rows);ep=64//bits;input_words=0;peak=0;burst=[]
    for ty in range(0,h,rows):
        r=min(rows,h-ty);begin=max(0,ty*s-p);end=min(ih,(ty+r-1)*s+k-p)
        pitch=ceil((end-begin)*ih+ep-1,ep)*ep;peak=max(peak,ci*pitch*2)
        for ch in range(ci):
            phase=(ch*ih*ih+begin*ih)%ep;n=ceil(phase+(end-begin)*ih,ep);input_words+=n;burst.append(min(n,64))
    if flow=='output_channel_group_weight_stationary':input_words*=ng if nt>1 else 1;weight_repeats=1
    else:weight_repeats=nt if ng>1 else 1
    weight_once=co*ceil(ci*k*k,8)*8;req_input=input_words*8;req_weight=weight_once*weight_repeats
    # Fixed spatial4 core masks the final four-position group at each row.
    groups=h*ceil(h,4)*ng;mac=groups*ci*k*k;activation=h*ceil(h,4)*co*4
    oep=64//outbits;logical_output=co*h*h*outbits//8
    output=sum((ceil(ch*h*h+min(h,ty+rows)*h,oep)-(ch*h*h+ty*h)//oep)*8 for ch in range(co) for ty in range(0,h,rows))
    preserve=sum((int((ch*h*h+ty*h)%oep!=0)+int((ch*h*h+min(h,ty+rows)*h)%oep!=0))*8 for ch in range(co) for ty in range(0,h,rows))
    return dict(rows_per_tile=rows,tiles=nt,output_groups=ng,pixel_peak_bytes=peak,weight_group_logical_bytes=min(16,co)*ci*k*k,weight_cache_allocated_bytes=18432,weight_repeats=weight_repeats,input_request_bytes=req_input,weight_request_bytes=req_weight,logical_output_bytes=logical_output,output_request_bytes=output,preserve_read_bytes=preserve,total_request_bytes=req_input+req_weight+output+preserve+co*12,maximum_input_burst_words=max(burst),median_input_burst_words=sorted(burst)[len(burst)//2],estimated_mac_cycles_II1=mac,estimated_activation_items=activation,estimated_cycles_lower_bound=mac+activation+ceil(req_input+req_weight+output+preserve,8),schedule_status='ESTIMATE_NOT_HLS_OR_BOARD_MEASUREMENT')

def main():
    cfg=json.loads((ROOT/'model_package/layer_config.json').read_text())
    manifest=json.loads((ROOT/'model_package/model_manifest.json').read_text())
    layers=[]
    for o in cfg:
        if o['op_type'] not in ('ConvSiLU','ConvLinear'):continue
        c=dict(o,input_shape=o['input_shapes'][0]);bits=o['input_bits'][0]
        choices={x:analyze(c,bits,o['activation_bits'],x) for x in ['spatial_tile_input_reuse','output_channel_group_weight_stationary']}
        for choice in choices.values():
            if choice['pixel_peak_bytes']>57344 or choice['rows_per_tile']*c['H']>448:raise ValueError('Unsafe tile '+o['name'])
        selected=min(choices,key=lambda name:choices[name]['total_request_bytes'])
        layers.append(dict(node=o['name'],input_shape=o['input_shapes'][0],output_shape=o['output_shape'],input_bits=bits,selected=selected,dataflow_mode=int(selected=='output_channel_group_weight_stationary'),reason='Lower estimated input/weight/preserve traffic under unchanged bounded tile policy',macs=o['Cin']*o['Cout']*o['H']*o['W']*o['kernel']**2,comparisons=choices))
    result=dict(architecture_id=manifest['architecture_id'],source_onnx_sha256=manifest['model_sha256'],hardware_abi=manifest['hardware_abi'],convolution_count=len(layers),macs=sum(x['macs'] for x in layers),preferred_parallelism='16 output channels x 4 adjacent spatial outputs',clock_mhz=100,one_core=True,allocated=dict(weight_bytes=18432,pixels_bytes=57344,output_tile_bytes=14336),schedule_status='ESTIMATE_NOT_HLS_OR_BOARD_MEASUREMENT',layers=layers)
    assert len(layers)==60
    (ROOT/'layer_dataflow_plan.json').write_text(json.dumps(result,indent=2));print('384 layer plan PASS',len(layers),'convolutions',result['macs'],'MACs',sum(x['dataflow_mode'] for x in layers),'mode1')
if __name__=='__main__':main()
