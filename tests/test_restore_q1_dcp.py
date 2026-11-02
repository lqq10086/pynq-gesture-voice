import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'restore_q1_dcp.py'
SPEC = importlib.util.spec_from_file_location('restore_q1_dcp', SCRIPT)
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


class RestoreDcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.payload = b'test checkpoint data'
        self.patches = [
            patch.object(restore, 'PROJECT_ROOT', self.root),
            patch.object(restore, 'EXPECTED_SHA256', hashlib.sha256(self.payload).hexdigest()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temp.cleanup)
        self.destination = self.root / restore.DESTINATION_RELATIVE
        self.destination.parent.mkdir(parents=True)

    def raw_source(self, contents):
        source = Path(self.temp.name) / 'source.dcp'
        source.write_bytes(contents)
        return source

    def test_restore_from_original_dcp(self):
        self.assertEqual(restore.restore(self.raw_source(self.payload)), 'restored')
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_restore_from_original_zip(self):
        source = Path(self.temp.name) / 'original.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr(restore.ORIGINAL_ZIP_MEMBER, self.payload)
        self.assertEqual(restore.restore(source), 'restored')
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_wrong_input_is_rejected_before_destination_is_created(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            restore.restore(self.raw_source(b'wrong data'))
        self.assertFalse(self.destination.exists())

    def test_existing_different_destination_is_never_overwritten(self):
        self.destination.write_bytes(b'do not overwrite')
        with self.assertRaises(FileExistsError):
            restore.restore(self.raw_source(self.payload))
        self.assertEqual(self.destination.read_bytes(), b'do not overwrite')

    def test_existing_matching_destination_is_idempotent(self):
        self.destination.write_bytes(self.payload)
        self.assertEqual(restore.restore(self.raw_source(self.payload)), 'already present')
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_zip_must_contain_exactly_one_expected_member(self):
        source = Path(self.temp.name) / 'bad.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('other/final_routed.dcp', self.payload)
        with self.assertRaisesRegex(ValueError, 'ZIP'):
            restore.restore(source)
        self.assertFalse(self.destination.exists())

    def test_missing_destination_directory_is_not_created(self):
        self.destination.parent.rmdir()
        with self.assertRaises(FileNotFoundError):
            restore.restore(self.raw_source(self.payload))
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
