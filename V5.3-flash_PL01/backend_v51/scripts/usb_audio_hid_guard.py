"""Temporarily release this receiver's HID bandwidth while voice capture is active.

Only vendor/product 3634:4155, an explicitly configured serial, and HID
interface 2 are eligible. USB audio remains bound while HID is suppressed.
"""
import argparse
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DEVICES=Path('/sys/bus/usb/devices')
DRIVER=Path('/sys/bus/usb/drivers/usbhid')
STATE=ROOT/'reports/usb_audio_hid_state.json'
IDENTITY={'idVendor':'3634','idProduct':'4155'}
SERIAL_ENV='V53_USB_AUDIO_SERIAL'


def configured_identity():
    serial=os.environ.get(SERIAL_ENV,'').strip()
    return dict(IDENTITY,serial=serial) if serial else None


def find_interface(identity=None):
    identity=identity or configured_identity()
    if not identity:return None
    matches=[]
    for device in DEVICES.iterdir():
        if ':' in device.name: continue
        try:
            if any((device/key).read_text().strip()!=value for key,value in identity.items()): continue
            interface=DEVICES/(device.name+':1.2')
            if ((interface/'bInterfaceClass').read_text().strip()=='03' and
                    (interface/'bInterfaceNumber').read_text().strip()=='02'):
                matches.append(interface)
        except FileNotFoundError: continue
    if len(matches)>1: raise RuntimeError('Duplicate receiver identity; HID state unchanged')
    return matches[0] if matches else None


def driver_name(interface):
    driver=interface/'driver'
    return driver.resolve().name if driver.exists() else None


def save(status,owned=False,interface=None,identity=None):
    result=dict(status=status,owned=owned,identity=identity or IDENTITY,interface=interface,
                limitation='Receiver HID buttons unavailable during voice capture; audio interfaces remain bound')
    STATE.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(result,indent=2))
    return result


def suppress():
    identity=configured_identity()
    previous=json.loads(STATE.read_text()) if STATE.exists() else {}
    if previous.get('owned') and previous.get('identity')!=identity:
        return save('PENDING_OWNED_DETACH',owned=True,interface=previous.get('interface'),identity=previous.get('identity'))
    if identity is None: return save('SKIPPED_SERIAL_NOT_CONFIGURED')
    interface=find_interface(identity)
    if interface is None:
        return save('RECEIVER_ABSENT',owned=bool(previous.get('owned')),
                    interface=previous.get('interface'),identity=identity)
    driver=driver_name(interface)
    if driver not in (None,'usbhid'): raise RuntimeError('Unexpected HID driver; unchanged')
    owned=bool(previous.get('owned') and previous.get('interface')==interface.name and
               previous.get('identity')==identity)
    if driver=='usbhid':
        (DRIVER/'unbind').write_text(interface.name)
        owned=True
    return save('HID_SUPPRESSED_FOR_VOICE',owned,interface.name,identity)


def restore():
    previous=json.loads(STATE.read_text()) if STATE.exists() else {}
    if not previous.get('owned'): return save('NO_OWNED_DETACH')
    identity=previous.get('identity')
    if (not isinstance(identity,dict) or not isinstance(identity.get('serial'),str) or
            not identity['serial'].strip() or
            any(identity.get(key)!=IDENTITY[key] for key in ('idVendor','idProduct'))):
        return save('OWNED_IDENTITY_INVALID',owned=True,interface=previous.get('interface'),identity=identity)
    interface=find_interface(identity)
    if interface is None:
        return save('RECEIVER_ABSENT',owned=True,interface=previous.get('interface'),identity=identity)
    if interface.name!=previous.get('interface'):
        return save('TOPOLOGY_CHANGED_NO_REBIND',owned=True,interface=previous.get('interface'),identity=identity)
    driver=driver_name(interface)
    if driver not in (None,'usbhid'): raise RuntimeError('Unexpected HID driver; unchanged')
    if driver is None: (DRIVER/'bind').write_text(interface.name)
    return save('HID_RESTORED',interface=interface.name,identity=identity)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--suppress',action='store_true')
    action.add_argument('--restore',action='store_true')
    args=parser.parse_args()
    print(json.dumps(suppress() if args.suppress else restore(),indent=2))
