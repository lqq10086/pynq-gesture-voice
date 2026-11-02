"""One cached foreground draw plus local stage alpha, without live-camera work."""
import time
import numpy as np

from town_creator.layers import PreparedUI
from .flat_ui import opaque_packet
from .spatial_layers import spatial_key


class CombinedRenderer:
    def __init__(self, app):
        self.owner = app
        self._canvas = np.zeros((app.height, app.width, 3), np.uint8)
        self._key = self._packet = None
        self.preparations = self.cache_hits = self.draw_calls = 0
        self.last_prepare_ms = 0.

    def visual_key(self, app, now):
        return (app.spatial.selected_level_id, bool(app.spatial.placement_enabled)) + spatial_key(app.spatial, now)

    @staticmethod
    def _revision(packet, revision, now):
        if packet.revision == int(revision):
            return packet
        return PreparedUI(int(revision), float(now), packet.width, packet.height,
                          packet.patches, ())

    def prepare(self, app, now, revision):
        started = time.perf_counter()
        key = self.visual_key(app, now)
        if key == self._key:
            self.cache_hits += 1
            return self._revision(self._packet, revision, now)
        self._canvas.fill(0)
        app.spatial.render(self._canvas, now, include_projection=False)
        spatial=app.spatial
        background=(spatial.projection_stage.coefficients(self._canvas, spatial.build_projection,
                    spatial.level.id, spatial.check_result) if spatial.stage=='build' else None)
        packet = opaque_packet(self._canvas, revision, now, background)
        self._packet, self._key = packet, key
        self.preparations += 1
        self.draw_calls += 1
        self.last_prepare_ms = (time.perf_counter()-started)*1000.
        return packet

    def release_inactive_scratch(self):
        pass  # The single reusable canvas has fixed size.

    def metrics(self):
        spatial = self.owner.spatial
        return dict(preparations=self.preparations, prepare_cache_hits=self.cache_hits,
                    last_prepare_ms=self.last_prepare_ms, mode='construction_only',
                    offscreen_draw_calls=self.draw_calls, scratch_bytes=self._canvas.nbytes,
                    spatial=dict(preparations=self.preparations,
                                 offscreen_draw_calls=self.draw_calls,
                                 last_prepare_ms=self.last_prepare_ms,
                                 scratch_bytes=self._canvas.nbytes,
                                 geometry_rasters=spatial.renderer.raster_count,
                                 projection_cache_builds=spatial.projection_stage.cache_build_count),
                    camera_free=True, glass_regions=0, opaque_ui=spatial.stage!='build',
                    display_compositor='PL local projection alpha with opaque foreground')
