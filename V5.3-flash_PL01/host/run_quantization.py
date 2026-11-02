"""CPU observer only: frozen train -> fresh W8pc/A16 binary-power package."""
import argparse
import copy
import json
from pathlib import Path
import time
import cv2
import numpy as np
import onnx
from onnx import helper
import onnxruntime as ort
from calibration_data import DATA,frozen_manifest,select_training,read_sample,preprocess,selection_manifest,sha
from fastest_graph import inventory,compile_package,write_package,MODEL

ROOT=Path(__file__).resolve().parents[1]


def session(model):
    options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
    return ort.InferenceSession(model.SerializeToString(),sess_options=options,providers=['CPUExecutionProvider'])


def run(args):
    graph,model,init=inventory(args.model)
    records=select_training(frozen_manifest(args.data_root)['records'],args.count)
    metadata=selection_manifest(records,args.data_root)
    names=list(dict.fromkeys(['images']+[x['quant_output'] for x in graph['convolutions']]))
    augmented=copy.deepcopy(model);del augmented.graph.output[:]
    augmented.graph.output.extend(helper.make_tensor_value_info(n,onnx.TensorProto.FLOAT,None) for n in names+graph['output_names'])
    observer=session(augmented);original=session(model)
    maxima={name:0. for name in names};parity=[];started=time.monotonic()
    # Three training probes only: both class presences and one reviewed negative.
    probe_kinds=set()
    for index,record in enumerate(records):
        image,label=read_sample(record,args.data_root);tensor=preprocess(image)
        arrays=observer.run(None,{'images':tensor})
        for name,value in zip(names,arrays):
            if not np.isfinite(value).all():raise ValueError('nonfinite observer value: '+name)
            maxima[name]=max(maxima[name],float(np.abs(value).max()))
        classes={int(float(row.split()[0])) for row in label.read_text().splitlines() if row.strip()}
        tags=classes or {'negative'}
        needed=tags-probe_kinds
        if needed:
            expected=original.run(graph['output_names'],{'images':tensor})
            errors=[float(np.abs(a-b).max()) for a,b in zip(expected,arrays[len(names):])]
            if max(errors)>1e-6:raise ValueError('augmented observer changes original FP32 heads')
            parity.append(dict(filename=record['filename'],image_sha256=record['image_sha256'],classes=sorted(classes),head_max_abs=errors))
            probe_kinds.update(tags)
        if (index+1)%10==0:
            print(json.dumps(dict(stage='TRAIN_ONLY_OBSERVER',done=index+1,total=len(records),elapsed_s=time.monotonic()-started)),flush=True)
    if probe_kinds!={0,1,'negative'}:raise ValueError('missing training parity class/negative coverage')
    compiled,params=compile_package(graph,init,maxima)
    metadata.update(status='CALIBRATION_COMPLETE',maximum=maxima,fp32_observer_parity=parity,
        elapsed_s=time.monotonic()-started,onnxruntime=ort.__version__,opencv=cv2.__version__,
        observer_device='CPUExecutionProvider two threads',validation_observed=0,pl_executed=False)
    write_package(args.output,compiled,params,metadata)
    report=ROOT/'reports';report.mkdir(exist_ok=True)
    (report/('quant_calibration_%d.json'%len(records))).write_text(json.dumps(dict(status='PASS',package=str(Path(args.output).resolve()),
        package_manifest_sha256=sha(Path(args.output)/'manifest.json'),count=len(records),validation_observed=0,
        conv_count=79,unique_weights=76,maximum_accumulator_bits=max(x['accumulator_required_bits'] for x in compiled['convolutions']),
        fp32_observer_parity=parity,elapsed_s=metadata['elapsed_s']),indent=2)+'\n')
    print(json.dumps(dict(status='PASS',package=str(args.output),count=len(records),elapsed_s=metadata['elapsed_s'])),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',type=Path,default=MODEL)
    p.add_argument('--data-root',type=Path,default=DATA);p.add_argument('--count',type=int,default=100,help='0 = all unique training records')
    p.add_argument('--output',type=Path,required=True);run(p.parse_args())
