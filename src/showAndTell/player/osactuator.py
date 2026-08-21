"""Real OS-input actuator for the Codex adapter (macOS CGEvent gestures).

Codex's record-and-replay captures real OS input, so its demonstrations must be
genuine CGEvent gestures. `OSActuator` locates each control with Playwright
(reusing the fixture's verified selectors) and clicks/types at the control's
on-screen coordinates with osinput. It is the actuator half of the generic
OS-input ops: `OSOps` (showAndTell.player.osops) passes it a Playwright locator
for every routed click/fill/press.

The CGEvent path can only be exercised on a real display with Accessibility
permission, so it is not covered by the headless test suite — the proxy that
routes to it is (tests/test_core_osinput.py).
"""
from __future__ import annotations

import time

from showAndTell.core import osinput

_KEYCODES = {
    "Enter": osinput.KEY_RETURN,
    "Return": osinput.KEY_RETURN,
    "Tab": osinput.KEY_TAB,
    "Backspace": osinput.KEY_BACKSPACE,
    "ArrowDown": osinput.KEY_DOWN,
    "ArrowUp": osinput.KEY_UP,
}


class OSActuator:
    """Routed actuation as real OS input. Reads/navigation never reach here —
    the proxy passes those through to the real page."""

    def __init__(self, activate) -> None:
        # activate(): bring the managed Chrome window frontmost so global clicks
        # land in it (the product's own window can overlap).
        self._activate = activate

    def reactivate(self, page) -> None:
        self._activate()

    def _content_origin(self, page) -> tuple[float, float]:
        m = page.evaluate("() => ({sx: window.screenX, sy: window.screenY,"
                          " oh: window.outerHeight, ih: window.innerHeight})")
        return m["sx"], m["sy"] + (m["oh"] - m["ih"])  # content top = window top + chrome height

    def click(self, page, locator, *, position=None) -> None:
        # Captured positions are offsets in the target's raw DOM rectangle.
        # Applying them to _actionable_box is wrong because that helper may
        # intentionally collapse the rectangle to a 1x1 hit-tested point; the
        # offset then lands hundreds of pixels away from the target.  Preserve
        # positioned clicks against the raw box and clamp stale coordinates
        # inside today's geometry.
        box = (self._actionable_box(page, locator) if position is None
               else self._visible_box(page, locator))
        ox, oy = self._content_origin(page)
        if position is None:
            x, y = box["width"] / 2, box["height"] / 2
        else:
            def inside(value, extent):
                inset = min(0.5, float(extent) / 2)
                return min(max(float(value), inset),
                           max(inset, float(extent) - inset))

            x = inside(position["x"], box["width"])
            y = inside(position["y"], box["height"])
        self._activate()
        osinput.click(ox + box["x"] + x, oy + box["y"] + y, settle=0.6)

    def drag(self, page, source, destination, *, source_position=None,
             target_position=None, path=None, drag_mode=None) -> None:
        # Drag offsets were captured against getBoundingClientRect(), so they
        # must be applied to the raw post-scroll locator box. _actionable_box
        # may intentionally collapse a click target to a 1x1 hit-tested point.
        source_box = self._visible_box(page, source)
        if drag_mode == "pointer":
            from .replay import _pointer_drag_geometry

            relative_start, relative_points = _pointer_drag_geometry(
                source_box, source_position, path)
            viewport_points = [
                (source_box["x"] + point["x"],
                 source_box["y"] + point["y"])
                for point in relative_points
            ]
            start = (source_box["x"] + relative_start["x"],
                     source_box["y"] + relative_start["y"])
            if not viewport_points or viewport_points[0] != start:
                viewport_points.insert(0, start)
            if len(viewport_points) < 2:
                raise RuntimeError("captured pointer drag has no movement path")
            ox, oy = self._content_origin(page)
            self._activate()
            osinput.drag([(ox + x, oy + y) for x, y in viewport_points])
            return

        destination_box = self._visible_box(page, destination)
        if drag_mode == "native":
            from .replay import _pointer_drag_geometry

            source_position, _ = _pointer_drag_geometry(
                source_box, source_position, [])
            target_position, _ = _pointer_drag_geometry(
                destination_box, target_position, [])
        sx = ((source_position or {}).get("x")
              if source_position is not None else source_box["width"] / 2)
        sy = ((source_position or {}).get("y")
              if source_position is not None else source_box["height"] / 2)
        start = (source_box["x"] + sx, source_box["y"] + sy)
        tx = ((target_position or {}).get("x")
              if target_position is not None else destination_box["width"] / 2)
        ty = ((target_position or {}).get("y")
              if target_position is not None else destination_box["height"] / 2)
        viewport_points = [
            (source_box["x"] + point["x"], source_box["y"] + point["y"])
            for point in (path or [])[:-1]
        ]
        destination_point = (
            destination_box["x"] + tx, destination_box["y"] + ty)
        if not viewport_points:
            viewport_points = [start]
        elif viewport_points[0] != start:
            viewport_points.insert(0, start)
        viewport_points.append(destination_point)
        ox, oy = self._content_origin(page)
        self._activate()
        osinput.drag([(ox + x, oy + y) for x, y in viewport_points])

    def _visible_box(self, page, locator) -> dict:
        """Return the locator's raw top-level viewport box after scrolling."""
        locator.wait_for(state="visible", timeout=15000)
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(200)
        box = locator.bounding_box(timeout=5000)
        if box is None:
            raise RuntimeError("element has no bounding box (not visible)")
        return box

    def _actionable_box(self, page, locator) -> dict:
        """Return a hit-tested box suitable for an unpositioned click.

        scroll_into_view_if_needed defaults to a 30s wait and can hang on a
        visible-but-restless element (seen live on GitLab issue pages, where the
        Vue discussion area keeps settling). Bound it and fall back to the
        bounding box, which is valid whenever the element is actually on screen."""
        # Wait for the control to actually render before measuring it: on the OS
        # path (slower, recorder running) a field like the comment box can take a
        # while to hydrate. A genuine miss raises TimeoutError so OSOps re-runs
        # the (entry-guarded) primitive, instead of bounding_box hanging its 30s.
        box = self._visible_box(page, locator)
        # A DOM rectangle is not necessarily a clickable rectangle. Inline and
        # overflowed controls can report a union taller than their painted hit
        # area (an observed worksheet label was 65px tall inside a 33px tab), so
        # the geometric centre landed below the control. Sample several points
        # and use the first one whose browser hit-test resolves to the locator or
        # one of its descendants. Fractions map the frame-local DOM point back
        # onto Playwright's top-level viewport box, so this also works in iframes.
        try:
            point = locator.evaluate(
                "el => {"
                "  const bounds = el.getBoundingClientRect();"
                "  if (!bounds.width || !bounds.height) return null;"
                "  const fractions = [[.5,.5],[.5,.25],[.25,.5],[.75,.5],"
                "                     [.5,.75],[.25,.25],[.75,.25],"
                "                     [.25,.75],[.75,.75]];"
                "  for (const [fx, fy] of fractions) {"
                "    const x = bounds.left + bounds.width * fx;"
                "    const y = bounds.top + bounds.height * fy;"
                "    const hit = el.ownerDocument.elementFromPoint(x, y);"
                "    if (hit && (hit === el || el.contains(hit)))"
                "      return {fx, fy};"
                "  }"
                "  return null;"
                "}")
            if point:
                x = box["x"] + box["width"] * point["fx"]
                y = box["y"] + box["height"] * point["fy"]
                box = {"x": x - 0.5, "y": y - 0.5,
                       "width": 1.0, "height": 1.0}
        except Exception:
            pass
        return box

    def fill(self, page, locator, text) -> None:
        """Reproduce replacement-style fills with real OS keystrokes.

        Browser captures emit cumulative input snapshots.  A human edit such
        as ``Hi`` -> Enter -> ``Hi\nthere`` therefore reaches this method more
        than once.  Apply only the single contiguous slice that changed
        (caret there, backspace the removed text, type the added text);
        clearing and retyping every snapshot makes the visible replay look
        corrupted and multiplies its duration.  Select-all replace is reserved
        for wholesale rewrites.  Verify the complete value, not a shared
        prefix.
        """
        for _ in range(4):
            focused = False
            try:
                focused = bool(locator.evaluate(
                    "el => el === document.activeElement"))
            except Exception:
                pass
            if not focused:
                # Focus can propagate hundreds of ms after a trusted click
                # while the recorder interceptor settles; poll patiently
                # after each click — an eager re-click is recorded by the
                # teaching product as its own duplicate step.
                for _ in range(3):
                    self.click(page, locator)
                    for _ in range(6):
                        page.wait_for_timeout(250)
                        if locator.evaluate(
                                "el => el === document.activeElement"):
                            focused = True
                            break
                    if focused:
                        break

            current = self._value(locator)
            if current == text:
                return
            # DOM focus proves which element inside Chrome owns keystrokes,
            # not which APP the window server routes them to. Re-activate
            # before every typing attempt or the keystrokes land in whatever
            # application is frontmost (e.g. the operator's terminal).
            self._activate()
            caret, remove, added = self._single_edit(current, text)
            if remove <= self._MAX_EDIT_BACKSPACES:
                # One contiguous slice changed: edit it the way a human would
                # (caret there, backspace the old slice, type the new one)
                # instead of visibly retyping the entire message.
                self._move_caret(locator, current, caret)
                for _ in range(remove):
                    osinput.key(osinput.KEY_BACKSPACE, settle=0.03)
                osinput.type_text(added)
            else:
                # Wholesale replacement using genuine keyboard input.  Keycode
                # 0 is the ANSI A key on macOS; Command+A then Backspace clears
                # the focused input without an invisible CDP mutation.
                osinput.key(0, modifiers=["Meta"])
                osinput.key(osinput.KEY_BACKSPACE)
                osinput.type_text(text)
            page.wait_for_timeout(300)
            if self._value(locator) == text:
                return
        locator.fill(text)  # last resort: guarantee the text is present

    # Larger rewrites read better as one select-all replace than as a long
    # char-by-char backspace walk nothing like the recorded session.
    _MAX_EDIT_BACKSPACES = 40

    @staticmethod
    def _single_edit(current: str, target: str) -> tuple[int, int, str]:
        """The one contiguous slice replacement turning current into target:
        (caret after the old slice, chars to backspace, text to type)."""
        prefix = 0
        limit = min(len(current), len(target))
        while prefix < limit and current[prefix] == target[prefix]:
            prefix += 1
        suffix = 0
        while (suffix < len(current) - prefix
               and suffix < len(target) - prefix
               and current[len(current) - suffix - 1]
               == target[len(target) - suffix - 1]):
            suffix += 1
        remove = len(current) - prefix - suffix
        end = len(target) - suffix if suffix else len(target)
        return prefix + remove, remove, target[prefix:end]

    @classmethod
    def _pure_insertion(cls, current: str, target: str) -> tuple[int, str] | None:
        """Return the one inserted slice when the old value is preserved."""
        caret, remove, added = cls._single_edit(current, target)
        if remove:
            return None
        return caret, added

    @staticmethod
    def _selection(locator) -> tuple[int, int] | None:
        try:
            selection = locator.evaluate(
                "el => typeof el.selectionStart === 'number'"
                " ? {start: el.selectionStart, end: el.selectionEnd} : null")
            if isinstance(selection, dict):
                return int(selection["start"]), int(selection["end"])
        except Exception:
            pass
        return None

    def _move_caret(self, locator, current: str, offset: int) -> None:
        """Move a real textarea/input caret without mutating it through CDP."""
        selection = self._selection(locator)
        if selection == (offset, offset):
            return
        if offset == len(current):
            osinput.key(osinput.KEY_DOWN, modifiers=["Meta"])
            if self._selection(locator) != (offset, offset):
                osinput.key(osinput.KEY_RIGHT, modifiers=["Meta"])
            return

        osinput.key(osinput.KEY_UP, modifiers=["Meta"])
        if offset == 0:
            return
        prefix = current[:offset]
        lines = prefix.count("\n")
        column = len(prefix.rsplit("\n", 1)[-1])
        for _ in range(lines):
            osinput.key(osinput.KEY_DOWN)
        for _ in range(column):
            osinput.key(osinput.KEY_RIGHT)
        if self._selection(locator) == (offset, offset):
            return
        # Wrapped lines can make ArrowDown land at a visual rather than logical
        # line.  A start-to-offset walk is slower but remains genuine OS input
        # and is used only as a correctness fallback.
        osinput.key(osinput.KEY_UP, modifiers=["Meta"])
        for _ in range(offset):
            osinput.key(osinput.KEY_RIGHT)

    def press(self, page, key) -> None:
        parts = str(key).split("+")
        code = _KEYCODES.get(parts[-1])
        if code is None:
            raise RuntimeError(f"OSActuator.press: unmapped key {key!r}")
        self._activate()  # keystrokes go to the frontmost app, not DOM focus
        modifiers = parts[:-1]
        if modifiers:
            osinput.key(code, modifiers=modifiers)
        else:
            osinput.key(code)

    def type_text(self, page, text) -> None:
        """Type into the control already focused by a preceding OS gesture."""
        self._activate()
        osinput.type_text(text)

    def paste_text(self, page, text) -> None:
        """Paste into the control already focused by a preceding OS gesture."""
        self._activate()
        osinput.paste_text(text)

    def select(self, page, locator, label) -> None:
        """Choose an option in a native <select> as real OS input: click it
        open (macOS native popup), type-ahead the option's visible label, and
        commit with Return. While the popup is open the renderer's event loop
        is blocked, so the waits between gestures are time.sleep, never
        page.*. Verify the selection landed and retry; Playwright
        select_option is the last resort so the demo still completes (the
        same trade-off fill makes)."""
        for _ in range(3):
            if self._selected_label(locator) == label:
                return  # entry-guard doubling as verify: already selected
            self.click(page, locator)
            time.sleep(0.5)
            osinput.type_text(label)
            time.sleep(0.3)
            osinput.key(osinput.KEY_RETURN)
            time.sleep(0.4)
        if self._selected_label(locator) == label:
            return
        locator.select_option(label=label)

    @staticmethod
    def _selected_label(locator) -> str:
        try:
            return (locator.evaluate(
                "el => el.selectedOptions && el.selectedOptions[0]"
                "      ? el.selectedOptions[0].label.trim() : ''") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _value(locator) -> str:
        try:
            v = locator.input_value()
            if v is not None:
                return v
        except Exception:
            pass
        try:
            return locator.inner_text() or ""
        except Exception:
            return ""
