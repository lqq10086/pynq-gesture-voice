"""Use the existing verified hardware stop, then restore receiver HID buttons."""
import argparse
from pathlib import Path
import subprocess
import sys
from stop_apps import stop

ROOT=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    args=parser.parse_args()
    stop(args.output)
    guard=ROOT/'scripts/usb_audio_hid_guard.py'
    if guard.exists():
        subprocess.run([sys.executable,'-B',str(guard),'--restore'],check=True)

if __name__=='__main__':main()
