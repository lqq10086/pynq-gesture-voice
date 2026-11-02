import ast, unittest
from pathlib import Path
from unittest.mock import patch
import board_entry

class EntryBindingTest(unittest.TestCase):
    def test_normal_entry_wires_menu_worker_without_detection_boxes(self):
        text=Path(board_entry.__file__).read_text(encoding="utf-8")
        tree=ast.parse(text)
        self.assertTrue(any(isinstance(n,ast.FunctionDef) and n.name=="create_ui_worker" for n in ast.walk(tree)))
        self.assertNotIn("hardware.poll_detection_overlay(",text)
        source=ast.unparse(tree)
        self.assertIn("app_factory=create_app",source)
        self.assertIn("ui_worker = create_ui_worker(",source)
        self.assertIn("ui_worker=ui_worker",source)

