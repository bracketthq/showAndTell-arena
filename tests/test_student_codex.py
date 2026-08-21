import sys
from types import SimpleNamespace

from showAndTell.students import codex as cx


def test_constants_sane():
    assert cx.DEFAULT_CODEX_CDP == 9333
    assert cx.DONE_PROMPT == "done recording"
    assert "record" in cx.START_PROMPT.lower()


def test_replay_installs_real_os_typing_and_paste_hooks():
    adapter = cx.CodexAdapter()
    adapter.activate = lambda: None
    adapter.actuator = SimpleNamespace(
        type_text=lambda _page, _text: None,
        paste_text=lambda _page, _text: None,
    )

    hooks = adapter.demo_kwargs(SimpleNamespace(mode="replay"))["input_hooks"]

    assert hooks["type_text"] is adapter.actuator.type_text
    assert hooks["paste_text"] is adapter.actuator.paste_text


def test_main_app_pids_matches_full_executable_and_ignores_helpers(monkeypatch):
    ps = """\
  101 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT --remote-debugging-port=9337
  102 /Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Helpers/Codex (Renderer) --type=renderer
  103 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
"""

    class Result:
        stdout = ps

    monkeypatch.setattr(cx.subprocess, "run", lambda *args, **kwargs: Result())

    assert cx._main_app_pids("/Applications/ChatGPT.app") == [101]


def test_main_app_pids_accepts_main_executable_without_args(monkeypatch):
    class Result:
        stdout = "  204 /Applications/Codex.app/Contents/MacOS/Codex\n"

    monkeypatch.setattr(cx.subprocess, "run", lambda *args, **kwargs: Result())

    assert cx._main_app_pids("/Applications/Codex.app") == [204]


def test_activate_managed_chrome_raises_exact_playwright_page(monkeypatch):
    events = []

    class App:
        def activateWithOptions_(self, options):
            events.append(("activate", options))

    class RunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid):
            events.append(("pid", pid))
            return App()

    page = SimpleNamespace(
        bring_to_front=lambda: events.append(("bring_to_front",)))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSApplicationActivateIgnoringOtherApps=77,
        NSRunningApplication=RunningApplication,
    ))
    monkeypatch.setattr(cx.time, "sleep", lambda seconds: events.append(
        ("sleep", seconds)))

    cx._activate_managed_chrome(page, 123)

    assert events == [
        ("pid", 123),
        ("activate", 77),
        ("bring_to_front",),
        ("sleep", 0.5),
    ]


def test_launch_quits_detected_main_app_before_relaunch(monkeypatch):
    events = []
    cdp_checks = iter((False, True))

    monkeypatch.setattr(cx, "_codex_app_path", lambda: "/Applications/ChatGPT.app")
    monkeypatch.setattr(cx.chrome, "wait_for_cdp",
                        lambda *args, **kwargs: next(cdp_checks))
    monkeypatch.setattr(cx, "_main_app_pids", lambda app_path: [101])
    monkeypatch.setattr(cx, "_quit_codex",
                        lambda app_path: events.append(("quit", app_path)))
    monkeypatch.setattr(
        cx.subprocess, "run",
        lambda command, **kwargs: events.append(("run", command)))

    cx.launch_codex(9338, out=lambda message: events.append(("out", message)))

    assert events[0][0] == "out"
    assert events[1] == ("quit", "/Applications/ChatGPT.app")
    assert events[2] == (
        "run",
        ["open", "-na", "/Applications/ChatGPT.app", "--args",
         "--remote-debugging-port=9338"],
    )


def test_codex_page_ignores_overlay_surfaces(monkeypatch):
    monkeypatch.setattr(cx.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cx, "_page_targets", lambda _port: ["app://-/index.html"])

    class Locator:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class Page:
        def __init__(self, url, title, composers=0):
            self.url = url
            self._title = title
            self._composers = composers

        def title(self):
            return self._title

        def locator(self, selector):
            return Locator(self._composers)

    overlay = Page(
        "app://-/avatar-overlay-composition-surface.html?surfaceId=activity-slot-0",
        "Codex Pet Composition Surface")
    main = Page("app://-/index.html", "Codex", composers=1)
    context = type("Context", (), {"pages": [overlay, main]})()
    browser = type("Browser", (), {"contexts": [context]})()
    chromium = type("Chromium", (), {
        "connect_over_cdp": lambda self, url: browser,
    })()
    pw = type("Playwright", (), {"chromium": chromium})()

    assert cx._codex_page(pw, 9333) is main


