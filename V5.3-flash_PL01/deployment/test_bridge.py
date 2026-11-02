import tempfile
import unittest
from pathlib import Path
from unittest import mock
import bridge

class BridgeTests(unittest.TestCase):
    def setUp(self):
        bridge.STOP_REQUESTED=False

    def test_active_requires_same_boot_pid_and_process_identity(self):
        info=dict(MainPID='42',ActiveState='active',SubState='running',ControlGroup='/system.slice/s.service')
        status=dict(phase='READY',owner_pid=42,boot_id='boot')
        handoff=dict(state='ACTIVE',owner_pid=42,boot_id='boot')
        with mock.patch.object(bridge,'proc_identity',return_value=True):
            self.assertTrue(bridge.active_matches('boot',info,status,handoff))
            self.assertFalse(bridge.active_matches('next',info,status,handoff))
            status['phase']='RUNNING'
            self.assertFalse(bridge.active_matches('boot',info,status,handoff))
            status['phase']='READY'
            status['owner_pid']=43
            self.assertFalse(bridge.active_matches('boot',info,status,handoff))
            status['owner_pid']=42; handoff['state']='UNKNOWN'
            self.assertFalse(bridge.active_matches('boot',info,status,handoff))

    def test_success_orders_stop_lock_validation_and_exec(self):
        calls=[]
        class FakePath:
            def read_text(self): return 'boot'
        with mock.patch.object(bridge,'BOOT',FakePath()), mock.patch.object(bridge.os,'geteuid',return_value=0,create=True), \
             mock.patch.object(bridge.signal,'signal'), mock.patch.object(bridge,'wait_for_active',side_effect=lambda boot:(42,'/system.slice/s.service')), \
             mock.patch.object(bridge,'stop_and_wait',side_effect=lambda *x:calls.append('stop')), \
             mock.patch.object(bridge,'validate_handoff_locked',side_effect=lambda *x:calls.append('validate')), \
             mock.patch.object(bridge.os,'chdir'), mock.patch.object(bridge.os,'execv',side_effect=lambda *x:calls.append('exec')), \
             mock.patch.object(Path,'exists',return_value=False):
            bridge.main()
        self.assertEqual(['stop','validate','exec'],calls)

    def test_stop_failure_never_validates_or_starts(self):
        class FakePath:
            def read_text(self): return 'boot'
        with mock.patch.object(bridge,'BOOT',FakePath()), mock.patch.object(bridge.os,'geteuid',return_value=0,create=True), \
             mock.patch.object(bridge.signal,'signal'), mock.patch.object(bridge,'wait_for_active',return_value=(42,'/system.slice/s.service')), \
             mock.patch.object(bridge,'stop_and_wait',side_effect=bridge.Abort('bad stop')), \
             mock.patch.object(bridge,'validate_handoff_locked') as validate, mock.patch.object(bridge.os,'execv') as execute:
            with self.assertRaises(bridge.Abort): bridge.main()
            validate.assert_not_called(); execute.assert_not_called()

    def test_shutdown_requested_after_handoff_never_execs(self):
        class FakePath:
            def read_text(self): return 'boot'
        def signal_during_validation(*args): bridge.request_stop()
        with mock.patch.object(bridge,'BOOT',FakePath()), mock.patch.object(bridge.os,'geteuid',return_value=0,create=True), \
             mock.patch.object(bridge.signal,'signal'), mock.patch.object(bridge,'wait_for_active',return_value=(42,'/system.slice/s.service')), \
             mock.patch.object(bridge,'stop_and_wait'), mock.patch.object(bridge,'validate_handoff_locked',side_effect=signal_during_validation), \
             mock.patch.object(bridge.os,'execv') as execute:
            with self.assertRaises(bridge.Abort): bridge.main()
            execute.assert_not_called()

    def test_normal_stop_requires_consistent_proofs_and_empty_cgroup(self):
        class FakePath:
            def read_text(self): return 'boot'
        info=dict(MainPID='0',ActiveState='inactive',SubState='dead',ControlGroup='')
        status=dict(phase='STOPPED',owner_pid=42,boot_id='boot',shutdown_verified=True)
        handoff=dict(state='QUIESCENT',owner_pid=42,boot_id='boot')
        cp=mock.Mock(returncode=0,stderr='')
        with mock.patch.object(bridge,'BOOT',FakePath()), mock.patch.object(bridge.subprocess,'run',return_value=cp), \
             mock.patch.object(bridge,'unit_info',return_value=info), mock.patch.object(bridge,'read_json',side_effect=[status,handoff]), \
             mock.patch.object(Path,'exists',return_value=False), mock.patch.object(bridge,'cgroup_empty',return_value=True):
            self.assertIs(handoff,bridge.stop_and_wait('boot',42,'/system.slice/s.service',timeout=1))

    def test_normal_stop_rejects_mismatched_owner(self):
        class FakePath:
            def read_text(self): return 'boot'
        info=dict(MainPID='0',ActiveState='inactive',SubState='dead',ControlGroup='')
        status=dict(phase='STOPPED',owner_pid=42,boot_id='boot',shutdown_verified=True)
        handoff=dict(state='QUIESCENT',owner_pid=43,boot_id='boot')
        cp=mock.Mock(returncode=0,stderr='')
        with mock.patch.object(bridge,'BOOT',FakePath()), mock.patch.object(bridge.subprocess,'run',return_value=cp), \
             mock.patch.object(bridge,'unit_info',return_value=info), mock.patch.object(bridge,'read_json',side_effect=[status,handoff]), \
             mock.patch.object(bridge.time,'monotonic',side_effect=[0,0,101]), mock.patch.object(bridge.time,'sleep'):
            with self.assertRaises(bridge.Abort): bridge.stop_and_wait('boot',42,'/system.slice/s.service',timeout=100)

    def test_idle_check_rejects_artifact_mismatch_and_active_dma(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            bit=Path(d)/'loaded.bit'; hwh=bit.with_suffix('.hwh')
            bit.write_bytes(b'bit'); hwh.write_text('<ROOT/>')
            sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            rec=dict(bitstream=dict(path=str(bit),sha256=sha(bit)),hwh=dict(path=str(hwh),sha256=sha(hwh)),
                     idle_registers=dict(mm2s_control=0,mm2s_status=1,s2mm_control=0,s2mm_status=1))
            bridge.check_quiescent_overlay(rec,str(bit))
            rec['idle_registers']['mm2s_control']=1
            with self.assertRaises(bridge.Abort): bridge.check_quiescent_overlay(rec,str(bit))
            rec['idle_registers']['mm2s_control']=0; bit.write_bytes(b'different')
            with self.assertRaises(bridge.Abort): bridge.check_quiescent_overlay(rec,str(bit))

if __name__=='__main__': unittest.main()
