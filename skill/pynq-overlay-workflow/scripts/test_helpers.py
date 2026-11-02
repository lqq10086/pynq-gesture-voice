"""Isolated behavioral tests; synthetic artifacts and mock PYNQ, never a live board."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from overlay_contract import load_contract
from probe_overlay import inspect


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'overlay').mkdir()
        self.bit = self.root / 'overlay/design.bit'
        self.hwh = self.bit.with_suffix('.hwh')
        self.bit.write_bytes(b'SYNTHETIC_NOT_A_BITSTREAM')
        self.hwh.write_text('<SYNTHETIC/>', encoding='utf-8')
        self.cfg = {'schema_version': 1, 'target_label': 'synthetic-a', 'build_id': 'test-build',
                    'overlay': {'bit': 'overlay/design.bit', 'hwh': 'overlay/design.hwh',
                                'bit_sha256': hashlib.sha256(self.bit.read_bytes()).hexdigest(),
                                'hwh_sha256': hashlib.sha256(self.hwh.read_bytes()).hexdigest()},
                    'ips': [{'name': 'sensor/control', 'type': 'test:ip:control:1.0',
                             'phys_addr': '0x40000000', 'addr_range': 4096,
                             'evidence': 'synthetic fixture',
                             'safe_reads': [{'name': 'ID', 'offset': 4, 'mask': '0xff',
                                             'expected': 7, 'side_effect': 'none',
                                             'evidence': 'synthetic read-only ID'}]}]}
        self.path = self.root / 'contract.json'

    def tearDown(self):
        self.temp.cleanup()

    def contract(self):
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')
        return load_contract(self.path)

    def run_probe(self, loaded=True, read=False, load=False, mutate=None, value=7):
        cfg, files, _ = self.contract()
        metadata = {ip['name']: {k: ip[k] for k in ('type', 'phys_addr', 'addr_range')} for ip in cfg['ips']}
        if mutate:
            mutate(metadata)
        calls = []

        class Overlay:
            def __init__(self, path, download):
                calls.append(('overlay', download))
                self.ip_dict = metadata
            def is_loaded(self):
                return loaded

        class MMIO:
            def __init__(self, base, length):
                calls.append(('mmio', base, length))
            def read(self, offset):
                calls.append(('read', offset))
                return value

        return inspect(cfg, files, Overlay, MMIO, load, read), calls

    def test_offline_valid(self):
        self.assertEqual(self.contract()[0]['ips'][0]['phys_addr'], 0x40000000)

    def test_tampered_artifact(self):
        self.bit.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.contract()

    def test_missing_hash(self):
        self.cfg['overlay']['bit_sha256'] = None
        with self.assertRaises(ValueError):
            self.contract()

    def test_path_escape(self):
        self.cfg['overlay']['bit'] = '../outside.bit'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.contract()

    def test_windows_absolute_path_rejected(self):
        self.cfg['overlay']['bit'] = 'C:/outside.bit'
        with self.assertRaises(ValueError):
            self.contract()

    def test_bool_not_address(self):
        self.cfg['ips'][0]['phys_addr'] = True
        with self.assertRaises(ValueError):
            self.contract()

    def test_bad_offset(self):
        for offset in (1, 4096):
            self.cfg['ips'][0]['safe_reads'][0]['offset'] = offset
            with self.assertRaises(ValueError):
                self.contract()

    def test_side_effect_read_rejected(self):
        self.cfg['ips'][0]['safe_reads'][0]['side_effect'] = 'read-to-clear'
        with self.assertRaises(ValueError):
            self.contract()

    def test_mask_constraint(self):
        self.cfg['ips'][0]['safe_reads'][0]['expected'] = 256
        with self.assertRaises(ValueError):
            self.contract()

    def test_metadata_default_no_mmio_or_download(self):
        report, calls = self.run_probe()
        self.assertEqual(calls, [('overlay', False)])
        self.assertEqual(report['identity_read'], 'NOT_RUN')

    def test_not_loaded_no_mmio(self):
        report, calls = self.run_probe(loaded=False, read=True)
        self.assertEqual(report['status'], 'NOT_RUN')
        self.assertEqual(calls, [('overlay', False)])

    def test_wrong_metadata_blocks_mmio(self):
        def wrong(metadata):
            metadata['sensor/control']['phys_addr'] = 0
        with self.assertRaisesRegex(ValueError, 'HWH mismatch'):
            self.run_probe(read=True, mutate=wrong)

    def test_selected_read_only(self):
        report, calls = self.run_probe(read=True)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(calls, [('overlay', False), ('mmio', 0x40000000, 4096), ('read', 4)])

    def test_identity_mismatch_fails(self):
        self.assertEqual(self.run_probe(read=True, value=8)[0]['status'], 'FAIL')

    def test_identity_mismatch_stops_further_reads(self):
        extra = deepcopy(self.cfg['ips'][0]['safe_reads'][0])
        extra.update(name='SECOND', offset=8)
        self.cfg['ips'][0]['safe_reads'].append(extra)
        report, calls = self.run_probe(read=True, value=8)
        self.assertEqual(report['status'], 'FAIL')
        self.assertNotIn(('read', 8), calls)

    def test_explicit_load(self):
        self.assertEqual(self.run_probe(load=True)[1], [('overlay', True)])
        with self.assertRaises(RuntimeError):
            self.run_probe(load=True, loaded=False)

    def test_alternative_layout(self):
        ip = self.cfg['ips'][0]
        ip.update(name='measurements_0', phys_addr='0xA1000000', addr_range=65536)
        ip['safe_reads'][0]['offset'] = 64
        report, calls = self.run_probe(read=True)
        self.assertEqual(report['status'], 'PASS')
        self.assertIn(('mmio', 0xA1000000, 65536), calls)
        self.assertIn(('read', 64), calls)

    def test_second_ip_rejected_before_any_mmio(self):
        ip = deepcopy(self.cfg['ips'][0]); ip['name'] = 'second'; ip['phys_addr'] = '0x50000000'
        self.cfg['ips'].append(ip)
        def wrong(metadata):
            metadata.pop('second')
        with self.assertRaisesRegex(ValueError, 'IP absent'):
            self.run_probe(read=True, mutate=wrong)

    def test_offline_cli_scope(self):
        self.contract()
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('check_overlay.py')), str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['board_test'], 'NOT_RUN')


if __name__ == '__main__':
    unittest.main(verbosity=2)
