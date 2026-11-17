"""Capture a bounded series of hover previews within one controlled window."""

import io
import json
import time
from collections.abc import Callable
from typing import Annotated

import win32gui
from fastmcp import Context
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from PIL import Image as PILImage, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pywintypes import error as Win32Error

from windows_mcp.desktop.control import ControlBlocked, get_controller
from windows_mcp.infrastructure import with_analytics


Point = tuple[StrictInt, StrictInt]
Bounds = tuple[StrictInt, StrictInt, StrictInt, StrictInt]
POLL_SECONDS = 0.05
STABLE_SECONDS = 0.10
MIN_CHANGE = 8.0
STABILITY_TOLERANCE = 1.5
MAX_CAPTURE_PIXELS = 2_500_000


class HoverTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loc: Point = Field(description="Desktop pixel coordinates [x, y] of the hover target.")
    region: Bounds = Field(description="Preview crop [left, top, right, bottom] in desktop pixels.")
    probe: Bounds = Field(description="A small, static title or panel area inside region to detect the preview.")


class _CaptureStopped(Exception):
    pass


def _client_bounds(hwnd: int) -> Bounds:
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    return (*win32gui.ClientToScreen(hwnd, (left, top)), *win32gui.ClientToScreen(hwnd, (right, bottom)))


def _contains(outer: Bounds, inner: Bounds) -> bool:
    left, top, right, bottom = inner
    return outer[0] <= left < right <= outer[2] and outer[1] <= top < bottom <= outer[3]


def _contains_point(bounds: Bounds, point: Point) -> bool:
    x, y = point
    return bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]


def _signature(image: PILImage.Image) -> PILImage.Image:
    return image.convert("RGB").resize((48, 32), PILImage.Resampling.BOX)


def _difference(first: PILImage.Image, second: PILImage.Image) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(first, second)).mean) / 3


def _wait_stable(
    sample: Callable[[], PILImage.Image],
    matches: Callable[[PILImage.Image], bool],
    timeout: float,
    reason: str,
    *,
    stability_tolerance: float = STABILITY_TOLERANCE,
) -> PILImage.Image:
    deadline = time.monotonic() + timeout
    candidate = None
    stable_since = 0.0
    while True:
        frame = sample()
        now = time.monotonic()
        if now >= deadline:
            raise _CaptureStopped(reason)
        if not matches(frame):
            candidate = None
        elif candidate is None or _difference(candidate, frame) > stability_tolerance:
            candidate = frame
            stable_since = now
        elif now - stable_since >= STABLE_SECONDS:
            return frame
        time.sleep(min(POLL_SECONDS, deadline - now))


def _capture_batch(desktop, controller, window_title, idle_loc, targets, timeout_ms):
    controller.checkpoint_current()
    if not controller.session_active():
        raise ControlBlocked("NO_CONTROL_SESSION", controller.status())
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or win32gui.GetWindowText(hwnd).casefold() != window_title.casefold():
        raise ValueError("window_title must match the foreground window from a fresh view")
    bounds = _client_bounds(hwnd)
    if not _contains_point(bounds, idle_loc) or tuple(desktop.get_cursor_location()) != idle_loc:
        raise ValueError("Start with the cursor at idle_loc in the foreground client area and previews closed")

    captures = []
    images = []
    prepared = []
    pixels = 0
    for target in targets:
        if target.loc == idle_loc or not _contains_point(bounds, target.loc):
            raise ValueError("Every hover target must be inside the client area and differ from idle_loc")
        if not _contains(bounds, target.region) or not _contains(target.region, target.probe):
            raise ValueError("Each region must be inside the client area and contain its probe")
        region = desktop.parse_region_selection(target.region)
        probe = desktop.parse_region_selection(target.probe)
        pixels += region.width() * region.height()
        prepared.append((target, region, probe))
    if pixels > MAX_CAPTURE_PIXELS:
        raise ValueError("The combined preview crops exceed 2,500,000 pixels; use smaller crops or batches")

    def check_window():
        controller.checkpoint_current()
        if not controller.session_active():
            raise ControlBlocked("NO_CONTROL_SESSION", controller.status())
        try:
            unchanged = win32gui.GetForegroundWindow() == hwnd and _client_bounds(hwnd) == bounds
        except Win32Error:
            unchanged = False
        if not unchanged:
            raise _CaptureStopped("window_changed")

    def capture(rect):
        check_window()
        frame = desktop.get_screenshot(capture_rect=rect)
        check_window()
        if frame.size != (rect.width(), rect.height()):
            raise _CaptureStopped("capture_region_changed")
        return frame

    started = time.monotonic()
    timeout = timeout_ms / 1000
    for index, (target, region, probe) in enumerate(prepared, start=1):
        try:
            def sample():
                return _signature(capture(probe))

            idle = sample()
            check_window()
            desktop.move(target.loc)
            hovered = time.monotonic()
            visible = _wait_stable(sample, lambda frame: _difference(idle, frame) >= MIN_CHANGE,
                                   timeout, "preview_timeout")
            elapsed_ms = round((time.monotonic() - hovered) * 1000)
            frame = capture(region)
            probe_box = (probe.left - region.left, probe.top - region.top,
                         probe.right - region.left, probe.bottom - region.top)
            if _difference(_signature(frame.crop(probe_box)), visible) > STABILITY_TOLERANCE:
                raise _CaptureStopped("preview_changed_during_capture")

            with io.BytesIO() as buffer:
                frame.save(buffer, format="PNG")
                images.append(Image(data=buffer.getvalue(), format="png"))
            captures.append({"loc": target.loc, "region": target.region, "ready_ms": elapsed_ms})

            check_window()
            desktop.move(idle_loc)
            # A game may keep animating behind the tooltip while it is visible.
            # Wait for the probe to leave the tooltip image for consecutive frames.
            clear_distance = max(MIN_CHANGE, _difference(idle, visible) / 2)
            _wait_stable(sample, lambda frame: _difference(visible, frame) >= clear_distance,
                         timeout, "preview_did_not_clear", stability_tolerance=255)
        except _CaptureStopped as exc:
            return [json.dumps({"status": "stopped", "reason": str(exc), "target": index,
                                "captures": captures, "elapsed_ms": round((time.monotonic() - started) * 1000)}),
                    *images]

    return [json.dumps({"status": "ok", "captures": captures,
                        "elapsed_ms": round((time.monotonic() - started) * 1000)}), *images]


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="HoverCapture",
        description=(
            "Move the cursor over up to six verified targets in the current window and capture each "
            "preview in one call. Requires an active ControlBegin session. Start on a fresh view with "
            "the cursor at idle_loc and previews closed. Give the exact foreground window title and "
            "a static probe area inside each crop where the preview will appear. Returns ordered PNGs "
            "and timing metadata. Stops when focus or geometry changes, a preview times out, or the "
            "user cancels. The probe detects visual changes, so verify preview text in the result."
        ),
        annotations=ToolAnnotations(
            title="HoverCapture", readOnlyHint=False, destructiveHint=True,
            idempotentHint=False, openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "HoverCapture-Tool", run_sync_in_thread=False)
    def hover_capture_tool(
        window_title: Annotated[str, Field(min_length=1)],
        idle_loc: Point,
        targets: Annotated[list[HoverTarget], Field(min_length=1, max_length=6)],
        timeout_ms: Annotated[StrictInt, Field(ge=250, le=3000)] = 1250,
        ctx: Context = None,
    ):
        return _capture_batch(get_desktop(), get_controller(), window_title, idle_loc, targets, timeout_ms)
