"""Build fresh 384 parameters and independent integer engineering references."""
import argparse
import copy
import csv
import json
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
from build_precision import (ROOT, PKG, graph, parameters, save_package, decode_session,
                             read, write, sha, sess, iou, execute, quant, helper, onnx, np, cv2)
from inference_reference import preprocess, decode
from v5_sources import SOURCE, EXPECTED_MODEL, REPORT, DATA, prepare, image


def tensor_from_source(path):
    # V5 dataset images are already in their training/export orientation.
    # FPGA sources are already mirrored upstream; never mirror again here.
    return preprocess(image(path))


def calibrate(model, shapes, ops, samples):
    names = ['images'] + [o['output_tensor'] for o in ops]
    probe = copy.deepcopy(model)
    del probe.graph.output[:]
    probe.graph.output.extend(helper.make_tensor_value_info(n, onnx.TensorProto.FLOAT, shapes[n]) for n in names)
    session = sess(probe.SerializeToString())
    cache = REPORT / 'calibration_384.json'
    if cache.exists():
        previous = read(cache)
        if previous.get('model_sha256') != EXPECTED_MODEL or previous.get('samples') != samples:
            raise ValueError('Calibration cache identity changed; preserve old result and choose a fresh output')
        return previous, session, names
    maxima = {name: 0.0 for name in names}
    percentiles = maxima.copy()
    started = time.monotonic()
    for index, row in enumerate(samples):
        tensor, _ = tensor_from_source(row['file'])
        for name, array in zip(names, session.run(None, {'images': tensor})):
            maxima[name] = max(maxima[name], float(np.abs(array).max()))
            percentiles[name] = max(percentiles[name], float(np.percentile(np.abs(array), 99.99)))
        if (index + 1) % 10 == 0:
            print('CALIBRATION', index + 1, '/ 100', 'elapsed_s', round(time.monotonic() - started, 2), flush=True)
    result = dict(model_sha256=EXPECTED_MODEL, samples=samples, maximum=maxima, percentile=percentiles,
                  image_count=100, source_split='V5_SNAPSHOT_TRAIN', independent_validation=False,
                  coverage_limit='Deterministic 100-image V5 train subset, including legacy and FPGA domains; no broad accuracy claim.',
                  preprocess='stored source orientation, no additional mirror; INTER_LINEAR letterbox384/pad114; RGB NCHW /255',
                  elapsed_s=time.monotonic() - started)
    write(cache, result)
    return result, session, names


def golden_reference(model, sample):
    folder = REPORT / 'fp32_golden'
    folder.mkdir(parents=True, exist_ok=True)
    tensor, geometry = tensor_from_source(sample['file'])
    expected = sess(model.SerializeToString()).run(None, {'images': tensor})[0]
    net = cv2.dnn.readNetFromONNX(np.frombuffer(model.SerializeToString(), dtype=np.uint8))
    net.setInput(tensor, 'images')
    actual = net.forward('output0')
    box_error = float(np.abs(expected[..., :4] - actual[..., :4]).max())
    probability_error = float(np.abs(expected[..., 4:] - actual[..., 4:]).max())
    passed = box_error < .1 and probability_error < .0001
    for name, array in [('input_nchw_rgb_fp32', tensor), ('output_decoded_fp32', expected)]:
        np.save(folder / (name + '.npy'), array)
        array.astype('<f4').tofile(folder / (name + '.bin'))
    write(REPORT / 'fp32_golden.json', dict(
        status='FP32_SOFTWARE_PARITY_PASS' if passed else 'FAILED', model_sha256=EXPECTED_MODEL,
        source=sample, geometry=geometry.__dict__, input_shape=list(tensor.shape), output_shape=list(expected.shape),
        max_box_error_pixels=box_error, max_probability_error=probability_error,
        tolerances=dict(box_pixels=.1, probability=.0001),
        detections=[dict(label=d.label, confidence=d.confidence, box=d.box) for d in decode(expected, geometry)],
        files={p.name: dict(sha256=sha(p), bytes=p.stat().st_size) for p in sorted(folder.iterdir())},
        scope='Fresh V5 OpenCV vs ONNXRuntime FP32 software parity; not PL or INT8 acceptance'))
    if not passed:
        raise ValueError('Fresh V5 OpenCV/ORT golden mismatch')