def test_codex_page_falls_back_to_composer_bearing_codex_page(monkeypatch):
    monkeypatch.setattr(cx.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cx, "_page_targets", lambda _port: ["app://-/x.html"])

    class Page:
        url = "app://-/future-shell.html"

        def title(self):
            return "Codex"

        def locator(self, selector):
            return type("Locator", (), {"count": lambda self: 1})()

    page = Page()
    context = type("Context", (), {"pages": [page]})()
    browser = type("Browser", (), {"contexts": [context]})()
    chromium = type("Chromium", (), {
        "connect_over_cdp": lambda self, url: browser,
    })()
    pw = type("Playwright", (), {"chromium": chromium})()

    assert cx._codex_page(pw, 9333) is page


def test_recording_live_matches_phrasings():
    assert cx._recording_live("Recording is now running.")
    assert cx._recording_live("Recording is started. Please demonstrate…")
    assert cx._recording_live("The recording is running for up to 30 minutes")
    assert cx._recording_live("Recording is on. Please demonstrate the task")
    # seen live 2026-07-17: no is/has, and 'perform' instead of 'demonstrate'
    assert cx._recording_live(
        "Recording started. You have up to 30 minutes—perform your task, "
        "then tell me “done recording.”")
    # the instruction fallback must detect confirmations with no
    # 'recording <state>' phrasing at all
    assert cx._recording_live(
        "You have up to 30 minutes—perform your task, then tell me "
        "“done recording.”")


def test_recording_live_rejects_pre_start_phrasings():
    # pre-start acknowledgments and refusals must NOT read as live
    assert not cx._recording_live("I'll start recording once you approve")
    assert not cx._recording_live("start recording on your screen now?")
    assert not cx._recording_live("I can't start recording on this Mac")
    assert not cx._recording_live("recording only works with permission")
    assert not cx._recording_live(
        "Use Record & Replay to start recording my screen. Just start it; "
        "I'll demonstrate a task, then tell you \"done recording\".")
    # Observed in real runs: the app confirms with "is active" AND "has started".
    assert cx._recording_live('Recording is active. Demonstrate your task, then tell me "done recording."')
    assert cx._recording_live("Recording is in progress.")
    assert cx._recording_live('Recording has started. Demonstrate your task, then tell me "done recording."')
    # Robust fallback: once live, Codex shows the demo instruction regardless of
    # the exact "recording …" phrasing.
    assert cx._recording_live('Demonstrate your task, then tell me "done recording." Maximum recording time: 30 minutes.')
    assert not cx._recording_live("Click Start screen recording to begin")


class _FakeLocator:
    """click() runs a recorded callback, or raises if the control is absent."""

    def __init__(self, on_click=None):
        self._on_click = on_click

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._on_click is not None else 0

    def click(self, timeout=None):
        if self._on_click is None:
            raise RuntimeError("locator not found")
        self._on_click()


class _KB:
    def __init__(self, events):
        self._events = events

    def press(self, k):
        self._events.append(("press", k))


class _ApprovePage:
    def __init__(self, body, events, allow_button=True):
        # allow_button models reality: a permission dialog renders the request
        # text AND an "Allow once" control together; with no dialog, neither exists.
        self._body = body
        self._allow = allow_button
        self.events = events
        self.keyboard = _KB(events)

    def evaluate(self, js):
        return self._body

    def get_by_text(self, text, exact=False):
        return _FakeLocator()  # "Yes, allow for this turn" not on the record dialog

    def get_by_role(self, role, name=None, exact=False):
        if name == "Allow once" and self._allow:
            return _FakeLocator(lambda: self.events.append(("click", "Allow once")))
        return _FakeLocator()


