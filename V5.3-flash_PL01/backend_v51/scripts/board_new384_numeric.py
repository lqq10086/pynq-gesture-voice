"""Real PL qualification against the frozen 384 integer oracle; never a FPS test."""
if __name__=='__main__':
    import sys
    if not any(arg in ('--help','-h','--validate-only') for arg in sys.argv[1:]):
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
        from owner_guard import run_entry_locked
        run_entry_locked(__file__)
        raise SystemExit(0)
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime')]
BOX_ATOL = .002
PROBABILITY_ATOL = .000002


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def checked_file(root, filename, expected):
    path = (root / filename).resolve()
    if root.resolve() not in path.parents or sha(path) != expected:
        raise ValueError('Oracle file path/hash mismatch: ' + filename)
    return path


def compare_integer(actual, expected):
    if actual.dtype != expected.dtype or actual.shape != expected.shape:
        raise ValueError('Integer tensor shape/dtype mismatch')
    error = actual.astype(np.int64) - expected.astype(np.int64)
    return dict(byte_exact=digest(actual) == digest(expected),
                mismatch_count=int(np.count_nonzero(error)), max_abs=int(np.abs(error).max()),
                actual_sha256=digest(actual), expected_sha256=digest(expected))


def compare_native_preprocess(model, raw565):
    """Check the actual deployed native library and fresh input scale on a frame."""
    from runtime.frame_reader import rgb565_to_bgr
    from runtime.inference import preprocess
    model.executor.prepare_input()
    actual, geometry = model.preprocessor.run(raw565)
    fp32, reference_geometry = preprocess(rgb565_to_bgr(raw565), 384, True)
    scale = np.float32(model.package['manifest']['input_scale'])
    expected = np.clip(np.rint(fp32 / scale), -32767, 32767).astype(np.int16)
    result = compare_integer(actual, expected)
    result['geometry_exact'] = geometry == reference_geometry
    result['passed'] = result['byte_exact'] and result['geometry_exact']
    result['scope'] = 'Actual native RGB565 mirror/INTER_LINEAR/pad114/RGB/quantize versus reference; no PL claim'
    return result


def compare_decoded(actual, expected):
    if actual.shape != (1, 9072, 7) or expected.shape != actual.shape or actual.dtype != np.float32 or expected.dtype != np.float32:
        raise ValueError('Expected float32 decoded [1,9072,7]')
    finite = bool(np.isfinite(actual).all() and np.isfinite(expected).all())
    error = np.abs(actual.astype(np.float64) - expected.astype(np.float64))
    box = float(error[..., :4].max()) if finite else None
    probability = float(error[..., 4:].max()) if finite else None
    return dict(passed=finite and box <= BOX_ATOL and probability <= PROBABILITY_ATOL,
                finite=finite, max_box_abs_pixels=box, max_probability_abs=probability,
                tolerances=dict(box_abs_pixels=BOX_ATOL, probability_abs=PROBABILITY_ATOL, relative=0),
                scope='Same exact integer raw heads: NumPy/ORT float32 sigmoid-exp roundoff only; NOT quantization tolerance')


