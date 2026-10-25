"""All window operations are replaced; no real focus/input changes in tests."""
import threading
from unittest.mock import Mock

import pytest

from windows_mcp.desktop import control_focus as focus


@pytest.fixture
def window(monkeypatch):
    placement = (0, 3, (0, 0), (0, 0), (100, 100, 1000, 800))
    origin = focus.WindowOrigin(10, 20, 30, 'Original', placement)
    state = {'foreground': 99, 'placement': placement}
    monkeypatch.setattr(focus, 'window_hung', lambda hwnd: False)
    monkeypatch.setattr(focus.win32gui, 'IsWindow', lambda hwnd: True)
    monkeypatch.setattr(focus.win32gui, 'GetForegroundWindow', lambda: state['foreground'])
    monkeypatch.setattr(focus.win32gui, 'GetClassName', lambda hwnd: 'Original')
    monkeypatch.setattr(focus.win32gui, 'GetWindowPlacement', lambda hwnd: state['placement'])
    monkeypatch.setattr(focus.win32gui, 'SetWindowPlacement', Mock(side_effect=lambda hwnd, value: state.update(placement=value)))
    monkeypatch.setattr(focus.win32gui, 'SetForegroundWindow', lambda hwnd: state.update(foreground=hwnd))
    monkeypatch.setattr(focus.win32gui, 'BringWindowToTop', Mock())
    monkeypatch.setattr(focus.win32process, 'GetWindowThreadProcessId', lambda hwnd: (20, 30) if hwnd == 10 else (40, 50))
    monkeypatch.setattr(focus.win32process, 'AttachThreadInput', Mock())
    monkeypatch.setattr(focus.win32api, 'GetCurrentThreadId', lambda: 60)
    return origin, state


def test_remember_records_handle_identity_and_placement(window):
    origin, state = window
    state['foreground'] = 10
    assert focus.remember_window() == origin


def test_restore_returns_window_and_original_maximized_placement(window):
    origin, state = window
    state['placement'] = (0, 1, (0, 0), (0, 0), (0, 0, 500, 500))
    assert focus.restore_window(origin) == 'restored'
    assert state['foreground'] == 10
    focus.win32gui.SetWindowPlacement.assert_called_once_with(10, (4, *origin.placement[1:]))
    assert focus.win32process.AttachThreadInput.call_args_list[-1].args == (60, 40, False)


@pytest.mark.parametrize('case,reason', [('closed','window_closed'), ('replaced','window_replaced'), ('hung','window_not_responding')])
def test_invalid_original_is_never_activated(window, monkeypatch, case, reason):
    origin, state = window
    if case == 'closed':
        monkeypatch.setattr(focus.win32gui, 'IsWindow', lambda hwnd: False)
    elif case == 'replaced':
        monkeypatch.setattr(focus.win32process, 'GetWindowThreadProcessId', lambda hwnd: (200, 300))
    else:
        monkeypatch.setattr(focus, 'window_hung', lambda hwnd: True)
    assert focus.restore_window(origin) == reason
    assert state['foreground'] == 99


def test_access_denied_does_not_fall_back_to_activation(window, monkeypatch):
    origin, state = window
    failure = OSError('access denied')
    failure.winerror = 5
    monkeypatch.setattr(focus.win32process, 'AttachThreadInput', Mock(side_effect=failure))
    with pytest.raises(OSError):
        focus.restore_window(origin)
    assert state['foreground'] == 99


def test_manager_returns_once_off_calling_thread():
    calls = []
    caller = threading.get_ident()
    manager = focus.FocusReturn(capture=lambda: 'origin', restore=lambda value: calls.append((value, threading.get_ident())) or 'restored')
    try:
        manager.remember(1)
        manager.request(2)
        manager.flush()
        manager.request(3)
        manager.flush()
        assert len(calls) == 1 and calls[0][0] == 'origin' and calls[0][1] != caller
        assert manager.status()['state'] == 'restored'
    finally:
        manager.close()


def test_queued_return_is_obsolete_when_another_cycle_started():
    restore = Mock(return_value='restored')
    manager = focus.FocusReturn(capture=lambda: 'new origin', restore=restore, is_current=lambda generation: generation == 4)
    try:
        manager.remember(3)
        manager.request(2)
        manager.flush()
        restore.assert_not_called()
        manager.request(4)
        manager.flush()
        restore.assert_called_once_with('new origin')
    finally:
        manager.close()


def test_cancel_during_capture_does_not_record_a_later_user_window():
    current = iter([True, False])
    restore = Mock()
    manager = focus.FocusReturn(capture=lambda: 'later user window', restore=restore,
                               capture_current=lambda token: next(current))
    try:
        manager.remember(1)
        manager.request(2)
        manager.flush()
        assert manager.status() == {'state':'skipped','reason':'capture_revoked'}
        restore.assert_not_called()
    finally:
        manager.close()


def test_superseded_return_is_not_retried_on_later_idle_state():
    restore = Mock(return_value='restored')
    manager = focus.FocusReturn(capture=lambda: 'old window', restore=restore,
                               is_current=lambda generation: generation == 4)
    try:
        manager.remember(1)
        manager.request(2)
        manager.flush()
        manager.request(4)
        manager.flush()
        restore.assert_not_called()
        assert manager.status()['reason'] == 'cycle_superseded'
    finally:
        manager.close()


def test_return_access_denial_is_reported_and_not_retried():
    error = OSError('denied')
    error.winerror = 5
    restore = Mock(side_effect=error)
    manager = focus.FocusReturn(capture=lambda: 'original', restore=restore)
    try:
        manager.remember(1)
        manager.request(2)
        manager.flush()
        manager.request(3)
        manager.flush()
        restore.assert_called_once_with('original')
        assert manager.status() == {'state':'failed','reason':'access_denied'}
    finally:
        manager.close()