def detection_comparison(reference, candidate, geometry):
    def records(raw):
        names = ['open_palm', 'pinch_point']
        return [dict(class_id=names.index(d.label), label=d.label, confidence=float(d.confidence),
                     box=list(d.box)) for d in decode(raw, geometry)]
    expected, actual = records(reference), records(candidate)
    available = set(range(len(actual)))
    matches, misses = [], []
    for index, row in enumerate(expected):
        choices = [j for j in available if actual[j]['label'] == row['label']]
        chosen = max(choices, key=lambda j: iou(row['box'], actual[j]['box'])) if choices else None
        if chosen is None:
            misses.append(index)
            continue
        available.remove(chosen)
        overlap = iou(row['box'], actual[chosen]['box'])
        drift = float(np.abs(np.array(row['box']) - actual[chosen]['box']).max()) * geometry.ratio
        drop = row['confidence'] - actual[chosen]['confidence']
        passed = overlap >= (.90 if row['label'] == 'pinch_point' else .85) and drop <= .10 and drift <= 8
        matches.append(dict(label=row['label'], fp32_index=index, integer_index=chosen, iou=overlap,
                            confidence_delta=-drop, input_coordinate_drift_px=drift, passed=passed))
    return dict(fp32_detections=expected, integer_detections=actual, matches=matches,
                misses=misses, extras=sorted(available),
                passed=not misses and not available and all(row['passed'] for row in matches))


