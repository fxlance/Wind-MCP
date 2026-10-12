import ctypes
import asyncio
from unittest.mock import Mock

import pytest
from fastmcp import FastMCP, Client

from windows_mcp import uia
from windows_mcp.desktop import control, control_hooks
from windows_mcp.tools.control_notifications import ControlToolGate, ControlNotifier
from windows_mcp.infrastructure.config import load_config, write_config


def owner(monkeypatch):
    result = control.ControlCoordinator(immediate_capture=True)
    monkeypatch.setattr(control._user32, "CallNextHookEx", lambda *args: 7)
    monkeypatch.setattr(uia, "IsKeyPressed", lambda key: False)
    result.set_health_probe(lambda: True)
    result.subscribe(lambda status: result.arm_visible(status['generation']) if status['state'] == 'ai' else None)
    with result._lock:
        result._set_locked('user')
    result._last_physical_event = control.time.monotonic()
    return result


def key(result, vk, down=True, injected=False):
    event = control._KeyHookData()
    event.vkCode, event.flags = vk, 0x10 if injected else 0
    return result._physical_key(0, 0x100 if down else 0x101, ctypes.addressof(event))


def test_capture_does_not_wait_for_idle_or_held_input(monkeypatch):
    result = owner(monkeypatch)
    result._pressed.add(0x41)
    result._mouse_down.add(1)
    token = result.begin_call('Type')
    result.checkpoint(token)
    assert result.status()['state'] == 'ai' and result._suppress


def test_mouse_keyboard_and_raw_input_do_not_take_over(monkeypatch):
    result = owner(monkeypatch)
    token = result.begin_call('Wait')
    assert key(result, 0x41) == 1
    event = control._MouseHookData()
    event.pt.x, event.pt.y = 1000, 500
    assert result._physical_mouse(0, 0x200, ctypes.addressof(event)) == 1
    monkeypatch.setattr(control_hooks, '_read_raw_mouse', lambda _: (1, 1000, 1000))
    result._raw_input(0)
    while not result._events.empty():
        result._handle(result._events.get_nowait())
    result.checkpoint(token)
    assert not result._fast_pending


def test_escape_releases_immediately_and_latches_until_explicit_resume(monkeypatch):
    result = owner(monkeypatch)
    token = result.begin_call('Type')
    releases = []
    result.input_ledger.press('key:ctrl', lambda: None, lambda: releases.append('up'), lambda: False)
    assert key(result, 0x1B) == 1
    assert not result._suppress
    with pytest.raises(control.ControlBlocked, match='USER_CANCELLED'):
        result.checkpoint(token)
    result._handle(result._events.get_nowait())
    assert releases == ['up']
    assert key(result, 0x1B, down=False) == 1
    result.end_call(token)
    with pytest.raises(control.ControlBlocked, match='USER_CANCELLED'):
        result.begin_call('Click')
    result.resume_control()
    resumed = result.begin_call('Click')
    result.checkpoint(resumed)


def test_injected_escape_is_not_a_user_cancellation(monkeypatch):
    result = owner(monkeypatch)
    token = result.begin_call('Shortcut')
    assert key(result, 0x1B, injected=True) == 7
    result.checkpoint(token)
    assert not result._escape_cancelled


def test_capture_keeps_input_blocked_but_escape_still_cancels(monkeypatch):
    result = owner(monkeypatch)
    token = result.begin_call('Screenshot')
    stamp = result.pause_for_capture()
    assert result._suppress and stamp == token
    assert key(result, 0x41) == 1
    result._handle(result._events.get_nowait())
    assert result.status()['state'] == 'ai'
    assert key(result, 0x1B) == 1
    result._handle(result._events.get_nowait())
    result.resume_after_capture(stamp, restored=True)
    assert not result._suppress


def test_failure_still_releases_input(monkeypatch):
    result = owner(monkeypatch)
    result.begin_call('Wait')
    result._fail_open()
    assert not result._suppress


def test_takeover_clears_preexisting_modifiers(monkeypatch):
    result = owner(monkeypatch)
    release = Mock()
    monkeypatch.setattr(uia, 'ReleaseKey', release)
    result._delivered_keys.add(0xA2)
    result._pressed.add(0xA2)
    result.begin_call('Type')
    release.assert_called_once_with(0xA2, waitTime=0)
    assert not result._delivered_keys
    assert key(result, 0xA2, down=False) == 1


def test_option_defaults_on_and_can_be_disabled(tmp_path):
    config = load_config(None)
    assert config.control.immediate_capture is True
    config.control.immediate_capture = False
    path = tmp_path / 'config.toml'
    write_config(config, path)
    assert load_config(path).control.immediate_capture is False
    path.write_text('[control]\nimmediate_capture="true"')
    with pytest.raises(ValueError, match='TOML boolean'):
        load_config(path)


@pytest.mark.asyncio
async def test_mcp_escape_cancels_a_running_wait_and_blocks_queued_calls(monkeypatch):
    result = owner(monkeypatch)
    mcp = FastMCP('immediate-test')
    mcp.add_middleware(ControlToolGate(result, ControlNotifier(result)))
    started = asyncio.Event()
    effects = []

    @mcp.tool(name='Wait')
    async def wait():
        started.set()
        await asyncio.sleep(10)
        effects.append('finished')

    async with Client(mcp) as client:
        running = asyncio.create_task(client.call_tool('Wait', raise_on_error=False))
        await asyncio.wait_for(started.wait(), 1)
        key(result, 0x1B)
        result._handle(result._events.get_nowait())
        response = await asyncio.wait_for(running, 1)
        assert response.is_error and 'USER_CANCELLED' in str(response.content)
        blocked = await client.call_tool('Wait', raise_on_error=False)
        assert blocked.is_error and 'USER_CANCELLED' in str(blocked.content)
        assert effects == []
