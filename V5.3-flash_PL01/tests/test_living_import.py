"""Exercise the imported drawing app through its isolated factory and UI plane."""
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from town_creator.layers import DisplayCompositor, OverlayPatch, PreparedUI
from town_creator.ui_plane import pack_ui_plane


ROOT = Path(__file__).resolve().parents[1]


class LivingImportTests(unittest.TestCase):
    def factory(self):
        try:
            return importlib.import_module('living_town').create_app
        except (ImportError, AttributeError) as error:
            self.fail('isolated living_town.create_app is unavailable: ' + str(error))

    def app(self, **kwargs):
        return self.factory()(1280, 720, **kwargs)

    def add_drawing(self, app, category, now=1.):
        # Same real fixture used by source tests/test_updated_town.py test_10.
        strokes = next(t.strokes for t in app.model.matcher.templates
                       if t.category == category)
        app._button('new', now)
        self.assertEqual(app.stage, 'edit')
        for stroke in strokes:
            self.assertTrue(app.model.begin_stroke(stroke[0]))
            for point in stroke[1:]:
                app.model.append_point(point)
            app.model.end_stroke()
        app._button('submit', now + .1)
        self.assertEqual(app.stage, 'scene')
        self.assertEqual(app.model.instances[-1].category, category)
        self.assertEqual(app.model.instances[-1].strokes, strokes)

    def test_factory_forces_packaged_assets_and_separate_work_file(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Path(folder)
            app = self.app(root=storage, work_path=storage / 'work.json')
            from living_town.updated_town import UpdatedTownApp
            self.assertIs(type(app), UpdatedTownApp)
            package = ROOT / 'living_town'
            self.assertEqual(app.root, package)
            self.assertEqual(app.renderer.root, package)
            self.assertEqual(app.model.rules.path, package / 'assets/town/templates.json')
            self.assertEqual(app.work_path, storage / 'living_work.json')
            self.assertFalse(app.work_path.exists())
            self.assertEqual(self.app(root=storage).work_path,
                             storage / 'works/living_work.json')
            self.assertEqual(self.app().work_path, ROOT / 'works/living_work.json')

    def test_real_strokes_submit_all_four_source_categories(self):
        app = self.app()
        for category in ('tree', 'house', 'car', 'cloud'):
            self.add_drawing(app, category)
        self.assertEqual(app.model.submit_count, 4)
        self.assertEqual(app.renderer._button_names(app),
                         ('close', 'new', 'copy', 'delete', 'land', 'road', 'play', 'replay'))

    def test_production_gesture_controller_preserves_real_paint_samples(self):
        app = self.app()
        app._button('new', .1)
        for index in range(4):
            app.observe((), 1. + .2 * index)
        for logical, now in (((50., 70.), 2.), ((80., 90.), 2.2)):
            point = app.renderer.logical_to_screen(logical)
            app.observe((dict(label='pinch_point', confidence=.95, center=point),), now)
        self.assertEqual(app.model.strokes, (((50., 70.), (80., 90.)),))
        before = app.model.strokes
        app.tick(2.3)
        self.assertEqual(app.model.strokes, before)
        self.assertEqual(app.model.match_count, 0)

    def test_prepared_ui_and_opaque_ink_pack_into_existing_plane(self):
        app = self.app()
        app._button('new', 1.)
        app.renderer.prepare(app, 1., 1)
        app.model.begin_stroke((100., 100.))
        app.model.append_point((250., 100.))
        app.model.end_stroke()
        packet = app.renderer.prepare(app, 1.1, 2)
        from living_town.layers import PreparedUI as ImportedPreparedUI
        self.assertIs(ImportedPreparedUI, PreparedUI)
        self.assertIs(type(packet), PreparedUI)
        self.assertTrue(all(type(p) is OverlayPatch for p in packet.patches))
        plane = np.empty((720, 1280, 8), np.uint8)
        pack_ui_plane(packet, 7, plane)
        x, y = map(int, app.renderer.logical_to_screen((175., 100.)))
        np.testing.assert_array_equal(plane[y, x], (15, 15, 15, 0, 0, 0, 7, 0))
        for color in ((255, 255, 255), (40, 120, 200)):
            video = np.full((720, 1280, 3), color, np.uint8)
            DisplayCompositor(1280, 720).compose_into(video, packet)
            np.testing.assert_array_equal(video[y, x], (15, 15, 15))
        self.assertEqual(app.model.match_count, 0)

    def test_actual_animation_prepares_changed_scene_without_inference_imports(self):
        loaded = set(sys.modules)
        app = self.app()
        for category in ('house', 'car', 'cloud'):
            self.add_drawing(app, category)
        original = app.model.snapshot()
        app._button('play', 10.)
        app.tick(10.1)
        first = app.renderer.prepare(app, 10.1, 1)
        first_plane = np.empty((720, 1280, 8), np.uint8)
        pack_ui_plane(first, 3, first_plane)
        app.tick(11.)
        second = app.renderer.prepare(app, 11., 2)
        second_plane = np.empty_like(first_plane)
        pack_ui_plane(second, 3, second_plane)
        self.assertTrue(app.animation.playing)
        self.assertGreater(app.animation.visual_time, .1)
        self.assertFalse(np.array_equal(first_plane[:, :, :6], second_plane[:, :, :6]))
        self.assertEqual(app.model.snapshot(), original)
        forbidden = ('torch', 'yolov5', 'inference', 'live_runtime', 'cv2.videoio')
        self.assertFalse([name for name in set(sys.modules) - loaded
                          if any(name == prefix or name.startswith(prefix + '.')
                                 for prefix in forbidden)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
