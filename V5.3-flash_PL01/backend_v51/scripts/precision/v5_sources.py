"""Deterministic V5 training-only selection and three disjoint engineering scenes."""
from collections import Counter
from pathlib import Path
from build_precision import ROOT, read, write, sha, graph, onnx, np, cv2

SOURCE = ROOT.parents[1] / 'AI/V5 model/gesture_yolov5n_384input_2output/gesture_yolov5n_384input_2output'
EXPECTED_MODEL = '4cdad59edc6b4f9c0e93e61a488c53b65127347fbe0c34e1465e28c89bb989f4'
REPORT = ROOT / 'reports/v5_model_update/gesture_numeric'
DATA = ROOT / 'reports/v5_model_update/gesture_data'


def select_calibration(records, excluded_hashes, count=100):
    """Proportional domain/kind strata, largest remainder, then SHA256 order."""
    groups, seen = {}, set()
    for row in sorted(records, key=lambda r: (r['image_sha256'], r['image'])):
        digest = row['image_sha256']
        if row['snapshot_role'] != 'train' or digest in excluded_hashes or digest in seen:
            continue
        seen.add(digest)
        groups.setdefault((row['domain'], row['sample_kind']), []).append(row)
    size = sum(map(len, groups.values()))
    if size < count:
        raise ValueError('Not enough unique disjoint training images')
    quotas = {key: count * len(rows) // size for key, rows in groups.items()}
    order = sorted(groups, key=lambda k: (-(count * len(groups[k]) % size), k))
    for key in order[:count - sum(quotas.values())]:
        quotas[key] += 1
    return [row for key in sorted(groups) for row in groups[key][:quotas[key]]]


def image(path):
    result = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    if result is None:
        raise ValueError('Unreadable image: ' + str(path))
    return result


def prepare():
    model_path = SOURCE / 'weights/gesture_best.onnx'
    if sha(model_path) != EXPECTED_MODEL:
        raise ValueError('Exact supplied V5 ONNX required')
    model, shapes, init, ops = graph(model_path)
    onnx.checker.check_model(model, full_check=True)
    heads = [o for o in ops if o['op_type'] == 'ConvLinear']
    if shapes['images'] != [1, 3, 384, 384] or shapes['output0'] != [1, 9072, 7]:
        raise ValueError('V5 input/output contract differs')
    if [shapes[o['output_tensor']] for o in heads] != [[1, 21, n, n] for n in (48, 24, 12)]:
        raise ValueError('V5 raw-head contract differs')
    split = read(SOURCE / 'split_manifest.json')
    verification = read(SOURCE / 'export_verification.json')
    validation = []
    for row in verification['checks']:
        if row.get('input') != 'real_validation_image':
            continue
        name = row['image'].replace('\\', '/').split('/')[-1]
        path = SOURCE / 'dataset/images/val' / name
        if sha(path) != row['image_sha256']:
            raise ValueError('Engineering source identity changed: ' + name)
        validation.append(dict(name=Path(name).stem + '_' + row['bucket'], file=str(path),
            sha256=sha(path), role='CALIBRATION_DISJOINT_V5_VALIDATION_ENGINEERING_SAMPLE',
            bucket=row['bucket'], preprocessing='stored source orientation; no additional mirror'))
    if len(validation) != 3 or len({s['sha256'] for s in validation}) != 3:
        raise ValueError('Expected three supplied real validation probes')
    samples = []
    for row in select_calibration(split['records'], {s['sha256'] for s in validation}):
        path = SOURCE / 'dataset/images/train' / row['image']
        label = SOURCE / 'dataset/labels/train' / Path(row['image']).with_suffix('.txt')
        if sha(path) != row['image_sha256'] or sha(label) != row['snapshot_label_sha256']:
            raise ValueError('Calibration source/label hash mismatch: ' + row['image'])
        samples.append(dict(file=str(path), sha256=sha(path), split='V5_SNAPSHOT_TRAIN',
            domain=row['domain'], sample_kind=row['sample_kind'], classes=row['converted_class_ids'],
            label_file=str(label), label_sha256=sha(label)))
    # One derived case, explicitly not a fourth independent scene.
    pinch = next(s for s in validation if s['bucket'] == 'pinch_point')
    src = image(pinch['file'])
    factor = min(960 / src.shape[1], 540 / src.shape[0])
    resized = cv2.resize(src, (round(src.shape[1] * factor), round(src.shape[0] * factor)), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((720, 1280, 3), 114, np.uint8)
    y, x = (720 - resized.shape[0]) // 2, (1280 - resized.shape[1]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    DATA.mkdir(parents=True, exist_ok=True)
    derived = DATA / 'golden_pinch_720p.png'
    ok, encoded = cv2.imencode('.png', canvas)
    if not ok:
        raise ValueError('PNG encode failed')
    encoded.tofile(derived)
    validation.append(dict(name='golden_pinch_720p', file=str(derived), sha256=sha(derived),
        role='DERIVED_FROM_2459_PINCH_NOT_AN_INDEPENDENT_FOURTH_SCENE',
        derived_from_sha256=pinch['sha256'], placement=dict(x=x,y=y,width=resized.shape[1],height=resized.shape[0],background=114)))
    if {s['sha256'] for s in samples} & {s['sha256'] for s in validation}:
        raise ValueError('Calibration/validation overlap')
    write(DATA / 'calibration_manifest.json', dict(samples=samples, model_sha256=EXPECTED_MODEL,
        source_manifest_sha256=sha(SOURCE / 'split_manifest.json'),
        selection='100 proportional domain/sample_kind strata, largest remainder, SHA256 ascending, unique hashes, training only',
        strata=dict(Counter(s['domain'] + '/' + s['sample_kind'] for s in samples)),
        role='CALIBRATION_ONLY_NOT_ACCURACY_EVALUATION',
        preprocessing='stored image orientation; no extra mirror; INTER_LINEAR letterbox384/pad114; RGB NCHW /255'))
    write(DATA / 'validation_manifest.json', dict(samples=validation, calibration_overlap=0,
        independent_original_scenes=3, derived_pinch_golden=1, independent_background_samples=1,
        limitation='Export verification images plus derived pinch; not held-out accuracy or live acceptance.'))
    write(REPORT / 'frozen_engineering_rules.json', dict(model_sha256=EXPECTED_MODEL,
        policy='W8 inputA16 model0..8A16 model9..23A8 rawHeadsA16', ordinary_confidence=.60, pinch_confidence=.45, nms_iou=.45,
        fp32_vs_integer=dict(preserve_formal_detections=True, no_extra_formal_detections=True,
            minimum_matched_iou=.85, maximum_confidence_drop=.10, maximum_input_coordinate_drift_px=8, pinch_minimum_iou=.90),
        integer_vs_pl='Bit exact every checked integer tensor; board NOT_RUN',
        scope='Engineering comparison only, not generalization or physical accuracy'))
    write(REPORT / 'model_audit.json', dict(model_path=str(model_path), model_sha256=EXPECTED_MODEL,
        onnx_checker='PASS', input=shapes['images'], output=shapes['output0'],
        raw_heads=[dict(name=o['output_tensor'], shape=shapes[o['output_tensor']]) for o in heads],
        convolutions=sum(o['op_type'].startswith('Conv') for o in ops), graph_operations=len(ops),
        model_contract_sha256=sha(SOURCE / 'model_contract.json'), original_contract=read(SOURCE / 'model_contract.json'),
        source_export_verification_sha256=sha(SOURCE / 'export_verification.json'),
        decoded_order='stride8/16/32 then anchor/y/x then cx/cy/w/h/objectness/open_palm/pinch_point'))
    return model_path, model, shapes, init, ops, samples, validation
