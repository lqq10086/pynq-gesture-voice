"""Optional bounded diagnostics; hardware safety logging remains independent.

Producers enqueue metadata only. A single daemon owns JSON/JPEG/files and marker
IPC. Optional source_frame references MUST be independent immutable PS copies,
never a live DMA/CMA view; no latest-frame lookup is performed here.
"""
import copy
import datetime
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import uuid

MARK_LABELS = ('open_palm', 'side_palm', 'pinch_point', 'released', 'success', 'failure')


def _json_default(value):
    if hasattr(value, 'item'):
        return value.item()
    raise TypeError('Non-scalar diagnostic value: ' + type(value).__name__)


class GestureTrace:
    def __init__(self, output_dir, mode='off', queue_size=256, max_file_bytes=2097152,
                 max_files=4, image_budget_bytes=8388608, image_every=10, identity=None):
        if mode not in ('off', 'light', 'detailed'):
            raise ValueError('Unknown GestureTrace mode')
        self.mode = mode
        self.enabled = mode != 'off'
        self.detailed = mode == 'detailed'
        self.session_id = uuid.uuid4().hex[:12] if self.enabled else None
        self.output_dir = Path(output_dir)
        self._counts = dict(accepted_events=0, written_events=0, dropped_events=0,
                            write_errors=0, oversized_events=0, rotated_files=0,
                            rotated_events=0, duplicate_inferences=0, marker_events=0,
                            marker_errors=0, image_bytes=0, image_files=0,
                            image_errors=0, image_budget_skipped=0,
                            image_frame_mismatches=0, image_queue_skipped=0)
        self._last_error = None
        self._closed = False
        self._last_result = -1
        self._image_pending = 0
        self._thread = None
        self._count_lock = threading.Lock()
        if not self.enabled:
            return
        if queue_size < 1 or max_file_bytes < 256 or max_files < 1 or image_every < 0:
            raise ValueError('GestureTrace queue/file bounds must be positive; image_every may be zero')
        self.max_file_bytes, self.max_files = int(max_file_bytes), int(max_files)
        self.image_budget_bytes, self.image_every = max(0, int(image_budget_bytes)), int(image_every)
        self.identity = copy.deepcopy(identity or {})
        self._queue = queue.Queue(maxsize=int(queue_size))
        self._stop = threading.Event()
        self._socket = None
        self._socket_path = None
        self._log = None
        self._logs = []
        self._sequence = 0
        self._log_bytes = 0
        self._thread = threading.Thread(target=self._writer, name='GestureTrace', daemon=True)
        self._thread.start()

    def _add(self, key, amount=1):
        with self._count_lock:
            self._counts[key] += amount

    def _error(self, error, key='write_errors'):
        self._add(key)
        self._last_error = type(error).__name__ + ': ' + str(error)[:240]

    def stats(self):
        with self._count_lock:
            counts = dict(self._counts)
        counts.update(session_id=self.session_id, mode=self.mode,
                      last_error=self._last_error,
                      close_complete=self._closed and (self._thread is None or not self._thread.is_alive()))
        return counts

    def emit(self, kind, payload, monotonic=None):
        if not self.enabled or self._closed:
            return False
        # Payloads are bounded scalar/list metadata, copied so app state cannot
        # mutate already queued evidence. Serialization happens only in writer.
        event = copy.deepcopy(payload)
        event.update(kind=str(kind), session_id=self.session_id,
                     monotonic=time.monotonic() if monotonic is None else float(monotonic))
        return self._enqueue(event)

    def _enqueue(self, event):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._add('dropped_events')
            return False
        self._add('accepted_events')
        return True

    def wants_image(self, result_id):
        return (self.enabled and self.detailed and not self._closed
                and self.image_every > 0
                and int(result_id) % self.image_every == 0
                and self._counts['image_bytes'] < self.image_budget_bytes)

    def record_inference(self, result, frame=None):
        if not self.enabled or self._closed:
            return False
        number = int(result['result_id'])
        if number <= self._last_result:
            self._add('duplicate_inferences')
            return False
        self._last_result = number
        event = copy.deepcopy(result)
        event.update(kind='inference', session_id=self.session_id,
                     frame_id=result.get('source_frame_id', result.get('frame_id')),
                     monotonic=result.get('inference_end', time.monotonic()))
        if frame is not None:
            if frame.get('source_frame_id', frame.get('frame_id')) != event['frame_id']:
                self._add('image_frame_mismatches')
            elif self.wants_image(number):
                # Existing capture copy is independent and never mutated/reused.
                # Bound pinned 720P copies separately from the metadata queue.
                with self._count_lock:
                    if self._image_pending < 2:
                        self._image_pending += 1
                        event['_source_frame'] = frame
                    else:
                        self._counts['image_queue_skipped'] += 1
        accepted = self._enqueue(event)
        if not accepted and '_source_frame' in event:
            with self._count_lock:
                self._image_pending -= 1
        return accepted

    def close(self, timeout=3.):
        self._closed = True
        if self._thread is not None:
            self._stop.set()
            self._thread.join(max(0., float(timeout)))
        return self.stats()

    def _setup(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if (self.output_dir / 'session.json').exists():
            raise FileExistsError('Use a new output directory for each trace session')
        marker_error = None
        try:
            self._socket_path = Path(tempfile.gettempdir()) / ('gt_' + self.session_id + '.sock')
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._socket.bind(str(self._socket_path))
            self._socket_path.chmod(0o600)
            self._socket.setblocking(False)
        except (AttributeError, OSError) as error:
            marker_error = str(error)
            self._error(error, 'marker_errors')
            if self._socket is not None:
                self._socket.close()
            self._socket = None
        metadata = dict(schema_version=1, session_id=self.session_id, mode=self.mode,
                        identity=self.identity, clock='receiver board time.monotonic seconds',
                        utc_created=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        marker_socket=str(self._socket_path) if self._socket is not None else None,
                        marker_error=marker_error, image_semantics='same inference source; per-image mirrored flag is authoritative',
                        max_file_bytes=self.max_file_bytes, max_files=self.max_files,
                        image_budget_bytes=self.image_budget_bytes, image_every=self.image_every,
                        queue_size=self._queue.maxsize)
        (self.output_dir / 'session.json').write_text(json.dumps(metadata, default=_json_default), encoding='utf-8')

    def _receive_markers(self):
        if self._socket is None:
            return
        for _ in range(32):
            try:
                payload = self._socket.recv(2048)
            except BlockingIOError:
                break
            received = time.monotonic()
            try:
                marker = json.loads(payload.decode('utf-8'))
                if marker.get('label') not in MARK_LABELS:
                    raise ValueError('Unknown manual label')
                hands = marker.get('hands')
                if hands is not None and (type(hands) is not int or hands not in (1, 2)):
                    raise ValueError('Invalid operator-declared hand count')
                event = dict(kind='human_marker', session_id=self.session_id,
                             monotonic=received, label=marker['label'],
                             note=str(marker.get('note', ''))[:512],
                             hands=hands,
                             origin='human_cli', trusted_label=True,
                             timestamp_semantics='board IPC receipt; operator gesture onset may differ')
                self._add('marker_events')
                self._enqueue(event)
            except (ValueError, AttributeError, UnicodeError) as error:
                self._error(error, 'marker_errors')

    def _images(self, event, frame):
        # All conversion, encoding and storage happens outside PL/inference.
        try:
            import cv2
            import numpy as np
            image = frame.get('bgr')
            if image is None:
                try:
                    from .frame_reader import rgb565_to_bgr
                except ImportError:
                    from frame_reader import rgb565_to_bgr
                geometry = event.get('geometry', {})
                image = rgb565_to_bgr(frame['raw565'], geometry.get('width', 1280), geometry.get('height', 720))
            if not isinstance(image, np.ndarray) or image.ndim != 3:
                raise ValueError('Expected independently owned source BGR image')
            height, width = image.shape[:2]
            scale = min(1., 640. / width)
            preview = cv2.resize(image, (round(width * scale), round(height * scale))) if scale < 1 else image.copy()
            overlay = preview.copy()
            for detection in event.get('detections', []):
                box = detection.get('source_xyxy')
                if box is None:
                    continue
                x1, y1, x2, y2 = [round(float(v) * scale) for v in box]
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 240, 80), 1)
                cv2.putText(overlay, '%s %.2f' % (detection.get('class_name', ''), detection.get('confidence', 0)),
                            (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 240, 80), 1)
            encoded = []
            mirrored = bool(frame.get('source_mirrored', False))
            preview_kind = 'source_mirrored_bgr_preview' if mirrored else 'source_raw_bgr_preview'
            overlay_kind = 'source_mirrored_overlay' if mirrored else 'source_unmirrored_overlay'
            for kind, pixels in ((preview_kind, preview), (overlay_kind, overlay)):
                ok, content = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    raise OSError('JPEG encoding failed')
                encoded.append((kind, content.tobytes()))
            required = sum(len(content) for _, content in encoded)
            if self._counts['image_bytes'] + required > self.image_budget_bytes:
                self._add('image_budget_skipped')
                return
            folder = self.output_dir / 'images'
            folder.mkdir(exist_ok=True)
            event['images'] = []
            for kind, content in encoded:
                relative = 'images/%06d_%s.jpg' % (event['result_id'], kind)
                (self.output_dir / relative).write_bytes(content)
                self._add('image_bytes', len(content))
                self._add('image_files')
                event['images'].append(dict(path=relative, kind=kind, frame_id=event['frame_id'],
                                            result_id=event['result_id'], scale=scale,
                                            source_dimensions=[width, height], mirrored=mirrored))
        except Exception as error:
            self._error(error, 'image_errors')

    def _write_event(self, event):
        frame = event.pop('_source_frame', None)
        if frame is not None:
            try:
                self._images(event, frame)
            finally:
                with self._count_lock:
                    self._image_pending -= 1
        data = (json.dumps(event, ensure_ascii=False, default=_json_default, separators=(',', ':')) + '\n').encode('utf-8')
        if len(data) > self.max_file_bytes:
            self._add('oversized_events')
            return
        if self._log is None or self._log_bytes + len(data) > self.max_file_bytes:
            if self._log is not None:
                self._log.close()
            self._sequence += 1
            path = self.output_dir / ('events-%06d.jsonl' % self._sequence)
            self._log = path.open('wb')
            self._log_bytes = 0
            self._logs.append([path, 0])
            if len(self._logs) > self.max_files:
                old, count = self._logs.pop(0)
                old.unlink()
                self._add('rotated_files')
                self._add('rotated_events', count)
        self._log.write(data)
        self._log.flush()
        self._log_bytes += len(data)
        self._logs[-1][1] += 1
        self._add('written_events')

    def _writer(self):
        storage_ready = False
        try:
            self._setup()
            storage_ready = True
        except Exception as error:
            self._error(error)
        while not self._stop.is_set() or not self._queue.empty():
            try:
                self._receive_markers()
                event = self._queue.get(timeout=.05)
            except queue.Empty:
                continue
            except Exception as error:
                self._error(error, 'marker_errors')
                continue
            try:
                if storage_ready:
                    self._write_event(event)
                else:
                    self._add('write_errors')
            except Exception as error:
                self._error(error)
            finally:
                if '_source_frame' in event:
                    event.pop('_source_frame')
                    with self._count_lock:
                        self._image_pending -= 1
                self._queue.task_done()
        if self._log is not None:
            try:
                self._log.close()
            except OSError as error:
                self._error(error)
        if self._socket is not None:
            self._socket.close()
            try:
                self._socket_path.unlink()
            except OSError as error:
                self._error(error, 'marker_errors')
        if storage_ready:
            try:
                summary = self.stats()
                summary['close_complete'] = True
                (self.output_dir / 'stats.json').write_text(json.dumps(summary), encoding='utf-8')
            except OSError as error:
                self._error(error)
