"""Two real pixel-domain bbox slots, committed together at a frame boundary."""
import math
import threading
import time

GPIO_ADDRESSES={
    'bbox_shadow_gpio0':0x41260000,'bbox_shadow_gpio1':0x41270000,
    'bbox_ack_gpio':0x41280000,'bbox_shadow_gpio2':0x412b0000,
    'bbox_shadow_gpio3':0x412c0000,
}
NAMES=('open_palm','pinch_point')


class DualBboxOverlay:
    def __init__(self,camera,timeout=1.):
        entries={name:camera.overlay.ip_dict.get(name,{}) for name in GPIO_ADDRESSES}
        for name,address in GPIO_ADDRESSES.items():
            entry=entries[name]
            if (int(entry.get('phys_addr',-1))!=address or int(entry.get('addr_range',0))!=0x10000 or
                    entry.get('type')!='xilinx.com:ip:axi_gpio:2.0'):
                raise ValueError('Qualified dual-slot GPIO is missing or misplaced: '+name)
        self.regs={name:camera._make_mmio(entry) for name,entry in entries.items()}
        for name in ('bbox_shadow_gpio0','bbox_shadow_gpio1','bbox_shadow_gpio2','bbox_shadow_gpio3'):
            self.regs[name].write(4,0);self.regs[name].write(12,0)
        self.toggle=int(self.regs['bbox_ack_gpio'].read(0))&1
        self.timeout=timeout;self.lock=threading.Lock();self.commits=0;self.last=None

    @staticmethod
    def select(detections):
        rows=[]
        for detection in detections:
            label,confidence=detection.label,float(detection.confidence)
            if label not in NAMES:continue
            if not math.isfinite(confidence) or not 0<=confidence<=1:
                raise ValueError('Expected finite detection confidence in [0,1]')
            if len(detection.box)!=4 or not all(math.isfinite(float(v)) for v in detection.box):
                raise ValueError('Expected four finite video coordinates')
            box=[round(float(v)) for v in detection.box]
            box=[max(0,min(v,1279 if i%2==0 else 719)) for i,v in enumerate(box)]
            if box[0]>box[2] or box[1]>box[3]:raise ValueError('Inverted detection box')
            rows.append(dict(label=label,class_id=NAMES.index(label),confidence=confidence,xyxy=box))
        # Palms are the requested two-hand display. Pinches use vacant slots;
        # this display selection never filters the application's detections.
        palms=sorted((r for r in rows if r['label']=='open_palm'),key=lambda r:-r['confidence'])[:2]
        other=sorted((r for r in rows if r['label']=='pinch_point'),key=lambda r:-r['confidence'])[:2-len(palms)]
        return sorted(palms+other,key=lambda r:(r['xyxy'][0],r['xyxy'][1]))

    def show(self,detections):
        rows=self.select(detections)
        payload=[]
        for row in rows:
            x1,y1,x2,y2=row['xyxy']
            color=row['class_id']  # Same green/cyan encoding as accepted RTL.
            payload.append((1|(color<<1)|(round(row['confidence']*65535)<<3),x1|(y1<<11),x2|(y2<<11)))
        payload += [(0,0,0)]*(2-len(payload))
        started=time.monotonic()
        with self.lock:
            ack=self.regs['bbox_ack_gpio']
            if int(ack.read(0))&1!=self.toggle:
                raise RuntimeError('Previous dual-slot frame commit is still pending; payload retained')
            first,second,third,fourth=(self.regs['bbox_shadow_gpio'+str(i)] for i in range(4))
            for register,offset,value in ((first,0,payload[0][0]),(first,8,payload[0][1]),(second,0,payload[0][2]),
                                          (third,0,payload[1][0]),(third,8,payload[1][1]),(fourth,0,payload[1][2])):
                register.write(offset,value)
            self.toggle ^= 1
            second.write(8,self.toggle)
            deadline=time.monotonic()+self.timeout
            while int(ack.read(0))&1!=self.toggle:
                if time.monotonic()>=deadline:
                    raise TimeoutError('Dual-slot frame-start ACK timed out; no forced reset')
                time.sleep(.002)
            finished=time.monotonic();self.commits+=1
            self.last=dict(shown=rows,visible_slots=len(rows),commit_toggle=self.toggle,
                           commit_ack_monotonic=finished,commit_ms=(finished-started)*1000,
                           semantics='Two active hardware slots, common frame-start ACK; not physical screen latency')
            return self.last

    def clear(self):return self.show(())


class OverlayPresenter:
    def __init__(self,camera,max_age_s=3.):
        self.client=DualBboxOverlay(camera);self.max_age_s=max_age_s
        self.last_sequence=0;self.last_finished=None;self.last_captured=None;self.visible_slots=0
        self.stale_clears=0;self.errors=0;self.last_result=None
        self.clear()

    def clear(self):
        result=self.client.clear();self.visible_slots=0;return result

    def poll_worker(self,worker,now=None):
        now=time.perf_counter() if now is None else now
        result=worker.poll(self.last_sequence)
        if result is not None:
            self.last_sequence=result.sequence;self.last_finished=result.finished_at;self.last_captured=result.captured_at
            invalid=bool(result.error) or now-result.captured_at>self.max_age_s
            rendered=self.client.show(()) if invalid else self.client.show(result.detections)
            self.visible_slots=rendered['visible_slots'];self.errors+=bool(result.error)
            self.last_result=dict(sequence=result.sequence,captured_at=result.captured_at,
                                  finished_at=result.finished_at,source_age_ms=(now-result.captured_at)*1000,
                                  discarded_stale=invalid,rendered=rendered)
            return rendered
        if self.visible_slots and self.last_captured is not None and now-self.last_captured>self.max_age_s:
            self.stale_clears+=1;return self.clear()
        return None

    def metrics(self):
        return dict(hardware_slots=2,visible_slots=self.visible_slots,last_sequence=self.last_sequence,
                    commits=self.client.commits,last_result=self.last_result,
                    last_commit=self.client.last,stale_clears=self.stale_clears,
                    inference_errors=self.errors,display_policy='two palms first; pinches use vacant slots; left-to-right',
                    max_result_age_s=self.max_age_s)
