import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'work_sample' / 'board' / 'board_smoke.py'
spec = importlib.util.spec_from_file_location('board_smoke_under_test', SCRIPT)
board_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board_smoke)


class BoardSmokeTests(unittest.TestCase):
    def run_smoke(self, actual_address):
        fake_pynq = types.ModuleType('pynq')
        fake_pynq.Overlay = lambda _bit: types.SimpleNamespace(ip_dict={
            'example_ip': {'phys_addr': actual_address, 'registers': {}}
        })
        with tempfile.TemporaryDirectory() as tmp:
            bit = Path(tmp) / 'fixture.bit'
            bit.touch()
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {'pynq': fake_pynq}), \
                 mock.patch.object(board_smoke, 'EXPECTED_IP', {'example_ip': '0x43c00000'}), \
                 contextlib.redirect_stdout(output):
                result = board_smoke.main(['--bit', str(bit)])
        return result, output.getvalue()

    def test_matching_address_succeeds(self):
        result, output = self.run_smoke(0x43c00000)
        self.assertEqual(result, 0)
        self.assertIn('RESULT: SMOKE_OK', output)

    def test_mismatched_address_fails_without_success_result(self):
        result, output = self.run_smoke(0x43c10000)
        self.assertEqual(result, 2)
        self.assertIn('MISMATCH', output)
        self.assertNotIn('RESULT: SMOKE_OK', output)

    def test_empty_contract_stops_before_loading_bitstream(self):
        fake_pynq = types.ModuleType('pynq')
        fake_pynq.Overlay = mock.Mock(return_value=types.SimpleNamespace(ip_dict={}))
        with tempfile.TemporaryDirectory() as tmp:
            bit = Path(tmp) / 'fixture.bit'
            bit.touch()
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {'pynq': fake_pynq}), \
                 mock.patch.object(board_smoke, 'EXPECTED_IP', {}), \
                 contextlib.redirect_stdout(output):
                result = board_smoke.main(['--bit', str(bit)])
        self.assertEqual(result, 3)
        self.assertIn('EXPECTED_IP', output.getvalue())
        self.assertNotIn('RESULT: SMOKE_OK', output.getvalue())
        fake_pynq.Overlay.assert_not_called()


if __name__ == '__main__':
    unittest.main()
