"""Run with Python directly: all native DLLs and input functions are fake.

No pytest conftest, desktop, hooks, browser or input is used. WIND_CONTROL_SOURCE
can point to the pinned package; --baseline reproduces its five-second race.
"""
from __future__ import annotations
import ctypes
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(os.environ.get('WIND_CONTROL_SOURCE', Path(__file__).parents[1] / 'src'))


class NoNativeCall:
    def __call__(self, *args, **kwargs):
        raise AssertionError('Unexpected native call in offline fixture')


class NoNativeDLL:
    def __getattr__(self, name):
        value = NoNativeCall()
        setattr(self, name, value)
        return value


for name, path in [('windows_mcp', SOURCE/'windows_mcp'),
                   ('windows_mcp.desktop', SOURCE/'windows_mcp/desktop')]:
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
uia = ModuleType('windows_mcp.uia')
uia.IsKeyPressed = lambda key: False
uia.ReleaseKey = uia.ReleaseMouse = uia.RightReleaseMouse = uia.MiddleReleaseMouse = NoNativeCall()
sys.modules['windows_mcp.uia'] = uia
exceptions = ModuleType('fastmcp.exceptions')
exceptions.ToolError = RuntimeError
sys.modules['fastmcp'] = ModuleType('fastmcp')
sys.modules['fastmcp.exceptions'] = exceptions
with patch.object(ctypes, 'WinDLL', lambda *args, **kwargs: NoNativeDLL()):
    control = importlib.import_module('windows_mcp.desktop.control')
    win32 = importlib.import_module('windows_mcp.desktop.control_win32')
trace_module = None
if '--baseline' not in sys.argv:
    trace_module = importlib.import_module('windows_mcp.desktop.control_trace')


def owner_ready():
    owner = control.ControlCoordinator(immediate_capture=True)
    owner._set_locked('ready')
    owner.set_health_probe(lambda: True)
    owner.subscribe(lambda value: owner.arm_visible(value['generation']) if value['state']=='ai' else None)
    token = owner.begin_call('OfflineFixture')
    return owner, token


def fake_user32(owner, *, during_rotation=None):
    handles = iter([11, 12, 21, 22])
    def install(*args):
        handle = next(handles)
        if handle == 21 and during_rotation:
            during_rotation()
        return handle
    return SimpleNamespace(SetWindowsHookExW=install, UnhookWindowsHookEx=lambda *a: 1,
        RegisterClassExW=lambda *a: 1, CreateWindowExW=lambda *a: 777,
        RegisterRawInputDevices=lambda *a: 1, PeekMessageW=lambda *a: 0,
        TranslateMessage=lambda *a: 1, DispatchMessageW=lambda *a: 1,
        DefWindowProcW=lambda *a: 0, DestroyWindow=lambda *a: 1,
        UnregisterClassW=lambda *a: 1)


def monitor_fixture(owner, during_rotation, after_rotation=None):
    clock = [100.0]
    native = fake_user32(owner, during_rotation=during_rotation)
    waits = []
    def wait(*args):
        waits.append(args)
        if len(waits) == 2:
            if after_rotation:
                after_rotation()
            owner._stop.set()
        clock[0] += 5.01
        return 0
    native.MsgWaitForMultipleObjectsEx = wait
    with patch.object(control, '_user32', native), patch.object(win32, '_user32', native), \
         patch.object(win32, '_kernel32', SimpleNamespace(GetModuleHandleW=lambda *a: None)), \
         patch.object(win32.time, 'monotonic', lambda: clock[0]):
        win32.run_input_monitor(owner)
    assert owner._startup_error is None, owner._startup_error


