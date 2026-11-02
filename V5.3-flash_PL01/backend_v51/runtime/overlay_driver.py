"""AXI GPIO shadow/commit client for the pixel-domain bbox overlay."""
import threading
import time


class BboxOverlay:
    def __init__(self, first_gpio, second_gpio, ack_gpio, timeout=1.0):
        self.first = first_gpio
        self.second = second_gpio
        self.ack = ack_gpio
        self.timeout = timeout
        self.lock = threading.Lock()
        self.first.write(4, 0)
        self.first.write(12, 0)
        self.second.write(4, 0)
        self.second.write(12, 0)
        self._toggle = self.ack.read(0) & 1

    def _commit(self, word0, word1, word2):
        with self.lock:
            if (self.ack.read(0) & 1) != self._toggle:
                raise RuntimeError('Previous bbox commit is still pending')
            self.first.write(0, word0)
            self.first.write(8, word1)
            self.second.write(0, word2)
            self._toggle ^= 1
            self.second.write(8, self._toggle)
            deadline = time.monotonic() + self.timeout
            while (self.ack.read(0) & 1) != self._toggle:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Bounding box frame commit was not acknowledged')
                time.sleep(.002)

    def set_box(self, xyxy, class_id, confidence):
        x1,y1,x2,y2 = [int(v) for v in xyxy]
        if not (0<=x1<=x2<1280 and 0<=y1<=y2<720):
            raise ValueError('Expected inclusive 1280x720 video coordinates')
        if class_id not in (0,1,2,3) or not 0<=confidence<=1:
            raise ValueError('Expected class 0..3 and confidence 0..1')
        q16 = round(confidence*65535)
        # Color is a hardware encoding, never a replacement for true class_id.
        display_color=(0,1,2,0)[class_id]
        self._commit(1|(display_color<<1)|(q16<<3), x1|(y1<<11), x2|(y2<<11))

    def clear(self):
        self._commit(0,0,0)
