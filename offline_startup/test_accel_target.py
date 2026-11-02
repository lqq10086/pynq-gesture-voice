import unittest
from pathlib import Path
from unittest import mock
import bridge


class TargetTests(unittest.TestCase):
    def test_defaults_preserve_previous_system(self):
        self.assertEqual(bridge.configured_path({}, "GESTURE_VOICE_CORE_ROOT", "/home/xilinx/jupyter_notebooks/V53_accel_town_20261030/V5.3-flash_PL01").as_posix(),
                         "/home/xilinx/jupyter_notebooks/V53_accel_town_20261030/V5.3-flash_PL01")
        self.assertEqual(bridge.configured_unit({}, "v45-redesign01.service"), "v45-redesign01.service")

    def test_custom_path_is_taken_literally_without_shell(self):
        raw = "/home/xilinx/demo ; $(touch BAD)/core"
        configured = bridge.configured_path({"GESTURE_VOICE_CORE_ROOT": raw}, "GESTURE_VOICE_CORE_ROOT", "/default")
        self.assertEqual(configured.as_posix(), raw)
        with mock.patch.object(bridge.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="MainPID=0\nActiveState=inactive\nSubState=dead\nControlGroup=\n")
            bridge.unit_info()
            args, kwargs = run.call_args
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)

    def test_invalid_paths_and_service_fail_closed(self):
        for value in ("", "relative/core", "/", "/home/../etc", "/home/core\nsecond"):
            with self.subTest(value=value):
                with self.assertRaises(bridge.Abort):
                    bridge.configured_path({"GESTURE_VOICE_CORE_ROOT": value}, "GESTURE_VOICE_CORE_ROOT", "/default")
        for unit in ("", "v45.service;touch bad", "../v45.service", "other.socket"):
            with self.subTest(unit=unit):
                with self.assertRaises(bridge.Abort):
                    bridge.configured_unit({"GESTURE_VOICE_PREVIOUS_UNIT": unit}, "v45-redesign01.service")

    def test_unit_has_environment_file_and_preserves_handoff_safety(self):
        unit = Path(__file__).with_name("v53-nobox.service").read_text()
        self.assertIn("EnvironmentFile=-/etc/default/gesture-voice", unit)
        self.assertIn("SendSIGKILL=no", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("clear_pl_statefile.service", unit)


if __name__ == "__main__":
    unittest.main()