def baseline():
    owner, token = owner_ready()
    proof = []
    def checkpoint_in_rotation():
        try:
            owner.checkpoint(token)
        except control.ControlBlocked as error:
            proof.append({'code':error.code, 'status':error.status,
                'token':token, 'generation':owner._generation, 'rotating':owner._rotating,
                'suppress':owner._suppress, 'visual_armed':owner._visual_armed,
                'emergency':owner._emergency, 'fast_takeover':owner._fast_takeover,
                'fast_pending':owner._fast_pending})
    monitor_fixture(owner, checkpoint_in_rotation)
    assert len(proof)==1 and proof[0]['code']=='CONTROL_PREEMPTED', proof
    print(json.dumps({'baseline':'PASS', 'native_calls':0, 'five_second_rotation':proof}))


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.token = owner_ready()

    def test_periodic_monitor_race_waits_for_replacement(self):
        entered = threading.Event()
        result = []
        worker = []
        def during_rotation():
            def check():
                entered.set()
                try:
                    self.owner.checkpoint(self.token)
                    result.append('PASS')
                except Exception as error:
                    result.append(error)
            task = threading.Thread(target=check)
            worker.append(task)
            task.start()
            self.assertTrue(entered.wait(1))
        def after_rotation():
            for task in worker:
                task.join(1)
                self.assertFalse(task.is_alive())
            self.assertEqual(result, ['PASS'])
        monitor_fixture(self.owner, during_rotation, after_rotation)
        events = self.owner.diagnostic()['events']
        self.assertTrue(any(e['event']=='rotation_begin' and e['reason']=='interval_elapsed' for e in events))
        self.assertTrue(any(e['event']=='rotation_end' for e in events))

    def test_rotation_handshake_retains_same_lease(self):
        self.owner._begin_rotation('interval_elapsed')
        completed = threading.Event()
        errors = []
        def check():
            try:
                self.owner.checkpoint(self.token)
            except Exception as error:
                errors.append(error)
            completed.set()
        task = threading.Thread(target=check)
        task.start()
        self.assertFalse(completed.wait(0.02))
        self.assertTrue(self.owner._suppress)
        self.owner._end_rotation()
        self.assertTrue(completed.wait(1))
        task.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.token, self.owner._generation)
        self.assertTrue(self.owner._suppress)

    def test_timeout_fails_closed(self):
        self.owner._rotation_wait_seconds = 0.025
        self.owner._begin_rotation('interval_elapsed')
        with self.assertRaises(control.ControlBlocked) as caught:
            self.owner.checkpoint(self.token)
        self.assertEqual(caught.exception.code, 'CONTROL_PREEMPTED')
        self.assertEqual(caught.exception.status['native_diagnostic']['failed_predicates'], ['rotating'])
        self.assertTrue(self.owner._rotating)

    def test_monitor_never_waits_for_itself(self):
        self.owner._thread = threading.current_thread()
        self.owner._begin_rotation('interval_elapsed')
        with self.assertRaises(control.ControlBlocked):
            self.owner.checkpoint(self.token)

    def test_other_predicates_never_wait(self):
        for field in ['_emergency','_fast_takeover','_fast_pending']:
            owner, token = owner_ready()
            owner._begin_rotation('interval_elapsed')
            setattr(owner, field, True)
            with patch.object(owner._rotation_done, 'wait', side_effect=AssertionError('must not wait')):
                with self.assertRaises(control.ControlBlocked):
                    owner.checkpoint(token)

    def test_generation_and_suppression_fail(self):
        for mutation in [lambda o: setattr(o,'_generation',o._generation+1),
                         lambda o: setattr(o,'_suppress',False),
                         lambda o: setattr(o,'_state','user')]:
            owner, token = owner_ready()
            mutation(owner)
            with self.assertRaises(control.ControlBlocked):
                owner.checkpoint(token)

    def test_physical_escape_interrupts_wait_and_stays_latched(self):
        self.owner._begin_rotation('interval_elapsed')
        result = []
        task = threading.Thread(target=lambda: self._capture_check(result))
        task.start()
        key = control._KeyHookData()
        key.vkCode = 0x1B
        self.assertEqual(self.owner._physical_key(0, 0x100, ctypes.addressof(key)), 1)
        task.join(1)
        self.assertFalse(task.is_alive())
        self.assertEqual(result[0].code, 'USER_CANCELLED')
        self.owner._end_rotation()
        with self.assertRaises(control.ControlBlocked) as caught:
            self.owner.checkpoint(self.token)
        self.assertEqual(caught.exception.code, 'USER_CANCELLED')
        self.assertTrue(self.owner._escape_cancelled)
        self.assertTrue(any(e['reason']=='physical_escape' for e in self.owner.diagnostic()['events']))

    def _capture_check(self, result):
        try:
            self.owner.checkpoint(self.token)
        except control.ControlBlocked as error:
            result.append(error)

    def test_fail_open_wakes_and_records_specific_cause(self):
        self.owner._begin_rotation('interval_elapsed')
        result = []
        task = threading.Thread(target=lambda:self._capture_check(result))
        task.start()
        self.owner._fail_open('indicator_unhealthy')
        task.join(1)
        self.assertFalse(task.is_alive())
        self.assertEqual(result[0].code,'CONTROL_PREEMPTED')
        self.assertFalse(self.owner._suppress)
        self.assertTrue(any(e['reason']=='indicator_unhealthy' for e in self.owner.diagnostic()['events']))

    def test_first_failure_is_preserved_before_secondary_errors(self):
        self.owner._suppress = False
        self._capture_check([])
        first = self.owner.diagnostic()['first_failure']
        self.owner._generation += 1
        self._capture_check([])
        trace = self.owner.diagnostic()
        self.assertEqual(trace['first_failure'], first)
        self.assertEqual(len([e for e in trace['events'] if e['event']=='checkpoint_blocked']),2)

    def test_bounded_trace_contains_no_key_text_or_coordinates(self):
        for _ in range(400):
            self.owner._trace('fixture', 'safe_reason')
        trace = self.owner.diagnostic()
        self.assertEqual(len(trace['events']), 256)
        self.assertGreater(trace['dropped_events'],0)
        allowed = {'seq','monotonic_ns','thread_id','event','reason','generation','state',
            'suppress','visual_armed','visual_paused','rotating','emergency','fast_takeover',
            'fast_pending','cancelled','rotation_id','token'}
        for event in trace['events']:
            self.assertEqual(set(event),allowed)

    def test_trace_writer_preserves_identity_and_first_causal_event(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'trace.ndjson'
            with trace_module.ControlTraceWriter(self.owner,path,
                    run_id='11111111-1111-4111-8111-111111111111',
                    procedure_id='22222222-2222-4222-8222-222222222222'):
                header = json.loads(path.read_text().splitlines()[0])
                self.assertEqual(header['sdk_version'],'0.8.8+fxlance.2')
                self.assertEqual(len(header['source_hashes']),4)
                self.owner._begin_rotation('interval_elapsed')
                self.owner._fail_open('indicator_unhealthy')
                self._capture_check([])
                self.owner._end_rotation(False)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            causes = [e for e in events if e.get('event')=='fail_open' and e.get('reason')=='indicator_unhealthy']
            self.assertEqual(len(causes),1)
            self.assertEqual(causes[0]['procedure_id'],header['procedure_id'])
            self.assertTrue(any(e.get('event')=='checkpoint_blocked' for e in events))
            primary = [e for e in events if e.get('event')=='native_first_failure']
            self.assertEqual(len(primary),1)
            self.assertIn('emergency',primary[0]['failed_predicates'])
            self.assertEqual(primary[0]['requested_token'],self.token)
            self.assertTrue(any(e.get('reason')=='hook_replacement_failed' for e in events))

    def test_trace_writer_refuses_existing_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'trace.ndjson'
            path.write_text('preserve')
            with self.assertRaises(FileExistsError):
                with trace_module.ControlTraceWriter(self.owner,path,
                        run_id='11111111-1111-4111-8111-111111111111',
                        procedure_id='22222222-2222-4222-8222-222222222222'):
                    pass
            self.assertEqual(path.read_text(),'preserve')

    def test_trace_writer_disk_failure_stops_control(self):
        with tempfile.TemporaryDirectory() as root:
            writer = trace_module.ControlTraceWriter(self.owner,Path(root)/'trace.ndjson',
                run_id='11111111-1111-4111-8111-111111111111',
                procedure_id='22222222-2222-4222-8222-222222222222')
            with patch.object(writer,'_drain',side_effect=OSError('fixture')):
                with self.assertRaises(RuntimeError):
                    with writer:
                        pass
            self.assertTrue(self.owner._emergency)
            self.assertFalse(self.owner._suppress)
            self.assertEqual(self.owner.diagnostic()['first_fault']['reason'],'diagnostic_sink_failed')

    def test_trace_writer_exports_late_lower_sequence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'trace.ndjson'
            with trace_module.ControlTraceWriter(self.owner,path,
                    run_id='11111111-1111-4111-8111-111111111111',
                    procedure_id='22222222-2222-4222-8222-222222222222') as writer:
                # Park the exporter and drain on this fixture thread only.
                writer._stop.set()
                writer._thread.join(1)
            snapshot = self.owner.diagnostic()
            late = dict(snapshot['events'][-1],seq=99,reason='late_event')
            early = dict(late,seq=100,reason='earlier_append')
            self.owner._trace_events.clear()
            self.owner._trace_events.append(early)
            writer._seen.clear()
            with path.open('a',encoding='utf-8') as writer._file:
                writer._drain()
                self.owner._trace_events.append(late)
                writer._drain()
                writer._drain()
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len([e for e in events if e.get('reason')=='late_event']),1)

    def test_ledger_alternate_failure_retains_first_record(self):
        first = self.owner.failure_diagnostic('input_ledger_blocked')['first_failure']
        self.owner._suppress = False
        self._capture_check([])
        self.assertEqual(self.owner.diagnostic()['first_failure'],first)


if __name__ == '__main__':
    if '--baseline' in sys.argv:
        baseline()
    elif '--existing' in sys.argv:
        os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        win32._kernel32.GetModuleHandleW = lambda *args: None
        import pytest
        sys.exit(pytest.main(['--noconftest','-q',
            str(Path(__file__).with_name('test_control.py')),
            str(Path(__file__).with_name('test_control_hook_rotation.py'))]))
    else:
        unittest.main()
