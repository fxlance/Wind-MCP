"""Optional private trace export, started before native lifespan by its caller.

Only this off-hook worker writes files. No keystrokes, coordinates, titles,
browser content, URLs or exception text are exported. Existing files are refused.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid


class ControlTraceWriter:
    def __init__(self, controller, path: Path, *, run_id: str, procedure_id: str):
        self.controller = controller
        self.path = Path(path)
        self.ids = {'run_id':str(uuid.UUID(run_id)), 'procedure_id':str(uuid.UUID(procedure_id))}
        self._stop = threading.Event()
        self._thread = None
        self._file = None
        self._last_sequence = 0
        self._seen = set()
        self._failure_sequence = None
        self._fault_sequence = None
        self.error_type = None

    def __enter__(self):
        source = Path(__file__).parent
        snapshot = self.controller.diagnostic()
        header = dict(schema=1, event='trace_header', sdk_version='0.8.8+fxlance.3',
            **self.ids, controller_id=snapshot['controller_id'], pid=snapshot['pid'],
            utc=datetime.now(timezone.utc).isoformat(), monotonic_ns=time.monotonic_ns(),
            source_hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest()
                for name in ['control.py','control_hooks.py','control_win32.py','control_trace.py']})
        self._file = self.path.open('x', encoding='utf-8')
        try:
            self._write(header)
            self._file.flush()  # Identity/hash receipt exists before hooks start.
            self._thread = threading.Thread(target=self._run, name='windows-mcp-private-trace', daemon=True)
            self._thread.start()
        except BaseException:
            self._file.close()
            raise
        return self

    def _write(self, value):
        self._file.write(json.dumps(value, ensure_ascii=True, separators=(',',':'))+'\n')

    def _drain(self):
        snapshot = self.controller.diagnostic()
        events = sorted(snapshot['events'], key=lambda value:value['seq'])
        # A hook and a worker may append out of sequence. Remember exported
        # IDs in the current ring, so a late lower ID is never silently lost.
        pending = [event for event in events if event['seq'] not in self._seen]
        if pending and pending[0]['seq']>self._last_sequence+1:
            self._write(dict(event='trace_gap', **self.ids,
                after_sequence=self._last_sequence, next_sequence=pending[0]['seq']))
        for event in pending:
            self._write(dict(event, **self.ids, controller_id=snapshot['controller_id'],pid=snapshot['pid']))
            self._last_sequence = max(self._last_sequence,event['seq'])
        self._seen = {event['seq'] for event in events}
        for field, sequence_field, event_name in [
                ('first_fault','_fault_sequence','native_first_fault'),
                ('first_failure','_failure_sequence','native_first_failure')]:
            value = snapshot[field]
            if value and value['seq'] != getattr(self,sequence_field):
                self._write(dict(value,event=event_name,**self.ids,
                    controller_id=snapshot['controller_id'],pid=snapshot['pid']))
                setattr(self,sequence_field,value['seq'])
        self._file.flush()

    def _run(self):
        try:
            while True:
                self._drain()
                if self._stop.wait(0.02):
                    self._drain()
                    return
        except Exception as error:
            self.error_type = type(error).__name__
            self.controller._fail_open('diagnostic_sink_failed')
        finally:
            try:
                self._file.close()
            except Exception as error:
                self.error_type = self.error_type or type(error).__name__
                self.controller._fail_open('diagnostic_sink_close_failed')

    def __exit__(self, *unused):
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            self.controller._fail_open('diagnostic_sink_stalled')
            raise RuntimeError('Private native trace writer did not stop')
        if self.error_type:
            raise RuntimeError('Private native trace writer failed: '+self.error_type)
