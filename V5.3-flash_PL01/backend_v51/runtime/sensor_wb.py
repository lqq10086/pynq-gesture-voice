"""Attach-only WB1 SCCB client. Imports do not load PYNQ or touch hardware.

Use ``with attach(root) as client``. Reads never change Sensor state. Writes
require allow_write=True, a new durable journal, and one hardware arm per command.
Repeated-bank gain sampling is stable sampling, not a simultaneous sensor latch.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import xml.etree.ElementTree as ET

LEGACY_STATUS = 0x08
LEGACY_ID = 0x10
ABI, STATUS, REQ0, REQ1, REQ2 = 0x18, 0x1c, 0x20, 0x24, 0x28
SUBMIT, ACCEPTED, COMPLETED, AUTHORIZE, FLAGS = 0x2c, 0x30, 0x34, 0x38, 0x3c
DATA0, DATA1 = 0xd4, 0xd8
EXPECTED_ABI = 0x57423431
EXPECTED_LEGACY_ID = 0x56323543
SUBMIT_MAGIC, ARM_MAGIC = 0x53434342, 0x57424731
BUSY, COMPLETION_VALID, ARMED, READY, FATAL_STATUS = 1, 2, 4, 8, 16
DONE, SUCCESS, NACK, TIMEOUT, VERIFY_FAIL = 1, 2, 4, 8, 16
POLICY_REJECT, COHERENCY_FAIL, ROLLED_BACK, ROLLBACK_FAILED, FATAL, DATA_VALID = 32, 64, 128, 256, 512, 1024
READ8, CURRENT_GAINS, MANUAL_GAINS, RMW8, SET_GAINS = 1, 2, 3, 0x10, 0x11
SUCCESS_FLAGS = DONE | SUCCESS | DATA_VALID
WRITE_MASKS = {0x5001: 1, 0x3406: 1, 0x5196: 0x20,
               0x3400: 15, 0x3402: 15, 0x3404: 15,
               0x3401: 255, 0x3403: 255, 0x3405: 255}
READ_RANGES = ((0x3400,0x3406), (0x3500,0x350d), (0x3a00,0x3a1f),
               (0x5180,0x51a4), (0x5380,0x538b), (0x5480,0x5490),
               (0x5580,0x558b), (0x5800,0x5849))
READ_SINGLE = {0x300a,0x300b,0x4300,0x501f,0x5000,0x5001,0x501d,0x503d,0x4741}
DEFAULT_LOCK = Path('/run/lock/ax7020-sensor-wb.lock')


class SensorWbError(RuntimeError):
    """No valid sensor value exists for the failed request."""
    def __init__(self, message, *, response=None):
        super().__init__(message)
        self.response = response


class _ExclusiveLock:
    def __init__(self, path): self.path, self.stream = Path(path), None
    def __enter__(self):
        import fcntl
        self.stream = self.path.open('a+')
        try: fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise
        return self
    def __exit__(self, *args): self.stream.close()


def _sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _artifact_digest(path,cache,*,initial):
    """Private attach cache: qualified bytes once, immutable stat identity later."""
    path=Path(path)
    def signature():
        stat=path.stat()
        return stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
    before=signature()
    if cache is None:
        digest=_sha(path)
    elif initial:
        digest=_sha(path)
    else:
        saved=cache.get(str(path))
        if saved is None or saved[0]!=before:
            raise SensorWbError('Qualified bit/HWH stat identity changed; attach/full verification required')
        digest=saved[1]
    if signature()!=before:raise SensorWbError('Bit/HWH changed during identity verification')
    if cache is not None and initial:cache[str(path)]=(before,digest)
    return digest


def _load_qualification(root):
    name = '_sensor_wb_candidate_qualification'
    spec = importlib.util.spec_from_file_location(name, root / 'runtime/qualification.py')
    module = importlib.util.module_from_spec(spec)
    # qualification.py has the project's established local package_contract import.
    sys.path.insert(0, str(root / 'runtime'))
    try: spec.loader.exec_module(module)
    finally: sys.path.pop(0)
    if Path(module.ROOT).resolve() != root:
        raise SensorWbError('Qualification root differs from attached candidate')
    return module


def read_identity(root, *, verify_qualification=True, _artifact_cache=None):
    """Verify a live formal owner and its own bit/HWH/numeric qualification.

    The initial call runs the full existing qualification validators. Subsequent
    calls compare qualified record hashes and live owner. Attach may privately
    cache verified bit/HWH digests while requiring unchanged file stat identity.
    No predecessor success flag is accepted as candidate qualification.
    """
    root = Path(root).resolve()
    module = _load_qualification(root)
    record_path = root / module.RECORD
    numeric_path = root / module.NUMERIC_REPORT
    latest = json.loads((root / 'reports/latest_run.json').read_text())
    pid = int(latest['pid'])
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    handoff = json.loads(Path('/run/ax7020-v300-handoff.json').read_text())
    if not boot or handoff.get('boot_id') != boot or handoff.get('state') != 'ACTIVE' or handoff.get('owner_pid') != pid:
        raise SensorWbError('Current boot, ACTIVE handoff and latest owner disagree')
    proc = Path('/proc') / str(pid)
    if (proc / 'cwd').resolve() != root or os.fsencode(str(root / 'run_board_apps.py')) not in (proc / 'cmdline').read_bytes().split(b'\0'):
        raise SensorWbError('Live formal owner does not belong to requested candidate root')
    start_tick = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    output = Path(latest['output']).resolve()
    if root / 'reports' not in output.parents:
        raise SensorWbError('Owner output escapes candidate reports')
    status_path = output / 'status.json'
    status = json.loads(status_path.read_text())
    if (status.get('status') != 'RUNNING_REAL_CAMERA_HDMI_APPS' or status.get('owner_pid') != pid
            or status.get('execution_role') != 'FORMAL_PL'
            or not 0 <= time.time() - status_path.stat().st_mtime <= 15):
        raise SensorWbError('Current owner is not a fresh, formal running camera owner')
    record = json.loads(record_path.read_text())
    fixed = record['fixed']
    bits = [name for name in fixed if name.endswith('.bit')]
    if len(bits) != 1: raise SensorWbError('Qualification does not bind exactly one bitstream')
    bit = (root / bits[0]).resolve()
    hwh = bit.with_suffix('.hwh')
    identities = {}
    for key, path in [('bitstream',bit), ('hwh',hwh)]:
        relative = path.relative_to(root).as_posix()
        digest = _artifact_digest(path,_artifact_cache,initial=verify_qualification)
        if (fixed.get(relative) != digest or handoff.get(key, {}).get('path') != str(path)
                or handoff.get(key, {}).get('sha256') != digest
                or status.get('artifact_identity', {}).get(str(path), {}).get('sha256') != digest):
            raise SensorWbError('Loaded '+key+' differs from candidate qualification or live owner')
        identities[key] = dict(path=str(path), sha256=digest)
    numeric = json.loads(numeric_path.read_text())
    decoder = numeric.get('identity', {}).get('decoder')
    if verify_qualification:
        module.verify_runtime(root=root)
        module.require_numeric(root, decoder)
    if status.get('numeric_qualification_sha256') != _sha(numeric_path):
        raise SensorWbError('Live owner numeric qualification hash differs')
    return dict(root=str(root), boot_id=boot, owner_pid=pid, owner_start_tick=start_tick,
                output=str(output), qualification_sha256=_sha(record_path),
                numeric_qualification_sha256=_sha(numeric_path), **identities)


def _control_range(hwh):
    root = ET.parse(hwh).getroot()
    modules = [m for m in root.findall('.//MODULE') if m.get('INSTANCE') == 'camera_control_0']
    if len(modules) != 1 or modules[0].get('VLNV') != 'xilinx.com:module_ref:camera_control_axi_v33:1.0':
        raise SensorWbError('Matching HWH has no recognized camera control module')
    ranges = {(int(m.get('BASEVALUE'),0), int(m.get('HIGHVALUE'),0))
              for m in root.findall('.//MEMRANGE') if m.get('INSTANCE') == 'camera_control_0'}
    if len(ranges) != 1: raise SensorWbError('Ambiguous camera control HWH address range')
    base, last = ranges.pop()
    if last - base + 1 < DATA1 + 4: raise SensorWbError('HWH range cannot contain WB1 endpoint')
    return base, last - base + 1


def attach(root=None, *, allow_write=False, mmio_factory=None, lock_path=DEFAULT_LOCK):
    """Attach to qualified running hardware. Never reload Overlay or allocate DMA."""
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    artifact_cache={}
    identity = read_identity(root,_artifact_cache=artifact_cache)
    base, size = _control_range(identity['hwh']['path'])
    if mmio_factory is None:
        from pynq import MMIO
        mmio_factory = MMIO
    mmio = mmio_factory(base, size)
    if int(mmio.read(LEGACY_ID)) != EXPECTED_LEGACY_ID:
        raise SensorWbError('Camera hardware identity mismatch')
    try:
        from .wb_maintenance import ClientMaintenance
    except ImportError:
        from wb_maintenance import ClientMaintenance
    identity_check=lambda: read_identity(root, verify_qualification=False,_artifact_cache=artifact_cache)
    return SensorWbClient(mmio, identity=identity,
        identity_check=identity_check,
        allow_write=allow_write, lock_factory=lambda: _ExclusiveLock(lock_path),
        maintenance=ClientMaintenance(identity,identity_check) if allow_write else None)


def _journal(path, record, *, initial=False):
    path = Path(path)
    target = path if initial else path.with_name(path.name+'.tmp')
    with target.open('x' if initial else 'w', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    if not initial: os.replace(target, path)
    if hasattr(os, 'O_DIRECTORY'):
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


class SensorWbClient:
    def __init__(self, mmio, *, identity, identity_check, allow_write=False,
                 lock_factory=lambda: _ExclusiveLock(DEFAULT_LOCK), timeout=40.,
                 sleep=time.sleep, monotonic=time.monotonic, poll_interval=.001,maintenance=None):
        self.mmio, self.identity, self.identity_check = mmio, identity, identity_check
        self.allow_write, self.lock_factory, self.timeout = allow_write, lock_factory, timeout
        self.sleep, self.monotonic, self.poll_interval = sleep, monotonic, poll_interval
        self._lock, self._poisoned, self.last_response = None, False, None
        self._command_lock = threading.RLock()
        self.history = []
        self.lock_clear_error = None
        self.maintenance,self.maintenance_error=maintenance,None
        self._maintenance_journal=None

    def __enter__(self):
        self._lock = self.lock_factory()
        self._lock.__enter__()
        try: self._check()
        except BaseException:
            self._lock.__exit__(*sys.exc_info())
            self._lock = None
            raise
        return self

    def __exit__(self, *args):
        try:
            # Explicitly enabled clients always leave the endpoint write-locked.
            if self.allow_write: self._clear_authorization(primary=args[1])
        finally:
            self._lock.__exit__(*args)
            self._lock = None

    def _check(self):
        if self._lock is None: raise SensorWbError('Use the client as an exclusive context manager')
        if self._poisoned: raise SensorWbError('Session poisoned by protocol/timeout/fatal; safely stop and reload')
        if self.identity_check() != self.identity: raise SensorWbError('Live owner, boot or qualified identity changed')
        if int(self.mmio.read(ABI)) != EXPECTED_ABI: raise SensorWbError('WB1 hardware ABI mismatch')
        status = int(self.mmio.read(STATUS))
        if status & FATAL_STATUS: raise SensorWbError('WB endpoint fatal; safely stop and reload')
        if status & BUSY or not status & READY or int(self.mmio.read(LEGACY_STATUS)) & 0x0d:
            raise SensorWbError('WB or legacy camera control is busy/not ready')

    def lock_writes(self):
        """Explicit host-lock operation; does not write any Sensor register."""
        self._check()
        self._clear_authorization()
        if int(self.mmio.read(STATUS)) & ARMED: raise SensorWbError('Hardware write lock did not clear arm')

    def _clear_authorization(self, primary=None):
        try:
            self.mmio.write(AUTHORIZE, 0)
        except Exception as error:
            self._poisoned = True
            self.lock_clear_error = str(error)
            if primary is not None:
                # Preserve original sensor flags/exception. The journal includes
                # the separate cleanup failure and no later request is allowed.
                primary.wb_lock_clear_error = self.lock_clear_error
                return
            raise SensorWbError('Hardware authorization clear failed: '+str(error),
                                response=self.last_response) from error

    def _request(self, opcode, req0=0, req1=0, req2=0, *, mutate=False):
        with self._command_lock:
            return self._request_locked(opcode,req0,req1,req2,mutate=mutate)

    def _request_locked(self, opcode, req0=0, req1=0, req2=0, *, mutate=False):
        self._check()
        old = int(self.mmio.read(ACCEPTED))
        expected = (old + 1) & 0xffffffff
        if mutate and not self.allow_write: raise PermissionError('Explicit allow_write=True is required')
        ticket=None
        response=None
        if mutate and self.maintenance is not None:
            if self._maintenance_journal is None: raise SensorWbError('Mutation requires its durable original journal')
            try: ticket=self.maintenance.begin(self._maintenance_journal,expected)
            except Exception as error: raise SensorWbError('No owner permission for Sensor write: '+str(error)) from error
        try:
            self._check()
            if int(self.mmio.read(ACCEPTED))!=old: raise SensorWbError('Sequence changed while waiting for owner write ACK')
            self.mmio.write(REQ0, req0 | opcode)
            self.mmio.write(REQ1, req1)
            self.mmio.write(REQ2, req2)
            if mutate:
                self.mmio.write(AUTHORIZE, ARM_MAGIC)
                if not int(self.mmio.read(STATUS)) & ARMED: raise SensorWbError('One-use hardware authorization failed')
            self.mmio.write(SUBMIT, SUBMIT_MAGIC)
            deadline = self.monotonic() + self.timeout
            while True:
                accepted = int(self.mmio.read(ACCEPTED))
                state = int(self.mmio.read(STATUS))
                if accepted not in (old, expected):
                    self._poisoned = True
                    raise SensorWbError('Unexpected accepted sequence; another controller or reset')
                if state & FATAL_STATUS and not state & COMPLETION_VALID:
                    self._poisoned = True
                    raise SensorWbError('WB endpoint entered fatal state without a result')
                if (accepted == expected and state & COMPLETION_VALID
                        and (not state & BUSY or state & FATAL_STATUS)):
                    sequence = int(self.mmio.read(COMPLETED))
                    flags = int(self.mmio.read(FLAGS))
                    data = int(self.mmio.read(DATA0)) | int(self.mmio.read(DATA1)) << 32
                    end_sequence = int(self.mmio.read(COMPLETED))
                    if sequence != expected or end_sequence != expected or int(self.mmio.read(ACCEPTED)) != expected:
                        self._poisoned = True
                        raise SensorWbError('Stale or torn response sequence')
                    response = dict(sequence=sequence, flags=flags, data=data, status=state)
                    self.last_response = response
                    self.history.append(dict(opcode=opcode, req0=req0|opcode, req1=req1,
                                             req2=req2, response=response))
                    if flags != SUCCESS_FLAGS or state & FATAL_STATUS:
                        if flags & (FATAL | ROLLBACK_FAILED | TIMEOUT) or state & FATAL_STATUS: self._poisoned = True
                        raise SensorWbError('Sensor request failed: flags=0x%03x' % flags, response=response)
                    return response
                if self.monotonic() >= deadline:
                    self._poisoned = True
                    raise SensorWbError('No matching accepted completion before host timeout; no automatic retry')
                self.sleep(self.poll_interval)
        except SensorWbError:
            raise
        except Exception as error:
            self._poisoned = True
            raise SensorWbError('MMIO transport failed; command outcome is uncertain: '+str(error)) from error
        finally:
            if mutate: self._finish_write(ticket,response,sys.exc_info()[1])

    def _finish_write(self,ticket,response,primary):
        cleanup_error=None
        try: self._clear_authorization(primary=primary)
        except Exception as error: cleanup_error=error
        if ticket is not None:
            try: self.maintenance.end(ticket,response,primary is None and cleanup_error is None)
            except Exception as error:
                self.maintenance_error=str(error);self._poisoned=True
                if primary is not None: primary.wb_maintenance_error=str(error)
                elif cleanup_error is None:
                    cleanup_error=SensorWbError('WB owner END/clean-window verification failed: '+str(error),response=response)
        if primary is None and cleanup_error is not None: raise cleanup_error

    def read8(self, address):
        if not isinstance(address,int) or not (address in READ_SINGLE or any(a <= address <= b for a,b in READ_RANGES)):
            raise ValueError('Sensor address is not on the read whitelist')
        response = self._request(READ8, address << 8)
        if response['data'] & ~255: raise SensorWbError('Malformed READ8 data', response=response)
        return response['data']

    def _gains(self, opcode):
        response = self._request(opcode)
        data = response['data']
        gains = tuple((data >> (16*i)) & 65535 for i in range(3))
        if data >> 48 or any(g > 4095 for g in gains): raise SensorWbError('Malformed gain bank data', response=response)
        return gains

    def current_gains(self): return self._gains(CURRENT_GAINS)
    def manual_gains(self): return self._gains(MANUAL_GAINS)

    def snapshot(self):
        """Hardware repeated-bank samples; AUTO low twelve bits may keep moving.

        Scattered raw byte samples are diagnostic, never a simultaneous bank.
        The restoration bank uses opcode3 and independently checked high nibbles.
        """
        controls = {format(a,'04x'):self.read8(a) for a in (0x5001,0x3406,0x5196)}
        raw = {format(a,'04x'):self.read8(a) for a in range(0x3400,0x3406)}
        manual = self.manual_gains()
        current = self.current_gains()
        raw_again = {format(a,'04x'):self.read8(a) for a in range(0x3400,0x3406)}
        controls_again = {format(a,'04x'):self.read8(a) for a in (0x5001,0x3406,0x5196)}
        packed = tuple(((raw[format(a,'04x')] & 15) << 8) | raw[format(a+1,'04x')]
                       for a in (0x3400,0x3402,0x3404))
        manual_mode=bool(controls['3406']&1)
        high_changed=any((raw[format(a,'04x')]^raw_again[format(a,'04x')])&0xf0 for a in (0x3400,0x3402,0x3404))
        if controls != controls_again or high_changed or (manual_mode and (raw != raw_again or packed != manual)):
            raise SensorWbError('Manual/control bank changed during original-state snapshot; no write authorized')
        bank=dict(raw)
        for index,address in enumerate((0x3400,0x3402,0x3404)):
            bank[format(address,'04x')]=(raw[format(address,'04x')]&0xf0)|(manual[index]>>8)
            bank[format(address+1,'04x')]=manual[index]&255
        return dict(controls=controls,manual_bytes=bank,manual_gains=list(manual),current_gains=list(current),
                    mode='manual' if manual_mode else 'auto',gain_bank_dynamic=not manual_mode,
                    gain_bank_source='HARDWARE_REPEATED_MANUAL_GAINS',
                    raw_byte_samples=dict(before=raw,after=raw_again,simultaneous=False))

    def _require_write(self):
        if not self.allow_write: raise PermissionError('Explicit allow_write=True is required')

    def _mutate(self, action, target, path, operation):
        with self._command_lock:
            return self._mutate_locked(action,target,path,operation)

    def _mutate_locked(self, action, target, path, operation):
        self._require_write()
        before = self.snapshot()
        record = dict(schema='sensor-wb1-journal-v1', state='PREPARED', action=action,
                      identity=self.identity, pre=before, target=target, prepared_unix_s=time.time())
        _journal(path, record, initial=True)
        previous_journal=self._maintenance_journal
        self._maintenance_journal=Path(path).resolve()
        first_window=len(self.maintenance.windows) if self.maintenance is not None else 0
        try:
            self._check()
            record['post'] = operation(before)
            record.update(state='COMPLETE', completed_unix_s=time.time())
        except BaseException as error:
            record.update(state='FAILED', error=str(error), completed_unix_s=time.time(),
                          last_response=self.last_response)
            if self.lock_clear_error is not None: record['lock_clear_error'] = self.lock_clear_error
            if self.maintenance_error is not None: record['maintenance_error']=self.maintenance_error
            if self.maintenance is not None: record['maintenance_windows']=self.maintenance.windows[first_window:]
            _journal(path, record)
            raise
        finally:
            self._maintenance_journal=previous_journal
        if self.maintenance is not None: record['maintenance_windows']=self.maintenance.windows[first_window:]
        _journal(path, record)
        return record

    def _rmw(self, address, mask, value):
        response = self._request(RMW8, address << 8 | mask << 24, value, mutate=True)
        data = response['data']
        old, target, verified = data & 255, (data >> 8) & 255, (data >> 16) & 255
        if data >> 24 or target != ((old & ~mask) | (value & mask)) or verified != target:
            self._poisoned = True
            raise SensorWbError('RMW success payload does not verify requested preserved-bit target', response=response)
        return dict(original=old, target=target, readback=verified, response=response)

    def rmw8(self, address, mask, value, journal):
        self._require_write()
        allowed = WRITE_MASKS.get(address,0)
        if not isinstance(mask,int) or not mask or mask & ~allowed or not isinstance(value,int) or not 0 <= value <= 255:
            raise ValueError('Nonzero mask must be a subset of the restricted WB mask; value is one byte')
        return self._mutate('RMW8', dict(address=address, mask=mask, value=value), journal,
                            lambda before:self._rmw(address,mask,value))

    def set_gains(self, red, green, blue, journal):
        self._require_write()
        gains = (red,green,blue)
        if any(not isinstance(g,int) or not 0x100 <= g <= 0xfff for g in gains):
            raise ValueError('Each manual gain must be 0x100..0xfff')
        def operation(before):
            response = self._request(SET_GAINS, req1=red << 8 | green << 20, req2=blue, mutate=True)
            actual = tuple((response['data'] >> (16*i)) & 65535 for i in range(3))
            if actual != gains or response['data'] >> 48:
                self._poisoned = True
                raise SensorWbError('SET_GAINS success payload differs from requested verified bank', response=response)
            after = self.snapshot()
            high_drift = any((after['manual_bytes'][format(a,'04x')] ^ before['manual_bytes'][format(a,'04x')]) & 0xf0
                             for a in (0x3400,0x3402,0x3404))
            other_controls = any(after['controls'][key] != before['controls'][key] for key in ('5001','5196'))
            if (tuple(after['manual_gains']) != gains or high_drift or other_controls
                    or after['controls']['3406'] != (before['controls']['3406'] | 1)):
                self._poisoned = True
                raise SensorWbError('Manual mode/bank or unrelated bits differ after SET_GAINS')
            return dict(gains=list(actual), response=response, snapshot=after)
        return self._mutate('SET_GAINS',dict(gains=list(gains)),journal,operation)

    def restore(self, saved_journal, journal):
        """Restore saved WB bank/target bits; any partial restore is FAILED.

        Hardware verifies/rolls back each command. The complete multi-command
        restore is not atomic. High nibbles cannot be written and must match.
        """
        self._require_write()
        saved = json.loads(Path(saved_journal).read_text())
        if saved.get('schema') != 'sensor-wb1-journal-v1' or saved.get('identity') != self.identity:
            raise SensorWbError('Original-state journal belongs to different hardware/boot/owner')
        original = saved['pre']
        def operation(before):
            for a in (0x3400,0x3402,0x3404):
                key = format(a,'04x')
                if (before['manual_bytes'][key] ^ original['manual_bytes'][key]) & 0xf0:
                    raise SensorWbError('Unwritable high nibble drifted; cannot claim complete original-bank restore')
            responses = [self._rmw(0x3406,1,1)]
            for a in range(0x3400,0x3406):
                responses.append(self._rmw(a,WRITE_MASKS[a],original['manual_bytes'][format(a,'04x')]))
            for a in (0x5001,0x5196):
                responses.append(self._rmw(a,WRITE_MASKS[a],original['controls'][format(a,'04x')]))
            verified_manual=self.snapshot()
            if (verified_manual['manual_bytes']!=original['manual_bytes']
                    or verified_manual['controls']['3406']!=(before['controls']['3406']|1)
                    or any((verified_manual['controls'][format(a,'04x')]^original['controls'][format(a,'04x')])&WRITE_MASKS[a]
                           for a in (0x5001,0x5196))):
                raise SensorWbError('Original gain bank restoration did not verify while manual mode held')
            responses.append(self._rmw(0x3406,1,original['controls']['3406']))
            after = self.snapshot()
            high_drift=any((after['manual_bytes'][format(a,'04x')]^original['manual_bytes'][format(a,'04x')])&0xf0
                           for a in (0x3400,0x3402,0x3404))
            if high_drift or (original['controls']['3406']&1 and after['manual_bytes'] != original['manual_bytes']) or any(
                    (after['controls'][format(a,'04x')] ^ original['controls'][format(a,'04x')]) & WRITE_MASKS[a]
                    for a in (0x5001,0x5196,0x3406)):
                raise SensorWbError('Final original-state restoration readback failed')
            return dict(snapshot=after,responses=responses,verified_manual_snapshot=verified_manual,
                        restoration='ORIGINAL_MANUAL_BANK_VERIFIED' if original['controls']['3406']&1
                        else 'ORIGINAL_CONTROLS_RESTORED_AUTO_GAINS_DYNAMIC')
        return self._mutate('RESTORE',dict(saved_journal=str(Path(saved_journal).resolve()),
                            original=original),journal,operation)
