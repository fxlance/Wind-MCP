"""Persistent capture tests: all physical input and display operations are fake."""
import asyncio
import ctypes
import time

import pytest
from fastmcp import FastMCP, Client

from windows_mcp import uia
from windows_mcp.desktop import control
from windows_mcp.desktop.control_context import current_owner, current_token
from windows_mcp.tools import control_status
from windows_mcp.tools.control_notifications import ControlNotifier, ControlToolGate


@pytest.fixture
def coordinator(monkeypatch):
    owner = control.ControlCoordinator(immediate_capture=True)
    monkeypatch.setattr(uia, "IsKeyPressed", lambda key: False)
    monkeypatch.setattr(control._user32, "CallNextHookEx", lambda *args: 7)
    owner.set_health_probe(lambda: True)
    owner.subscribe(lambda status: owner.arm_visible(status['generation'])
                    if status['state'] == 'ai' else None)
    owner._set_locked('ready')
    return owner


def begin(owner, session='one', timeout=300):
    identity = current_owner.set(session)
    token = owner.begin_call('ControlBegin')
    handle = current_token.set(token)
    try:
        return owner.begin_session(session, timeout)
    finally:
        owner.end_call(token)
        current_token.reset(handle)
        current_owner.reset(identity)


def escape(owner):
    event = control._KeyHookData()
    event.vkCode = 0x1B
    assert owner._physical_key(0, 0x100, ctypes.addressof(event)) == 1
    while not owner._events.empty():
        owner._handle(owner._events.get_nowait())


def test_session_survives_normal_call_grace_and_end_releases_holds(coordinator):
    begin(coordinator)
    coordinator._lease_until = time.monotonic() - 1
    assert coordinator.status()['state'] == 'ai'
    assert coordinator.session_active() and coordinator._suppress
    releases = []
    coordinator.input_ledger.press('key:ctrl', lambda: None,
                                   lambda: releases.append('up'), lambda: False)
    result = coordinator.end_session('one')
    assert result['state'] == 'ready'
    assert not result['control_session']['active']
    assert not coordinator._suppress
    assert releases == ['up']
    assert coordinator.end_session('one')['state'] == 'ready'


def test_other_session_cannot_use_renew_or_release_capture(coordinator):
    begin(coordinator)
    for action in (lambda: coordinator.check_session_owner('two'),
                   lambda: coordinator.keep_session('two'),
                   lambda: coordinator.end_session('two')):
        with pytest.raises(control.ControlBlocked, match='CONTROL_SESSION_BUSY'):
            action()
    assert coordinator.session_active()


def test_timeout_releases_and_requires_explicit_resume(coordinator):
    begin(coordinator, timeout=30)
    coordinator._session_until = time.monotonic() - 1
    coordinator._handle(('tick',))
    status = coordinator.status()
    assert status['state'] == 'user' and status['cancelled']
    assert status['control_session']['end_reason'] == 'session_timeout'
    assert not coordinator._suppress
    with pytest.raises(control.ControlBlocked, match='USER_CANCELLED'):
        coordinator.keep_session('one')
    with pytest.raises(control.ControlBlocked, match='USER_CANCELLED'):
        begin(coordinator)
    coordinator.resume_control()
    assert begin(coordinator)['control_session']['active']


def test_escape_between_calls_is_global_and_end_does_not_resume(coordinator):
    begin(coordinator)
    escape(coordinator)
    assert not coordinator._suppress
    assert not coordinator.status()['control_session']['active']
    coordinator.end_session('one')
    for session in ('one', 'two'):
        with pytest.raises(control.ControlBlocked, match='USER_CANCELLED'):
            begin(coordinator, session)
    coordinator.resume_control()
    assert begin(coordinator, 'two')['control_session']['active']


def test_keepalive_renews_but_status_does_not(coordinator):
    begin(coordinator)
    coordinator._session_until = time.monotonic() + 2
    assert coordinator.status()['control_session']['remaining_seconds'] <= 2
    assert coordinator.keep_session('one')['control_session']['remaining_seconds'] > 299


@pytest.mark.parametrize('timeout', [0, 29, 901, -1, True, float('inf')])
def test_bad_timeout_never_creates_session(coordinator, timeout):
    with pytest.raises(ValueError):
        begin(coordinator, timeout=timeout)
    assert not coordinator.status()['control_session']['active']


