"""Isolated public-audio checks; no sound device or board is accessed."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import board_entry

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'public_usb_audio_hid_guard', ROOT / 'backend_v51/scripts/usb_audio_hid_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
LOADER_SPEC = importlib.util.spec_from_file_location(
    'public_usb_audio_check', ROOT / 'backend_v51/scripts/load_usb_audio_board.py')
loader = importlib.util.module_from_spec(LOADER_SPEC)
LOADER_SPEC.loader.exec_module(loader)


class FakeDevices:
    """Map Linux sysfs interface names to portable temporary file names."""
    def __init__(self, root):
        self.root = root

    def iterdir(self):
        return self.root.iterdir()

    def __truediv__(self, name):
        return self.root / name.replace(':', '__')


class SystemAudioSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cards = self.root / 'cards'
        self.cards.write_text('0 [WirelessMic]: USB microphone\n', encoding='utf-8')
        self.real_path = Path
        self.config = {'voice': {'enabled': True}}
        self.commands = []

    def path_for_test(self, name):
        return self.cards if str(name) == '/proc/asound/cards' else self.real_path(name)

    def run_for_test(self, args, **kwargs):
        self.commands.append(args)
        return SimpleNamespace(returncode=0)

    def test_missing_system_arecord_disables_voice_before_device_actions(self):
        with patch('shutil.which', return_value=None), \
             patch.object(board_entry, 'Path', side_effect=self.path_for_test), \
             patch.object(board_entry.subprocess, 'run', side_effect=self.run_for_test):
            status = board_entry.audio_setup(self.config, self.root)
        self.assertEqual(status, 'UNAVAILABLE_ARECORD_NOT_FOUND')
        self.assertFalse(self.config['voice']['enabled'])
        self.assertEqual(self.commands, [])

    def test_system_audio_does_not_load_private_bundled_modules(self):
        with patch('shutil.which', return_value='/usr/bin/arecord'), \
             patch.object(board_entry, 'Path', side_effect=self.path_for_test), \
             patch.object(board_entry.subprocess, 'run', side_effect=self.run_for_test):
            status = board_entry.audio_setup(self.config, self.root)
        self.assertEqual(status, 'ENUMERATED_CAPTURE_PENDING')
        self.assertTrue(self.config['voice']['enabled'])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(Path(self.commands[0][2]).name, 'usb_audio_hid_guard.py')

    def test_legacy_loader_checks_system_dependencies_without_loading_modules(self):
        with patch('shutil.which', return_value='/usr/bin/arecord'), \
             patch.object(loader, 'Path', side_effect=self.path_for_test), \
             patch('subprocess.run', side_effect=AssertionError('must not load modules')):
            self.assertEqual(loader.main(), 0)

    def test_legacy_loader_reports_missing_arecord(self):
        with patch('shutil.which', return_value=None):
            self.assertEqual(loader.main(), 1)


class HidIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.devices = self.root / 'devices'
        self.driver = self.root / 'usbhid'
        self.devices.mkdir()
        self.driver.mkdir()
        (self.driver / 'bind').write_text('not bound', encoding='utf-8')
        (self.driver / 'unbind').write_text('not unbound', encoding='utf-8')
        self.state = self.root / 'state.json'
        self.patches = [patch.object(guard, 'DEVICES', FakeDevices(self.devices)),
                        patch.object(guard, 'DRIVER', self.driver),
                        patch.object(guard, 'STATE', self.state),
                        patch.object(guard, 'IDENTITY', {'idVendor': '1111', 'idProduct': '2222', 'serial': 'first'})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def device(self, name, serial):
        device = self.devices / name
        device.mkdir()
        for key, value in [('idVendor', '1111'), ('idProduct', '2222'), ('serial', serial)]:
            (device / key).write_text(value, encoding='utf-8')
        interface = self.devices / (name + '__1.2')
        interface.mkdir()
        (interface / 'bInterfaceClass').write_text('03', encoding='utf-8')
        (interface / 'bInterfaceNumber').write_text('02', encoding='utf-8')
        return interface

    def test_no_serial_never_unbinds_a_matching_receiver(self):
        self.device('1-1', 'first')
        with patch.dict(os.environ, {}, clear=True), patch.object(guard, 'driver_name', return_value='usbhid'):
            result = guard.suppress()
        self.assertEqual(result['status'], 'SKIPPED_SERIAL_NOT_CONFIGURED')
        self.assertEqual((self.driver / 'unbind').read_text(encoding='utf-8'), 'not unbound')

    def test_configured_serial_selects_only_its_own_receiver(self):
        self.device('1-1', 'first')
        expected = self.device('1-2', 'selected')
        with patch.dict(os.environ, {'V53_USB_AUDIO_SERIAL': 'selected'}, clear=True), \
             patch.object(guard, 'driver_name', return_value='usbhid'):
            self.assertEqual(guard.find_interface(), expected)
            result = guard.suppress()
        self.assertTrue(result['owned'])
        self.assertEqual((self.driver / 'unbind').read_text(encoding='utf-8'), expected.name)

    def test_unknown_serial_does_not_unbind_another_receiver(self):
        self.device('1-1', 'first')
        with patch.dict(os.environ, {'V53_USB_AUDIO_SERIAL': 'different'}, clear=True), \
             patch.object(guard, 'driver_name', return_value='usbhid'):
            result = guard.suppress()
        self.assertEqual(result['status'], 'RECEIVER_ABSENT')
        self.assertEqual((self.driver / 'unbind').read_text(encoding='utf-8'), 'not unbound')

    def test_restore_uses_owned_record_even_when_environment_is_gone(self):
        expected = self.device('1-2', 'selected')
        self.state.write_text(json.dumps({'owned': True, 'interface': expected.name,
            'identity': {'idVendor': '1111', 'idProduct': '2222', 'serial': 'selected'}}), encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True), patch.object(guard, 'driver_name', return_value=None):
            result = guard.restore()
        self.assertEqual(result['status'], 'HID_RESTORED')
        self.assertEqual((self.driver / 'bind').read_text(encoding='utf-8'), expected.name)

    def test_missing_serial_preserves_an_existing_owned_detach(self):
        identity = {'idVendor': '1111', 'idProduct': '2222', 'serial': 'selected'}
        self.state.write_text(json.dumps({'owned': True, 'interface': '1-2:1.2',
                                         'identity': identity}), encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True):
            result = guard.suppress()
        self.assertTrue(result['owned'])
        self.assertEqual(result['identity'], identity)
        self.assertEqual(result['interface'], '1-2:1.2')


if __name__ == '__main__':
    unittest.main()
