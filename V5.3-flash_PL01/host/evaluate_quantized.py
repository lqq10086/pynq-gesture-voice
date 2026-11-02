"""All 507 frozen control images; CPU integer oracle, never observer input."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np
from calibration_data import DATA,frozen_manifest,read_sample as training_read,preprocess,sha
from fastest_graph import MODEL_SHA,inventory
from integer_graph import IntegerGraph
from run_quantization import session

TOOLS=Path('D:/Desktop/YOLOv6Lite-M/scripts');sys.path.insert(0,str(TOOLS))
from generate_predictions import validation_samples,read_sample
from evaluate_predictions import evaluate_records,write_results,iou,prepare_predictions
sys.path.insert(0,'D:/Desktop/YOLO-FastestV2/scripts')
from fastest_numpy import decode_heads,postprocess,restore_boxes

ROOT=Path(__file__).resolve().parents[1]
BASE=Path('D:/Desktop/YOLO-FastestV2/evaluation/fastest')


def max_f1(records):
    result={}
    for domain in ('overall','FPGA','legacy'):
        selected=records if domain=='overall' else [x for x in records if x['domain']==domain]
        categories={}
        for category in (0,1):
            pairs=[];total=0
            for record in selected:
                gt=[x for x in record['gt'] if x[4]==category];total+=len(gt);used=set()
                preds=sorted((x for x in prepare_predictions(record['pred']) if x[5]==category),key=lambda x:-x[4])
                for pred in preds:
                    match=max(((iou(pred,g),j) for j,g in enumerate(gt) if j not in used),default=(0.,-1))
                    correct=match[0]>=.5
                    if correct:used.add(match[1])
                    pairs.append((float(pred[4]),int(correct)))
            pairs.sort(key=lambda x:-x[0]);tp=fp=0;best=dict(f1=0.,threshold=None,precision=0.,recall=0.)
            for j,(score,correct) in enumerate(pairs):
                tp+=correct;fp+=1-correct
                if j+1<len(pairs) and pairs[j+1][0]==score:continue
                f1=2*tp/(total+tp+fp) if total+tp+fp else 0.
                if f1>best['f1']:best=dict(f1=f1,threshold=score,precision=tp/(tp+fp),recall=tp/total if total else 0.)
            categories[str(category)]=dict(best,gt_instances=total,scope='DATA_DERIVED_MAX_F1_FOR_REPORT_ONLY_FIXED_DEPLOYMENT_THRESHOLDS_UNCHANGED')
        result[domain]=categories
    return result


def parity_training(runner,package,data_root):
    samples=json.loads((package/'calibration.json').read_text())['samples'];covered=set();rows=[]
    _,model,_=inventory();reference=session(model)
    for row in samples:
        image,label=training_read(row,data_root);classes={int(float(x.split()[0])) for x in label.read_text().splitlines() if x.strip()};tags=classes or {'negative'}
        if not tags-covered:continue
        tensor=preprocess(image);actual=runner.run(tensor,floating=True);expected=reference.run(None,{'images':tensor})
        errors=[float(np.abs(a-b).max()) for a,b in zip(actual,expected)]
        rows.append(dict(filename=row['filename'],classes=sorted(classes),image_sha256=row['image_sha256'],head_max_abs=errors))
        if max(errors)>=1e-4:raise ValueError('manual graph FP32 reference differs from frozen ONNX')
        covered.update(tags)
        if covered=={0,1,'negative'}:break
    if covered!={0,1,'negative'}:raise ValueError('training parity coverage incomplete')
    return rows


def run(args):
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    _,samples=validation_samples(args.data_root);package=Path(args.package);runner=IntegerGraph(package,device='cpu',threads=2)
    parity=parity_training(runner,package,args.data_root)
    baseline_meta=json.loads((BASE/'metadata.json').read_text())
    if baseline_meta['runtime']['model_sha256']!=MODEL_SHA or baseline_meta['data_manifest_sha256']!=sha(Path(args.data_root)/'manifest.json') or baseline_meta['predictions_sha256']!=sha(BASE/'predictions.jsonl'):raise ValueError('baseline evidence identity differs')
    baseline=[json.loads(x) for x in (BASE/'predictions.jsonl').read_text().splitlines() if x.strip()]
    baseline_by_id={x['image']:x for x in baseline};expected=[x['partition']+'/'+x['filename'] for x in samples]
    if set(baseline_by_id)!=set(expected):raise ValueError('baseline 507-image coverage differs')
    records=[];stats=defaultdict(lambda:dict(saturation_count=0,elements=0,minimum=32767,maximum=-32768));begun=time.perf_counter()
    golden={'2402.png','2493.png','2410.png'}
    with (output/'predictions.jsonl').open('x',encoding='utf-8') as stream:
        for index,sample in enumerate(samples):
            image,gt=read_sample(args.data_root,sample);tensor=preprocess(image)
            heads,values=runner.run(tensor,capture=True) if sample['filename'] in golden else (runner.run(tensor),None)
            decoded=decode_heads(heads)
            pred=restore_boxes(postprocess(decoded[0],confidence=.001,iou=.45,max_det=300),image.shape)
            if len(pred):pred=pred[(pred[:,2]>pred[:,0])&(pred[:,3]>pred[:,1])]
            identifier=sample['partition']+'/'+sample['filename']
            if gt!=baseline_by_id[identifier]['gt']:raise ValueError('baseline GT differs from current frozen label')
            record=dict(image=identifier,domain=sample['domain'],sample_kind=sample['sample_kind'],width=image.shape[1],height=image.shape[0],
                gt=gt,pred=pred.tolist(),partition=sample['partition'],session=sample.get('session'),negative_confirmed=sample.get('negative_confirmed'),
                image_sha256=sample['image_sha256'],label_sha256=sample['label_sha256'])
            stream.write(json.dumps(record)+'\n');stream.flush();records.append(record)
            for op in runner.last_telemetry:
                row=stats[op['index']];row['saturation_count']+=op['saturation_count'];row['elements']+=op['elements'];row['minimum']=min(row['minimum'],op['minimum']);row['maximum']=max(row['maximum'],op['maximum'])
            if values is not None:
                folder=output/'golden'/Path(sample['filename']).stem;folder.mkdir(parents=True)
                np.savez_compressed(folder/'integer_tensors.npz',images=np.rint(np.ldexp(tensor,-runner.graph['tensor_exponents']['images'])).astype(np.int16),**values)
                np.savez(folder/'probability_heads.npz',**dict(zip(runner.graph['output_names'],heads)))
                (folder/'source.json').write_text(json.dumps(record,indent=2)+'\n')
            if (index+1)%25==0:print(json.dumps(dict(stage='INTEGER_FULL_GRAPH_CONTROL_EVALUATION',done=index+1,total=len(samples),elapsed_s=time.perf_counter()-begun)),flush=True)
    (output/'expected_images.json').write_text(json.dumps(expected,indent=2)+'\n')
    metrics=evaluate_records(records,expected);write_results(metrics,output/'metrics')
    fp32=evaluate_records(baseline,expected)
    maximum=dict(integer=max_f1(records),fp32=max_f1(baseline))
    comparison={}
    for domain in ('FPGA','legacy','overall'):
        a=metrics['scopes'][domain];b=fp32['scopes'][domain]
        comparison[domain]=dict(images=a['images'],fp32_ap50=b['coco']['mean_ap50'],integer_ap50=a['coco']['mean_ap50'],
            ap50_drop=b['coco']['mean_ap50']-a['coco']['mean_ap50'],per_class={},
            fp32_negative_images_with_fp=b['fixed_threshold']['negative_images_with_fp'],integer_negative_images_with_fp=a['fixed_threshold']['negative_images_with_fp'],
            negative_images=a['fixed_threshold']['negative_images'])
        for category in ('0','1'):
            f=b['fixed_threshold']['per_class'][category];q=a['fixed_threshold']['per_class'][category]
            comparison[domain]['per_class'][category]=dict(fp32=f,integer=q,recall_drop=f['recall']-q['recall'],
                fp32_ap50=b['coco']['per_class'][category]['ap50'],integer_ap50=a['coco']['per_class'][category]['ap50'])
    passed=comparison['FPGA']['ap50_drop']<=.01 and all(x['recall_drop']<=.02 for x in comparison['FPGA']['per_class'].values())
    transitions=[]
    for q,b in zip(metrics['scopes']['overall']['fixed_threshold']['image_counts'],fp32['scopes']['overall']['fixed_threshold']['image_counts']):
        if any(q[k]!=b[k] for k in ('tp','fp','fn')):transitions.append(dict(image=q['image'],domain=q['domain'],sample_kind=q['sample_kind'],fp32={k:b[k] for k in ('tp','fp','fn')},integer={k:q[k] for k in ('tp','fp','fn')}))
    summary=dict(status='OFFLINE_ACCURACY_GATE_PASS' if passed else 'OFFLINE_ACCURACY_GATE_FAIL',board_executed=False,
        package=str(package.resolve()),package_manifest_sha256=sha(package/'manifest.json'),model_sha256=MODEL_SHA,
        data_manifest_sha256=sha(Path(args.data_root)/'manifest.json'),images=507,observer_validation_overlap=0,
        fp32_manual_graph_parity=parity,comparisons=comparison,max_f1=maximum,
        source_sha256={p.relative_to(ROOT).as_posix():sha(p) for p in sorted((ROOT/'host').glob('*.py'))}|{'INTEGER_CONTRACT.md':sha(ROOT/'INTEGER_CONTRACT.md')},
        gate=dict(fpga_ap50_maximum_drop=.01,fpga_per_class_fixed_recall_maximum_drop=.02,thresholds=[.60,.45],nms_iou=.45),
        changed_image_counts=transitions,elapsed_s=time.perf_counter()-begun,
        execution='CPU float64 convolution with proven integer partial-sum bound <2^53; int64 RNE/saturation; not board timing',
        limitations=['Existing model-selection/control validation, no untouched new test claim','Max-F1 thresholds are descriptive only; deployment thresholds remain .60/.45','Negative review inherited from SHA-bound earlier review, not a new manual audit'])
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (output/'saturation.json').write_text(json.dumps(dict(endpoint_counts_not_proven_clipping_fraction=True,layers=dict(stats)),indent=2)+'\n')
    print(json.dumps(dict(status=summary['status'],images=507,FPGA=comparison['FPGA'],elapsed_s=summary['elapsed_s'])),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--data-root',type=Path,default=DATA);run(p.parse_args())
