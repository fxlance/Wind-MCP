import json
from types import SimpleNamespace

import pytest
from PIL import Image

from windows_mcp.tools import hover
from windows_mcp.uia import Rect


IDLE = (30, 30)
FIRST = (110, 110)
SECOND = (350, 110)


class SimulatedDesktop:
    def __init__(self):
        self.cursor = IDLE
        self.moves = []
        self.visible_targets = {FIRST, SECOND}
        self.idle_colours = None
        self.hwnd = 1
        self.switch_on_move = None

    def get_cursor_location(self):
        return self.cursor

    def parse_region_selection(self, bounds):
        return Rect(*bounds)

    def move(self, loc):
        self.moves.append(loc)
        self.cursor = loc
        if loc == self.switch_on_move:
            self.hwnd = 2

    def get_screenshot(self, *, capture_rect):
        colour = "gold" if self.cursor in self.visible_targets else "black"
        if self.cursor == IDLE and self.moves and self.idle_colours:
            colour = next(self.idle_colours)
        return Image.new("RGB", (capture_rect.width(), capture_rect.height()), colour)


@pytest.fixture
def batch(monkeypatch):
    desktop = SimulatedDesktop()
    now = 0.0

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(hover, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    monkeypatch.setattr(hover.win32gui, "GetForegroundWindow", lambda: desktop.hwnd)
    monkeypatch.setattr(hover.win32gui, "GetWindowText", lambda hwnd: "Example")
    monkeypatch.setattr(hover, "_client_bounds", lambda hwnd: (0, 0, 500, 500))
    controller = SimpleNamespace(session_active=lambda: True, checkpoint_current=lambda: None)
    targets = [hover.HoverTarget(loc=point, region=(150, 150, 320, 320), probe=(160, 160, 280, 200))
               for point in (FIRST, SECOND)]

    def run():
        text, *images = hover._capture_batch(desktop, controller, "Example", IDLE, targets, 400)
        return json.loads(text), images

    return desktop, targets, run


def test_returns_ordered_previews_and_clears_between_targets(batch):
    desktop, _, run = batch
    info, images = run()
    assert info["status"] == "ok"
    assert len(images) == 2
    assert [entry["loc"] for entry in info["captures"]] == [list(FIRST), list(SECOND)]
    assert all(entry["ready_ms"] >= 100 for entry in info["captures"])
    assert desktop.moves == [FIRST, IDLE, SECOND, IDLE]


def test_missing_popup_preserves_already_captured_frames(batch):
    desktop, _, run = batch
    desktop.visible_targets.remove(SECOND)
    info, images = run()
    assert (info["status"], info["reason"], info["target"]) == ("stopped", "preview_timeout", 2)
    assert len(images) == 1
    assert desktop.moves == [FIRST, IDLE, SECOND]


def test_animated_background_does_not_block_a_cleared_preview(batch):
    from itertools import cycle

    desktop, _, run = batch
    desktop.idle_colours = cycle(("gray", "navy", "white"))
    info, images = run()
    assert info["status"] == "ok"
    assert len(images) == 2


def test_focus_loss_stops_before_sampling_the_new_window(batch):
    desktop, _, run = batch
    desktop.switch_on_move = FIRST
    info, images = run()
    assert info["reason"] == "window_changed"
    assert images == []
    assert desktop.moves == [FIRST]


def test_rejects_invalid_later_target_before_moving(batch):
    desktop, targets, run = batch
    targets[1] = targets[1].model_copy(update={"loc": (500, 400)})
    with pytest.raises(ValueError, match="inside the client area"):
        run()
    assert desktop.moves == []
