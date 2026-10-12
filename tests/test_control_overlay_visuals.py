"""Regression checks for the accepted AI-control glow and notice."""

import pytest

from windows_mcp.desktop import control_overlay, control_overlay_art, flash_overlay


def _assert_premultiplied(bgra: bytes) -> None:
    for offset in range(0, len(bgra), 4):
        blue, green, red, alpha = bgra[offset : offset + 4]
        assert blue <= alpha and green <= alpha and red <= alpha


def _alpha_at(bgra: bytes, width: int, x: int, y: int) -> int:
    return bgra[(y * width + x) * 4 + 3]


def test_notice_is_one_small_escape_hint():
    assert control_overlay_art._NOTICE_TITLE == ""
    assert control_overlay_art._NOTICE_HINT == "Escape для разблокировки"
    for screen_width in (300, 1920):
        notice = control_overlay_art._notice_bitmap(screen_width)
        assert notice is not None
        width, height, bgra = notice
        assert width <= min(350, screen_width - 32) and height < 40
        _assert_premultiplied(bgra)
        assert _alpha_at(bgra, width, 0, 0) == 0
        assert max(bgra[3::4]) > 180


def test_breath_opacity_is_smooth_periodic_and_never_disappears():
    opacity = control_overlay_art._breath_opacity
    period = control_overlay_art._BREATH_PERIOD_SECONDS
    assert opacity(0) == opacity(period) == 255
    assert opacity(period / 2) == control_overlay_art._BREATH_MIN_ALPHA == 140
    assert opacity(period / 4) == opacity(3 * period / 4)
    frames = [opacity(index * control_overlay._REFRESH_SECONDS) for index in range(61)]
    assert all(140 <= value <= 255 for value in frames)
    assert max(abs(a - b) for a, b in zip(frames, frames[1:])) <= 7