def test_approve_clicks_allow_once_on_record_dialog():
    events = []
    page = _ApprovePage("Allow ChatGPT to record your actions on your Mac?", events)

    assert cx._approve(page) is True
    assert ("click", "Allow once") in events


def test_approve_noop_without_dialog():
    events = []  # no dialog up -> no Allow control present
    page = _ApprovePage("Recording is live for up to 30 minutes.", events, allow_button=False)

    assert cx._approve(page) is False
    assert events == []


def test_approve_grants_regardless_of_request_wording():
    # The request prose varies per permission (record / edit file / run command);
    # _approve must grant based on the Allow control being present, NOT the wording.
    events = []
    page = _ApprovePage(
        "I need permission to update the installed skill and run its structural validation.",
        events)
    assert cx._approve(page) is True
    assert ("click", "Allow once") in events


def test_send_approves_then_types_and_submits():
    events = []

    class Box:
        def wait_for(self, state=None, timeout=None):
            events.append(("wait_for", state))

        def click(self):
            events.append("click")

        def type(self, t):
            events.append(("type", t))

    class Loc:
        def __init__(self):
            self._box = Box()

        @property
        def first(self):
            return self._box

    class Page:
        def __init__(self):
            self.keyboard = _KB(events)

        def evaluate(self, js):
            return ""  # no approval dialog pending

        def locator(self, sel):
            return Loc()

    cx._send(Page(), "hello")
    assert ("wait_for", "visible") in events
    assert ("type", "hello") in events and ("press", "Enter") in events


import re as _re


def _css_matches(compound: str, attrs: dict) -> bool:
    """Evaluate the small CSS subset the sidebar selectors use — chained
    ``[attr]`` / ``[attr="value"]`` / ``:not([attr="value"])`` / ``:visible``
    — against one row's attribute dict (``visible`` keyed like an attribute)."""
    compound = compound.strip()
    if compound.endswith(":visible"):
        if not attrs.get("visible", True):
            return False
        compound = compound[: -len(":visible")]
    for negated, name, value in _re.findall(
            r'(:not\()?\[([\w-]+)(?:="([^"]*)")?\]\)?', compound):
        present = name in attrs and (value == "" or attrs[name] == value)
        if bool(negated) == present:
            return False
    return True


class _SidebarPage:
    """A fake desktop page whose sidebar rows are attribute dicts; the
    ``[data-app-action-sidebar-thread-id]`` locators are evaluated for real,
    everything else answers as an empty locator."""

    url = "app://-/index.html"

    def __init__(self, rows, events):
        self.rows = rows
        self.events = events

    def _matches(self, selector):
        return [row for row in self.rows
                if any(_css_matches(part, row) for part in selector.split(", "))]

    def locator(self, selector):
        page = self
        matches = self._matches(selector)

        class Locator:
            @property
            def first(self):
                return self

            def count(self):
                return len(matches)

            def wait_for(self, state=None, timeout=None):
                page.events.append(("wait", state))
                if not page._matches(selector):
                    raise AssertionError(f"nothing matched {selector!r}")

            def get_attribute(self, name):
                return page._matches(selector)[0].get(name)

            def click(self, force=False):
                page.events.append(("click", matches[0]["data-app-action-sidebar-thread-id"]))
                for row in page.rows:  # the app moves both flags to the clicked row
                    open_ = row is matches[0]
                    row["data-app-action-sidebar-thread-active"] = str(open_).lower()
                    row["data-app-action-sidebar-thread-selected"] = str(open_).lower()
                    if open_:
                        row["aria-current"] = "page"
                    else:
                        row.pop("aria-current", None)

        return Locator()


def _row(thread_id, *, active=None, selected=None, current=False, visible=True):
    row = {"data-app-action-sidebar-thread-id": thread_id, "visible": visible}
    if active is not None:
        row["data-app-action-sidebar-thread-active"] = active
    if selected is not None:
        row["data-app-action-sidebar-thread-selected"] = selected
    if current:
        row["aria-current"] = "page"
    return row


