"""One-pass native frame conversion; exact existing OpenCV fallback on PC."""
import argparse
import ctypes
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
LIBRARY_PATH = ROOT / 'native/libframe_color.so'
_library = None
native_load_error = None
if os.name == 'nt':
    native_load_error = 'Linux native library disabled on Windows; using OpenCV fallback'
if LIBRARY_PATH.exists() and os.name != 'nt':
    try:
        _library = ctypes.CDLL(str(LIBRARY_PATH))
        for name in ('raw565_to_mirrored_bgr', 'bgr_to_rgb565'):
            function = getattr(_library, name)
            function.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
            function.restype = None
    except (OSError, AttributeError) as error:
        native_load_error = str(error)
        _library = None
native_used = _library is not None
_tint_mix = getattr(_library, 'tint_mix_masked', None) if native_used else None
_constant_mix = getattr(_library, 'blend_constant_masked', None) if native_used else None
native_blend_used = _tint_mix is not None and _constant_mix is not None
_mirror565 = getattr(_library, 'mirror_rgb565', None) if native_used else None
native_rgb565_used = _mirror565 is not None
_window_composite = getattr(_library, 'composite_window_rgb8', None) if native_used else None
if _window_composite is not None:
    _window_composite.argtypes = (ctypes.c_void_p,) * 5 + (ctypes.c_int,) * 8 + (ctypes.c_size_t,) * 3
    _window_composite.restype = None
if _mirror565 is not None:
    _mirror565.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    _mirror565.restype = None
if _tint_mix is not None:
    _tint_mix.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_int, ctypes.c_int, ctypes.c_size_t, ctypes.c_float,
                         ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8)
    _tint_mix.restype = None
if _constant_mix is not None:
    _constant_mix.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                             ctypes.c_int, ctypes.c_size_t, ctypes.c_float,
                             ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8)
    _constant_mix.restype = None


def _destination(destination, shape, source):
    result = np.empty(shape, np.uint8) if destination is None else np.asarray(destination)
    if result.dtype != np.uint8 or result.shape != shape or not result.flags.c_contiguous:
        raise ValueError('Destination must be contiguous uint8 with shape %r' % (shape,))
    if not result.flags.writeable or np.shares_memory(source, result):
        raise ValueError('Destination must be writable and must not overlap the source')
    return result


def raw565_to_mirrored_bgr(raw, width=1280, height=720, destination=None):
    source = np.asarray(raw)
    if width <= 0 or height <= 0:
        raise ValueError('Frame dimensions must be positive')
    if source.dtype not in (np.dtype(np.uint8), np.dtype('<u2')) or not source.flags.c_contiguous:
        raise ValueError('RGB565 source must be contiguous uint8 or little-endian uint16')
    if source.nbytes != width * height * 2:
        raise ValueError('RGB565 source size does not match the frame dimensions')
    result = _destination(destination, (height, width, 3), source)
    if native_used:
        _library.raw565_to_mirrored_bgr(source.ctypes.data, result.ctypes.data, width, height)
    else:
        from runtime.frame_reader import rgb565_to_bgr
        cv2.flip(rgb565_to_bgr(source, width, height), 1, dst=result)
    return result


def mirror_rgb565(raw, width=1280, height=720, destination=None):
    """Mirror complete two-byte pixels directly into a private output page."""
    source = np.asarray(raw)
    if (width <= 0 or height <= 0 or source.nbytes != width * height * 2
            or source.dtype not in (np.dtype(np.uint8), np.dtype('<u2'))
            or not source.flags.c_contiguous):
        raise ValueError('RGB565 source must match contiguous 8/16-bit frame dimensions')
    result = _destination(destination, (height, width, 2), source)
    if _mirror565 is not None:
        _mirror565(source.ctypes.data, result.ctypes.data, width, height)
    else:
        cv2.flip(source.view(np.uint8).reshape(height, width, 2), 1, dst=result)
    return result


def bgr_to_rgb565(bgr, destination=None):
    source = np.asarray(bgr)
    if source.dtype != np.uint8 or source.ndim != 3 or source.shape[2] != 3 or not source.flags.c_contiguous:
        raise ValueError('BGR source must be contiguous uint8 with shape (height, width, 3)')
    height, width = source.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError('Frame dimensions must be positive')
    result = _destination(destination, (height, width, 2), source)
    if native_used:
        _library.bgr_to_rgb565(source.ctypes.data, result.ctypes.data, width, height)
    else:
        cv2.cvtColor(source, cv2.COLOR_BGR2BGR565, dst=result)
    return result


