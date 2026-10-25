"""Return to the window preceding an explicit control session, off the hook thread."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import threading
import time

import win32api
import win32gui
import win32process

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_user32.IsHungAppWindow.argtypes = [wintypes.HWND]
_user32.IsHungAppWindow.restype = wintypes.BOOL


def window_hung(hwnd):
    return bool(_user32.IsHungAppWindow(hwnd))


@dataclass(frozen=True)
class WindowOrigin:
    hwnd: int
    thread_id: int
    process_id: int
    class_name: str
    placement: tuple


def remember_window() -> WindowOrigin | None:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
    return WindowOrigin(hwnd, thread_id, process_id, win32gui.GetClassName(hwnd),
                        win32gui.GetWindowPlacement(hwnd))


def restore_window(origin: WindowOrigin) -> str:
    """No keyboard tricks, reopening, elevation or retry after access denial."""
    if not win32gui.IsWindow(origin.hwnd):
        return 'window_closed'
    if window_hung(origin.hwnd):
        return 'window_not_responding'
    if (win32process.GetWindowThreadProcessId(origin.hwnd) !=
            (origin.thread_id, origin.process_id)
            or win32gui.GetClassName(origin.hwnd) != origin.class_name):
        return 'window_replaced'
    # Use the saved placement rather than maximizing all restored windows.
    if win32gui.GetWindowPlacement(origin.hwnd) != origin.placement:
        flags, show, minimum, maximum, normal = origin.placement
        win32gui.SetWindowPlacement(origin.hwnd, (flags | 4, show, minimum, maximum, normal))
    current = win32gui.GetForegroundWindow()
    if current and window_hung(current):
        return 'foreground_not_responding'
    current_tid = win32api.GetCurrentThreadId()
    foreground_tid = win32process.GetWindowThreadProcessId(current)[0] if current else 0
    attached = []
    try:
        # Same documented activation technique as Desktop.bring_window_to_top.
        # Unlike that older helper, an AttachThreadInput error ends this attempt.
        if current != origin.hwnd:
            for tid in dict.fromkeys((foreground_tid, origin.thread_id)):
                if tid and tid != current_tid:
                    win32process.AttachThreadInput(current_tid, tid, True)
                    attached.append(tid)
            win32gui.SetForegroundWindow(origin.hwnd)
            win32gui.BringWindowToTop(origin.hwnd)
    finally:
        for tid in reversed(attached):
            win32process.AttachThreadInput(current_tid, tid, False)
    deadline = time.monotonic() + .5
    while True:
        foreground_ok = win32gui.GetForegroundWindow() == origin.hwnd
        placement_ok = win32gui.GetWindowPlacement(origin.hwnd)[1:] == origin.placement[1:]
        if foreground_ok and placement_ok:
            return 'restored'
        if time.monotonic() >= deadline:
            return 'placement_not_restored' if foreground_ok else 'activation_not_granted'
        time.sleep(.01)


class FocusReturn:
    """One serialized return worker. New input cannot overlap a running return."""
    def __init__(self, *, capture=remember_window, restore=restore_window,
                 is_current=lambda generation: True, capture_current=lambda token: True,
                 before_restore=lambda generation: None):
        self.capture, self.restore = capture, restore
        self.is_current, self.before_restore = is_current, before_restore
        self.capture_current = capture_current
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wind-focus-return')
        self._origin = None
        self._token = None
        self._future = None
        self._state = 'idle'
        self._reason = None
        self._closed = False

    def before_input(self):
        # No coordinator lock is held here. A queued, obsolete return is checked
        # again by generation before activation; a running return finishes first.
        with self._lock:
            pass

    def remember(self, token):
        with self._lock:
            self._token = token
            self._origin = None
            self._reason = None
            try:
                if not self.capture_current(token):
                    self._state, self._reason = 'skipped', 'capture_revoked'
                    return
                self._origin = self.capture()
                if not self.capture_current(token):
                    self._origin = None
                    self._state, self._reason = 'skipped', 'capture_revoked'
                    return
                self._state = 'remembered' if self._origin is not None else 'unavailable'
                if self._origin is None:
                    self._reason = 'no_foreground_window'
            except Exception as exc:
                self._state, self._reason = 'failed', self._error(exc)

    @staticmethod
    def _error(exc):
        return 'access_denied' if getattr(exc, 'winerror', None) == 5 else type(exc).__name__

    def request(self, generation):
        # Called from state listeners: enqueue only, never run activation on the
        # input monitor, and never wait for the worker/foreground window here.
        if self._closed:
            return
        try:
            self._future = self._executor.submit(self._return, generation)
        except RuntimeError:
            if not self._closed:
                raise

    def _return(self, generation):
        with self._lock:
            if self._origin is None or self._token is None or self._token >= generation:
                return
            if not self.is_current(generation):
                # User/new input superseded this return. Do not keep the old
                # window around to steal focus on a later ready/user transition.
                self._origin = None
                self._state, self._reason = 'skipped', 'cycle_superseded'
                return
            origin = self._origin
            self._origin = None  # Every remembered window gets at most one attempt.
            self._state = 'returning'
            try:
                self.before_restore(generation)
                outcome = self.restore(origin)
                self._state = 'restored' if outcome == 'restored' else 'skipped'
                self._reason = None if outcome == 'restored' else outcome
            except Exception as exc:
                self._state, self._reason = 'failed', self._error(exc)

    def status(self):
        # Advisory only; status must not block the monitor while Windows activates.
        return {'state': self._state, 'reason': self._reason}

    def flush(self, timeout=1.5):
        future = self._future
        if future is not None:
            try:
                future.result(timeout=timeout)
            except TimeoutError:
                self._state, self._reason = 'pending', 'activation_still_running'

    def close(self):
        self._closed = True
        self.flush()
        self._executor.shutdown(wait=False, cancel_futures=False)
