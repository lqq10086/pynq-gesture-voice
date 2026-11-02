"""Startup identity binding. Never repairs, regenerates, or trusts manifest flags."""
import hashlib,json
from pathlib import Path
try:
    from .package_contract import freeze_metadata
except ImportError:
    from package_contract import freeze_metadata

ROOT=Path(__file__).resolve().parents[1]
VERSION='V5.2_PL_ACCEL01_ROTATE02'
RECORD='runtime/runtime_qualification.json'
NUMERIC_REPORT='reports/application_numeric/summary.json'
FIXED={'model_package/model_manifest.json': 'ebecd934138182f28c6ca9d92181831eaaf4d2e1d21e02279c787d808f519748', 'model_package/reference.onnx': '4cdad59edc6b4f9c0e93e61a488c53b65127347fbe0c34e1465e28c89bb989f4', 'runtime/hardware/camera_v32_384.bit': 'b28294463632fd45a10b9bcba0704e6eb626c656960276b5cf204ae007e16ab1', 'runtime/hardware/camera_v32_384.hwh': 'e33d31fe8eb0cbc1c5756ef9d70a3dc01cb5c8845788276280efab2146974549', 'layer_dataflow_plan.json': '0d5298480e800ab5ef3dbad69ee7d95469c156da13edc5419a0b84aac2241b74'}
SOURCES=('runtime/package_contract.py', 'runtime/pipeline.py', 'runtime/inference.py', 'runtime/quantized_decode.py', 'native/quantized_decode.cpp', 'native/libquantized_decode.so', 'runtime/region_executor.py', 'runtime/direct_preprocess.py', 'runtime/native_scheduler.py', 'native/host_scheduler.c', 'native/libhost_scheduler.so', 'runtime/native_batch_scheduler.py', 'native/batch_scheduler.c', 'native/libbatch_scheduler.so', 'runtime/input_range.py', 'runtime/qualification.py', 'runtime/collection.py', 'run_board_apps.py', 'start_apps.py', 'runtime/owner_guard.py', 'runtime/gesture_trace.py', 'apps/gesture_app/board_app.py', 'apps/gesture_app/core.py', 'apps/gesture_app/zoom_background.py', 'apps/gesture_app/detection_bridge.py', 'apps/gesture_app/detection_overlay.py', 'apps/gesture_app/assets/label_open_palm.png', 'apps/gesture_app/assets/label_side_palm.png', 'apps/gesture_app/assets/label_pinch_point.png', 'apps/gesture_app/assets/label_thumbs_up.png', 'apps/gesture_app/games.py', 'apps/gesture_app/window_resize.py', 'apps/gesture_app/palm_ball.py', 'runtime/native/direct_preprocess.cpp', 'runtime/native/direct_preprocess.h', 'runtime/native/libdirect_preprocess.so', 'native/libinput_range.so', 'hdmi_output.py', 'frame_color.py', 'native/frame_color.cpp', 'native/libframe_color.so', 'runtime/video_statistics.py', 'runtime/camera_control.py', 'runtime/voice_bridge.py', 'apps/gesture_app/block_builder.py', 'apps/gesture_app/pseudo3d.py', 'scripts/board_new384_numeric.py', 'reports/new384_board_inputs/manifest.json', 'voice_control/__init__.py', 'voice_control/audio_source.py', 'voice_control/classifier.py', 'voice_control/diagnostics.py', 'voice_control/features.py', 'voice_control/session.py', 'voice_control/vad.py', 'reports/VIDEO_HARDWARE_GATE.json', 'runtime/sensor_wb.py', 'runtime/wb_profile.py', 'runtime/wb_maintenance.py', 'scripts/sensor_wb_cli.py', 'start_v34.py', 'scripts/restore_v34.py', 'voice_assets/config_en8.json', 'voice_assets/ds_cnn_en8.onnx', 'voice_assets/norm_stats_en8.npz', 'voice_assets/thresholds.json', 'voice_assets/PROVENANCE.json', 'configs/voice.json', 'runtime/frame_reader.py', 'configs/sensor_wb_profile.json', 'reports/v51_hardware_reuse.json', 'reports/v51_v50_VIDEO_HARDWARE_GATE.json', 'scripts/deploy_v34.py', 'scripts/deployment_transaction.py', 'scripts/boardctl.py', 'scripts/finalize_v51_identity.py', 'START.ps1', 'STOP.ps1', 'ROLLBACK.ps1', 'apps/gesture_app/__init__.py', 'apps/gesture_app/engine.py', 'apps/gesture_app/frame_io.py', 'apps/gesture_app/inference.py', 'apps/gesture_app/palm_plane.py', 'apps/gesture_app/pynq_dma.py', 'apps/gesture_app/pynq_io.py', 'apps/gesture_app/runner.py', 'apps/gesture_app/spatial_ui.py', 'bootstrap_runtime.py', 'reports/field_wb/field_wb.py', 'run_after_cosim.py', 'runtime/activate_package.py', 'runtime/baseline_compat.py', 'runtime/board_acceptance.py', 'runtime/cold_boot_handoff.py', 'runtime/display_owner.py', 'runtime/hardware_idle.py', 'runtime/input_recording.py', 'runtime/inspect_failed_start.py', 'runtime/overlay_driver.py', 'runtime/phase.py', 'runtime/run_realtime.py', 'runtime/safe_stop.py', 'runtime/shutdown_owner.py', 'runtime/smoothing.py', 'runtime/start_background.py', 'runtime/test_package_switch.py', 'runtime/user_phase_sequence.py', 'runtime/user_recording.py', 'runtime/verify_deployment.py', 'scripts/board_transport.py', 'scripts/build_wb1.py', 'scripts/check_ov5640_clock.py', 'scripts/finalize_v5_models.py', 'scripts/finalize_wb_identity.py', 'scripts/load_usb_audio_board.py', 'scripts/precision/build_dataflow_plan.py', 'scripts/precision/build_precision.py', 'scripts/precision/generate_384.py', 'scripts/precision/test_v5_precision.py', 'scripts/precision/v5_sources.py', 'scripts/read_candidate_gate.py', 'scripts/reference/inference_reference.py', 'scripts/reference/integer_reference.py', 'scripts/review_hardware_384_D.py', 'scripts/review_hardware_v33.py', 'scripts/review_wb1_hardware.py', 'scripts/rollback_v34.py', 'scripts/run_video_rtl_tests.py', 'scripts/usb_audio_hid_guard.py', 'stop_apps.py', 'stop_v34.py', 'tests/test_pl_camera_display.py', 'tests/test_sensor_wb.py', 'tests/test_sensor_wb_imports.py', 'tests/test_v51_identity.py', 'tests/test_v51_startup_health.py', 'tests/test_v5_candidate_scope.py', 'tests/test_v5_gesture_runtime.py', 'tests/test_voice_v5.py', 'tests/test_wb_candidate_paths.py', 'tests/test_wb_maintenance.py', 'tests/test_wb_profile.py', 'tests/test_wb_rebind.py', 'tests/test_wb_startup.py', 'voice_assets/reference/inference/kws_inference.py', 'voice_assets/reference/realtime/kws_demo.py', 'voice_assets/reference/realtime/kws_realtime.py', 'voice_assets/reference/realtime/vad.py', 'voice_assets/reference/tools/_eval_vad_pipeline_en8.py', 'voice_assets/reference/training/common.py', 'voice_assets/reference/training/featurize.py', 'application_identity.json', 'audio_support/module_manifest.json', 'reports/pl_accel01_hardware_review.json', 'reports/pl_accel01_spatial6_wb_rebind/proof.json', 'scripts/prepare_camera_wb_rebind.py', 'tests/test_wb_pipeline_rebind.py')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify_runtime(root=ROOT,overlay=None,execution_plan=None):
    root=Path(root)
    record=json.loads((root/RECORD).read_text())
    if record.get('version')!=VERSION or record.get('fixed')!=FIXED:
        raise ValueError('Runtime qualification has a different approved identity')
    if set(record.get('sources',{}))!=set(SOURCES):raise ValueError('Incomplete runtime qualification')
    for name,digest in {**FIXED,**record['sources']}.items():
        if sha(root/name)!=digest:raise ValueError('Runtime qualification invalidated by '+name)
    if overlay is not None:
        bit=Path(overlay.bitfile_name)
        if sha(bit)!=FIXED['runtime/hardware/camera_v32_384.bit'] or sha(bit.with_suffix('.hwh'))!=FIXED['runtime/hardware/camera_v32_384.hwh']:
            raise ValueError('Overlay differs from qualified D4')
    if execution_plan is not None and execution_plan!=json.loads((root/'layer_dataflow_plan.json').read_text()):
        raise ValueError('Execution configuration differs from qualified plan')
    return freeze_metadata(record)

