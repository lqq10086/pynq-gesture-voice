"""Independent PL camera, real model worker, and presented application layers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT/'backend_v51'
sys.path.insert(0,str(ROOT))
os.environ.setdefault('XILINX_XRT','/usr')


def atomic(path,value):
    temporary = path.with_suffix('.tmp')
    def convert(x):
        if hasattr(x,'tolist'):return x.tolist()
        if hasattr(x,'item'):return x.item()
        return str(x)
    temporary.write_text(json.dumps(value,indent=2,default=convert),encoding='utf-8')
    temporary.replace(path)


def install_stop_flag():
    # Python can dispatch a signal while Event.wait holds its non-reentrant
    # condition lock. A signal handler must never call Event.set there.
    stop=[False]
    def request_stop(*_):stop[0]=True
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,request_stop)
    return stop


def checked_identity():
    """Hash once; recheck all bound file stat identities before each WB poll."""
    from board_identity import verify_experimental as verify,experimental_bound_files as bound_files
    files = bound_files(ROOT)
    def signature(path):
        s=path.stat()
        return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns
    signatures={p:signature(p) for p in set(files)}
    identity=verify(ROOT)
    if set(bound_files(ROOT))!=set(files):
        raise RuntimeError('Bound file set changed during identity verification')
    if any(signature(p)!=saved for p,saved in signatures.items()):
        raise RuntimeError('Bound sources changed during identity verification')
    def current():
        if any(signature(p)!=saved for p,saved in signatures.items()):
            raise RuntimeError('Bound source or numeric identity changed during execution')
        return identity
    return current


def audio_setup(config,output):
    voice=config.get('voice',{})
    if not voice.get('enabled'):return 'DISABLED_BY_CONFIGURATION'
    if not shutil.which('arecord'):
        voice['enabled']=False
        return 'UNAVAILABLE_ARECORD_NOT_FOUND'
    cards=Path('/proc/asound/cards')
    if not cards.exists() or 'WirelessMic' not in cards.read_text():
        voice['enabled']=False
        return 'UNAVAILABLE_WIRELESS_MIC_NOT_ENUMERATED'
    guard=BACKEND/'scripts/usb_audio_hid_guard.py'
    with (output/'audio_hid.log').open('x') as log:
        result=subprocess.run([sys.executable,'-B',str(guard),'--suppress'],stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:
        voice['enabled']=False
        return 'UNAVAILABLE_HID_RESERVATION_FAILED'
    return 'ENUMERATED_CAPTURE_PENDING'


def create_ui_worker(config, output):
    from town_creator.live_runtime import UIWorker
    from application_menu import create_app
    return UIWorker(1280, 720, ROOT, config, work_path=Path(output)/'work.json',
                    app_factory=create_app)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration',type=float,default=0)
    parser.add_argument('--exercise',action='store_true')
    args=parser.parse_args()
    output=args.output.resolve()
    if ROOT/'reports' not in output.parents:raise ValueError('Output must be a new project reports directory')
    output.mkdir(parents=True,exist_ok=False)
    stop=install_stop_flag()
    status=dict(version=ROOT.name,owner_pid=os.getpid(),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        phase='PREFLIGHT',output=str(output),shutdown_verified=False,
        display_path='PL_CAMERA_WITH_INDEPENDENT_UI',target_numeric='NOT_QUALIFIED',
        real_gesture_acceptance='NOT_TESTED',real_voice_acceptance='NOT_TESTED',obs_status='PENDING_USER')
    hardware=live=None
    exercise=None
    failed=False
    audio_reserved=False
    def publish(phase=None):
        if phase:status['phase']=phase;print(ROOT.name+' '+phase,flush=True)
        status['sampled_monotonic']=time.monotonic()
        atomic(output/'status.json',status)
    publish()
    try:
        identity_check=checked_identity()
        status['identity']=identity_check()
        config=json.loads((ROOT/'board_config.json').read_text(encoding='utf-8'))
        import cv2
        cv2.setNumThreads(1)
        import hardware_driver
        from examples.live_existing_pipeline import LiveApplication
        from video_statistics import VideoStatistics,snapshot_rates
        from run_realtime import read_video_health
        hardware=hardware_driver.HardwareSystem.__new__(hardware_driver.HardwareSystem)
        publish('HARDWARE_INITIALIZING')
        hardware.__init__(output_dir=output,identity_check=identity_check)
        status['cpu_opencv_threads']=cv2.getNumThreads()
        status['wb']=hardware.wb
        status['audio_hardware']=audio_setup(config,output)
        audio_reserved=status['audio_hardware']=='ENUMERATED_CAPTURE_PENDING'
        live=LiveApplication.__new__(LiveApplication)
        if args.exercise:
            from town_creator.live_runtime import UIWorker
            from scripts.board_serial_fixture import SerialExerciseApp
            ui_worker=UIWorker(1280,720,ROOT,config,app_factory=SerialExerciseApp)
        else:
            ui_worker=create_ui_worker(config, output)
        live.__init__(hardware,hardware.runner,config,root=ROOT,
                      work_path=output/'work.json',defer_conversion=True,ui_worker=ui_worker)
        stats=VideoStatistics(hardware.camera.control_mmio)
        previous=stats.snapshot()
        axi_hz=next(x['mhz'] for x in hardware.camera._check_clocks()['fclk'] if x['index']==0)*1e6
        if args.exercise:
            from scripts.board_serial_fixture import SerialExercise
            exercise=SerialExercise()
        started=time.monotonic();next_status=0.
        previous_hdmi=None
        publish('RUNNING')
        while not stop[0] and (not args.duration or time.monotonic()-started<args.duration):
            now=time.monotonic()
            live.submit_ui(hardware)
            if live.app.worker.error:raise RuntimeError(live.app.worker.error)
            ui=live.app.ui
            if ui is not None:
                flags=ui.control_status()
                if flags.get('error') or flags.get('command_error'):
                    raise RuntimeError(str(flags))
                if flags.get('quit_requested'):stop[0]=True
                if exercise is not None:
                    exercise.step(ui,time.perf_counter())
                    if exercise.done:stop[0]=True
            if live.last_presentation_error or live.failed_presentations:
                raise RuntimeError(live.last_presentation_error or 'UI presentation failed')
            if now>=next_status:
                current=stats.snapshot()
                rates=snapshot_rates(previous,current,axi_hz)
                health=read_video_health(hardware.camera)
                if not health['healthy'] or any(rates[k] for k in
                    ('dvp_drop_events','fifo_overflow_events','dvp_axis_errors','s2mm_axis_errors','s2mm_axi_errors')):
                    raise RuntimeError('Camera/VDMA steady video errors')
                hardware_metrics=hardware.metrics()
                hdmi=hardware_metrics['ui_plane']['status']['video_count']
                if previous_hdmi is not None:
                    delta=(hdmi-previous_hdmi[0])&0xffffffff
                    if not 0<delta<0x80000000:raise RuntimeError('HDMI timing counter stopped or reset')
                    status['hdmi_timing_hz_including_repeated_camera_frames']=delta/(now-previous_hdmi[1])
                previous_hdmi=(hdmi,now)
                status.update(pipeline=live.metrics(),hardware=hardware_metrics,
                    hardware_rates=rates,hardware_statistics=current,video=health,
                    uptime_s=now-started)
                if exercise is not None:status['exercise']=exercise.metrics()
                publish()
                previous=current;next_status=now+2
            time.sleep(.005)
        if args.exercise and not exercise.done:raise RuntimeError('Exercise ended before all cases completed')
        if exercise is not None:status['exercise']=exercise.metrics()
    except BaseException as error:
        failed=True
        status.update(error=repr(error),traceback=traceback.format_exc())
        print(status['traceback'],flush=True)
    finally:
        def cleanup_publish(phase):
            try:publish(phase)
            except Exception as error:
                print('Cleanup status write failed: '+repr(error),flush=True)
        cleanup_publish('STOPPING')
        while True:
            try:
                if live is not None and not live.close():
                    raise RuntimeError('Application worker still owns resources')
                if live is not None:
                    try:status['pipeline']=live.metrics()
                    except Exception as error:status.setdefault('shutdown_telemetry_errors',[]).append(repr(error))
                if hardware is not None:
                    try:status['hardware']=hardware.metrics()
                    except Exception as error:status.setdefault('shutdown_telemetry_errors',[]).append(repr(error))
                    hardware.shutdown()
                    status['handoff']=getattr(hardware,'handoff',None)
                status['shutdown_verified']=True
                break
            except BaseException as error:
                status.update(shutdown_error=repr(error),shutdown_verified=False)
                cleanup_publish('RETAINED_OWNER_RETRY')
                time.sleep(2)
        if audio_reserved:
            guard=BACKEND/'scripts/usb_audio_hid_guard.py'
            with (output/'audio_restore.log').open('w') as log:
                subprocess.run([sys.executable,'-B',str(guard),'--restore'],stdout=log,stderr=subprocess.STDOUT)
        cleanup_publish('FAILED' if failed else 'STOPPED')
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