def _masked_roi(roi_bgr, mask):
    roi = np.asarray(roi_bgr)
    if (roi.dtype != np.uint8 or roi.ndim != 3 or roi.shape[2] != 3
            or not roi.flags.writeable or roi.strides[2] != 1 or roi.strides[1] != 3
            or roi.strides[0] < roi.shape[1] * 3):
        raise ValueError('ROI must be writable uint8 BGR with positive contiguous-pixel row stride')
    checked_mask = None if mask is None else np.asarray(mask)
    if checked_mask is not None and (checked_mask.dtype != np.uint8
            or checked_mask.shape != roi.shape[:2] or not checked_mask.flags.c_contiguous):
        raise ValueError('Mask must be contiguous uint8 with the ROI height and width')
    return roi, checked_mask


def _blend_fraction(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError('Native blend fraction must be finite and between 0 and 1')
    return result


def tint_mix_masked(roi_bgr, soft_bgr, mask, strength, tint):
    """Optional board fast path. False leaves the ROI unchanged for fallback."""
    if _tint_mix is None:
        return False
    roi, checked_mask = _masked_roi(roi_bgr, mask)
    soft = np.asarray(soft_bgr)
    if checked_mask is None or soft.dtype != np.uint8 or soft.shape != roi.shape or not soft.flags.c_contiguous:
        raise ValueError('Tint mixing needs a uint8 mask and contiguous uint8 blurred BGR')
    _tint_mix(roi.ctypes.data, soft.ctypes.data, checked_mask.ctypes.data,
              roi.shape[1], roi.shape[0], roi.strides[0], _blend_fraction(strength), *tint)
    return True


def blend_constant_masked(roi_bgr, mask, color, opacity):
    """Optional board fast path; mask=None blends every pixel in the ROI."""
    if _constant_mix is None:
        return False
    roi, checked_mask = _masked_roi(roi_bgr, mask)
    _constant_mix(roi.ctypes.data, None if checked_mask is None else checked_mask.ctypes.data,
                  roi.shape[1], roi.shape[0], roi.strides[0], _blend_fraction(opacity), *color)
    return True


def composite_window_bgr(destination, before, after, source_outside, target_outside, clip_x, clip_y):
    """Optional fused masked difference/linear resize/add; preserve float rounding."""
    if _window_composite is None:
        return False
    destination, _ = _masked_roi(destination, None)
    for source in (before, after):
        if (source.dtype != np.uint8 or source.shape != before.shape or source.ndim != 3
                or source.shape[2] != 3 or source.strides[2] != 1 or source.strides[1] != 3
                or source.strides[0] < source.shape[1]*3 or np.shares_memory(destination, source)):
            raise ValueError('Window sources must be independent positive-stride BGR ROIs')
    for mask in (source_outside, target_outside):
        if mask.dtype != np.bool_ or mask.ndim != 2 or not mask.flags.c_contiguous:
            raise ValueError('Window outside masks must be contiguous boolean arrays')
    h,w=destination.shape[:2];sh,sw=before.shape[:2];th,tw=target_outside.shape
    if (source_outside.shape != (sh,sw) or min(h,w,sh,sw,th,tw)<=0
            or clip_x<0 or clip_y<0 or clip_x+w>tw or clip_y+h>th):
        raise ValueError('Window masks and clipping must match the source and destination')
    _window_composite(destination.ctypes.data,before.ctypes.data,after.ctypes.data,
                      source_outside.ctypes.data,target_outside.ctypes.data,sw,sh,tw,th,
                      clip_x,clip_y,w,h,destination.strides[0],before.strides[0],after.strides[0])
    return True


def _verify_masked_blends(rng):
    if _tint_mix is None or _constant_mix is None:
        return dict(status='NOT_RUN_NO_NATIVE_BLEND_SYMBOLS')
    maximum = 0
    cases = 0
    for strength in (0.0, 0.52, 0.83, 1.0):
        base = rng.randint(0, 256, size=(80, 180, 3)).astype(np.uint8)
        before = base.copy()
        roi = base[5:70, 13:161]
        soft = rng.randint(0, 256, size=roi.shape).astype(np.uint8)
        soft_before = soft.copy()
        mask = rng.randint(0, 3, size=roi.shape[:2]).astype(np.uint8)
        tint = (41, 44, 47)
        tinted = cv2.addWeighted(soft, .5, np.full_like(soft, tint), .5, 0)
        expected = before.copy()
        expected_roi = expected[5:70, 13:161]
        mixed = cv2.addWeighted(expected_roi, 1 - strength, tinted, strength, 0)
        np.copyto(expected_roi, mixed, where=mask[:, :, None] > 0)
        assert tint_mix_masked(roi, soft, mask, strength, tint)
        difference = int(np.max(np.abs(base.astype(np.int16) - expected.astype(np.int16))))
        assert difference <= 1, 'Native tint blend exceeded one rounding level'
        assert np.array_equal(soft, soft_before), 'Tint blend modified blurred input'
        assert np.array_equal(roi[mask == 0], before[5:70, 13:161][mask == 0]), 'Tint blend modified masked-out pixels'
        outside = before.copy()
        outside[5:70, 13:161] = roi
        assert np.array_equal(base, outside), 'Tint blend wrote outside the strided ROI'
        maximum = max(maximum, difference)
        cases += 1
    for mask_enabled in (False, True):
        for opacity in (0.06, 0.42, 0.9):
            base = rng.randint(0, 256, size=(80, 180, 3)).astype(np.uint8)
            before = base.copy()
            roi = base[5:70, 13:161]
            mask = rng.randint(0, 3, size=roi.shape[:2]).astype(np.uint8) if mask_enabled else None
            color = (32, 37, 42)
            expected = before.copy()
            expected_roi = expected[5:70, 13:161]
            mixed = cv2.addWeighted(expected_roi, 1 - opacity, np.full_like(roi, color), opacity, 0)
            if mask is None:
                expected_roi[:] = mixed
            else:
                np.copyto(expected_roi, mixed, where=mask[:, :, None] > 0)
            assert blend_constant_masked(roi, mask, color, opacity)
            difference = int(np.max(np.abs(base.astype(np.int16) - expected.astype(np.int16))))
            assert difference <= 1, 'Native constant blend exceeded one rounding level'
            outside = before.copy()
            outside[5:70, 13:161] = roi
            assert np.array_equal(base, outside), 'Constant blend wrote outside the strided ROI'
            if mask is not None:
                assert np.array_equal(roi[mask == 0], before[5:70, 13:161][mask == 0]), 'Constant blend modified masked-out pixels'
            maximum = max(maximum, difference)
            cases += 1
    validation_cases = 0
    for invalid in (base[::-1], base[:, ::2], base[:, :, ::-1]):
        try:
            blend_constant_masked(invalid, None, (32, 37, 42), .06)
        except ValueError:
            validation_cases += 1
        else:
            raise AssertionError('Native blend accepted an unsupported ROI stride')
    bad_mask = np.ones(roi.shape[:2], np.uint8)[:, ::-1]
    try:
        blend_constant_masked(roi, bad_mask, (32, 37, 42), .06)
    except ValueError:
        validation_cases += 1
    else:
        raise AssertionError('Native blend accepted a noncontiguous mask')
    return dict(status='PASS', cases=cases, strided_roi=True, mask_none_tested=True,
                rejected_invalid_stride_cases=validation_cases,
                blend_math='Q16 coefficient and integer nearest-even output; first tint half is exact nearest-even',
                allowed_max_abs_difference=1, observed_max_abs_difference=maximum,
                scope='Caller opt-in board fast rendering path only')


def _verify_window_composite(rng):
    if _window_composite is None:
        return dict(status='NOT_RUN_NO_NATIVE_WINDOW_SYMBOL')
    for index in range(18):
        before=rng.randint(0,256,(11,17,3)).astype(np.uint8)
        after=rng.randint(0,256,before.shape).astype(np.uint8)
        th,tw=((7,9),(11,17),(15,23))[index%3]
        source_mask=np.ascontiguousarray(rng.randint(0,5,(11,17))==0)
        target_mask=np.ascontiguousarray(rng.randint(0,5,(th,tw))==0)
        base=rng.randint(0,256,(th+2,tw+4,3)).astype(np.uint8)
        expected=base.copy();actual=base.copy()
        difference=cv2.subtract(after,before,dtype=cv2.CV_32F)
        difference[source_mask]=0.
        difference=cv2.resize(difference,(tw,th),interpolation=cv2.INTER_LINEAR)
        difference[target_mask]=0.
        clip_x=index%2;clip_y=(index//2)%2
        roi=expected[1:th+1-clip_y,2:tw+2-clip_x]
        roi[:]=np.clip(np.rint(roi.astype(np.float32)+difference[clip_y:,clip_x:]),0,255).astype(np.uint8)
        composite_window_bgr(actual[1:th+1-clip_y,2:tw+2-clip_x],before,after,source_mask,target_mask,clip_x,clip_y)
        assert np.array_equal(actual,expected),'Native window composite differs from float32 reference'
    return dict(status='PASS',cases=18,byte_exact=True,strided_destination=True,clipped=True)


def verify(require_native=False):
    from runtime.frame_reader import rgb565_to_bgr
    if require_native and not native_used:
        raise RuntimeError('Native library unavailable: %s' % (native_load_error or LIBRARY_PATH))
    if require_native and not native_blend_used:
        raise RuntimeError('Native color library lacks required masked blend symbols')
    if require_native and _mirror565 is None:
        raise RuntimeError('Native color library lacks V3.3 RGB565 mirror symbol')
    if require_native and _window_composite is None:
        raise RuntimeError('Native color library lacks V3.3 fused window symbol')
    raw = np.arange(65536, dtype='<u2').reshape(256, 256)
    before = raw.copy()
    expected = cv2.flip(rgb565_to_bgr(raw, 256, 256), 1)
    decoded = np.full((256, 256, 3), 173, np.uint8)
    actual = raw565_to_mirrored_bgr(raw, 256, 256, decoded)
    assert np.array_equal(actual, expected), 'Decode/mirror differs from existing cvLUT + cvFlip'
    assert np.array_equal(raw, before), 'Decode altered the caller-owned source'
    assert np.shares_memory(actual, decoded), 'Decode did not use the caller-owned destination'
    mirrored565 = mirror_rgb565(raw, 256, 256)
    assert np.array_equal(mirrored565.view('<u2').reshape(256, 256), raw[:, ::-1]), 'RGB565 direct mirror differs'
    for width in (1, 7, 8, 9, 13):
        odd = np.arange(5 * width, dtype='<u2').reshape(5, width)
        assert np.array_equal(mirror_rgb565(odd, width, 5).view('<u2').reshape(5, width), odd[:, ::-1]), 'RGB565 scalar/vector tail differs'
    packed = bgr_to_rgb565(actual)
    assert np.array_equal(packed.view('<u2').reshape(256, 256), raw[:, ::-1]), 'RGB565 roundtrip differs'
    rng = np.random.RandomState(20261008)
    random_bgr = rng.randint(0, 256, size=(256, 256, 3)).astype(np.uint8)
    random_before = random_bgr.copy()
    packed_destination = np.full((256, 256, 2), 173, np.uint8)
    actual_packed = bgr_to_rgb565(random_bgr, packed_destination)
    assert np.array_equal(actual_packed, cv2.cvtColor(random_bgr, cv2.COLOR_BGR2BGR565)), 'BGR pack byte parity differs'
    assert np.array_equal(random_bgr, random_before), 'Pack altered the caller-owned source'
    assert np.shares_memory(actual_packed, packed_destination), 'Pack did not use the caller-owned destination'
    masked_blend_parity = _verify_masked_blends(rng)
    return dict(status='PASS', native_used=native_used, native_blend_used=native_blend_used,
                native_rgb565_mirror=_mirror565 is not None,
                library_path=str(LIBRARY_PATH),
                exhaustive_rgb565_codes=65536, random_bgr_pixels=65536,
                decode_reference='existing RGB565 cvtColor + expansion LUT + horizontal flip',
                encode_reference='cv2.COLOR_BGR2BGR565', caller_sources_unchanged=True,
                caller_destinations_used=True, byte_exact=True,
                masked_blend_parity=masked_blend_parity,window_composite_parity=_verify_window_composite(rng))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true', required=True)
    parser.add_argument('--require-native', action='store_true')
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.require_native), indent=2))