def validate_oracle(root, package):
    path = root / 'reports/new384_board_inputs/manifest.json'
    oracle = json.loads(path.read_text())
    manifest = package['manifest']
    if (not oracle.get('complete') or oracle['model_sha256'] != manifest['model_sha256'] or
            oracle.get('model_manifest_sha256') != sha(root / 'model_package/model_manifest.json') or
            oracle.get('package_files') != manifest['files'] or
            oracle.get('architecture_id') != manifest['architecture_id']):
        raise ValueError('Oracle is incomplete or bound to different numeric parameters')
    if len(package['operations']) != 85 or len(package['descriptors']) != 87:
        raise ValueError('Expected full 85-operation / 87-invocation graph')
    records = oracle['records']
    if len(records) != 4 or len({r['name'] for r in records}) != 4 or oracle['independent_original_scenes'] != 3 or oracle['derived_pinch_golden'] != 1:
        raise ValueError('Expected three independent originals and one derived pinch golden')
    calibration = json.loads(package['blobs']['calibration_manifest.json'])
    if {r['source_sha256'] for r in records} & {r['sha256'] for r in calibration['samples']}:
        raise ValueError('Calibration/engineering sample overlap')
    for record in records:
        if record['input_shape'] != [3, 384, 384] or record['input_dtype'] != 'int16':
            raise ValueError('Oracle input contract mismatch')
        qpath = checked_file(root, record['input_file'], record['input_sha256'])
        quantized = np.fromfile(qpath, '<i2').reshape(3, 384, 384)
        fpath = checked_file(root, record['fp32_input_file'], record['fp32_input_sha256'])
        fp32 = np.load(fpath, allow_pickle=False)
        if fp32.shape != (1, 3, 384, 384) or fp32.dtype != np.float32:
            raise ValueError('Oracle FP32 input contract mismatch')
        expected_input = np.clip(np.rint(fp32[0] / np.float32(manifest['input_scale'])), -32767, 32767).astype(np.int16)
        if not np.array_equal(expected_input, quantized):
            raise ValueError('Oracle input does not use the current input scale')
        tensor_path = checked_file(root, record['expected_tensors_file'], record['expected_tensors_sha256'])
        if len(record['expected_tensors']) != 85:
            raise ValueError('Oracle missing graph tensors')
        with np.load(tensor_path, allow_pickle=False) as tensors:
            if set(tensors.files) != {'images'} | {'op%02d' % i for i in range(85)} or not np.array_equal(tensors['images'], quantized):
                raise ValueError('Oracle NPZ tensor set/input mismatch')
            for item, operation in zip(record['expected_tensors'], package['operations']):
                expected = dict(index=operation['index'], tensor=operation['output_tensor'],
                                shape=operation['output_shape'][1:], dtype='int16' if operation['activation_bits'] == 16 else 'int8',
                                scale=operation['output_scale'])
                if {k: item[k] for k in expected} != expected:
                    raise ValueError('Oracle tensor topology/scale mismatch')
                array = tensors['op%02d' % item['index']]
                if list(array.shape) != item['shape'] or str(array.dtype) != item['dtype'] or digest(array) != item['sha256']:
                    raise ValueError('Oracle tensor data/hash mismatch')
            if len(record['raw_head_files']) != 3:
                raise ValueError('Oracle requires all three raw heads')
            for head, boundary in zip(record['raw_head_files'], package['boundaries']):
                array = np.load(checked_file(root, head['file'], head['sha256']), allow_pickle=False)
                if head['tensor'] != boundary['name'] or head['scale'] != boundary['scale'] or not np.array_equal(array, tensors['op%02d' % head['index']]):
                    raise ValueError('Raw head artifact differs from complete tensor oracle')
        for prefix in ('decoded_integer', 'decoded_fp32'):
            decoded = np.load(checked_file(root, record[prefix + '_file'], record[prefix + '_sha256']), allow_pickle=False)
            if decoded.dtype != np.float32 or decoded.shape != (1, 9072, 7) or not np.isfinite(decoded).all():
                raise ValueError('Invalid decoded oracle')
        if record['geometry'].get('inference_mirror') is not False:
            raise ValueError('Oracle input is already mirrored; a second mirror is forbidden')
    return oracle


def safe_release(camera, executor, owner, mark_quiescent):
    camera.halt_video()
    if executor is not None:
        executor.wait_idle()
    output = getattr(camera, 'hdmi_output', None)
    if output is not None:
        output.close()
    camera.stop_video()
    if executor is not None:
        executor.close()
    proof = mark_quiescent(camera, executor)
    owner.close()
    return proof