def test_selected_thread_id_reads_the_active_row_not_the_sticky_selected_one():
    """Newer sidebars mark the open chat ``thread-active="true"``/``aria-current``
    and keep ``thread-selected="true"`` on whatever row was last clicked; a
    freshly sent chat is ``selected="false"`` for its whole life."""
    page = _SidebarPage([
        _row("local:old-clicked", active="false", selected="true"),
        _row("local:client-new-thread:abc", active="true", selected="false",
             current=True),
    ], [])
    assert cx._selected_thread_id(page) == "local:client-new-thread:abc"


def test_selected_thread_id_honours_legacy_selected_marker():
    page = _SidebarPage([
        _row("local:other", selected="false"),
        _row("local:recording", selected="true"),
    ], [])
    assert cx._selected_thread_id(page) == "local:recording"


def test_selected_thread_id_reports_when_no_row_is_current():
    page = _SidebarPage([_row("local:home-only", active="false", selected="true")], [])

    class Timeout(Exception):
        pass

    import showAndTell.core.pwerrors as pwerrors
    original = pwerrors.PWTimeout
    pwerrors.PWTimeout = Timeout
    try:
        def wait_for(self, state=None, timeout=None):
            raise Timeout()
        loc = page.locator(cx._current_thread_selector())
        type(loc).wait_for = wait_for  # the only row is not current -> app-side timeout
        page.locator = lambda selector, _loc=loc, _p=page: (
            _loc if selector == cx._current_thread_selector()
            else _SidebarPage.locator(_p, selector))
        try:
            cx._selected_thread_id(page)
        except RuntimeError as exc:
            assert "never marked the new chat as current" in str(exc)
            assert "1 thread rows" in str(exc)
        else:
            raise AssertionError("must fail closed without a current row")
    finally:
        pwerrors.PWTimeout = original


def test_restore_thread_returns_to_recording_chat_after_navigation():
    """The user opened another chat: only ``active``/``aria-current`` moved
    (this build's ``selected`` follows clicks) — restore must click back."""
    events = []
    page = _SidebarPage([
        _row("local:recording", active="false", selected="false"),
        _row("local:other", active="true", selected="true", current=True),
    ], events)

    cx._restore_thread(page, "local:recording")

    assert ("click", "local:recording") in events
    assert cx._selected_thread_id(page) == "local:recording"


def test_restore_thread_clicks_when_recording_row_is_only_sticky_selected():
    """From the new-chat home the recording row keeps ``selected="true"`` but
    ``active="false"`` — that is *not* the open chat; sending there would start
    a new conversation, so restore must click back first."""
    events = []
    page = _SidebarPage([
        _row("local:recording", active="false", selected="true"),
        _row("local:other", active="false", selected="false"),
    ], events)

    cx._restore_thread(page, "local:recording")

    assert ("click", "local:recording") in events
    assert page.rows[0]["data-app-action-sidebar-thread-active"] == "true"


def test_restore_thread_is_a_noop_on_the_open_recording_chat():
    events = []
    page = _SidebarPage([
        _row("local:recording", active="true", selected="false", current=True),
    ], events)
    cx._restore_thread(page, "local:recording")
    assert events == []


def test_restore_thread_legacy_selected_marker_counts_as_open():
    events = []
    page = _SidebarPage([_row("local:recording", selected="true")], events)
    cx._restore_thread(page, "local:recording")
    assert events == []


def test_current_thread_selector_shapes():
    generic = cx._current_thread_selector()
    assert generic.count("[data-app-action-sidebar-thread-id]") == 3
    assert ":visible" not in generic
    specific = cx._current_thread_selector("local:x", visible=True)
    assert specific.count('[data-app-action-sidebar-thread-id="local:x"]') == 3
    assert specific.count(":visible") == 3
    # the legacy marker never wins over an explicit "not active"
    assert (':not([data-app-action-sidebar-thread-active="false"])'
            in generic.split(", ")[-1])


