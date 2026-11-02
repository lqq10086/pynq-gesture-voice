"""Centered camera zoom using one reusable destination and OpenCV resize."""
import cv2
import numpy as np


class ZoomBackground:
    """Render uint8 BGR camera frames at the app's fixed .75..2.5 zoom range.

    At 1x the source itself is returned; the caller owns its existing in-place
    policy. Other zooms return one private buffer, overwritten on the next call.
    The source must be the camera frame, not this renderer's previous output.

    Integer crop/resize dimensions are rounded to the nearest pixel. Centering
    uses ceil((extent-scaled_extent)/2): this compensates for resize's half-pixel
    sampling versus the old warp's (width/2, height/2) center. Nearest-exact
    sampling keeps the ARM display responsive; fine background edges can alias
    or disappear when shrinking. UI and detection labels are drawn afterwards.
    """

    def __init__(self):
        self._destination = None

    def render(self, source, zoom):
        if not .75 <= zoom <= 2.5:
            raise ValueError('Background zoom must be between .75 and 2.5')
        if zoom == 1.:
            return source
        height, width = source.shape[:2]
        if self._destination is None or self._destination.shape != source.shape:
            self._destination = np.empty(source.shape, dtype=source.dtype)
        destination = self._destination
        if zoom > 1.:
            crop_width = max(1, round(width / zoom))
            crop_height = max(1, round(height / zoom))
            left = (width-crop_width+1) // 2
            top = (height-crop_height+1) // 2
            cv2.resize(source[top:top+crop_height, left:left+crop_width],
                       (width, height), dst=destination, interpolation=cv2.INTER_NEAREST_EXACT)
        else:
            scaled_width = max(1, round(width * zoom))
            scaled_height = max(1, round(height * zoom))
            left = (width-scaled_width+1) // 2
            top = (height-scaled_height+1) // 2
            destination.fill(18)
            cv2.resize(source, (scaled_width, scaled_height),
                       dst=destination[top:top+scaled_height, left:left+scaled_width],
                       interpolation=cv2.INTER_NEAREST_EXACT)
        return destination
