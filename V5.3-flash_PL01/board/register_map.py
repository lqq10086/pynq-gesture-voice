"""Extract the new IP's AXI-Lite ABI from its actual generated Verilog."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
CONTROL_SOURCE=ROOT/'hls/build_csim/solution1/syn/verilog/fastest_accel_control_s_axi.v'
ARGUMENTS=('input_r','weights','bias','shift','output_r','channel_offsets','channel_shifts',
           'op','ci','co','ih','iw','kernel','stride','pad','groups','relu','use_channel_offsets','input_span_elements')
CANONICAL_KEYS=('control_offset','return_offset','start_mask','done_mask','idle_mask','auto_restart_mask','arguments','argument_order')


def map_digest(value):
    return hashlib.sha256(json.dumps({key:value[key] for key in CANONICAL_KEYS},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parse_registers(path=CONTROL_SOURCE):
    path=Path(path);data=path.read_bytes();text=data.decode('utf-8')
    addresses={name:int(value,16) for name,value in re.findall(r'\b(ADDR_[A-Z0-9_]+)\s*=\s*\d+\x27h([0-9a-fA-F]+)',text)}
    ports={name:int(high)+1 for high,name in re.findall(r'output\s+wire\s+\[(\d+):0\]\s+(\w+)',text)}
    arguments={}
    for name in ARGUMENTS:
        bits=ports.get(name);stem='ADDR_'+name.upper()+'_DATA_'
        if bits not in (32,64) or stem+'0' not in addresses:raise ValueError('unrecognized generated argument '+name)
        high=addresses.get(stem+'1')
        if (bits==64)!=(high is not None):raise ValueError('port/register width disagreement: '+name)
        arguments[name]=dict(width_bits=bits,low_offset=addresses[stem+'0'],high_offset=high,
            role='pointer' if name in ARGUMENTS[:7] else 'scalar')
    masks={}
    for label in ('ap_start','ap_done','ap_idle','auto_restart'):
        found=re.search(r'//\s+bit\s+(\d+)\s+-\s+'+label+r'\b',text)
        if not found:raise ValueError('control bit not found: '+label)
        masks[label]=1<<int(found.group(1))
    canonical=dict(control_offset=addresses['ADDR_AP_CTRL'],return_offset=addresses['ADDR_AP_RETURN_0'],
        start_mask=masks['ap_start'],done_mask=masks['ap_done'],idle_mask=masks['ap_idle'],auto_restart_mask=masks['auto_restart'],
        arguments=arguments,argument_order=list(ARGUMENTS))
    canonical['map_sha256']=map_digest(canonical)
    canonical['source_sha256']=hashlib.sha256(data).hexdigest();canonical['source_file']=str(path.resolve())
    canonical['minimum_mmio_bytes']=max([canonical['control_offset'],canonical['return_offset']]+[v for x in arguments.values() for v in (x['low_offset'],x['high_offset']) if v is not None])+4
    return canonical


def assert_compatible(expected,qualified):
    if expected.get('map_sha256')!=map_digest(expected):raise ValueError('register map payload/fingerprint mismatch')
    for key in ('map_sha256','source_sha256'):
        if not expected.get(key) or expected[key]!=qualified.get(key):raise ValueError('loaded hardware register qualification differs: '+key)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,default=CONTROL_SOURCE);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    result=parse_registers(args.source);args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(map_sha256=result['map_sha256'],source_sha256=result['source_sha256'],pointer_widths={x:result['arguments'][x]['width_bits'] for x in ARGUMENTS[:7]})))