def test_new_task_switches_to_chatgpt_then_opens_projectless_chat():
    events = []

    class Locator:
        def __init__(self, name="", count=1):
            self.name = name
            self._count = count

        @property
        def first(self):
            return self

        def count(self):
            return self._count

        def wait_for(self, state=None, timeout=None):
            events.append(("wait_for", self.name, state, timeout))

        def click(self, timeout=None, **kwargs):
            events.append(("click", self.name, timeout, kwargs))

        def filter(self, has_text=None):
            return Locator(has_text)

        def get_attribute(self, name):
            events.append(("attr", self.name, name))
            return None

    class Page:
        def __init__(self):
            self.chatgpt = Locator(cx._CHATGPT_MODE_BUTTON, count=0)

        def get_by_role(self, role, name=None, exact=False):
            events.append(("role", role, name, exact))
            if name == cx._CHATGPT_MODE_BUTTON:
                return self.chatgpt
            return Locator(name or role)

        def get_by_label(self, name, exact=False):
            events.append(("label", name, exact))
            return Locator(name)

        def locator(self, selector):
            events.append(("locator", selector))
            inactive = {cx._ACTIVE_PROJECT_SELECTOR, cx._CHANGE_PROJECT_SELECTOR}
            return Locator(selector, count=0 if selector in inactive else 1)

        def wait_for_timeout(self, timeout):
            events.append(("wait", timeout))

    cx._new_task(Page())
    assert ("click", cx._CODEX_MODE_BUTTON, 5000, {}) in events
    assert ("click", "ChatGPT", 5000, {}) in events
    assert ("click", "New chat", 5000, {"force": True}) in events
    assert ("locator", cx._NEW_CHAT_HOME_SELECTOR) in events
    assert ("locator", cx._COMPOSER_SELECTOR) in events
    # the "Work" match had no aria-pressed, so it is not the mode toggle
    assert ("click", cx._WORK_MODE_TOGGLE, 5000, {}) not in events
    assert events[-1] == ("wait", 1200)


def _work_toggle_page(events, pressed):
    """A new-chat home whose composer carries the Chat/Work mode toggle."""

    class Locator:
        def __init__(self, name="", count=1, attrs=None):
            self.name = name
            self._count = count
            self.attrs = attrs or {}

        @property
        def first(self):
            return self

        def count(self):
            return self._count

        def wait_for(self, state=None, timeout=None):
            events.append(("wait_for", self.name))

        def click(self, timeout=None, **kwargs):
            events.append(("click", self.name))

        def get_attribute(self, name):
            return self.attrs.get(name)

    class Page:
        def get_by_role(self, role, name=None, exact=False):
            if name == cx._CHATGPT_MODE_BUTTON:
                return Locator(name)
            if name == cx._WORK_MODE_TOGGLE:
                return Locator(name, attrs={"aria-pressed": pressed})
            return Locator(name or role)

        def get_by_label(self, name, exact=False):
            return Locator(name)

        def locator(self, selector):
            inactive = {cx._ACTIVE_PROJECT_SELECTOR, cx._CHANGE_PROJECT_SELECTOR}
            return Locator(selector, count=0 if selector in inactive else 1)

        def wait_for_timeout(self, timeout):
            events.append(("wait", timeout))

    return Page()


def test_new_task_presses_work_toggle_when_home_defaults_to_chat():
    events = []
    cx._new_task(_work_toggle_page(events, pressed="false"))
    assert ("click", cx._WORK_MODE_TOGGLE) in events
    # the projectless proof is checked after the mode switch, not before
    assert events.index(("click", cx._WORK_MODE_TOGGLE)) < events.index(
        ("wait_for", cx._PROJECTLESS_COMPOSER_BUTTON))


def test_new_task_leaves_work_toggle_alone_when_already_pressed():
    events = []
    cx._new_task(_work_toggle_page(events, pressed="true"))
    assert ("click", cx._WORK_MODE_TOGGLE) not in events
    assert ("wait_for", cx._PROJECTLESS_COMPOSER_BUTTON) in events


def test_new_task_fails_closed_when_chat_remains_in_project():
    class Locator:
        def __init__(self, *, active=False):
            self.active = active

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self.active else 0

        def wait_for(self, state=None, timeout=None):
            pass

        def click(self, timeout=None, **kwargs):
            pass

    class Page:
        def get_by_role(self, role, name=None, exact=False):
            return Locator(active=name == cx._CHATGPT_MODE_BUTTON)

        def get_by_label(self, name, exact=False):
            return Locator()

        def locator(self, selector):
            return Locator(active=selector == cx._ACTIVE_PROJECT_SELECTOR)

        def wait_for_timeout(self, timeout):
            pass

    try:
        cx._new_task(Page())
    except RuntimeError as exc:
        assert "projectless chat" in str(exc)
    else:
        raise AssertionError("project-local chat must not be accepted")


