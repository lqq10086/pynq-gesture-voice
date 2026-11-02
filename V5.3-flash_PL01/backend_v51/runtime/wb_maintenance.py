"""Bounded per-write owner ACK protocol; raw video counters are never reduced.

Only DVP drop events inside an identity-bound, acknowledged WB interval can be
classified UNCLASSIFIED_DURING_WB. This does not prove their individual cause or
count as steady acceptance. All other errors remain fatal. One full clean video
statistics interval after END is mandatory before another Sensor write.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

SCHEMA='wb-maintenance-v1'
REQUEST='wb_maintenance_request.json'
ACK='wb_maintenance_ack.json'
LOG='wb_maintenance_intervals.jsonl'
OTHER_ERRORS=('fifo_overflow_events','dvp_axis_errors','s2mm_axis_errors','s2mm_axi_errors')


class MaintenanceError(RuntimeError): pass


def write_json(path,value):
    path=Path(path)
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def owner_identity(root,output,status):
    """Same immutable identity as sensor_wb.read_identity, from this live owner."""
    root,output=Path(root).resolve(),Path(output).resolve()
    pid=os.getpid()
    if status['owner_pid']!=pid: raise MaintenanceError('Owner identity PID differs')
    bit=[p for p in status['artifact_identity'] if p.endswith('.bit')]
    if len(bit)!=1: raise MaintenanceError('Owner must bind exactly one loaded bit')
    bit=Path(bit[0]);hwh=bit.with_suffix('.hwh')
    proc=Path('/proc')/str(pid)
    tick=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
    return dict(root=str(root),output=str(output),owner_pid=pid,owner_start_tick=tick,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                qualification_sha256=hashlib.sha256((root/'runtime/runtime_qualification.json').read_bytes()).hexdigest(),
                numeric_qualification_sha256=status['numeric_qualification_sha256'],
                bitstream=dict(path=str(bit),sha256=status['artifact_identity'][str(bit)]['sha256']),
                hwh=dict(path=str(hwh),sha256=status['artifact_identity'][str(hwh)]['sha256']))


class ClientMaintenance:
    def __init__(self,identity,identity_check,*,monotonic=time.monotonic,sleep=time.sleep,ack_timeout=3.5):
        self.identity,self.identity_check=identity,identity_check
        self.output=Path(identity['output']).resolve()
        self.monotonic,self.sleep,self.ack_timeout=monotonic,sleep,ack_timeout
        self.windows=[]

    def _wait(self,ticket,states,timeout=None):
        deadline=min(ticket['deadline_monotonic'],self.monotonic()+(self.ack_timeout if timeout is None else timeout))
        while True:
            if self.identity_check()!=self.identity: raise MaintenanceError('Live owner changed while waiting for maintenance ACK')
            path=self.output/ACK
            if path.is_file():
                ack=json.loads(path.read_text())
                if ack.get('nonce')==ticket['nonce']:
                    if ack.get('identity')!=self.identity: raise MaintenanceError('Maintenance ACK has wrong owner identity')
                    if ack.get('state') in ('REJECTED','FAILED'): raise MaintenanceError('Owner rejected maintenance: '+ack.get('error','unknown'))
                    if ack.get('state') in states:
                        if ack.get('expected_sequence')!=ticket['expected_sequence']: raise MaintenanceError('Maintenance ACK sequence differs')
                        return ack
            if self.monotonic()>=deadline: raise MaintenanceError('No bounded matching owner maintenance ACK; no Sensor write/retry authorized')
            self.sleep(.02)

    def begin(self,journal,expected_sequence):
        path=Path(journal).resolve()
        if self.output not in path.parents: raise MaintenanceError('Mutation journal must be inside current owner output')
        original=json.loads(path.read_text())
        if original.get('schema')!='sensor-wb1-journal-v1' or original.get('identity')!=self.identity or original.get('state')!='PREPARED':
            raise MaintenanceError('Durable original-state journal is not PREPARED for this owner')
        now=self.monotonic()
        ticket=dict(schema=SCHEMA,nonce=uuid.uuid4().hex,identity=self.identity,journal=str(path),
                    expected_sequence=expected_sequence,created_monotonic=now,deadline_monotonic=now+45.,stage='BEGIN')
        write_json(self.output/REQUEST,ticket)
        ack=self._wait(ticket,{'BEGIN_ACK'})
        ticket['begin_ack']=ack
        self.windows.append(ticket)
        return ticket

    def end(self,ticket,response,success):
        request={k:v for k,v in ticket.items() if k!='begin_ack'}
        request.update(stage='END',response=response,operation_state='COMPLETE' if success else 'FAILED')
        write_json(self.output/REQUEST,request)
        # Owner drain is bounded at 5 s; allow one 1.5 s health-tick/ACK margin.
        ack=self._wait(ticket,{'CLOSED_COMPLETE','CLOSED_FAILED'},timeout=max(self.ack_timeout,6.5))
        ticket['end_ack']=ack
        return ack


class OwnerMaintenance:
    def __init__(self,output,identity,*,monotonic=time.monotonic):
        self.output,self.identity=Path(output).resolve(),identity
        self.monotonic=monotonic
        self.active=None
        self.last_nonce=None
        self.intervals=[]

    def _ack(self,request,state,**extra):
        ack=dict(schema=SCHEMA,nonce=request.get('nonce'),identity=self.identity,state=state,
                 expected_sequence=request.get('expected_sequence'),owner_monotonic=self.monotonic(),**extra)
        write_json(self.output/ACK,ack)
        return ack

    def _log(self,event):
        with (self.output/LOG).open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event)+'\n');stream.flush();os.fsync(stream.fileno())

    def _validate(self,request,now):
        if (request.get('schema')!=SCHEMA or request.get('identity')!=self.identity
                or not re.fullmatch('[0-9a-f]{32}',str(request.get('nonce','')))):
            raise MaintenanceError('Maintenance nonce/owner identity differs')
        created,deadline=request['created_monotonic'],request['deadline_monotonic']
        if not 0<deadline-created<=45.001 or not created<=now<deadline:
            raise MaintenanceError('Maintenance window is expired or unbounded')
        path=Path(request['journal']).resolve()
        if self.output not in path.parents: raise MaintenanceError('Maintenance journal escapes current owner output')
        journal=json.loads(path.read_text())
        if (journal.get('schema')!='sensor-wb1-journal-v1' or journal.get('identity')!=self.identity
                or journal.get('state') not in ('PREPARED','COMPLETE','FAILED')):
            raise MaintenanceError('Mutation journal identity/state differs')
        sequence=request['expected_sequence']
        if type(sequence) is not int or not 0<=sequence<=0xffffffff: raise MaintenanceError('Invalid mutation sequence')

    def _ready(self,video,endpoint,request,rates):
        # video is read_video_health(camera), freshly read by the actual owner.
        # camera_control decodes PL_COUNTS[25:22]; ov5640_af RUN is 12. RUN is
        # command-ACK state, not evidence that an optical image is focused.
        live=video['live'];command=video['status']['command']
        if (not video['healthy'] or live.get('af_state')!=12 or live.get('af_error')!=0
                or not live.get('config_done') or live.get('pending')
                or any(command.get(k) for k in ('busy','fatal','request_error'))):
            raise MaintenanceError('Fresh video/config/AF RUN12 without error is required before write ACK')
        if endpoint['abi']!=0x57423431 or endpoint['status'] & 0x11 or not endpoint['status']&8:
            raise MaintenanceError('WB endpoint not ready or fatal before write ACK')
        if (endpoint['accepted']+1)&0xffffffff!=request['expected_sequence']:
            raise MaintenanceError('WB request sequence changed before write ACK')
        if rates['dvp_drop_events'] or any(rates[k] for k in OTHER_ERRORS):
            raise MaintenanceError('No clean pre-write video statistics baseline')

    def evaluate(self,previous,current,rates,video,endpoint_factory):
        now=self.monotonic()
        closed_failed=False
        request_path=self.output/REQUEST
        request=json.loads(request_path.read_text()) if request_path.is_file() else None
        endpoint=endpoint_factory() if self.active is not None or (request and request.get('nonce')!=self.last_nonce) else None
        try:
            command=video.get('status',{}).get('command',{})
            if not video['healthy'] or any(rates[k] for k in OTHER_ERRORS):
                raise MaintenanceError('Video/FIFO/AXIS/AXI error is never exempt from maintenance')
            if self.active is not None and (video.get('live',{}).get('af_error')!=0
                    or any(command.get(key) for key in ('fatal','request_error'))):
                raise MaintenanceError('AF/legacy control error during WB maintenance')
            if endpoint is not None and endpoint['status']&0x10:
                raise MaintenanceError('WB FATAL is never exempt from maintenance')
            if self.active is None:
                if rates['dvp_drop_events']: raise MaintenanceError('Uncontrolled DVP drop outside a WB transition')
                if request and request.get('nonce')!=self.last_nonce:
                    try:
                        self._validate(request,now)
                        if request['stage']!='BEGIN': raise MaintenanceError('No active BEGIN for this END')
                        if request_path.resolve().parent!=self.output: raise MaintenanceError('Request path escapes owner output')
                        self._ready(video,endpoint,request,rates)
                    except (MaintenanceError,KeyError,ValueError,OSError) as error:
                        self.last_nonce=request.get('nonce');self._ack(request,'REJECTED',error=str(error))
                    else:
                        self.active=dict(request=request,begin_monotonic=now,first_statistics=current,
                                         last_statistics=current,end_seen=None)
                        self._ack(request,'BEGIN_ACK',pre_write_video=video,statistics=current)
                        self._log(dict(event='BEGIN_ACK',nonce=request['nonce'],board_monotonic=now,
                                       classification='WB_TRANSITION_NOT_STEADY_ACCEPTANCE',statistics=current,video=video))
            else:
                active=self.active;original=active['request']
                self._validate(original,now)
                if request is None or request.get('nonce')!=original['nonce'] or request.get('identity')!=self.identity:
                    raise MaintenanceError('Active maintenance request/owner changed')
                if any(request.get(key)!=original.get(key) for key in
                       ('schema','nonce','identity','journal','expected_sequence','created_monotonic','deadline_monotonic')):
                    raise MaintenanceError('Immutable maintenance request fields changed')
                if request['stage']=='END':
                    self._validate(request,now)
                    response=request.get('response')
                    if request.get('operation_state') not in ('COMPLETE','FAILED'):
                        raise MaintenanceError('END has no explicit operation outcome')
                    if request['operation_state']=='COMPLETE' and (response is None or response.get('flags')!=0x403):
                        raise MaintenanceError('END cannot claim success for a failed/unverified Sensor result')
                    if response is not None:
                        if (response.get('sequence')!=original['expected_sequence']
                                or endpoint['accepted']!=original['expected_sequence']
                                or endpoint['completed']!=original['expected_sequence']
                                or endpoint['flags']!=response.get('flags') or endpoint['status']&0x11):
                            raise MaintenanceError('END does not match the actual completed WB command')
                    elif endpoint['accepted']!=(original['expected_sequence']-1)&0xffffffff or endpoint['status']&0x11:
                        raise MaintenanceError('Failed command without response has uncertain hardware state')
                    if active['end_seen'] is None:
                        active['end_seen']=now;active['end_request']=request;active['end_baseline']=current
                    # A sub-1 s sample followed by a 1.3 s tick can need >3 s.
                    elif now-active['end_seen']>5.:
                        raise MaintenanceError('No clean complete video statistics interval after END')
                    elif (previous==active['last_statistics']
                            and previous.get('host_request_monotonic',-1e9)>=active['end_seen']
                            and not rates['dvp_drop_events'] and rates['window_s']>=1.
                            and current['sequence']!=previous['sequence']
                            and all(rates[k]>0 for k in ('vsync_events','pl_complete_frames','s2mm_complete_frames'))):
                        successful=request.get('operation_state')=='COMPLETE'
                        closed_failed=not successful
                        interval=dict(nonce=original['nonce'],expected_sequence=original['expected_sequence'],
                            journal=original['journal'],begin_monotonic=active['begin_monotonic'],end_monotonic=now,
                            classification='WB_TRANSITION_NOT_STEADY_ACCEPTANCE',operation_state=request.get('operation_state'),
                            first_statistics=active['first_statistics'],last_statistics=current,
                            clean_statistics_window=dict(previous=previous,current=current,rates=rates))
                        self.intervals.append(interval)
                        self._log(dict(event='CLOSED',**interval))
                        self._ack(original,'CLOSED_COMPLETE' if successful else 'CLOSED_FAILED',interval=interval)
                        self.last_nonce=original['nonce'];self.active=None
                elif request['stage']!='BEGIN': raise MaintenanceError('Unknown active maintenance stage')
                if self.active is not None: self.active['last_statistics']=current
            transition=self.active is not None
            classification=('UNCLASSIFIED_DURING_WB' if transition else
                            'FAILED_WB_WITH_CLEAN_VIDEO_WINDOW' if closed_failed else 'STEADY')
            if transition:
                self._log(dict(event='STATISTICS',nonce=self.active['request']['nonce'],board_monotonic=now,
                    classification=classification,previous=previous,current=current,rates=rates))
            return dict(hardware_healthy=not transition and not closed_failed,transition=transition,classification=classification,
                        rates=rates,active_nonce=self.active['request']['nonce'] if transition else None,
                        interval_log=str(self.output/LOG),completed_intervals=len(self.intervals))
        except (MaintenanceError,KeyError,ValueError,OSError) as error:
            if self.active is not None:
                if not self.active.get('failure_recorded'):
                    self.intervals.append(dict(nonce=self.active['request']['nonce'],
                        expected_sequence=self.active['request']['expected_sequence'],journal=self.active['request']['journal'],
                        begin_monotonic=self.active['begin_monotonic'],end_monotonic=now,
                        classification='FAILED_WB_TRANSITION_NOT_STEADY_ACCEPTANCE',operation_state='FAILED',
                        exclude_through_owner_shutdown=True,first_statistics=self.active['first_statistics'],
                        last_statistics=current,error=str(error)))
                    self.active['failure_recorded']=True
                self._ack(self.active['request'],'FAILED',error=str(error),previous_statistics=previous,
                          current_statistics=current,rates=rates)
                self._log(dict(event='FAILED',nonce=self.active['request']['nonce'],board_monotonic=now,
                               previous=previous,current=current,rates=rates,error=str(error)))
            raise MaintenanceError(str(error)) from error

    def performance_metadata(self):
        active=self.active
        open_interval=None if active is None else dict(
            nonce=active['request']['nonce'],expected_sequence=active['request']['expected_sequence'],
            journal=active['request']['journal'],begin_monotonic=active['begin_monotonic'],end_monotonic=None,
            state='UNKNOWN_END',classification='WB_TRANSITION_NOT_STEADY_ACCEPTANCE',
            exclude_through_owner_shutdown=True,first_statistics=active['first_statistics'],
            last_statistics=active['last_statistics'])
        return dict(clock='time.monotonic',interval_log=str(self.output/LOG),intervals=self.intervals,
                    open_interval=open_interval,
                    policy='Exclude samples whose source/compute/display intervals intersect WB transitions from steady acceptance; raw counters are preserved and individual DVP drop causes are unclassified.')
