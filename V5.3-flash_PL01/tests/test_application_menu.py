import importlib
import importlib.util
from pathlib import Path
import sys
import time
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spatial_build.runtime import InferenceResult
from town_creator.layers import DisplayCompositor, PreparedUI


def pinch(point):
    return (dict(label='pinch_point', confidence=.95, center=point),)


def result(sequence, epoch, captured_at, detections=()):
    return InferenceResult(sequence, tuple(detections), (720, 1280, 3),
                           captured_at, captured_at, 0., None, sequence,
                           captured_at, 0., mode_epoch=epoch)


def center(rect):
    return ((rect[0]+rect[2])/2., (rect[1]+rect[3])/2.)


class ApplicationMenuTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('application_menu'),
                             'the shared four-display selection page is missing')
        self.factory = importlib.import_module('application_menu').create_app
        self.app = self.factory(1280, 720, root=ROOT)

    def test_four_cards_directly_enter_original_spatial_levels_or_town(self):
        from combined.serial_app import SerialApp
        self.assertEqual(set(self.app.layout),
                         {'close', 'display_1', 'display_2', 'display_3', 'display_4'})
        for number, level_id in ((1, 'intro'), (2, 'steps'), (3, 'notch')):
            with self.subTest(number=number):
                app = self.factory(1280, 720, root=ROOT)
                self.assertEqual(app.hit_test(center(app.layout['display_%s' % number])),
                                 ('button', 'display_%s' % number))
                app._button('display_%s' % number, time.perf_counter())
                self.assertIsInstance(app.active, SerialApp)
                self.assertEqual(app.stage, 'build')
                self.assertEqual(app.active.spatial.level.id, level_id)

    def test_real_card_pinch_directly_opens_original_construction(self):
        from combined.serial_app import SerialApp
        self.assertEqual(self.app.stage, 'menu')
        self.assertIsNone(self.app.active)
        point = center(self.app.layout['display_1'])
        self.assertEqual(self.app.hit_test(point), ('button', 'display_1'))
        now = time.perf_counter()
        self.app.observe(pinch(point), now)
        self.app.observe(pinch(point), now+.2)
        self.assertEqual(self.app.active_application, 'spatial')
        self.assertIsInstance(self.app.active, SerialApp)
        self.assertEqual(self.app.stage, 'build')
        self.assertEqual(self.app.active.spatial.level.id, 'intro')

    def test_original_build_check_observe_back_and_outer_close(self):
        self.app._button('display_1', time.perf_counter())
        self.assertEqual(self.app.stage, 'build')
        board = self.app.active.spatial.board
        for x, y, z in sorted(self.app.active.spatial.level.coords, key=lambda p: p[2]):
            self.assertIsNotNone(board.add(x, y))
        self.app._button('check', time.perf_counter())
        self.assertEqual(self.app.active_module, 'observe')
        self.assertEqual(self.app.stage, 'observe')
        self.assertFalse(self.app.inference_context[1])
        now = time.perf_counter()
        self.app.renderer.prepare(self.app, now, 31)
        scene = self.app.renderer.prepared_scene
        self.assertIsNotNone(scene)
        self.assertFalse(self.app.set_displayed_scene(scene, 31, now, (-1, None)))
        self.assertTrue(self.app.set_displayed_scene(scene, 31, now, self.app.mode_revision))
        self.assertIs(self.app.active.spatial.displayed_scene, scene)
        self.assertTrue(self.app.go_back())
        self.assertEqual(self.app.stage, 'menu')
        self.assertFalse(self.app.quit_requested)
        self.app._button('close', time.perf_counter())
        self.assertTrue(self.app.quit_requested)

    def test_build_close_or_back_returns_to_same_four_cards(self):
        for action in ('close', 'back'):
            with self.subTest(action=action):
                self.app._button('display_2', time.perf_counter())
                if action == 'close':
                    self.app._button('close', time.perf_counter())
                else:
                    self.app.go_back()
                self.assertEqual(self.app.stage, 'menu')
                self.assertIsNone(self.app.active)
                self.assertFalse(self.app.quit_requested)
                self.assertIn('display_4', self.app.layout)

    def test_entry_requires_fresh_release_before_place_toggle(self):
        self.app._button('display_1', time.perf_counter())
        point = center(self.app.active.spatial.layout['palette'])
        now = time.perf_counter()
        self.app.observe(pinch(point), now)
        self.app.observe(pinch(point), now+.3)
        self.assertFalse(self.app.active.spatial.placement_enabled)
        self.app.observe((), now+.4)
        self.app.observe(pinch(point), now+.5)
        self.app.observe(pinch(point), now+.7)
        self.assertTrue(self.app.active.spatial.placement_enabled)

    def test_old_capture_epoch_and_duplicate_sequence_cannot_act(self):
        old_epoch = self.app.module_epoch
        old_time = time.perf_counter()
        self.app._button('display_1', time.perf_counter())
        self.app.observe_inference(result(1, self.app.module_epoch, old_time), time.perf_counter())
        self.assertTrue(self.app.metrics['entry_pinch_release_required'])
        fresh = time.perf_counter()
        self.app.observe_inference(result(2, old_epoch, fresh), fresh)
        self.assertTrue(self.app.metrics['entry_pinch_release_required'])
        self.app.observe_inference(result(3, self.app.module_epoch, fresh), fresh)
        self.assertFalse(self.app.metrics['entry_pinch_release_required'])
        point = center(self.app.active.spatial.layout['palette'])
        self.app.observe_inference(result(4, self.app.module_epoch, fresh+.1, pinch(point)), fresh+.1)
        self.app.observe_inference(result(4, self.app.module_epoch, fresh+.4, pinch(point)), fresh+.4)
        self.assertFalse(self.app.active.spatial.placement_enabled)
        self.app.observe_inference(result(5, self.app.module_epoch, fresh+.5, pinch(point)), fresh+.5)
        self.assertTrue(self.app.active.spatial.placement_enabled)

    def test_voice_timestamp_boundary_and_original_commands(self):
        now = time.monotonic()-.05
        payload = dict(label='back', clock='monotonic', created_at=now,
                       utterance_ended_at=now, expires_at=now+10.)
        self.app._button('display_1', time.perf_counter())
        self.assertFalse(self.app.accepts_voice(payload))
        now = time.monotonic()
        payload.update(created_at=now, utterance_ended_at=now)
        self.assertTrue(self.app.accepts_voice(payload))
        self.assertTrue(self.app.apply_voice_command(payload['label']))
        self.assertEqual(self.app.stage, 'menu')
        self.assertTrue(self.app.apply_voice_command('two'))
        self.assertEqual(self.app.stage, 'build')
        self.assertEqual(self.app.active.spatial.level.id, 'steps')
        self.assertTrue(self.app.apply_voice_command('back'))
        self.assertEqual(self.app.stage, 'menu')
        self.assertTrue(self.app.apply_voice_command('four'))
        self.assertEqual(self.app.active_application, 'town')

    def test_town_off_returns_directly_to_menu(self):
        self.assertTrue(self.app.apply_voice_command('four'))
        self.assertTrue(self.app.apply_voice_command('off'))
        self.assertEqual(self.app.stage, 'menu')
        self.assertIsNone(self.app.active)

    def test_town_edit_off_returns_menu_and_back_stays_stepwise(self):
        self.app.apply_voice_command('four')
        self.app.active._switch('edit')
        self.assertTrue(self.app.apply_voice_command('back'))
        self.assertEqual(self.app.active_application, 'town')
        self.assertEqual(self.app.stage, 'scene')
        self.app.active._switch('edit')
        self.assertTrue(self.app.apply_voice_command('off'))
        self.assertEqual(self.app.stage, 'menu')

    def test_menu_packet_preserves_live_background_and_child_renderer(self):
        packet = self.app.renderer.prepare(self.app, time.perf_counter(), 12)
        self.assertIsInstance(packet, PreparedUI)
        self.assertEqual(packet.revision, 12)
        camera = np.full((720, 1280, 3), 21, np.uint8)
        DisplayCompositor(1280, 720).compose_into(camera, packet)
        np.testing.assert_array_equal(camera[0, 0], (21, 21, 21))
        x, y = map(int, center(self.app.layout['display_4']))
        self.assertTrue(np.any(camera[y, x] != 21))
        self.app._button('display_1', time.perf_counter())
        packet = self.app.renderer.prepare(self.app, time.perf_counter(), 13)
        self.assertIsInstance(packet, PreparedUI)
        self.assertEqual(packet.revision, 13)
        now = time.perf_counter()
        self.assertEqual(self.app.renderer.presentation_guard_key(self.app, now),
                         self.app.renderer.visual_key(self.app, now))

    def test_real_town_edit_back_and_scene_close_return_to_menu(self):
        from living_town.updated_town import UpdatedTownApp
        self.app._button('display_4', time.perf_counter())
        self.assertIsInstance(self.app.active, UpdatedTownApp)
        self.assertEqual(self.app.stage, 'scene')
        self.app._button('new', time.perf_counter())
        self.assertEqual(self.app.stage, 'edit')
        self.app._button('close', time.perf_counter())
        self.assertEqual(self.app.stage, 'scene')
        self.app._button('close', time.perf_counter())
        self.assertEqual(self.app.stage, 'menu')
        self.assertFalse(self.app.quit_requested)

    def test_real_town_held_pinch_cannot_draw_before_release(self):
        self.app._button('display_4', time.perf_counter())
        self.app._button('new', time.perf_counter())
        child = self.app.active
        point = center(child.layout['board'])
        now = time.perf_counter()
        self.app.observe(pinch(point), now)
        self.app.observe(pinch(point), now+.2)
        self.assertEqual(child.model.point_count, 0)
        self.app.observe((), now+.3)
        self.app.observe(pinch(point), now+.4)
        self.app.observe(pinch(point), now+.6)
        self.assertGreater(child.model.point_count, 0)
        packet = self.app.renderer.prepare(self.app, now+.6, 21)
        self.assertIsInstance(packet, PreparedUI)
        self.assertEqual(packet.revision, 21)

    def test_town_animation_uses_child_presentation_guard(self):
        self.app._button('display_4', time.perf_counter())
        child = self.app.active
        now = time.perf_counter()
        self.app._button('play', now)
        self.app.tick(now+.1)
        guard = self.app.renderer.presentation_guard_key(self.app, now+.1)
        self.app.renderer.prepare(self.app, now+.1, 1)
        self.app.tick(now+.3)
        self.assertEqual(guard, self.app.renderer.presentation_guard_key(self.app, now+.3))
        self.assertEqual(guard[2], child.renderer.presentation_guard_key(child, now+.3))
        self.app._button('road', now+.4)
        self.assertNotEqual(guard, self.app.renderer.presentation_guard_key(self.app, now+.4))

    def test_event_batch_ends_at_application_transition(self):
        now = time.perf_counter()
        self.app.handle_events((dict(kind='click', intent='button', target='display_4'),
                                dict(kind='click', intent='button', target='new')), now)
        self.assertEqual(self.app.stage, 'scene')
        self.assertEqual(self.app.active_application, 'town')

    def test_ui_worker_keeps_ack_and_epoch_gate_around_menu_entry(self):
        from town_creator.live_runtime import UIWorker
        worker = UIWorker(1280, 720, ROOT, {}, app_factory=self.factory)
        self.addCleanup(worker.close)

        def wait_for(predicate):
            deadline = time.perf_counter()+3.
            while time.perf_counter() < deadline:
                status = worker.status()
                self.assertIsNone(status['error'])
                self.assertIsNone(status['command_error'])
                if predicate(status):
                    return status
                time.sleep(.005)
            self.fail('UI worker did not reach expected state: %r' % worker.status())

        status = wait_for(lambda state: state['ready'])
        now = time.perf_counter()
        worker.offer_result(result(1, 0, now))
        time.sleep(.025)
        self.assertEqual(worker.status()['last_result_sequence'], 0)
        worker.acknowledge(status['published_revision'])
        wait_for(lambda state: state['last_result_sequence'] == 1)
        worker.command('button', 'display_1')
        status = wait_for(lambda state: state['stage'] == 'build' and state['ready'])
        self.assertGreater(status['app']['module_epoch'], 0)
        worker.acknowledge(status['published_revision'])
        worker.offer_result(result(2, 0, time.perf_counter()))
        status = wait_for(lambda state: state['stale_results'] > 0)
        self.assertEqual(status['stage'], 'build')
        self.assertTrue(status['app']['entry_pinch_release_required'])


if __name__ == '__main__':
    unittest.main()