def test_selected_thread_id_reads_a_hidden_row(monkeypatch):
    """A collapsed sidebar section hides the selected row without changing
    which chat is selected — identification must not require visibility."""
    waits = []

    class Locator:
        @property
        def first(self):
            return self

        def wait_for(self, state=None, timeout=None):
            waits.append(state)
            assert state == "attached", "identification must not wait for visible"

        def get_attribute(self, name):
            return "local:hidden-row"

    class Page:
        url = "app://-/index.html"

        def locator(self, selector):
            return Locator()

    assert cx._selected_thread_id(Page()) == "local:hidden-row"
    assert waits == ["attached"]


def test_restore_thread_accepts_a_selected_but_hidden_row():
    """Selection is app state, not visibility: no clicks when the recording
    chat is still selected inside a collapsed section."""
    clicks = []

    class Locator:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def wait_for(self, state=None, timeout=None):
            raise AssertionError("no waiting needed for an already-selected chat")

        def click(self, force=False):
            clicks.append(force)

    selectors = []

    class Page:
        def locator(self, selector):
            selectors.append(selector)
            return Locator()

    cx._restore_thread(Page(), "local:recording")
    assert clicks == []
    assert ":visible" not in selectors[0]  # the selected-check must not require it


class _SettingsPage:
    """Fake app page for the plugin-state reader: get_by_* return clickable
    stubs; evaluate answers the probe scripts by recognizable substrings."""

    def __init__(self, toggle, *, settings_rendered=True, body=""):
        self.toggle = toggle
        self.settings_rendered = settings_rendered
        self.body = body
        self.clicks = []

    class _Loc:
        def __init__(self, page, name):
            self.page, self.name = page, name

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self):
            self.page.clicks.append(self.name)

    def get_by_role(self, role, name=None):
        return self._Loc(self, name)

    def get_by_text(self, text):
        return self._Loc(self, text)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        if "Installed" in script:
            return True
        if "record" in script and "switch" in script:
            return self.toggle
        if "Manage plugins" in script:
            return self.settings_rendered
        if "document.body.innerText" in script:
            return self.body
        raise AssertionError(f"unexpected script: {script[:60]}")


def test_record_replay_state_reads_the_toggle():
    assert cx._record_replay_state(_SettingsPage("true")) == "enabled"
    assert cx._record_replay_state(_SettingsPage("false")) == "disabled"


def test_record_replay_state_missing_when_settings_lack_the_row():
    page = _SettingsPage(None, settings_rendered=True)
    assert cx._record_replay_state(page) == "missing"
    assert page.clicks[-1] == "New chat"  # the app view is restored


def test_record_replay_state_unreadable_when_settings_never_render():
    assert cx._record_replay_state(
        _SettingsPage(None, settings_rendered=False)) is None


def test_verify_codex_ready_passes_when_signed_in_and_enabled():
    page = _SettingsPage("true", body="ChatGPT New chat Projects")
    warnings = []
    cx._verify_codex_ready(page, out=warnings.append)
    assert warnings == []


def test_verify_codex_ready_gates_on_a_signed_out_app():
    import pytest

    page = _SettingsPage("true", body="Welcome back Log in Sign up")
    with pytest.raises(SystemExit, match="not signed in"):
        cx._verify_codex_ready(page, out=lambda _m: None)


def test_verify_codex_ready_gates_on_a_missing_plugin():
    import pytest

    page = _SettingsPage(None, body="ChatGPT New chat")
    with pytest.raises(SystemExit, match="not installed for this account"):
        cx._verify_codex_ready(page, out=lambda _m: None)


def test_verify_codex_ready_fails_open_when_settings_are_unreadable():
    page = _SettingsPage(None, settings_rendered=False, body="ChatGPT")
    warnings = []
    cx._verify_codex_ready(page, out=warnings.append)
    assert any("could not verify" in w for w in warnings)
