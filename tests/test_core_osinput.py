"""osinput must IMPORT everywhere (drivers.py pulls it at module level, so a
hard Quartz import would break pytest collection and every CLI entry on
Linux) — it only needs to WORK on macOS."""
import subprocess
import sys

import pytest

CODE = """
import sys
class BlockQuartz:
    def find_module(self, name, path=None):
        if name.split(".")[0] in ("Quartz", "ApplicationServices", "AppKit"):
            return self
    def load_module(self, name):
        raise ImportError(f"blocked: {name}")
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in ("Quartz", "ApplicationServices", "AppKit"):
            raise ImportError(f"blocked: {name}")
        return None
sys.meta_path.insert(0, BlockQuartz())
for m in [m for m in sys.modules if m.split(".")[0] in ("Quartz", "ApplicationServices", "AppKit")]:
    del sys.modules[m]
import showAndTell.core.osinput as osinput
assert osinput.Quartz is None
assert osinput.NSPasteboard is None
try:
    osinput.click(1, 1)
except RuntimeError as e:
    assert "macOS" in str(e)
    print("IMPORT-OK")
"""


def test_osinput_imports_and_degrades_without_quartz():
    r = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True)
    assert "IMPORT-OK" in r.stdout, r.stderr


def test_osinput_accessibility_preflight_names_python_app(monkeypatch):
    from showAndTell.core import osinput

    monkeypatch.setattr(osinput, "AXIsProcessTrusted", lambda: False)

    with pytest.raises(SystemExit) as exc:
        osinput.assert_trusted()

    message = str(exc.value)
    assert "Accessibility" in message
    assert "Python.app" in message


class _FakeQuartz:
    """Records CGEvent posts so modifier handling is assertable without
    hijacking the desktop. Flag mask values mirror CGEventFlags."""
    kCGEventFlagMaskCommand = 0x100000
    kCGEventFlagMaskShift = 0x20000
    kCGEventFlagMaskControl = 0x40000
    kCGEventFlagMaskAlternate = 0x80000
    kCGEventFlagsChanged = 12
    kCGHIDEventTap = 0

    def __init__(self):
        self.posted = []

    def CGEventCreateKeyboardEvent(self, _source, keycode, down):
        return {"keycode": keycode, "down": down, "flags": None,
                "type": "keydown" if down else "keyup"}

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, event, length, string):
        event["unicode"] = string

    def CGEventSetType(self, event, event_type):
        event["type"] = ("flagschanged"
                         if event_type == self.kCGEventFlagsChanged
                         else event_type)

    def CGEventPost(self, _tap, event):
        self.posted.append(dict(event))


def test_modified_key_releases_the_modifier_in_os_state(monkeypatch):
    """A Cmd-chord must not latch Command in the window-server HID state:
    the letter events carry the flag, bracketed by real modifier press and
    release transitions. Without the trailing release every later keystroke
    and click inherits Command (typing 't' opens a tab)."""
    from showAndTell.core import osinput

    fake = _FakeQuartz()
    monkeypatch.setattr(osinput, "Quartz", fake)
    monkeypatch.setattr(osinput.time, "sleep", lambda _s: None)

    osinput.key(0, modifiers=["Meta"])

    cmd = fake.kCGEventFlagMaskCommand
    letters = [e for e in fake.posted if e["keycode"] == 0]
    assert [(e["type"], e["flags"]) for e in letters] == [
        ("keydown", cmd), ("keyup", cmd)]

    transitions = [e for e in fake.posted if e["type"] == "flagschanged"]
    assert len(transitions) == 2, (
        "modifier must be pressed and released as flags-changed events")
    press, release = transitions
    assert fake.posted.index(press) < fake.posted.index(letters[0])
    assert fake.posted.index(release) > fake.posted.index(letters[1])
    assert press["flags"] & cmd
    assert not release["flags"] & cmd, (
        "release must clear Command or the OS keeps it held forever")


