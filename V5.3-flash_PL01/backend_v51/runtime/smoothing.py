"""Lightweight class confirmation, bbox EMA and stale-result timeout."""


class BoxSmoother:
    def __init__(self, alpha=.5, confirmations=2, timeout_s=2.0):
        if not 0<alpha<=1 or confirmations<1 or timeout_s<=0:
            raise ValueError('Invalid smoothing settings')
        self.alpha=alpha
        self.confirmations=confirmations
        self.timeout_s=timeout_s
        self.clear()

    def clear(self):
        self.confirmed=None
        self.candidate=None
        self.candidate_count=0
        self.last_seen=None

    def observe(self, raw, now):
        if self.expired(now):
            self.clear()
        if raw is None:
            self.clear()
            return None
        self.last_seen=now
        class_id=raw['class_id']
        if self.confirmed is not None and class_id==self.confirmed['class_id']:
            old=self.confirmed['video_xyxy']
            new=raw['video_xyxy']
            box=tuple(round(self.alpha*a+(1-self.alpha)*b) for a,b in zip(new,old))
            self.confirmed={**raw,'video_xyxy':box}
            self.candidate=None;self.candidate_count=0
        else:
            # Never label an old class/position as the current observation.
            self.confirmed=None
            if class_id==self.candidate:
                self.candidate_count+=1
            else:
                self.candidate=class_id;self.candidate_count=1
            if self.candidate_count>=self.confirmations:
                self.confirmed=dict(raw)
                self.candidate=None;self.candidate_count=0
        return self.confirmed

    def expired(self, now):
        return self.last_seen is not None and now-self.last_seen>=self.timeout_s