def require_numeric(root,decoder):
    """Formal app gate; the numeric qualification entry itself can run first."""
    root=Path(root);numeric_path=root/NUMERIC_REPORT
    record=json.loads(numeric_path.read_text())
    manifest=json.loads((root/'model_package/model_manifest.json').read_text())
    expected=dict(model_sha256=FIXED['model_package/reference.onnx'],
        package_manifest_sha256=FIXED['model_package/model_manifest.json'],
        bitstream_sha256=FIXED['runtime/hardware/camera_v32_384.bit'],
        hwh_sha256=FIXED['runtime/hardware/camera_v32_384.hwh'],
        schedule_sha256=FIXED['layer_dataflow_plan.json'],
        architecture_id=manifest['architecture_id'],
        hardware_abi='v32-384-chw-w8-mixed-a8a16-q16-q22-q24',decoder=decoder,
        runtime_qualification_sha256=sha(root/RECORD))
    if record.get('status')!='PL384_NUMERIC_PASS' or record.get('shutdown_verified') is not True or record.get('identity')!=expected:
        raise ValueError('Current code requires matching, safely completed board numeric qualification')
    samples=record.get('samples',[])
    native=record.get('native_preprocess_samples',[])
    oracle=json.loads((root/'reports/new384_board_inputs/manifest.json').read_text())
    expected_inputs=[(s['name'],s['input_sha256']) for s in oracle['records']]
    if (len(samples)!=4 or samples[0].get('pipeline_phase7_protocol_observed') is not True
            or sum(len(s.get('tensors',[])) for s in samples)!=340
            or sum(len(s.get('head_tail_guards',[])) for s in samples)!=12
            or [(s.get('name'),s.get('input_sha256')) for s in samples]!=expected_inputs
            or not all(s.get('passed') is True
                       and s.get('decoded_integer_comparison',{}).get('passed') is True
                       and s.get('decoder_reference_comparison',{}).get('byte_exact') is True
                       and s.get('profile_87_invocations_complete') is True
                       and all(t.get('byte_exact') is True for t in s['tensors'])
                       and all(g.get('tail_intact') is True for g in s['head_tail_guards']) for s in samples)
            or len(native)!=5 or not all(n.get('passed') is True and n.get('byte_exact') is True
                                         and n.get('geometry_exact') is True for n in native)
            or record.get('oracle_manifest_sha256')!=sha(root/'reports/new384_board_inputs/manifest.json')):
        raise ValueError('New model qualification requires complete exact tensors, guards and live native preprocessing')
    return sha(numeric_path)