def test_type_text_types_newlines_as_return_keystrokes(monkeypatch):
    """Chrome drops a "\\n" injected as a Unicode string on keycode 0, so a
    multi-line value typed that way collapses onto one line (and the replay
    wipes and retypes it forever). Newlines must be genuine Return presses."""
    from showAndTell.core import osinput

    fake = _FakeQuartz()
    monkeypatch.setattr(osinput, "Quartz", fake)
    monkeypatch.setattr(osinput.time, "sleep", lambda _s: None)

    osinput.type_text("a\nb")

    downs = [e for e in fake.posted if e["down"]]
    assert [e.get("unicode", e["keycode"]) for e in downs] == [
        "a", osinput.KEY_RETURN, "b"]
    assert "unicode" not in downs[1], "newline must be a Return key event"


def test_paste_text_writes_clipboard_and_sends_trusted_command_v(monkeypatch):
    from showAndTell.core import osinput

    fake_quartz = _FakeQuartz()

    class Pasteboard:
        value = None

        def clearContents(self):
            self.value = None

        def setString_forType_(self, value, value_type):
            assert value_type == "text-type"
            self.value = value
            return True

    pasteboard = Pasteboard()

    class Pasteboards:
        @staticmethod
        def generalPasteboard():
            return pasteboard

    monkeypatch.setattr(osinput, "Quartz", fake_quartz)
    monkeypatch.setattr(osinput, "NSPasteboard", Pasteboards)
    monkeypatch.setattr(osinput, "NSPasteboardTypeString", "text-type")
    monkeypatch.setattr(osinput.time, "sleep", lambda _s: None)

    osinput.paste_text("4\t4467.6\n5\t1200")

    assert pasteboard.value == "4\t4467.6\n5\t1200"
    command = fake_quartz.kCGEventFlagMaskCommand
    letters = [event for event in fake_quartz.posted
               if event["keycode"] == osinput.KEY_V]
    assert [(event["type"], event["flags"]) for event in letters] == [
        ("keydown", command), ("keyup", command)]


