"""Deterministic training-only selection, with hash-bound frozen inputs."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np

DATA=Path('D:/Desktop/YOLOv6Lite-M/data_snapshot')
MANIFEST_SHA='c4259e2b6196c0459604d7ee9ebff4d7f2a3cde50f59b50bd101e5e0a75892c7'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def select_training(records,count=100):
    excluded={x['image_sha256'] for x in records if x['partition']!='train'}
    groups={};seen=set()
    for row in sorted(records,key=lambda x:(x['image_sha256'],x['filename'])):
        digest=row['image_sha256']
        if row['partition']!='train' or digest in excluded or digest in seen:continue
        seen.add(digest);groups.setdefault((row['domain'],row['sample_kind']),[]).append(row)
    size=sum(map(len,groups.values()))
    if count==0:count=size
    if count>size or count<1:raise ValueError('calibration count exceeds unique disjoint training data')
    quota={k:count*len(v)//size for k,v in groups.items()}
    remainder=sorted(groups,key=lambda k:(-(count*len(groups[k])%size),k))
    for k in remainder[:count-sum(quota.values())]:quota[k]+=1
    return [x for k in sorted(groups) for x in groups[k][:quota[k]]]


def frozen_manifest(root=DATA):
    root=Path(root)
    if sha(root/'manifest.json')!=MANIFEST_SHA:raise ValueError('frozen data manifest changed')
    return json.loads((root/'manifest.json').read_text(encoding='utf-8-sig'))


def read_sample(record,root=DATA):
    root=Path(root);relative=Path(record['filename'])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe relative image')
    image=root/'images'/record['partition']/relative
    label=(root/'labels'/record['partition']/relative).with_suffix('.txt')
    if sha(image)!=record['image_sha256'] or sha(label)!=record['label_sha256']:raise ValueError('frozen source SHA differs: '+str(image))
    value=cv2.imdecode(np.fromfile(image,np.uint8),cv2.IMREAD_COLOR)
    if value is None:raise ValueError('image decode failed')
    return value,label


def preprocess(image):
    resized=cv2.resize(image,(384,384),interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(resized.transpose(2,0,1),dtype=np.float32)[None]/np.float32(255.)


def selection_manifest(records,root=DATA):
    return dict(data_manifest_sha256=MANIFEST_SHA,source='frozen train only',count=len(records),
        strata=dict(Counter(x['domain']+'/'+x['sample_kind'] for x in records)),
        preprocessing='stored BGR, no extra mirror/CCM/augmentation, INTER_LINEAR stretch384, NCHW FP32 /255',
        samples=[dict(x,image=str(Path(root)/'images'/x['partition']/x['filename'])) for x in records])