def test_layer_opacity_updates_only_on_change_and_fails_closed(monkeypatch):
    layer = object.__new__(control_overlay._Layer)
    layer.hwnd = 123
    layer.x, layer.y, layer.width, layer.height = 10, 20, 1, 1
    layer.bgra = bytes((0, 0, 0, 0))
    layer.opacity = None
    calls = []

    def push_bitmap(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(flash_overlay, "_push_bitmap", push_bitmap)
    layer.set_opacity(200)
    layer.set_opacity(200)
    layer.set_opacity(140)
    assert calls == [
        ((123, 10, 20, 1, 1, layer.bgra), {"opacity": 200}),
        ((123, 10, 20, 1, 1, layer.bgra), {"opacity": 140}),
    ]

    def failed_upload(*args, **kwargs):
        raise OSError("upload failed")

    monkeypatch.setattr(flash_overlay, "_push_bitmap", failed_upload)
    with pytest.raises(OSError, match="upload failed"):
        layer.set_opacity(255)
    assert layer.opacity == 140


def test_edge_and_cursor_have_soft_glow_without_a_solid_contour():
    size = control_overlay_art._BORDER
    assert size == 28  # Permanently half the upstream 56-pixel frame.
    for side in ("left", "right", "top", "bottom"):
        width = size * 3 if side in ("top", "bottom") else size
        height = size if side in ("top", "bottom") else size * 3
        edge = control_overlay_art._edge_bitmap(width, height, side, (45, 145, 255))
        assert len(edge) == width * height * 4
        _assert_premultiplied(edge)
        alphas = (
            [_alpha_at(edge, width, x, height // 2) for x in range(width)]
            if side in ("left", "right")
            else [_alpha_at(edge, width, width // 2, y) for y in range(height)]
        )
        if side in ("right", "bottom"):
            alphas.reverse()
        assert alphas[0] == max(alphas) == 240  # Twice the previous edge opacity.
        assert alphas[-1] == 0
        assert all(a >= b for a, b in zip(alphas, alphas[1:]))
        assert max(a - b for a, b in zip(alphas, alphas[1:])) <= 16
        assert 30 <= alphas[20] <= 50  # The glow remains broad but fades inward.

    glow = control_overlay_art._cursor_bitmap((45, 145, 255))
    width = control_overlay_art._CURSOR_SIZE
    assert len(glow) == width**2 * 4
    _assert_premultiplied(glow)
    alphas = [glow[((width // 2) * width + x) * 4 + 3] for x in range(width // 2, width)]
    assert glow[3] == 0  # Transparent square corners.
    assert alphas[0] == 0 < alphas[10]  # Cursor center is fully transparent.
    assert alphas[10] > alphas[25] > alphas[-1]
    assert alphas[-1] == 0
    assert max(alphas) == 176  # Twice the previous cursor glow peak of 88.
    assert max(abs(a - b) for a, b in zip(alphas, alphas[1:])) < 30


def test_corner_glow_joins_edges_without_double_opacity():
    border = control_overlay_art._BORDER
    width = border * 4
    top = control_overlay_art._edge_bitmap(width, border, "top", (45, 145, 255))
    bottom = control_overlay_art._edge_bitmap(width, border, "bottom", (45, 145, 255))
    left = control_overlay_art._edge_bitmap(border, border, "left", (45, 145, 255))
    assert _alpha_at(top, width, 0, 0) == control_overlay_art._glow_alpha(0)
    assert _alpha_at(top, width, width - 1, 0) == control_overlay_art._glow_alpha(0)
    for horizontal in (top, bottom):
        for x_depth in (1, 3, 5, 10, 20, 27):
            side_alpha = _alpha_at(left, border, x_depth, border // 2)
            for y_depth in (1, 3, 5, 10, 20, 27):
                y = y_depth if horizontal is top else border - 1 - y_depth
                straight_alpha = _alpha_at(horizontal, width, width // 2, y)
                expected = control_overlay_art._glow_alpha(min(x_depth, y_depth))
                assert expected <= max(side_alpha, straight_alpha)
                assert _alpha_at(horizontal, width, x_depth, y) == expected
                assert _alpha_at(horizontal, width, width - 1 - x_depth, y) == expected


def test_multimonitor_creates_narrow_clickthrough_layers(monkeypatch):
    made = []

    class FakeLayer:
        def __init__(self, x, y, width, height, bgra, name, *, breathes=True):
            made.append((x, y, width, height, name, breathes))

        def close(self):
            pass

    monkeypatch.setattr(control_overlay, "_Layer", FakeLayer)
    monkeypatch.setattr(control_overlay._user32, "GetCursorPos", lambda ptr: True)
    rects = ((-1920, 0, 0, 1080), (0, 0, 2560, 1440))
    layers, ring = control_overlay._build_layers(rects, pending=False)
    assert len(layers) == 10 and ring is None
    border = control_overlay_art._BORDER
    assert made[0][:4] == (-1920, 0, 1920, border)
    assert made[2][:4] == (-1920, border, border, 1080 - 2 * border)
    assert made[3][:4] == (-border, border, border, 1080 - 2 * border)
    assert made[4][1] == 8 and made[4][4:] == ("0_notice", False)
    assert abs((made[4][0] + made[4][2] / 2) - (-1920 / 2)) <= 1
    assert made[5][:4] == (0, 0, 2560, border)
    assert made[9][1] == 8 and made[9][4:] == ("1_notice", False)
    assert abs((made[9][0] + made[9][2] / 2) - 1280) <= 1
    assert not any(name == "cursor" or "notice_glow" in name for _, _, _, _, name, _ in made)
    assert all(w <= border or h <= border for _, _, w, h, name, _ in made if not name.endswith("notice"))



@pytest.mark.parametrize("width", [300, 400])
def test_notice_glow_stays_on_its_monitor_when_narrow(monkeypatch, width):
    made = []

    class FakeLayer:
        def __init__(self, x, y, layer_width, height, bgra, name, *, breathes=True):
            made.append((x, y, layer_width, height, name))

        def close(self):
            pass

    monkeypatch.setattr(control_overlay, "_Layer", FakeLayer)
    monkeypatch.setattr(control_overlay._user32, "GetCursorPos", lambda ptr: True)
    control_overlay._build_layers(((100, 0, 100 + width, 300),), pending=False)
    glow = next(layer for layer in made if layer[4] == "0_notice")
    assert 100 <= glow[0]
    assert glow[0] + glow[2] <= 100 + width


def test_layer_failure_closes_prior_windows(monkeypatch):
    closed = []

    class FailingLayer:
        count = 0

        def __init__(self, *args):
            self.count = FailingLayer.count
            FailingLayer.count += 1
            if self.count == 2:
                raise OSError("window creation failed")

        def close(self):
            closed.append(self.count)

    monkeypatch.setattr(control_overlay, "_Layer", FailingLayer)
    with pytest.raises(OSError, match="window creation failed"):
        control_overlay._build_layers(((0, 0, 800, 600),), pending=False)
    assert closed == [1, 0]