def test_codex_os_actuator_routes_paste_through_real_os_input(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    calls = []
    monkeypatch.setattr(osinput, "paste_text", calls.append)

    OSActuator(lambda: calls.append("activate")).paste_text(
        object(), "a captured paste")

    assert calls == ["activate", "a captured paste"]


def test_unmodified_key_posts_no_modifier_transitions(monkeypatch):
    from showAndTell.core import osinput

    fake = _FakeQuartz()
    monkeypatch.setattr(osinput, "Quartz", fake)
    monkeypatch.setattr(osinput.time, "sleep", lambda _s: None)

    osinput.key(osinput.KEY_RETURN)

    assert [(e["type"], e["keycode"], e["flags"]) for e in fake.posted] == [
        ("keydown", osinput.KEY_RETURN, None),
        ("keyup", osinput.KEY_RETURN, None)]


def test_codex_os_actuator_supports_spreadsheet_editing_keys(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    pressed = []
    monkeypatch.setattr(osinput, "key", pressed.append)
    actuator = OSActuator(lambda: None)

    actuator.press(None, "Tab")
    actuator.press(None, "Backspace")

    assert pressed == [osinput.KEY_TAB, osinput.KEY_BACKSPACE]


def test_codex_os_actuator_preserves_modified_key_chords(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    pressed = []
    monkeypatch.setattr(
        osinput, "key",
        lambda keycode, **kwargs: pressed.append((keycode, kwargs)))

    OSActuator(lambda: None).press(None, "Meta+Enter")

    assert pressed == [(osinput.KEY_RETURN, {"modifiers": ["Meta"]})]


def test_codex_os_fill_appends_only_unseen_cumulative_suffix(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "Hi\n"

        def evaluate(self, script):
            if "? {start:" in script:
                return {"start": len(self.value), "end": len(self.value)}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    typed = []
    keys = []
    monkeypatch.setattr(osinput, "type_text", lambda value: (
        typed.append(value), setattr(locator, "value", locator.value + value)))
    monkeypatch.setattr(osinput, "key", lambda *args, **kwargs: keys.append((args, kwargs)))
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(actuator, "click", lambda *_args, **_kwargs: None)

    actuator.fill(Page(), locator, "Hi\nthere")

    assert typed == ["there"]
    assert keys == []
    assert locator.value == "Hi\nthere"


def test_codex_os_fill_does_not_reclick_an_already_focused_field(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "Hi"

        def evaluate(self, script):
            if "document.activeElement" in script:
                return True
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    monkeypatch.setattr(
        osinput, "type_text",
        lambda value: setattr(locator, "value", locator.value + value))
    actuator = OSActuator(lambda: None)
    clicks = []
    monkeypatch.setattr(
        actuator, "click", lambda *_args, **_kwargs: clicks.append(True))

    actuator.fill(Page(), locator, "Hi there")

    assert clicks == []
    assert locator.value == "Hi there"


def test_codex_os_fill_activates_chrome_before_typing_into_a_focused_field(
        monkeypatch):
    """DOM focus says which element inside Chrome owns keystrokes, not which
    APP owns them. The no-click fast path must still bring Chrome frontmost or
    every OS keystroke lands in whatever application the user last touched."""
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "Hi"

        def evaluate(self, script):
            if "? {start:" in script:
                return {"start": len(self.value), "end": len(self.value)}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    calls = []
    monkeypatch.setattr(osinput, "type_text", lambda value: (
        calls.append("type"),
        setattr(locator, "value", locator.value + value)))
    monkeypatch.setattr(osinput, "key", lambda *a, **k: calls.append("key"))
    actuator = OSActuator(lambda: calls.append("activate"))
    monkeypatch.setattr(actuator, "click", lambda *_a, **_k: None)

    actuator.fill(Page(), locator, "Hi there")

    assert "type" in calls
    assert "activate" in calls[:calls.index("type")], (
        "fill must activate Chrome before posting OS keystrokes")


def test_codex_os_press_activates_chrome_before_the_keystroke(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    calls = []
    monkeypatch.setattr(
        osinput, "key", lambda *a, **k: calls.append(("key", a, k)))
    actuator = OSActuator(lambda: calls.append("activate"))

    actuator.press(None, "Enter")

    assert calls == ["activate", ("key", (osinput.KEY_RETURN,), {})]


def test_codex_os_fill_moves_a_clicked_midfield_caret_to_the_end(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "Hi"
        focused = False
        at_end = False

        def evaluate(self, script):
            if "document.activeElement" in script:
                return self.focused
            if "? {start:" in script:
                position = len(self.value) if self.at_end else 1
                return {"start": position, "end": position}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    keys = []

    def key(code, **kwargs):
        keys.append((code, kwargs))
        locator.at_end = True

    monkeypatch.setattr(osinput, "key", key)
    monkeypatch.setattr(
        osinput, "type_text",
        lambda value: setattr(locator, "value", locator.value + value))
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(
        actuator, "click",
        lambda *_args, **_kwargs: setattr(locator, "focused", True))

    actuator.fill(Page(), locator, "Hi there")

    assert keys == [(osinput.KEY_DOWN, {"modifiers": ["Meta"]})]
    assert locator.value == "Hi there"


def test_codex_os_fill_types_only_an_insertion_before_preserved_text(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "ORIGINAL"
        caret = 4

        def evaluate(self, script):
            if "document.activeElement" in script:
                return True
            if "? {start:" in script:
                return {"start": self.caret, "end": self.caret}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    keys = []

    def key(code, **kwargs):
        keys.append((code, kwargs))
        if code == osinput.KEY_UP and kwargs.get("modifiers") == ["Meta"]:
            locator.caret = 0

    def type_text(value):
        locator.value = locator.value[:locator.caret] + value + locator.value[locator.caret:]
        locator.caret += len(value)

    monkeypatch.setattr(osinput, "key", key)
    monkeypatch.setattr(osinput, "type_text", type_text)
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(actuator, "click", lambda *_args, **_kwargs: None)

    actuator.fill(Page(), locator, "Header\n\nORIGINAL")

    assert keys == [(osinput.KEY_UP, {"modifiers": ["Meta"]})]
    assert locator.value == "Header\n\nORIGINAL"


def test_codex_os_fill_replaces_diverged_value_and_verifies_all_text(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        # Long wholesale divergence: char-by-char backspacing would look
        # nothing like the recorded session, so select-all replace is right.
        value = "x" * 200

        def evaluate(self, _script):
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    keys = []

    def key(code, **kwargs):
        keys.append((code, kwargs))
        if code == osinput.KEY_BACKSPACE:
            locator.value = ""

    monkeypatch.setattr(osinput, "key", key)
    monkeypatch.setattr(
        osinput, "type_text",
        lambda value: setattr(locator, "value", locator.value + value))
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(actuator, "click", lambda *_args, **_kwargs: None)

    actuator.fill(Page(), locator, "correct")

    assert keys == [(0, {"modifiers": ["Meta"]}),
                    (osinput.KEY_BACKSPACE, {})]
    assert locator.value == "correct"


def test_codex_os_fill_makes_a_small_edit_without_retyping_the_message(
        monkeypatch):
    """The recorded human deleted one trailing space and pressed Return.
    Replaying that as select-all + full retype makes the demo visibly retype
    the whole message; a single-slice edit must backspace the removed text
    and type only the new slice."""
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = "sign off. \n\n-------- Original --------\nHello"
        caret = 0

        def evaluate(self, script):
            if "document.activeElement" in script:
                return True
            if "? {start:" in script:
                return {"start": self.caret, "end": self.caret}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    target = "sign off.\n\n\n-------- Original --------\nHello"
    locator = Locator()
    keys = []

    def key(code, **kwargs):
        keys.append((code, kwargs))
        if code == osinput.KEY_UP and kwargs.get("modifiers") == ["Meta"]:
            locator.caret = 0
        elif code == osinput.KEY_RIGHT and not kwargs.get("modifiers"):
            locator.caret += 1
        elif code == osinput.KEY_BACKSPACE:
            locator.value = (locator.value[:locator.caret - 1]
                             + locator.value[locator.caret:])
            locator.caret -= 1

    def type_text(value):
        for ch in value:
            locator.value = (locator.value[:locator.caret] + ch
                             + locator.value[locator.caret:])
            locator.caret += 1

    monkeypatch.setattr(osinput, "key", key)
    monkeypatch.setattr(osinput, "type_text", type_text)
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(actuator, "click", lambda *_args, **_kwargs: None)

    actuator.fill(Page(), locator, target)

    assert locator.value == target
    assert (0, {"modifiers": ["Meta"]}) not in keys, (
        "a one-character edit must not select-all and retype the message")
    assert [code for code, _ in keys].count(osinput.KEY_BACKSPACE) == 1


def test_codex_os_actuator_replays_drag_path_as_screen_input(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 10, "sy": 20, "oh": 100, "ih": 80}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def __init__(self, box):
            self.box = box

        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return self.box

        def evaluate(self, _script):
            return None

    paths = []
    monkeypatch.setattr(osinput, "drag", paths.append)
    actuator = OSActuator(lambda: None)
    actuator.drag(
        Page(),
        Locator({"x": 100, "y": 50, "width": 80, "height": 40}),
        Locator({"x": 300, "y": 200, "width": 100, "height": 60}),
        source_position={"x": 5, "y": 6},
        target_position={"x": 7, "y": 8},
        path=[{"x": 5, "y": 6}, {"x": 90, "y": 40}, {"x": 200, "y": 90}],
    )

    assert paths == [[(115, 96), (200, 130), (317, 248)]]


def test_codex_os_actuator_pointer_drag_uses_final_path_without_destination(
        monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 10, "sy": 20, "oh": 100, "ih": 80}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return {"x": 100, "y": 50, "width": 5, "height": 30}

        def evaluate(self, _script):
            # Drag geometry must never be rebased onto the 1x1 hit point used
            # by unpositioned clicks.
            return {"fx": 0.5, "fy": 0.5}

    paths = []
    monkeypatch.setattr(osinput, "drag", paths.append)
    OSActuator(lambda: None).drag(
        Page(), Locator(), None,
        source_position={"x": 4.6, "y": 15}, target_position=None,
        path=[{"x": 4.6, "y": 15}, {"x": 40, "y": 15},
              {"x": 80, "y": 15}],
        drag_mode="pointer",
    )

    assert paths == [[(114.5, 105.0), (149.9, 105.0), (189.9, 105.0)]]


def test_codex_os_pointer_drag_clamps_current_handle_and_preserves_delta(
        monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 0, "sy": 0, "oh": 0, "ih": 0}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return {"x": 100, "y": 50, "width": 4, "height": 30}

    paths = []
    monkeypatch.setattr(osinput, "drag", paths.append)
    OSActuator(lambda: None).drag(
        Page(), Locator(), None,
        source_position={"x": 4.6, "y": 15}, target_position=None,
        path=[{"x": 4.6, "y": 15}, {"x": 50, "y": 15}],
        drag_mode="pointer",
    )

    assert paths == [[(103.5, 65.0), (148.9, 65.0)]]


def test_codex_os_native_drag_clamps_source_and_destination(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 0, "sy": 0, "oh": 0, "ih": 0}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def __init__(self, box):
            self.box = box

        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return self.box

    paths = []
    monkeypatch.setattr(osinput, "drag", paths.append)
    OSActuator(lambda: None).drag(
        Page(),
        Locator({"x": 10, "y": 20, "width": 5, "height": 30}),
        Locator({"x": 100, "y": 20, "width": 4, "height": 20}),
        source_position={"x": 5, "y": 30},
        target_position={"x": -2, "y": 25}, path=[],
        drag_mode="native",
    )

    assert paths == [[(14.5, 49.5), (100.5, 39.5)]]


def test_codex_os_actuator_clicks_a_browser_hit_tested_point(monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 10, "sy": 20, "oh": 100, "ih": 80}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return {"x": 100, "y": 50, "width": 80, "height": 60}

        def evaluate(self, _script):
            return {"fx": 0.5, "fy": 0.25}

    clicks = []
    monkeypatch.setattr(
        osinput, "click",
        lambda x, y, settle=0.4: clicks.append((x, y, settle)))

    OSActuator(lambda: None).click(Page(), Locator())

    assert clicks == [(150.0, 105.0, 0.6)]


def test_codex_os_positioned_click_uses_raw_box_and_clamps_stale_offset(
        monkeypatch):
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def evaluate(self, _script):
            return {"sx": 10, "sy": 20, "oh": 100, "ih": 80}

        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        def wait_for(self, **_kwargs):
            pass

        def scroll_into_view_if_needed(self, **_kwargs):
            pass

        def bounding_box(self, **_kwargs):
            return {"x": 100, "y": 50, "width": 80, "height": 60}

        def evaluate(self, _script):
            raise AssertionError("positioned click must not use hit-test box")

    clicks = []
    monkeypatch.setattr(
        osinput, "click",
        lambda x, y, settle=0.4: clicks.append((x, y, settle)))

    OSActuator(lambda: None).click(
        Page(), Locator(), position={"x": 277, "y": 21})

    assert clicks == [(189.5, 111.0, 0.6)]


def test_os_locator_retries_until_trusted_click_reaches_target(monkeypatch):
    from showAndTell.player import osops
    from showAndTell.player.osops import OSLocatorProxy
    monkeypatch.setattr(osops.time, "sleep", lambda _s: None)

    class Locator:
        seen = False

        def evaluate(self, script, arg=None):
            if "addEventListener" in script:
                return True
            if "removeEventListener" in script:
                return None  # cleanup
            seen, self.seen = self.seen, False
            return seen  # delivery poll

    class Actuator:
        def __init__(self):
            self.clicks = 0

        def click(self, page, locator, position=None):
            self.clicks += 1
            if self.clicks == 2:
                locator.seen = True

    locator, actuator = Locator(), Actuator()
    OSLocatorProxy(locator, object(), actuator).click()

    assert actuator.clicks == 2


def test_os_locator_uses_one_click_when_delivery_probe_is_unavailable():
    from showAndTell.player.osops import OSLocatorProxy

    class Locator:
        def evaluate(self, _script, _arg=None):
            return None

    class Actuator:
        def __init__(self):
            self.calls = []

        def click(self, page, locator, position=None):
            self.calls.append((page, locator, position))

    page, locator, actuator = object(), Locator(), Actuator()
    OSLocatorProxy(locator, page, actuator).click(position={"x": 1, "y": 2})

    assert actuator.calls == [(page, locator, {"x": 1, "y": 2})]


def test_os_locator_waits_for_late_click_delivery_before_reclicking(
        monkeypatch):
    """The extension's input interceptor delays trusted-click delivery by a
    beat; an immediate probe read then misses it and the retry posts a
    duplicate click that Teach faithfully records as its own step (seen live:
    consecutive 'Click on Close button' steps). The probe must wait for late
    delivery instead of re-clicking."""
    from showAndTell.player import osops
    from showAndTell.player.osops import OSLocatorProxy
    monkeypatch.setattr(osops.time, "sleep", lambda _s: None)

    class Locator:
        polls = 0

        def evaluate(self, script, arg=None):
            if "addEventListener" in script:
                return True
            if "removeEventListener" in script:
                return None  # cleanup
            # Delivery lands late: the first poll misses, a later poll sees it.
            self.polls += 1
            return self.polls >= 3

    class Actuator:
        clicks = 0

        def click(self, page, locator, position=None):
            self.clicks += 1

    locator, actuator = Locator(), Actuator()
    OSLocatorProxy(locator, object(), actuator).click()

    assert actuator.clicks == 1, (
        "late delivery must extend the probe wait, not duplicate the click")


def test_codex_os_fill_waits_for_late_focus_instead_of_reclicking(monkeypatch):
    """Focus can propagate hundreds of ms after a trusted click while the
    recorder interceptor settles; re-clicking on a 200ms deadline emits
    duplicate steps into the taught session."""
    from showAndTell.core import osinput
    from showAndTell.player.osactuator import OSActuator

    class Page:
        def wait_for_timeout(self, _timeout):
            pass

    class Locator:
        value = ""
        focus_checks = 0

        def evaluate(self, script):
            if "document.activeElement" in script:
                # Focus arrives only on the third poll after the click.
                self.focus_checks += 1
                return self.focus_checks >= 3
            if "? {start:" in script:
                return {"start": len(self.value), "end": len(self.value)}
            return True

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = Locator()
    clicks = []
    monkeypatch.setattr(
        osinput, "type_text",
        lambda value: setattr(locator, "value", locator.value + value))
    monkeypatch.setattr(osinput, "key", lambda *a, **k: None)
    actuator = OSActuator(lambda: None)
    monkeypatch.setattr(
        actuator, "click", lambda *_a, **_k: clicks.append(True))

    actuator.fill(Page(), locator, "hello")

    assert locator.value == "hello"
    assert len(clicks) == 1, (
        "late focus must extend the poll, not duplicate the click")
