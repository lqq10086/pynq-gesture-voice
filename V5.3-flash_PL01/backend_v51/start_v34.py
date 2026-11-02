"""Prepare project-local USB audio, then use the unchanged safe application launcher."""
from pathlib import Path
import argparse
import subprocess
import sys

ROOT=Path(__file__).resolve().parent

def main():
    arguments=sys.argv[1:]
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--voice-mode',choices=('off','exclusive','gated'),default='exclusive')
    parser.add_argument('--display-path',choices=('pl-camera',),default='pl-camera')
    settings,_=parser.parse_known_args(arguments)
    if '--display-path' not in arguments and not any(a.startswith('--display-path=') for a in arguments):
        arguments=arguments+['--display-path',settings.display_path]
    voice_off=settings.voice_mode=='off'
    if not voice_off and not any(a in ('-h','--help') for a in arguments):
        from start_apps import preflight_owner
        preflight_owner()
        loader=ROOT/'scripts/load_usb_audio_board.py'
        if loader.exists():
            result=subprocess.run([sys.executable,'-B',str(loader)])
            if result.returncode:
                print('USB_AUDIO_UNAVAILABLE: starting gesture/video with safe voice fallback',flush=True)
            else:
                guard=ROOT/'scripts/usb_audio_hid_guard.py'
                if guard.exists():
                    result=subprocess.run([sys.executable,'-B',str(guard),'--suppress'])
                    if result.returncode:
                        print('USB_HID_RESERVATION_FAILED: voice may be unavailable',flush=True)
    code=subprocess.call([sys.executable,'-B',str(ROOT/'start_apps.py')]+arguments)
    if code or any(a in ('-h','--help') for a in arguments):
        return code
    import json
    latest=json.loads((ROOT/'reports/latest_run.json').read_text())
    if latest.get('status')=='RUNNING_HEALTH_VERIFIED':
        from runtime.wb_profile import apply_accepted_profile
        try:
            print(json.dumps(apply_accepted_profile(ROOT)),flush=True)
        except BaseException:
            # A profile failure cannot leave a new, falsely approved run active.
            # Existing stop verifies ownership and retains resources until idle.
            subprocess.run([sys.executable,'-B',str(ROOT/'stop_v34.py')],check=True)
            raise
    return code

if __name__=='__main__':raise SystemExit(main())
