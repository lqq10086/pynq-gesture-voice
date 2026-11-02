import time
import unittest
from pathlib import Path

from town_creator.layers import PreparedUI
from town_creator.live_runtime import UIWorker


class Renderer:
    def visual_key(self, app, now):
        return app.layout, app.pose

    def prepare(self, app, now, revision):
        return PreparedUI(revision, now, app.width, app.height, ())


class GuardedRenderer(Renderer):
    def presentation_guard_key(self, app, now):
        return app.layout


class App:
    stage = 'menu'
    quit_requested = debug = False

    def __init__(self, width, height, **kwargs):
        self.width, self.height = width, height
        self.layout = self.pose = self.observations = 0
        self.renderer = GuardedRenderer()

    @property
    def metrics(self):
        return dict(observations=self.observations)

    def tick(self, now):
        self.pose = int(now * 50)

    def observe(self, detections, now):
        self.observations += 1
        return []

    def cancel_interaction(self, now=None):
        pass


class LayoutChangingApp(App):
    def tick(self, now):
        super().tick(now)
        self.layout += 1


class StaticLegacyApp(App):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.renderer = Renderer()

    def tick(self, now):
        pass


class AnimationGuardTests(unittest.TestCase):
    def worker(self, factory):
        worker = UIWorker(160, 90, Path(__file__).resolve().parents[1], {},
                          app_factory=factory, prepare_delay=.08)
        self.addCleanup(worker.close)
        return worker

    def wait(self, predicate, timeout=1.):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return False

    def test_animation_publishes_when_preparation_exceeds_pose_interval(self):
        worker = self.worker(App)
        self.assertTrue(self.wait(lambda: worker.latest() is not None))
        self.assertIsNone(worker.status()['error'])

    def test_interaction_layout_changes_still_discard_stale_packet(self):
        worker = self.worker(LayoutChangingApp)
        self.assertTrue(self.wait(lambda: worker.metrics()['discarded_preparations'] >= 2))
        self.assertIsNone(worker.latest())

    def test_legacy_renderer_keeps_display_ack_input_gate(self):
        worker = self.worker(StaticLegacyApp)
        self.assertTrue(self.wait(lambda: worker.latest() is not None))
        worker.offer_manual((), time.perf_counter())
        time.sleep(.06)
        self.assertEqual(worker.status()['accepted_detection_updates'], 0)
        self.assertTrue(worker.acknowledge(worker.latest().revision))
        self.assertTrue(self.wait(lambda: worker.status()['accepted_detection_updates'] == 1))


if __name__ == '__main__':
    unittest.main()
