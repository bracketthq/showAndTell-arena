"""OS-level trusted input on macOS (CGEvent) — the only input Teach recorders count.

Synthetic CDP/JS input is invisible to (or swallowed by) product recorders like
Claude-in-Chrome's Teach mode. CGEvent posts go through the window server and
are indistinguishable from a human. Requires Accessibility permission for the
process that runs the command (System Settings → Privacy & Security →
Accessibility → your terminal).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

try:
    import Quartz
    from ApplicationServices import AXIsProcessTrusted
    from AppKit import NSPasteboard, NSPasteboardTypeString
except ImportError:                     # non-macOS: importable, unusable
    Quartz = None
    NSPasteboard = None
    NSPasteboardTypeString = None

    def AXIsProcessTrusted() -> bool:   # keeps assert_trusted() honest off-mac
        return False


def _require_quartz() -> None:
    if Quartz is None:
        raise RuntimeError("OS-level input requires macOS (Quartz unavailable)")

KEY_RETURN = 36
KEY_TAB = 48
KEY_BACKSPACE = 51
KEY_RIGHT = 124
KEY_DOWN = 125
KEY_UP = 126
KEY_V = 9

_MODIFIER_FLAG_NAMES = {
    "Control": "kCGEventFlagMaskControl",
    "Ctrl": "kCGEventFlagMaskControl",
    "Meta": "kCGEventFlagMaskCommand",
    "Command": "kCGEventFlagMaskCommand",
    "Alt": "kCGEventFlagMaskAlternate",
    "Option": "kCGEventFlagMaskAlternate",
    "Shift": "kCGEventFlagMaskShift",
}

_MODIFIER_KEYCODES = {          # left-hand modifier virtual keycodes
    "kCGEventFlagMaskCommand": 55,
    "kCGEventFlagMaskShift": 56,
    "kCGEventFlagMaskAlternate": 58,
    "kCGEventFlagMaskControl": 59,
}


def assert_trusted() -> None:
    if AXIsProcessTrusted():
        return
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)
        # pops the system permission dialog and pre-registers this binary in
        # the Accessibility list, so granting is one toggle instead of a
        # manual add-and-locate of a framework path
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except ImportError:
        pass
    python_app = Path(sys.base_prefix) / "Resources" / "Python.app"
    raise SystemExit(
        "OS-level input needs Accessibility permission — macOS just opened "
        "the request. Grant it (System Settings → Privacy & Security → "
        f"Accessibility → enable {python_app}), then retry.")


def move(x: float, y: float) -> None:
    _require_quartz()
    e = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y),
                                       Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def click(x: float, y: float, settle: float = 0.4) -> None:
    _require_quartz()
    move(x, y)
    time.sleep(0.15)
    for etype in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        e = Quartz.CGEventCreateMouseEvent(None, etype, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.06)
    time.sleep(settle)


def drag(points, settle: float = 0.6) -> None:
    """Drag through screen-coordinate points as trusted macOS mouse input."""
    _require_quartz()
    points = list(points)
    if len(points) < 2:
        raise ValueError("a drag needs at least a start and end point")
    move(*points[0])
    time.sleep(0.12)
    down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, points[0], Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.06)
    for point in points[1:]:
        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, point,
            Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)
    up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, points[-1], Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    time.sleep(settle)


def _flags_changed(mod_keycode: int, down: bool, held_flags: int) -> None:
    e = Quartz.CGEventCreateKeyboardEvent(None, mod_keycode, down)
    Quartz.CGEventSetType(e, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(e, held_flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    time.sleep(0.02)


def key(keycode: int, settle: float = 0.15, modifiers=()) -> None:
    _require_quartz()
    chord = []                  # (modifier keycode, flag) in press order
    flags = 0
    for modifier in modifiers:
        flag_name = _MODIFIER_FLAG_NAMES.get(str(modifier))
        if flag_name is None:
            raise ValueError(f"unsupported key modifier: {modifier!r}")
        flag = getattr(Quartz, flag_name)
        if not flags & flag:
            chord.append((_MODIFIER_KEYCODES[flag_name], flag))
        flags |= flag
    # Modifiers must be pressed and released as their own flags-changed
    # transitions. Setting flags on the letter events alone latches the
    # modifier in the window-server HID state indefinitely, and every later
    # NULL-source keystroke or click inherits it (typing 't' becomes Cmd+T).
    held = 0
    for mod_keycode, flag in chord:
        held |= flag
        _flags_changed(mod_keycode, True, held)
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.04)
    for mod_keycode, flag in reversed(chord):
        held &= ~flag
        _flags_changed(mod_keycode, False, held)
    time.sleep(settle)


def type_text(text: str, settle: float = 0.2) -> None:
    """Type an arbitrary string via CGEvent Unicode injection (no keycode
    mapping). Real OS keystrokes, so product recorders capture them.

    Newlines are the exception: Chrome drops a "\\n" injected as a Unicode
    string on keycode 0, silently collapsing a multi-line value onto one
    line, so they are typed as genuine Return presses instead."""
    _require_quartz()
    for ch in text:
        if ch == "\r":
            continue            # "\r\n" would double the Return
        if ch == "\n":
            key(KEY_RETURN, settle=0.05)
            continue
        for down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(e, len(ch), ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
            time.sleep(0.01)
    time.sleep(settle)


def paste_text(text: str, settle: float = 0.2) -> None:
    """Paste plain text through the macOS clipboard and a trusted Cmd+V.

    The shortcut travels through the same CGEvent plane as every other replay
    gesture, so recorders see a genuine paste and spreadsheet editors retain
    tab/newline cell boundaries.
    """
    _require_quartz()
    if NSPasteboard is None:
        raise RuntimeError("OS-level paste requires macOS AppKit")
    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    if not pasteboard.setString_forType_(str(text), NSPasteboardTypeString):
        raise RuntimeError("failed to write replay text to the macOS clipboard")
    key(KEY_V, modifiers=["Meta"], settle=settle)