def memory_snapshot():
    return dict(process=[line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('VmRSS:', 'VmHWM:'))],
                cma=[line for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('CmaTotal:', 'CmaFree:'))])


def add_head_guards(executor):
    executor.wait_idle()
    guards = []
    for head in executor.boundaries:
        count = int(np.prod(head['shape']))
        buffer = executor._alloc(count + 64, np.int16)
        buffer.fill(23130); buffer.flush()
        # Previous buffers remain owned until verified shutdown; no early CMA free.
        executor.tensors[head['name']] = buffer
        guards.append((head['name'], count, buffer))
    return guards


def record_numeric(root, output, model, record, guards, first):
    folder = output / record['name']; folder.mkdir()
    executor = model.executor
    qinput = np.fromfile(root / record['input_file'], '<i2').reshape(3, 384, 384)
    for _, count, buffer in guards:
        buffer[count:] = 23130; buffer.flush()
    heads, profile = executor.run({'images': qinput}, collect_phase_cycles=first, copy_outputs=False)
    rows, actual_tensors = [], {}
    with np.load(root / record['expected_tensors_file'], allow_pickle=False) as expected:
        for item in record['expected_tensors']:
            buffer = executor.tensors[item['tensor']]; buffer.invalidate()
            array = np.array(buffer[:int(np.prod(item['shape']))], copy=True).reshape(item['shape'])
            key = 'op%02d' % item['index']; actual_tensors[key] = array
            rows.append(dict(index=item['index'], tensor=item['tensor'], shape=item['shape'], dtype=str(array.dtype),
                             **compare_integer(array, expected[key])))
    np.savez_compressed(folder / 'actual_tensors.npz', **actual_tensors)
    tails = []
    for name, count, buffer in guards:
        buffer.invalidate()
        tails.append(dict(tensor=name, logical_elements=count, guard_elements=int(buffer.size - count),
                          tail_intact=bool(np.all(buffer[count:] == 23130))))
    decoded, decode_times = model.decode_heads(heads)
    # Qualify the very same helper used by forward_tensor, then compare to the
    # retained dequantize/sigmoid path with no tolerance for LUT differences.
    from pipeline import IndependentDecoder
    reference_heads = [np.multiply(heads[b['name']][None], np.float32(b['scale']), dtype=np.float32)
                       for b in model.package['boundaries']]
    reference = IndependentDecoder(model.package['manifest']['detect']).decode(reference_heads)
    exact_decode = dict(byte_exact=digest(decoded) == digest(reference),
                        max_abs=float(np.abs(decoded - reference).max()),
                        actual_sha256=digest(decoded), reference_sha256=digest(reference))
    np.save(folder / 'actual_decoded.npy', decoded)
    decode_result = compare_decoded(decoded, np.load(root / record['decoded_integer_file'], allow_pickle=False))
    from inference import postprocess
    detections = postprocess(decoded, record['geometry'], .45, .45, 20, model.config['classes'], enabled_classes=(0, 1))
    detections = [d for d in detections if d['class_id'] == 1 or d['confidence'] >= .60]
    phase_complete = not first or len(profile['phase_cycles']) == 87
    phase7_observed = None
    if first:
        phases = profile['phase_cycles']
        phase7_observed = bool(phase_complete and profile.get('phase_counter_semantics') == 'pipeline_phase7_v1'
            and all(phases[i].get('phase7_protocol_observed') is True
                    for i,descriptor in enumerate(executor.descriptors)
                    if descriptor['op_type'].startswith('Conv')))
    result = dict(name=record['name'], input_sha256=record['input_sha256'], source_sha256=record['source_sha256'],
                  role=record['role'], geometry=record['geometry'], tensors=rows, head_tail_guards=tails,
                  decoded_integer_comparison=decode_result, decoder_reference_comparison=exact_decode,
                  decoder_identity=model.decoder_identity, detections=detections,
                  expected_integer_detections=record['integer_detections'], fp32_detections=record['fp32_detections'],
                  profile=profile, profile_87_invocations_complete=phase_complete,
                  pipeline_phase7_protocol_observed=phase7_observed,
                  **decode_times,
                  artifacts={p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in folder.iterdir()})
    result['passed'] = all(row['byte_exact'] for row in rows) and all(t['tail_intact'] for t in tails) and decode_result['passed'] and exact_decode['byte_exact'] and phase_complete and (not first or phase7_observed)
    save(folder / 'result.json', result)
    return result


