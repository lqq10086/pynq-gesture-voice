"""Generate reproducible oracle fixtures with real model geometries, no board calls."""
import hashlib
import json
from pathlib import Path
import numpy as np
from integer_reference import conv_i16_w8

ROOT=Path(__file__).resolve().parents[1]


def generate():
    destination=ROOT/'reports/quant_fixtures';destination.mkdir(parents=True,exist_ok=True)
    geometries=[('stem',3,24,384,3,2,1,1),('dw3',24,24,96,3,2,1,24),
                ('dw5',72,72,24,5,1,2,72),('pw288',288,72,24,1,1,0,1),
                ('ties',1,8,1,1,1,0,1)]
    rows=[]
    for seed,(name,ci,co,ih,kernel,stride,pad,groups) in enumerate(geometries):
        folder=destination/name;folder.mkdir(exist_ok=False)
        rng=np.random.default_rng(seed)
        x=rng.integers(-32768,32768,(1,ci,ih,ih),dtype=np.int16)
        w=rng.integers(-127,128,(co,ci//groups,kernel,kernel),dtype=np.int8)
        bias=rng.integers(-(2**31),2**31,co,dtype=np.int64)
        shift=rng.integers(-18,-11,co,dtype=np.int32)
        if name=='ties':
            x[:]=1;w[:]=1;bias=np.array([-10,-8,-6,-4,-2,0,2,4],np.int64);shift[:]=-1
        output=conv_i16_w8(x,w,bias,shift,stride=stride,pad=pad,groups=groups,relu=name in ('stem','dw5'))
        arrays={'x':x,'w':w,'bias':bias,'shift':shift,'expected':output}
        files={}
        for key,value in arrays.items():
            path=folder/(key+'.bin');data=value.astype(value.dtype.newbyteorder('<')).tobytes();path.write_bytes(data)
            files[path.name]=dict(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),dtype=str(value.dtype),shape=list(value.shape))
        case=dict(name=name,ci=ci,co=co,ih=ih,iw=ih,kernel=kernel,stride=stride,pad=pad,groups=groups,
            relu=name in ('stem','dw5'),oh=output.shape[2],ow=output.shape[3],files=files,
            scope='DETERMINISTIC_SYNTHETIC_VALUES_REAL_MODEL_GEOMETRY_NOT_BOARD_RESULT')
        (folder/'case.json').write_text(json.dumps(case,indent=2)+'\n');rows.append(case)
        print('FIXTURE',name,'elements',output.size,flush=True)
    (destination/'manifest.json').write_text(json.dumps(dict(status='ORACLE_FIXTURES_READY',cases=rows),indent=2)+'\n')


if __name__=='__main__':generate()
