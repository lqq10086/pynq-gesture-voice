import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
CORE = PACKAGE / 'V5.3-flash_PL01'
sys.path.insert(0, str(CORE))

from public_accel_preflight import REPORT, EXPECTED, bound_routed_files, verify_static
from hardware_scripts.route_gate import check_route
from board_identity import experimental_bound_files, verify_experimental
import board_entry


class PublicPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / CORE.name
        shutil.copytree(CORE, cls.root)
        cls.report = cls.root / REPORT

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_public_candidate_without_dcp_has_explicit_scope(self):
        result = verify_static(self.root)
        self.assertEqual(result['dcp_validation'], 'NOT_INCLUDED')
        self.assertEqual(result['route_validation'], 'REPORTS_CHECKED_DCP_NOT_CHECKED')
        self.assertFalse(result['board_qualified'])
        identity = verify_experimental(self.root)
        self.assertFalse(identity['production_qualified'])
        self.assertEqual(identity['dcp_validation'], 'NOT_INCLUDED')
        self.assertEqual(identity['route_validation'], 'REPORTS_CHECKED_DCP_NOT_CHECKED')

    def test_default_route_gate_still_requires_dcp(self):
        with self.assertRaisesRegex(ValueError, 'final_routed.dcp'):
            check_route(self.report, self.report / 'baseline_cdc_unwaived.rpt')

    def test_changed_model_is_rejected(self):
        self.assert_rejected_after_change('models/gesture_best.onnx')

    def test_changed_bitstream_is_rejected(self):
        self.assert_rejected_after_change(REPORT + '/flash_pl01.bit')

    def test_changed_hwh_is_rejected(self):
        self.assert_rejected_after_change(REPORT + '/flash_pl01.hwh')

    def test_changed_route_report_is_rejected(self):
        self.assert_rejected_after_change(REPORT + '/timing_summary.rpt')

    def test_missing_route_report_is_rejected(self):
        path = self.report / 'camera_input_setup.rpt'
        backup = path.read_bytes()
        try:
            path.unlink()
            with self.assertRaises((ValueError, FileNotFoundError)):
                verify_static(self.root)
        finally:
            path.write_bytes(backup)

    def test_unexpected_dcp_is_rejected(self):
        path = self.report / 'final_routed.dcp'
        try:
            path.write_bytes(b'not the qualified checkpoint')
            with self.assertRaises(ValueError):
                verify_static(self.root)
        finally:
            path.unlink(missing_ok=True)

    def test_changed_runtime_identity_is_rejected(self):
        self.assert_rejected_after_change('backend_v51/public_accel_runtime_identity.json')

    def test_report_file_list_cannot_escape_report_directory(self):
        path = self.report / 'build_result.json'
        backup = path.read_bytes()
        try:
            record = json.loads(backup)
            record['route']['artifact_sha256']['..'] = '0' * 64
            changed = json.dumps(record).encode()
            path.write_bytes(changed)
            with patch.dict(EXPECTED, {REPORT + '/build_result.json': hashlib.sha256(changed).hexdigest()}):
                with self.assertRaises(ValueError):
                    bound_routed_files(self.root)
        finally:
            path.write_bytes(backup)

    def test_present_dcp_is_bound_to_running_identity(self):
        path = self.report / 'final_routed.dcp'
        try:
            path.write_bytes(b'present checkpoint')
            self.assertIn(path, set(experimental_bound_files(self.root)))
        finally:
            path.unlink(missing_ok=True)

    def test_checked_route_reports_are_bound_to_running_identity(self):
        files = set(experimental_bound_files(self.root))
        self.assertIn(self.report / 'timing_summary.rpt', files)
        self.assertIn(self.report / 'camera_input_setup.rpt', files)
        self.assertIn(self.report / 'cdc_unwaived_view.rpt', files)
        self.assertNotIn(self.report / 'final_routed.dcp', files)

    def test_identity_rejects_files_appearing_during_verification(self):
        first = self.report / 'timing_summary.rpt'
        added = self.report / 'camera_input_setup.rpt'
        with patch.object(board_entry, 'ROOT', self.root), \
             patch('board_identity.experimental_bound_files', side_effect=[[first], [first, added]]), \
             patch('board_identity.verify_experimental', return_value={'scope': 'STATIC'}):
            with self.assertRaisesRegex(RuntimeError, 'Bound'):
                board_entry.checked_identity()

    def test_identity_checks_bound_file_changes_after_start(self):
        path = self.report / 'timing_summary.rpt'
        backup = path.read_bytes()
        try:
            with patch.object(board_entry, 'ROOT', self.root), \
                 patch('board_identity.experimental_bound_files', return_value=[path]), \
                 patch('board_identity.verify_experimental', return_value={'scope': 'STATIC'}):
                current = board_entry.checked_identity()
                self.assertEqual(current()['scope'], 'STATIC')
                path.write_bytes(backup + b'changed')
                with self.assertRaisesRegex(RuntimeError, 'Bound'):
                    current()
        finally:
            path.write_bytes(backup)

    def assert_rejected_after_change(self, name):
        path = self.root / name
        backup = path.read_bytes()
        try:
            path.write_bytes(backup + b'corruption')
            with self.assertRaises((ValueError, FileNotFoundError)):
                verify_static(self.root)
        finally:
            path.write_bytes(backup)


if __name__ == '__main__':
    unittest.main()