def run(args):
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    status_path = output / 'status.json'
    result = dict(status='STARTING', owner_pid=os.getpid(), board_unix_s=time.time(), shutdown_verified=False,
                  board_access_attempted=False, samples=[], video_samples=[], native_preprocess_samples=[],
                  scope='Real PL frozen-input numeric qualification with live OV5640/HDMI between numeric steps; NOT video FPS or physical gesture acceptance',
                  physical_gesture_acceptance='PENDING', fps_status='NOT_MEASURED', independent_original_scenes=3, derived_golden_cases=1)
    owner = camera = model = None
    stop = threading.Event()
    def request_stop(signum, frame):
        stop.set()
    def check_stop():
        if stop.is_set():
            raise InterruptedError('Graceful stop requested')
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)
    save(status_path, result)
    try:
        from package_contract import validate_package
        package = validate_package(ROOT / 'model_package')
        oracle = validate_oracle(ROOT, package)
        result['oracle_manifest_sha256'] = sha(ROOT / 'reports/new384_board_inputs/manifest.json')
        result['package_manifest_sha256'] = sha(ROOT / 'model_package/model_manifest.json')
        result['model_identity'] = {k: package['manifest'][k] for k in ('model_sha256', 'architecture_id', 'hardware_abi', 'input_shape', 'input_bits', 'input_scale', 'classes', 'detect')}
        from pipeline import MiniPipeline
        model = MiniPipeline(ROOT / 'model_package', 'hybrid')
        result['decoder_identity'] = model.decoder_identity
        if args.validate_only:
            result['status'] = 'SOFTWARE_PREFLIGHT_PASS_NOT_BOARD_ACCEPTANCE'
            return 0
        from camera_control import CameraControlSession
        from frame_reader import CompletedFrameReader
        from hardware_idle import require_pynq_device
        from owner_guard import acquire_owner, mark_quiescent
        from region_executor import check_global_addresses
        from run_realtime import read_video_health
        bit = (args.bit or ROOT / 'runtime/hardware/camera_v32_384.bit').resolve()
        artifacts = [bit, bit.with_suffix('.hwh'), ROOT / 'layer_dataflow_plan.json']
        result['hardware_artifacts'] = {str(p): dict(bytes=p.stat().st_size, sha256=sha(p)) for p in artifacts}
        result['identity'] = dict(model_sha256=package['manifest']['model_sha256'],
                                  package_manifest_sha256=sha(ROOT / 'model_package/model_manifest.json'),
                                  bitstream_sha256=sha(bit), hwh_sha256=sha(bit.with_suffix('.hwh')),
                                  architecture_id=package['manifest']['architecture_id'], hardware_abi=package['manifest']['hardware_abi'],
                                  schedule_sha256=sha(ROOT / 'layer_dataflow_plan.json'), decoder=model.decoder_identity,
                                  runtime_qualification_sha256=sha(ROOT / 'runtime/runtime_qualification.json'))
        result['schedule_sha256'] = sha(ROOT / 'layer_dataflow_plan.json')
        result['device_preflight'] = require_pynq_device()
        result['memory_before'] = memory_snapshot()
        camera = CameraControlSession()
        check_stop(); owner = acquire_owner(); result['board_access_attempted'] = True
        camera.load_overlay(bit)
        result['display_path'] = 'PL_CAMERA_NO_UI'
        result['overlay_load_count'] = 1
        model.attach(camera)
        guards = add_head_guards(model.executor)
        result['all_owned_cma_intervals'] = check_global_addresses([camera, model.executor])
        result['owned_buffer_bytes'] = {name: sum(int(b.nbytes) for b in obj.buffers) for name, obj in [('camera', camera), ('ai_including_guards', model.executor)]}
        result['clocks'] = camera._check_clocks()
        result['backend'] = model.identity()
        result['descriptors'] = model.executor.descriptors
        reader = CompletedFrameReader(camera)
        def preview():
            check_stop()
            deadline = time.monotonic() + 5
            while True:
                try:
                    frame = reader.read(timeout=2); break
                except RuntimeError as error:
                    if 'stable VDMA buffer window' not in str(error) or time.monotonic() >= deadline:
                        raise
                    check_stop()
            display = dict(mode='PL_CAMERA_NO_UI', ps_frame_submission=False,
                           acknowledgement='NOT_APPLICABLE_CONTINUOUS_PL_CAMERA')
            health = read_video_health(camera)
            preprocessing = compare_native_preprocess(model, frame['raw565'])
            preprocessing['source_frame_id'] = frame['source_frame_id']
            result['native_preprocess_samples'].append(preprocessing)
            if not preprocessing['passed']:
                raise RuntimeError('Native preprocessing differs from new-model reference')
            result['video_samples'].append(dict(source_frame_id=frame['source_frame_id'], source_monotonic=frame['source_frame_complete_monotonic'],
                                               raw_sha256=digest(frame['raw565']), hdmi=display, video=health))
            if not health['healthy']:
                raise RuntimeError('Live video unhealthy during numeric qualification')
        result['status'] = 'RUNNING_PL_NUMERIC'
        preview(); save(status_path, result)
        for index, record in enumerate(oracle['records']):
            check_stop()
            row = record_numeric(ROOT, output, model, record, guards, index == 0)
            result['samples'].append(row)
            preview(); result['memory_current'] = memory_snapshot(); save(status_path, result)
            if not row['passed']:
                raise RuntimeError('PL integer/guard/PS decode qualification failed: ' + record['name'])
        result['camera_distinct_raw_sha256'] = len({row['raw_sha256'] for row in result['video_samples']})
        result['status'] = 'PL384_NUMERIC_PASS_PENDING_SAFE_SHUTDOWN'
    except BaseException as error:
        result.update(status='FAILED', error=repr(error), traceback=traceback.format_exc())
        print(result['traceback'], flush=True)
    finally:
        final_status = result['status']
        while owner is not None:
            try:
                proof = safe_release(camera, model.executor if model else None, owner, mark_quiescent)
                owner = None
                result.update(status='PL384_NUMERIC_PASS' if final_status == 'PL384_NUMERIC_PASS_PENDING_SAFE_SHUTDOWN' else final_status,
                              shutdown_verified=True, handoff=proof, memory_after=memory_snapshot())
            except BaseException as error:
                result.update(status='RETAINED_OWNER_RETRY', shutdown_verified=False, shutdown_error=repr(error))
                try:
                    save(status_path, result)
                except OSError as write_error:
                    print('Status write failed; retaining resources: ' + repr(write_error), flush=True)
                print('Retaining DMA/CMA and owner until halt and idle: ' + repr(error), flush=True)
                time.sleep(5)
        save(status_path, result)
        save(output / 'summary.json', result)
    return 0 if result['status'] == 'PL384_NUMERIC_PASS' and result['shutdown_verified'] else 1


if __name__ in ('__main__','__ax7020_locked_main__'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New unique directory; existing directories are rejected')
    parser.add_argument('--bit', type=Path, help='Qualified unified V3.2 video + IP3.2 bit/HWH pair')
    parser.add_argument('--validate-only', action='store_true', help='Local package/oracle validation only; never accesses board')
    raise SystemExit(run(parser.parse_args()))