def validate(model, shapes, ops, params, info, session, names, validation):
    decoder = decode_session(model, ops, shapes)
    full = sess(model.SerializeToString())
    heads = [o for o in ops if o['op_type'] == 'ConvLinear']
    selected = {0, 1}
    for kind in ('Add', 'Concat', 'MaxPool', 'Resize'):
        selected.update(o['index'] for o in ops if o['op_type'] == kind)
    selected.update(o['index'] for o in ops if o['op_type'] == 'ConvLinear'
                    or o.get('Cin', 0) >= 256 and o.get('H') == 12)
    rows, records = [], []
    board_inputs = ROOT / 'reports/new384_board_inputs'
    board_inputs.mkdir(exist_ok=True)
    for sample in validation:
        started = time.monotonic()
        tensor, geometry = tensor_from_source(sample['file'])
        fpvalues = dict(zip(names, session.run(None, {'images': tensor})))
        fp32 = full.run(None, {'images': tensor})[0]
        qinput = quant(tensor[0], info['images']['scale'], info['images']['bits'])
        destination = REPORT / 'oracle' / sample['name']
        destination.mkdir(parents=True, exist_ok=True)
        qinput.tofile(destination / 'input_int16.bin')
        np.save(destination / 'input_fp32.npy', tensor)
        tensors, metrics = execute(ops, params, {'images': qinput}, fpvalues,
                                   fixtures=dict(path=str(destination), indices=selected) if sample['name'] == 'golden_pinch_720p' else None)
        outputs = {o['output_tensor']: tensors[o['output_tensor']].astype(np.float32)[None] * np.float32(o['output_scale']) for o in heads}
        decoded = decoder.run(None, outputs)[0]
        np.savez_compressed(destination / 'integer_tensors.npz', images=qinput,
                            **{'op%02d' % o['index']: tensors[o['output_tensor']] for o in ops})
        np.save(destination / 'fp32_decoded.npy', fp32)
        np.save(destination / 'integer_decoded.npy', decoded)
        comparison = detection_comparison(fp32, decoded, geometry)
        board_dir = board_inputs / sample['name']
        board_dir.mkdir(exist_ok=True)
        for filename in ('input_int16.bin', 'input_fp32.npy', 'integer_tensors.npz',
                         'integer_decoded.npy', 'fp32_decoded.npy'):
            shutil.copy2(destination / filename, board_dir / filename)
        head_files = []
        for op in heads:
            target = board_dir / ('head_op%02d.npy' % op['index'])
            array = tensors[op['output_tensor']]
            np.save(target, array)
            head_files.append(dict(index=op['index'], tensor=op['output_tensor'],
                                   file=target.relative_to(ROOT).as_posix(), shape=list(array.shape),
                                   dtype=str(array.dtype), scale=op['output_scale'], sha256=sha(target)))
        record = dict(name=sample['name'], input_file=(board_dir / 'input_int16.bin').relative_to(ROOT).as_posix(),
                      input_sha256=sha(board_dir / 'input_int16.bin'), input_shape=list(qinput.shape), input_dtype='int16',
                      fp32_input_file=(board_dir / 'input_fp32.npy').relative_to(ROOT).as_posix(),
                      fp32_input_sha256=sha(board_dir / 'input_fp32.npy'), source_file=sample['file'],
                      source_sha256=sample['sha256'], role=sample['role'],
                      geometry=dict(geometry.__dict__, scale=geometry.ratio, inference_mirror=False,
                                    coordinate_space='stored_source; V5 FPGA sources already mirrored upstream; no additional mirror'),
                      fp32_detections=comparison['fp32_detections'], integer_detections=comparison['integer_detections'],
                      expected_tensors=[dict(index=m['index'], tensor=m['tensor'], shape=m['shape'], dtype=m['dtype'],
                                             sha256=m['sha256'], scale=ops[m['index']]['output_scale']) for m in metrics],
                      expected_tensors_file=(board_dir / 'integer_tensors.npz').relative_to(ROOT).as_posix(),
                      expected_tensors_sha256=sha(board_dir / 'integer_tensors.npz'),
                      expected_tensors_npz_keys='images, op00..op84', raw_head_files=head_files,
                      decoded_integer_file=(board_dir / 'integer_decoded.npy').relative_to(ROOT).as_posix(),
                      decoded_integer_sha256=sha(board_dir / 'integer_decoded.npy'),
                      decoded_fp32_file=(board_dir / 'fp32_decoded.npy').relative_to(ROOT).as_posix(),
                      decoded_fp32_sha256=sha(board_dir / 'fp32_decoded.npy'))
        records.append(record)
        write(board_inputs / 'manifest.json', dict(schema=1, model_sha256=EXPECTED_MODEL,
              input_format='little-endian signed int16 CHW, no header, already quantized with manifest input_scale',
              tensor_hash_format='SHA256 of C-order little-endian integer tensor bytes, no NumPy header',
              arithmetic='V3.2 W8/mixed A8/A16; bit-exact integer-to-PL comparison required',
              independent_original_scenes=3, derived_pinch_golden=1, records=records,
              complete=len(records) == len(validation), board_validated=False))
        input_quant_error = qinput.astype(np.float64) * info['images']['scale'] - tensor[0]
        top_scores = lambda values: (values[0, :, 4:5] * values[0, :, 5:]).max(axis=0).tolist()
        row = dict(sample=sample, model_sha256=EXPECTED_MODEL, geometry=geometry.__dict__,
                   input_quant_max_error=float(np.abs(input_quant_error).max()),
                   input_quant_rmse=float(np.sqrt(np.mean(input_quant_error ** 2))),
                   elapsed_s=time.monotonic() - started, comparison=comparison,
                   fp32_per_class_max_scores=top_scores(fp32), integer_per_class_max_scores=top_scores(decoded),
                   intermediate_metrics=metrics,
                   raw_head_metrics=[metrics[o['index']] for o in heads],
                   oracle_files={p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in destination.iterdir() if p.is_file()},
                   tensor_order=[dict(key='op%02d' % o['index'], index=o['index'], name=o['output_tensor']) for o in ops])
        write(destination / 'manifest.json', row)
        rows.append(row)
        print('INTEGER_VALIDATION', sample['name'], comparison['passed'], round(row['elapsed_s'], 2), flush=True)
    passed = all(row['comparison']['passed'] for row in rows)
    write(REPORT / 'integer_validation.json', dict(
        status='ENGINEERING_REGRESSION_PASS' if passed else 'ENGINEERING_REGRESSION_FAILED',
        model_sha256=EXPECTED_MODEL, rules_source='frozen_engineering_rules.json',
        independent_original_scenes=3, derived_golden_cases=1,
        board_integer_comparison='NOT_RUN', physical_gesture_acceptance='PENDING',
        max_layer_clipped_fraction=max(m['clipped_fraction'] for row in rows for m in row['intermediate_metrics']),
        scope='Quantization engineering comparison: V5 open/pinch/background validation images plus derived pinch; not generalization accuracy.',
        samples=rows))
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibrate-only', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    started = time.monotonic()
    cv2.setNumThreads(2)
    model_path, model, shapes, init, ops, samples, validation = prepare()
    if args.prepare_only:
        print('PREPARED', len(samples), 'calibration', len(validation), 'engineering inputs', flush=True)
        return
    golden_reference(model, validation[-1])
    cal, session, names = calibrate(model, shapes, ops, samples)
    configured, params, info = parameters(ops, init, cal, end=8, input_bits=16)
    if max(o.get('accumulator_width', 0) for o in configured) > 34:
        raise ValueError('Fresh parameters exceed the preserved 34-bit accumulator contract')
    PKG.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model_path, PKG / 'reference.onnx')
    save_package(PKG, configured, params, info, shapes, 'FRESH_V5_2CLASS_W8_MIXED_A8_A16_UNVERIFIED_HARDWARE', model_path)
    write(PKG / 'calibration_manifest.json', cal)
    write(PKG / 'precision_contract.json', dict(model_sha256=EXPECTED_MODEL,
        policy='inputA16;model0..8A16;model9..23A8;LINEARheadsA16;poolresizepreserveinputdtype',
        weight_generation='All60 W8 from exact384 ONNX; fresh symmetric per-Cout scales',
        rounding=dict(input='nearest-even', bias='nearest-even', convolution_to_silu='floor((acc*mult+2^23)/2^24)',
                      output='nearest-even signed Q24', branches='Q22 Add sum before RNE; Concat per branch RNE'),
        maximum_accumulator_width=max(o.get('accumulator_width', 0) for o in configured),
        maximum_requant_product_width=max(o.get('requant_product_width', 0) for o in configured),
        tensor_contract={name: dict(value, shape=shapes[name]) for name, value in info.items()},
        software_numeric_status='PENDING', hardware_numeric_status='NOT_RUN'))
    manifest = read(PKG / 'model_manifest.json')
    manifest.update(output_shape=[1, 9072, 7], source_contract_sha256=sha(SOURCE / 'model_contract.json'),
                    calibration_count=100, fresh_parameters=True, software_numeric_status='PENDING', hardware_numeric_status='NOT_RUN')
    manifest['files'] = {p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in sorted(PKG.iterdir()) if p.is_file() and p.name != 'model_manifest.json'}
    write(PKG / 'model_manifest.json', manifest)
    from build_dataflow_plan import main as build_plan
    build_plan()
    print('PACKAGE_READY', manifest['architecture_id'], flush=True)
    if not args.calibrate_only:
        passed = validate(model, shapes, configured, params, info, session, names, validation)
        contract = read(PKG / 'precision_contract.json')
        contract['software_numeric_status'] = 'ENGINEERING_REGRESSION_PASS' if passed else 'ENGINEERING_REGRESSION_FAILED'
        write(PKG / 'precision_contract.json', contract)
        manifest = read(PKG / 'model_manifest.json')
        manifest['software_numeric_status'] = 'ENGINEERING_REGRESSION_PASS' if passed else 'ENGINEERING_REGRESSION_FAILED'
        manifest['files'] = {p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in sorted(PKG.iterdir()) if p.is_file() and p.name != 'model_manifest.json'}
        write(PKG / 'model_manifest.json', manifest)
        oracle_path = ROOT / 'reports/new384_board_inputs/manifest.json'
        oracle = read(oracle_path)
        oracle.update(model_manifest_sha256=sha(PKG / 'model_manifest.json'),
                      architecture_id=manifest['architecture_id'], package_files=manifest['files'])
        write(oracle_path, oracle)
        write(REPORT / 'generation_summary.json', dict(model_sha256=EXPECTED_MODEL, architecture_id=manifest['architecture_id'],
            model_manifest_sha256=sha(PKG / 'model_manifest.json'), elapsed_s=time.monotonic() - started,
            calibration_count=len(samples), engineering_inputs=len(validation), integer_tensors=85*len(validation),
            software_numeric_status=manifest['software_numeric_status'], board_numeric_status='NOT_RUN',
            generated_scripts={str(p.relative_to(ROOT)):sha(p) for folder in ('scripts/precision','scripts/reference') for p in sorted((ROOT/folder).glob('*.py'))}))
        print('GENERATION_COMPLETE', round(time.monotonic() - started, 2), manifest['software_numeric_status'], flush=True)
        if not passed:
            raise SystemExit('Quantization engineering limits failed; retain evidence, do not declare validated.')


if __name__ == '__main__':
    main()