def test_native_failure_drops_capture_session(coordinator):
    begin(coordinator)
    coordinator._fail_open('fixture_failure')
    assert not coordinator._suppress
    assert coordinator.status()['state'] == 'unavailable'
    assert not coordinator.session_active()


def test_session_screenshot_never_hides_indicator(monkeypatch, coordinator):
    from windows_mcp.desktop import service, control_overlay
    begin(coordinator)
    desktop = service.Desktop.__new__(service.Desktop)
    monkeypatch.setattr(service, 'get_controller', lambda: coordinator)
    def forbidden(*args, **kwargs):
        raise AssertionError('Persistent screenshot must not hide/pause the frame')
    monkeypatch.setattr(coordinator, 'pause_for_capture', forbidden)
    monkeypatch.setattr(control_overlay, 'suspend_for_capture', forbidden)
    monkeypatch.setattr(service.flash_overlay, 'show_capture_flash', forbidden)
    monkeypatch.setattr(service.flash_overlay, 'cancel_active_flash', lambda: True)
    expected = object()
    monkeypatch.setattr(service.screenshot_capture, 'capture', lambda rect: (expected, 'fixture'))
    assert desktop.get_screenshot() is expected
    assert coordinator.session_active() and coordinator._suppress


@pytest.mark.asyncio
async def test_mcp_verbs_bind_to_real_client_session(monkeypatch, coordinator):
    monkeypatch.setattr(control_status, 'get_controller', lambda: coordinator)
    server = FastMCP('capture-test')
    gate = ControlToolGate(coordinator, ControlNotifier(coordinator))
    server.add_middleware(gate)
    control_status.register(server, get_desktop=lambda: None, get_analytics=lambda: None)
    effects = []

    @server.tool(name='Action')
    def action():
        effects.append('action')
        return 'ok'

    async with Client(server, mode='legacy') as first, Client(server, mode='legacy') as other:
        await first.call_tool('ControlBegin')
        coordinator._lease_until = time.monotonic() - 1
        await first.call_tool('Action')
        await first.call_tool('ControlKeepAlive')
        for name in ('Action', 'ControlBegin', 'ControlEnd', 'ControlKeepAlive'):
            result = await other.call_tool(name, raise_on_error=False)
            assert result.is_error and 'CONTROL_SESSION_BUSY' in str(result.content)
        assert effects == ['action']
        await first.call_tool('ControlEnd')
        await other.call_tool('ControlBegin')
        escape(coordinator)
        for client in (first, other):
            result = await client.call_tool('Action', raise_on_error=False)
            assert result.is_error and 'USER_CANCELLED' in str(result.content)
        await other.call_tool('ControlEnd')
        await other.call_tool('ControlStatus', {'resume': True})
        await other.call_tool('Action')


@pytest.mark.asyncio
async def test_cancelled_owner_request_releases_persistent_session(monkeypatch, coordinator):
    monkeypatch.setattr(control_status, 'get_controller', lambda: coordinator)
    server = FastMCP('capture-test')
    server.add_middleware(ControlToolGate(coordinator, ControlNotifier(coordinator)))
    control_status.register(server, get_desktop=lambda: None, get_analytics=lambda: None)
    entered = asyncio.Event()

    @server.tool(name='Wait')
    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async with Client(server, mode='legacy') as client:
        await client.call_tool('ControlBegin')
        pending = asyncio.create_task(client.call_tool('Wait'))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    for _ in range(50):
        if not coordinator.session_active():
            break
        await asyncio.sleep(.01)
    assert not coordinator.session_active()


@pytest.mark.asyncio
async def test_stateless_transport_cannot_leave_an_unowned_capture(monkeypatch, coordinator):
    monkeypatch.setattr(control_status, 'get_controller', lambda: coordinator)
    server = FastMCP('capture-test')
    server.add_middleware(ControlToolGate(coordinator, ControlNotifier(coordinator)))
    control_status.register(server, get_desktop=lambda: None, get_analytics=lambda: None)
    async with Client(server, mode='2026-07-28') as client:
        result = await client.call_tool('ControlBegin', raise_on_error=False)
        assert result.is_error and 'PERSISTENT_SESSION_REQUIRED' in str(result.content)
    assert not coordinator.session_active()
    assert not coordinator._suppress
