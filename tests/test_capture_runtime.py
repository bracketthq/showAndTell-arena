"""Compilation of managed-Chrome gestures into a replayable demonstration."""
from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest
from types import SimpleNamespace

from showAndTell.player import replay
from showAndTell.applications.browser.runtime import (
    auto_login,
    runtime_application_url,
    wait_ready,
)
from showAndTell.core.pwerrors import PlaywrightError
from showAndTell.demonstration.events import align_narration
from showAndTell.demonstration.compiler import render_demonstrate
from showAndTell.capture.runtime import ManagedCapture, _START_HINTS, author_draft


SURFACES = [
    {"id": "gitlab", "application": "gitlab", "label": "GitLab",
     "url": "http://127.0.0.1:8023/explore", "fixture": "gitlab"},
]


def test_managed_capture_avoids_an_occupied_cdp_port():
    from showAndTell.capture.runtime import _available_cdp_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        blocked = occupied.getsockname()[1]
        selected = _available_cdp_port(blocked)

    assert selected != blocked
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", selected))


@pytest.mark.parametrize(("surface_count", "expected"), [
    (0, 90.0),
    (1, 90.0),
    (3, 210.0),
])
def test_managed_capture_startup_timeout_scales_with_selected_apps(
    tmp_path, surface_count, expected
):
    capture = ManagedCapture(tmp_path, [{} for _ in range(surface_count)])
    assert capture._startup_timeout() == expected


def test_front_chrome_tab_reads_active_front_window(monkeypatch):
    from showAndTell.capture import runtime as managed_capture

    monkeypatch.setattr(managed_capture.sys, "platform", "darwin")
    monkeypatch.setattr(
        managed_capture.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="http://fixture.test/orders\nOrders\n",
            stderr="",
        ),
    )

    assert managed_capture._front_chrome_tab() == (
        "http://fixture.test/orders", "Orders")


def test_front_chrome_tab_is_disabled_off_macos(monkeypatch):
    from showAndTell.capture import runtime as managed_capture

    monkeypatch.setattr(managed_capture.sys, "platform", "linux")
    monkeypatch.setattr(
        managed_capture.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("osascript must not run off macOS")),
    )

    assert managed_capture._front_chrome_tab() is None


def _events():
    return [
        {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/users/sign_in",
         "at_ms": 0, "description": "open GitLab"},
        {"type": "fill", "page": "page", "selectors": ["#user_login", "[name=\"user[login]\"]"],
         "selector": "#user_login", "value": "captured@example.test", "value_source": "email",
         "frame_url": None, "target": {"name": "Username"}, "at_ms": 500},
        {"type": "fill", "page": "page", "selectors": ["#user_password"],
         "selector": "#user_password", "value": "<password>", "value_source": "password",
         "frame_url": None, "target": {"name": "Password"}, "at_ms": 900},
        {"type": "click", "page": "page", "selectors": ["button[type=\"submit\"]"],
         "selector": "button[type=\"submit\"]", "frame_url": None,
         "target": {"name": "Sign in"}, "at_ms": 1200},
    ]


def test_render_demonstrate_uses_selector_fallbacks_and_credential_sources():
    source = render_demonstrate(_events(), SURFACES)
    ast.parse(source)
    assert "app_url.rstrip('/') + '/users/sign_in'" in source
    assert "creds.get('email'" in source
    assert "creds.get('password')" in source
    assert "#user_login" in source and 'button[type="submit"]' in source


def test_render_demonstrate_preserves_press_modifiers_and_audited_override():
    event = {
        "type": "press", "page": "page", "at_ms": 100,
        "selector": "#comment", "selectors": ["#comment"],
        "target": {"tag": "div", "name": "draft"},
        "key": "Enter", "modifiers": [], "replay_modifiers": ["Meta"],
    }

    source = render_demonstrate([event], SURFACES)

    assert "target.press('Meta+Enter')" in source


def test_render_demonstrate_replays_omitted_wheel_before_canvas_click():
    event = {
        "type": "click", "page": "page", "at_ms": 100,
        "selector": "#canvas", "selectors": ["#canvas"],
        "target": {"tag": "canvas", "name": ""},
        "position": {"x": 177, "y": 28},
        "replay_wheel_delta_y": -600,
        "replay_wheel_steps": 20,
    }

    source = render_demonstrate([event], SURFACES)

    assert "target.hover(position={'x': 177, 'y': 28})" in source
    assert "for _ in range(20):" in source
    assert "    current.mouse.wheel(0, -600)" in source
    assert "    current.wait_for_timeout(40)" in source
    assert "current.wait_for_timeout(300)" in source
    assert "target.click(position={'x': 177, 'y': 28}, force=True)" in source


def test_pointer_drag_codegen_does_not_require_a_destination_locator():
    event = {
        "type": "drag", "page": "page", "at_ms": 100,
        "selector": "#handle", "selectors": ["#handle"],
        "target": {"tag": "span", "selectors": ["#handle"]},
        "destination": {"tag": "div", "selectors": ["#incidental-hit"]},
        "source_position": {"x": 4.6, "y": 10},
        "target_position": {"x": 20, "y": 10},
        "path": [{"x": 4.6, "y": 10}, {"x": 80, "y": 10}],
        "drag_mode": "pointer",
    }

    source = render_demonstrate([event], SURFACES)

    assert "destination = None" in source
    assert "#incidental-hit" not in source
    assert "drag_mode='pointer'" in source


def test_pointer_drag_replay_releases_at_captured_final_path_point():
    calls = []

    class Mouse:
        def down(self):
            calls.append(("down",))

        def move(self, x, y):
            calls.append(("move", x, y))

        def up(self):
            calls.append(("up",))

    class Source:
        def scroll_into_view_if_needed(self):
            calls.append(("scroll",))

        def bounding_box(self):
            return {"x": 100, "y": 200, "width": 5, "height": 30}

        def hover(self, *, position):
            calls.append(("hover", position))

    page = SimpleNamespace(mouse=Mouse())
    replay.drag(
        page, Source(), None, {"x": 4.6, "y": 15}, None,
        [{"x": 4.6, "y": 15}, {"x": 40, "y": 15},
         {"x": 80, "y": 15}],
        drag_mode="pointer",
    )

    assert calls == [
        ("scroll",),
        ("hover", {"x": 4.5, "y": 15.0}),
        ("move", 104.5, 215.0),
        ("down",),
        ("move", 139.9, 215.0),
        ("move", 179.9, 215.0),
        ("up",),
    ]


def test_pointer_drag_replay_measures_after_scroll_and_translates_for_width_drift():
    calls = []

    class Mouse:
        def move(self, x, y):
            calls.append(("move", x, y))

        def down(self):
            calls.append(("down",))

        def up(self):
            calls.append(("up",))

    class Source:
        scrolled = False

        def scroll_into_view_if_needed(self):
            self.scrolled = True

        def bounding_box(self):
            # The stale pre-scroll location must never become the path origin.
            return ({"x": 100, "y": 200, "width": 4, "height": 30}
                    if self.scrolled else
                    {"x": 900, "y": 700, "width": 5, "height": 30})

        def hover(self, *, position):
            calls.append(("hover", position))

    replay.drag(
        SimpleNamespace(mouse=Mouse()), Source(), None,
        {"x": 4.6, "y": 15}, None,
        [{"x": 4.6, "y": 15}, {"x": 50, "y": 15}],
        drag_mode="pointer",
    )

    # Start clamps to 3.5 in today's 4px handle. The 45.4px demonstrated
    # displacement remains 45.4px rather than snapping back to x=50 absolute.
    assert calls[0] == ("hover", {"x": 3.5, "y": 15.0})
    assert calls[1] == ("move", 103.5, 215.0)
    assert calls[2] == ("down",)
    assert calls[3] == ("move", 148.9, 215.0)
    assert calls[4] == ("up",)


def test_native_drag_replay_clamps_both_positions_inside_current_boxes():
    calls = []

    class Locator:
        def __init__(self, box):
            self.box = box

        def bounding_box(self):
            return self.box

        def drag_to(self, destination, **kwargs):
            calls.append((destination, kwargs))

    source = Locator({"x": 10, "y": 20, "width": 5, "height": 30})
    destination = Locator({"x": 100, "y": 20, "width": 4, "height": 20})

    replay.drag(
        SimpleNamespace(), source, destination,
        {"x": 5, "y": 30}, {"x": -2, "y": 25}, [],
        drag_mode="native",
    )

    assert calls == [(destination, {
        "source_position": {"x": 4.5, "y": 29.5},
        "target_position": {"x": 0.5, "y": 19.5},
    })]


def _multi_click(**extra):
    return {"type": "click", "page": "page", "at_ms": 0, "frame_url": None,
            "selector": "#open", "selectors": ["#open"],
            "target": {"tag": "button", "role": "button", "name": "Open"},
            **extra}


def test_rendered_multi_click_emits_semantic_click_count():
    """Two .click() calls each dispatch clickCount=1 and never regenerate the
    dblclick the operator's application reacted to; the consolidated action
    must compile to Playwright's semantic multi-click."""
    source = render_demonstrate([_multi_click(click_count=2)], SURFACES)
    ast.parse(source)
    assert "target.click(click_count=2)" in source


def test_rendered_positioned_multi_click_composes_position_force_and_count():
    event = _multi_click(click_count=2, position={"x": 552, "y": 409},
                         target={"tag": "canvas", "role": "canvas", "name": ""})
    source = render_demonstrate([event], SURFACES)
    assert ("target.click(position={'x': 552, 'y': 409}, force=True, "
            "click_count=2)") in source


def test_rendered_field_multi_click_keeps_the_forced_click():
    event = _multi_click(click_count=2,
                         target={"tag": "input", "role": "textbox", "name": ""})
    source = render_demonstrate([event], SURFACES)
    assert "target.click(force=True, click_count=2)" in source


def test_rendered_checkbox_double_click_stays_set_checked():
    """Boolean controls reproduce recorded STATE, never gesture mechanics —
    the consolidated final state drives set_checked and the count is ignored."""
    event = _multi_click(
        click_count=2, checked_after=False,
        target={"tag": "input", "role": "checkbox", "name": "Has Variants",
                "input_type": "checkbox"})
    source = render_demonstrate([event], SURFACES)
    assert "_replay.set_checked(_REPLAY, target, False)" in source
    assert "click_count" not in source


def test_rendered_independent_repeated_clicks_stay_two_calls():
    """Consolidation is evidence-driven; unconsolidated repeats never merge."""
    source = render_demonstrate(
        [_multi_click(), _multi_click(at_ms=400)], SURFACES)
    assert source.count("target.click()") == 2
    assert "click_count" not in source


def test_primary_navigation_uses_runtime_when_captured_surface_port_drifted():
    surfaces = [{
        "id": "erpnext",
        "application": "erpnext",
        "url": "http://capture.test:8180/login",
        "credentials": {},
    }]
    events = [{
        "type": "goto",
        "page": "page",
        "url": "http://capture.test:8080/desk/item/view/list?disabled=0",
    }]

    source = render_demonstrate(events, surfaces)

    assert (
        "pages['page'].goto(app_url.rstrip('/') + "
        "'/desk/item/view/list?disabled=0'"
    ) in source
    assert "pages['page'].goto('http://capture.test:8080" not in source


def test_rendered_demonstrate_executes_the_captured_actions():
    namespace = {}
    exec(render_demonstrate(_events(), SURFACES), namespace)
    calls = []

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def count(self):
            return 1

        def is_visible(self):
            return True

        def fill(self, value):
            calls.append(("fill", self.selector, value))

        def click(self, **kwargs):
            calls.append(("click", self.selector, kwargs))

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.frames = []

        def goto(self, url, **kwargs):
            self.url = url
            calls.append(("goto", url, kwargs))

        def bring_to_front(self):
            calls.append(("front", self.url))

        def locator(self, selector):
            return Locator(selector)

    steps = []
    namespace["demonstrate"](
        Page(), "http://fixture.test", {"email": "operator", "password": "secret"},
        lambda key, _page, description: steps.append((key, description)),
    )
    assert calls == [
        ("goto", "http://fixture.test/users/sign_in",
         {"wait_until": "domcontentloaded"}),
        ("front", "http://fixture.test/users/sign_in"),
        ("fill", "#user_login", "operator"),
        ("fill", "#user_password", "secret"),
        ("click", 'button[type="submit"]', {}),
    ]
    assert [key for key, _ in steps] == ["action-001", "action-002", "action-003"]


def test_rendered_fill_descends_from_a_stable_wrapper_to_its_input():
    """ERPNext puts data-name on a field wrapper, not the fillable control."""
    event = {
        "type": "fill", "page": "page", "at_ms": 0,
        "selector": '[data-name="Item Code"]',
        "selectors": ['[data-name="Item Code"]'],
        "value": "NW-0050", "value_source": "literal", "frame_url": None,
        "target": {"tag": "input", "role": "textbox", "name": ""},
    }
    namespace = {}
    exec(render_demonstrate([event], SURFACES), namespace)
    calls = []

    class Input:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def evaluate(self, _expression):
            return "input"

        def fill(self, value):
            calls.append(("fill", value))

    class Wrapper:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def evaluate(self, _expression):
            return "div"

        def locator(self, selector):
            assert selector == "input"
            return Input()

    class Page:
        url = "http://127.0.0.1:8023/explore"
        frames = []

        def bring_to_front(self):
            pass

        def locator(self, selector):
            assert selector == '[data-name="Item Code"]'
            return Wrapper()

    namespace["demonstrate"](
        Page(), "http://127.0.0.1:8023", {}, lambda *_args: None)

    assert calls == [("fill", "NW-0050")]


def test_rendered_recipient_fill_uses_the_visible_token_input():
    """Roundcube's recorded textarea is an off-screen Select2 source.

    The visible sibling input owns the caret and the Enter key that turns an
    address into a recipient chip, so both replay actions must resolve there.
    """
    event = {
        "type": "fill", "page": "page", "at_ms": 0,
        "selector": "#_to", "selectors": ["#_to"],
        "value": "sales-desk@showAndTell.test", "value_source": "literal",
        "frame_url": None,
        "target": {"tag": "textarea", "role": "combobox", "name": "_to"},
    }
    namespace = {}
    exec(render_demonstrate([event], SURFACES), namespace)
    calls = []

    class Proxy:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def fill(self, value):
            calls.append(("fill", value))

    class TokenSource:
        def count(self):
            return 1

        def is_visible(self):
            # Playwright considers opacity:0 elements visible for actionability.
            return True

        def evaluate(self, _expression):
            return "textarea"

        def get_attribute(self, name):
            return "true" if name == "data-recipient-input" else None

        def locator(self, selector):
            assert selector == (
                'xpath=following-sibling::ul[contains(@class, "recipient-input")]//input')
            return Proxy()

    class Page:
        url = "http://127.0.0.1:8082/?_task=mail&_action=compose"
        frames = []

        def bring_to_front(self):
            pass

        def locator(self, selector):
            assert selector == "#_to"
            return TokenSource()

    namespace["demonstrate"](
        Page(), "http://127.0.0.1:8082", {}, lambda *_args: None)

    assert calls == [("fill", "sales-desk@showAndTell.test")]


def test_roundcube_send_waits_for_server_acknowledgement():
    surfaces = [{
        "id": "mail",
        "application": "roundcube",
        "label": "Mail",
        "url": "http://127.0.0.1:8082/",
        "credentials": {},
    }]
    event = {
        "type": "click",
        "page": "page",
        "at_ms": 0,
        "selector": "button.send",
        "selectors": ["role=button[name=\"Send\"]", "button.send"],
        "frame_url": None,
        "target": {"tag": "button", "role": "button", "name": "Send"},
    }

    source = render_demonstrate([event], surfaces)

    click = source.index("target.click()")
    wait = source.index(
        "_replay.wait_for_application_completion(current, 'roundcube', 'send')"
    )
    assert click < wait


def test_replay_completion_ignores_a_page_without_a_declared_surface():
    event = {
        "type": "click",
        "page": "page2",
        "at_ms": 0,
        "selector": "button.send",
        "selectors": ["button.send"],
        "frame_url": None,
        "target": {"tag": "button", "role": "button", "name": "Send"},
    }

    source = render_demonstrate([event], SURFACES)

    assert "target.click()" in source
    assert "wait_for_application_completion" not in source


def test_roundcube_send_does_not_treat_a_page_read_error_as_success():
    from showAndTell.applications.roundcube import browser

    calls = []

    class MessageList:
        def wait_for(self, *, state, timeout):
            calls.append(("list", state, timeout))

    class Page:
        reads = 0

        @property
        def url(self):
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError("page replaced during navigation")
            if self.reads == 2:
                return "http://mail.test/?_task=mail&_action=compose"
            return "http://mail.test/?_task=mail&_mbox=INBOX"

        def locator(self, selector):
            assert selector == "#messagelist"
            return MessageList()

        def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    browser.wait_for_replay_completion(Page(), "send")

    assert calls == [
        ("wait", 100),
        ("wait", 100),
        ("list", "attached", 250),
    ]


def test_roundcube_send_does_not_accept_an_unrelated_navigation(monkeypatch):
    from showAndTell.applications.roundcube import browser

    times = iter((0.0, 31.0))
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(times))

    class Page:
        url = "http://mail.test/?_task=login"

        def wait_for_timeout(self, _milliseconds):
            raise AssertionError("the expired deadline should fail immediately")

    with pytest.raises(RuntimeError, match="did not confirm message delivery"):
        browser.wait_for_replay_completion(Page(), "send")


def test_rendered_click_promotes_svg_path_to_clickable_parent():
    """Calendar arrows expose a captured <path>, but their <svg> is hit-tested."""
    event = {
        "type": "click", "page": "page", "at_ms": 0,
        "selectors": ["nav svg > path"], "frame_url": None,
        "target": {"tag": "path", "role": "path", "name": ""},
        "description": "click next month",
    }
    namespace = {}
    exec(render_demonstrate([event], SURFACES), namespace)
    calls = []

    class Locator:
        def __init__(self, tag):
            self.tag = tag

        def count(self):
            return 1

        def is_visible(self):
            return True

        def evaluate(self, _expression):
            return self.tag

        def locator(self, selector):
            assert self.tag == "path" and selector == "xpath=.."
            return Locator("svg")

        def click(self, **_kwargs):
            calls.append(self.tag)

    class Page:
        url = "http://127.0.0.1:8023/explore"
        frames = []

        def bring_to_front(self):
            pass

        def locator(self, selector):
            assert selector == "nav svg > path"
            return Locator("path")

    namespace["demonstrate"](
        Page(), "http://127.0.0.1:8023", {}, lambda *_args: None)

    assert calls == ["svg"]


def test_rendered_click_expands_a_collapsed_hierarchical_menu():
    """A clean ERPNext sidebar can collapse a parent open during capture."""
    event = {
        "type": "click", "page": "page", "at_ms": 0,
        "selectors": [
            'role=link[name="Stock Projected Qty"]',
            '[data-id="Reports"] [data-id="Stock Projected Qty"] a',
        ],
        "frame_url": None,
        "target": {"tag": "a", "role": "link", "name": "Stock Projected Qty"},
        "description": "click Stock Projected Qty",
    }
    namespace = {}
    exec(render_demonstrate([event], SURFACES), namespace)
    state = {"expanded": False, "calls": []}

    class Locator:
        def __init__(self, kind):
            self.kind = kind

        def count(self):
            return 1 if self.kind in {"parent", "child"} else 0

        def is_visible(self):
            return self.kind == "parent" or (
                self.kind == "child" and state["expanded"])

        def click(self, **_kwargs):
            state["calls"].append(self.kind)
            if self.kind == "parent":
                state["expanded"] = True

    class Page:
        url = "http://127.0.0.1:8023/explore"
        frames = []

        def bring_to_front(self):
            pass

        def locator(self, selector):
            if selector == '[data-id="Reports"] [data-id="Stock Projected Qty"] a':
                return Locator("child")
            return Locator("missing")

        def get_by_role(self, *_args, **_kwargs):
            return Locator("child")

        def get_by_text(self, text, *, exact):
            assert exact is True
            return Locator("parent" if text == "Reports" else "child")

    namespace["demonstrate"](
        Page(), "http://127.0.0.1:8023", {}, lambda *_args: None)

    assert state["calls"] == ["parent", "child"]


def test_rendered_demonstrate_brings_the_acting_page_to_front_on_switch():
    """Multi-app replay must keep the acted-on tab visible.

    Opening the second surface steals focus at t=0, and CDP happily drives a
    background tab — so without explicit activation the human (and the product
    recording the screen) watches the wrong application for minutes.
    """
    surfaces = [SURFACES[0], {
        "id": "onlyoffice", "application": "onlyoffice",
        "url": "http://127.0.0.1:8081/onlyoffice/editor/task-setup",
        "credentials": {},
    }]
    events = [
        {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/explore",
         "at_ms": 0},
        {"type": "goto", "page": "page2",
         "url": "http://127.0.0.1:8081/onlyoffice/editor/task-setup", "at_ms": 0},
        {"type": "click", "page": "page", "selectors": ["#a"], "selector": "#a",
         "frame_url": None, "target": {"name": "A"}, "at_ms": 100},
        {"type": "click", "page": "page", "selectors": ["#b"], "selector": "#b",
         "frame_url": None, "target": {"name": "B"}, "at_ms": 200},
        {"type": "click", "page": "page2", "selectors": ["#c"], "selector": "#c",
         "frame_url": None, "target": {"name": "C"}, "at_ms": 300},
        {"type": "click", "page": "page", "selectors": ["#d"], "selector": "#d",
         "frame_url": None, "target": {"name": "D"}, "at_ms": 400},
    ]
    source = render_demonstrate(events, surfaces)
    ast.parse(source)
    lines = [line.strip() for line in source.splitlines()]
    fronts = [line for line in lines if line.endswith(".bring_to_front()")]
    # First action, then each page switch — never between same-page actions.
    assert fronts == [
        "pages['page'].bring_to_front()",
        "pages['page2'].bring_to_front()",
        "pages['page'].bring_to_front()",
    ]
    assert source.index("pages['page2'].bring_to_front()") > source.index("#b")
    assert source.index("pages['page2'].bring_to_front()") < source.index("#c")


def test_rendered_demonstrate_preserves_passive_switch_and_drag_path():
    surfaces = [SURFACES[0], {
        "id": "orders", "application": "orders",
        "url": "http://127.0.0.1:8088/orders", "credentials": {},
    }]
    events = [
        {"type": "goto", "page": "page", "url": SURFACES[0]["url"],
         "at_ms": 0},
        {"type": "goto", "page": "page2", "url": surfaces[1]["url"],
         "at_ms": 0},
        {"type": "tab_switch", "page": "page2", "title": "Orders",
         "description": "switch to Orders", "at_ms": 100},
        {"type": "drag", "page": "page2", "selector": "#card",
         "selectors": ["#card"], "frame_url": None,
         "target": {"name": "Order 12", "selectors": ["#card"]},
         "destination": {"name": "Approved", "selectors": ["#approved"]},
         "source_position": {"x": 10, "y": 10},
         "target_position": {"x": 40, "y": 20},
         "path": [{"x": 10, "y": 10}, {"x": 180, "y": 20}],
         "description": "drag Order 12 to Approved", "at_ms": 200},
        {"type": "tab_switch", "page": "page", "title": "GitLab",
         "description": "switch to GitLab", "at_ms": 300},
    ]

    source = render_demonstrate(events, surfaces)
    ast.parse(source)

    assert source.count(".bring_to_front()") == 2
    assert source.index("pages['page2'].bring_to_front()") < source.index(
        "drag Order 12 to Approved")
    assert "_replay.drag(current, target, destination" in source
    assert "on_step('action-003', current, 'switch to GitLab')" in source


def test_rendered_tab_switch_gates_on_the_restored_applications_readiness():
    """Foregrounding a tab is a navigation-shaped moment and gates like one.

    A background tab's timers are throttled, so the view the operator saw can
    still be building itself when the tab comes forward; the application's own
    readiness hook — the same one every goto runs — must answer before the
    next gesture fires.
    """
    surfaces = [SURFACES[0], {
        "id": "roundcube", "application": "roundcube",
        "url": "http://127.0.0.1:8082/", "credentials": {},
    }]
    source = render_demonstrate([{
        "type": "tab_switch", "page": "page2",
        "url": "http://127.0.0.1:8082/?_task=mail&_action=compose",
        "description": "switch to compose", "at_ms": 100,
    }], surfaces)

    gate = "_replay.wait_ready(pages['page2'], 'roundcube')"
    assert gate in source
    assert source.index("pages['page2'].bring_to_front()") < source.index(gate)


def test_rendered_initial_tab_switch_emits_no_readiness_gate():
    """The initial switch only names which tab starts in front; the goto that
    created each page already gated it."""
    source = render_demonstrate([{
        "type": "tab_switch", "page": "page", "initial": True,
        "url": SURFACES[0]["url"], "at_ms": 0,
    }], SURFACES)

    assert "wait_ready" not in source


def test_align_narration_attaches_voice_keys_to_nearest_actions():
    events = align_narration(_events(), [
        {"at_ms": 450, "text": "Enter the username."},
        {"at_ms": 1180, "text": "Sign in."},
    ])
    assert events[1]["narration_keys"] == ["voice-001"]
    assert events[3]["narration_keys"] == ["voice-002"]


def test_initial_foreground_tab_is_context_not_a_narrated_action():
    events = align_narration([
        {"type": "goto", "page": "page", "url": SURFACES[0]["url"],
         "at_ms": 0},
        {"type": "tab_switch", "page": "page", "initial": True,
         "description": "start on GitLab", "at_ms": 0},
        {"type": "tab_switch", "page": "page2",
         "description": "switch to Orders", "at_ms": 500},
    ], [{"at_ms": 25, "text": "Now I switch to Orders."}])

    assert "narration_keys" not in events[1]
    assert events[2]["narration_keys"] == ["voice-001"]


def test_author_draft_writes_complete_replay_scaffold(tmp_path):
    metadata = {
        "slug": "captured-gitlab", "title": "Captured GitLab",
        "summary": "Sign in and open an issue.", "fixture": "gitlab",
        "applications": ["gitlab"], "primary_application": "gitlab",
        "surfaces": SURFACES,
    }
    events = author_draft(tmp_path, metadata, {"events": _events()}, [
        {"at_ms": 450, "text": "Enter the username."},
    ])
    assert len(events) == 4
    ast.parse((tmp_path / "demonstrate.py").read_text())
    assert (tmp_path / "task.toml").is_file()
    assert json.loads((tmp_path / "demo/seed.json").read_text())["_capture_note"]
    assert (tmp_path / "capture_bundle/traces.js").is_file()
    narration = [json.loads(line) for line in
                 (tmp_path / "demo/narration_script.jsonl").read_text().splitlines()]
    assert narration[0]["key"] == "voice-001"
    assert [row["key"] for row in narration] == [
        "voice-001", "action-002", "action-003",
    ]


def test_author_draft_trace_preserves_press_modifiers(tmp_path):
    metadata = {
        "slug": "captured-shortcut", "title": "Captured shortcut",
        "summary": "Submit a comment.", "fixture": "gitlab",
        "applications": ["gitlab"], "primary_application": "gitlab",
        "surfaces": SURFACES,
    }
    event = {
        "type": "press", "page": "page", "at_ms": 100,
        "selector": "#comment", "selectors": ["#comment"],
        "target": {"tag": "div", "name": "draft"},
        "key": "Enter", "modifiers": ["Meta"],
    }

    author_draft(tmp_path, metadata, {"events": [event]}, [])

    trace = (tmp_path / "capture_bundle/traces.js").read_text()
    assert 'await page.press("#comment", "Meta+Enter")' in trace


def test_author_draft_saves_exported_seed_and_setup_replay_fallback(tmp_path):
    setup = [
        {"type": "goto", "page": "page",
         "url": "http://127.0.0.1:8023/admin/seed", "at_ms": 0},
        {"type": "fill", "page": "page", "selectors": ["#title"],
         "selector": "#title", "value": "Seed row", "value_source": "literal",
         "frame_url": None, "at_ms": 200},
        {"type": "click", "page": "page", "selectors": ["#save"],
         "selector": "#save", "frame_url": None, "at_ms": 300},
    ]
    metadata = {
        "slug": "captured-seed", "title": "Captured seed", "summary": "Seed it.",
        "fixture": "gitlab", "applications": ["gitlab"],
        "primary_application": "gitlab", "surfaces": SURFACES,
    }
    seed = {"gitlab": {"projects": [{"name": "Seed row"}]}}
    author_draft(
        tmp_path, metadata,
        {"events": _events(), "setup_events": setup}, [],
        seed=seed, replay_setup=True,
    )
    assert json.loads((tmp_path / "demo/seed.json").read_text()) == seed
    saved_setup = [json.loads(line) for line in
                   (tmp_path / "demo/seed_events.jsonl").read_text().splitlines()]
    assert saved_setup == setup
    source = (tmp_path / "demonstrate.py").read_text()
    ast.parse(source)
    assert "def seed(" in source and "def demonstrate(" in source
    assert source.index("#title") < source.index("#user_login")


def test_auto_login_routes_to_native_adapter(monkeypatch):
    calls = []
    adapter = SimpleNamespace(login=lambda page, url, credentials:
                              calls.append((page, url, credentials)))
    registry = SimpleNamespace(
        names=lambda: ("gitlab", "kiwix"),
        has_browser=lambda name: name == "gitlab",
        browser=lambda _name: adapter,
    )
    monkeypatch.setattr(
        "showAndTell.applications.registry.default_registry", lambda: registry)
    surface = {
        "id": "gitlab", "url": "http://gitlab.test/explore",
        "credentials": {"email": "author", "password": "secret"},
    }
    page = object()
    assert auto_login(page, surface) is True
    assert calls == [(page, "http://gitlab.test", surface["credentials"])]
    assert auto_login(page, {"id": "kiwix", "url": "http://wiki.test",
                             "credentials": {}}) is False


def test_replay_waits_for_the_application_before_acting_on_it():
    """A recorded timestamp says nothing about how long an application needs.

    A canvas editor has its controls in the DOM seconds before they are wired
    up: a gesture dispatched at its recorded moment resolves, applies to
    nothing, and is lost with no error — so the navigation is gone while the
    typing that followed it still happens, in the wrong place.
    """
    source = render_demonstrate(_events(), SURFACES)
    ast.parse(source)
    assert "_replay.wait_ready(" in source
    assert "_replay.wait_ready(pages['page'], 'gitlab')" in source
    # after the navigation that starts the surface, before the first gesture
    assert (source.index("pages['page'].goto(")
            < source.index("_replay.wait_ready(pages['page'], 'gitlab')")
            < source.index("target.fill("))


def test_seed_stage_also_waits_for_the_application_it_navigates():
    setup = [{"type": "goto", "page": "page",
              "url": "http://127.0.0.1:8023/explore", "at_ms": 0}]
    source = render_demonstrate(_events(), SURFACES, setup)
    ast.parse(source)
    seed, demonstrate = source.index("def seed("), source.index("def demonstrate(")
    assert "_replay.wait_ready(" in source[seed:demonstrate]


def test_wait_ready_asks_the_application_browser_plane(monkeypatch):
    seen = []
    adapter = SimpleNamespace(ready=lambda page: seen.append(page))
    registry = SimpleNamespace(
        names=lambda: ("onlyoffice", "kiwix"),
        has_browser=lambda name: name == "onlyoffice",
        browser=lambda _name: adapter,
    )
    monkeypatch.setattr(
        "showAndTell.applications.registry.default_registry", lambda: registry)
    page = object()
    assert wait_ready(page, "onlyoffice") is True
    assert seen == [page]
    # An application that declares no readiness gate is already ready, and an
    # unknown one must never break a replay.
    assert wait_ready(page, "kiwix") is False
    assert wait_ready(page, "nosuchapp") is False


def test_rendered_setup_replays_automatic_login_before_seed_navigation():
    setup = [
        {"type": "login", "page": "page", "surface_id": "gitlab"},
        {"type": "goto", "page": "page",
         "url": "http://127.0.0.1:8023/explore"},
    ]
    source = render_demonstrate([], SURFACES, setup)
    ast.parse(source)
    assert "auto_login as _auto_login" in source
    assert "_CAPTURED_SURFACES['gitlab']" in source
    assert source.index("    _auto_login(") < source.index("    pages['page'].goto(")
    assert source.index("def seed(") < source.index("def demonstrate(")


def test_demonstrated_login_is_visible_without_disabling_seed_authentication():
    surfaces = [{
        "id": "erpnext", "application": "erpnext",
        "url": "http://capture.test:8080/login",
        "credentials": {"email": "Administrator", "password": "captured"},
        "login_replay": "demonstrated",
    }]
    setup = [{"type": "login", "page": "page", "surface_id": "erpnext"}]
    events = [
        {"type": "goto", "page": "page",
         "url": "http://capture.test:8280/login?redirect-to=%2Fdesk%2Fjob-applicant"},
        {"type": "fill", "page": "page", "selector": "#login_email",
         "selectors": ["#login_email"], "target": {"tag": "input"},
         "value": "Administrator", "value_source": "email"},
        {"type": "fill", "page": "page", "selector": "#login_password",
         "selectors": ["#login_password"], "target": {"tag": "input"},
         "value": "<password>", "value_source": "password"},
        {"type": "click", "page": "page", "selector": "button",
         "selectors": ["button"],
         "target": {"tag": "button", "role": "button", "name": "Continue"},
         "url": "http://capture.test:8280/login?redirect-to=%2Fdesk%2Fjob-applicant",
         "replay_wait_for_response_path": "/api/method/login",
         "replay_post_login_path": "/desk/job-applicant"},
    ]

    source = render_demonstrate(events, surfaces, setup)
    ast.parse(source)
    seed, demonstrate = source.split("def demonstrate(", 1)

    assert "_auto_login(pages['page']" in seed
    assert "_auto_login(pages['page']" not in demonstrate
    assert "app_url.rstrip('/') + '/login?redirect-to=%2Fdesk%2Fjob-applicant'" in demonstrate
    assert "creds.get('email')" in demonstrate
    assert "creds.get('password')" in demonstrate
    assert ("with current.expect_response(lambda response: "
            "response.request.method == 'POST' and "
            "response.url.endswith('/api/method/login')" in demonstrate)
    assert "    target.click()" in demonstrate
    assert "if not _login_response_info.value.ok:" in demonstrate
    assert "app_url.rstrip('/') + '/desk/job-applicant'" in demonstrate
    assert "_replay.wait_after_login(current, 'erpnext')" in demonstrate
    # A failed captured absolute redirect recovers on the same live-origin URL.
    assert ("_replay.recover(pages['page'], app_url.rstrip('/') + "
            "'/desk/job-applicant', 'erpnext'" in demonstrate)


def test_wait_after_login_tolerates_polling_and_runs_application_readiness(
        monkeypatch):
    from showAndTell.applications.browser import runtime as browser_runtime

    calls = []

    class Page:
        def wait_for_load_state(self, state, *, timeout):
            calls.append((state, timeout))
            raise TimeoutError("application keeps polling")

    monkeypatch.setattr(browser_runtime, "wait_ready", lambda page, application:
                        calls.append((page, application)))
    page = Page()

    replay.wait_after_login(page, "erpnext")

    assert calls == [("networkidle", 30_000), (page, "erpnext")]


def test_recovery_emits_every_narration_key_without_counting_extra_skips():
    class Page:
        url = "http://example.test/current"

    calls = []
    skipped = []
    page = Page()

    replay.recover(
        page, "", None, "click unavailable target",
        ["voice-001", "voice-002"],
        lambda key, callback_page, description: calls.append(
            (key, callback_page, description)
        ),
        skipped, RuntimeError("not found"),
    )

    assert skipped == ["voice-001"]
    assert calls == [
        ("voice-001", page, "skipped click unavailable target: not found"),
        ("voice-002", page, "skipped click unavailable target: not found"),
    ]


def test_recovery_emits_every_narration_key_after_opening_recorded_url(
        monkeypatch):
    class Page:
        url = "http://example.test/current"

        def goto(self, url, *, wait_until):
            assert wait_until == "domcontentloaded"
            self.url = url

        def wait_for_load_state(self, state, *, timeout):
            assert (state, timeout) == ("networkidle", 30_000)

    monkeypatch.setattr(replay, "landed", lambda _page, _application: True)
    calls = []
    skipped = []
    page = Page()

    replay.recover(
        page, "http://example.test/recorded", None, "click Orders",
        ["voice-001", "voice-002"],
        lambda key, callback_page, description: calls.append(
            (key, callback_page, description)
        ),
        skipped, RuntimeError("not found"),
    )

    assert skipped == []
    assert calls == [
        ("voice-001", page,
         "click Orders (recovered by opening http://example.test/recorded)"),
        ("voice-002", page,
         "click Orders (recovered by opening http://example.test/recorded)"),
    ]


def test_recovery_emits_every_narration_key_after_retyping(monkeypatch):
    class Page:
        url = "http://example.test/current"

    monkeypatch.setattr(replay, "retype", lambda _page, _refill: True)
    calls = []
    skipped = []
    page = Page()

    replay.recover(
        page, "", None, "click calendar day",
        ["voice-001", "voice-002"],
        lambda key, callback_page, description: calls.append(
            (key, callback_page, description)
        ),
        skipped, RuntimeError("not found"), refill=([], {}, "2026-08-25"),
    )

    assert skipped == []
    assert calls == [
        ("voice-001", page,
         "click calendar day (recovered by retyping its known value)"),
        ("voice-002", page,
         "click calendar day (recovered by retyping its known value)"),
    ]


def test_compiler_preserves_grouped_narration_keys_on_recovery():
    source = render_demonstrate(
        [{
            "type": "click",
            "page": "page",
            "selector": "button",
            "selectors": ["button"],
            "target": {"tag": "button", "role": "button", "name": "Run"},
            "description": "click Run",
            "narration_keys": ["voice-001", "voice-002"],
        }],
        SURFACES,
    )

    assert (
        "'click Run', ['voice-001', 'voice-002'], on_step, _skipped, exc"
        in source
    )
    assert "on_step('voice-001', current, 'click Run')" in source
    assert "on_step('voice-002', current, 'click Run')" in source


def test_reactive_checkbox_waits_for_the_first_gesture_to_settle():
    calls = []

    class Target:
        reads = 0

        def is_checked(self):
            self.reads += 1
            if self.reads == 2:
                raise PlaywrightError("replacement node is not readable yet")
            return self.reads >= 4

        def set_checked(self, checked, *, force):
            calls.append(("set_checked", checked, force))
            raise PlaywrightError("reactive render raced verification")

        def click(self, *, force):
            raise AssertionError("a delivered Boolean gesture must not repeat")

        def evaluate(self, *_args):
            raise AssertionError("replay must not mutate controlled DOM state")

    target = Target()
    replay.set_checked(replay.new_replay_state(), target, True)
    assert calls == [("set_checked", True, True)]
    assert target.reads == 4


def test_routed_checkbox_waits_after_exactly_one_visible_click():
    calls = []

    class Target:
        reads = 0

        def is_checked(self):
            self.reads += 1
            return self.reads >= 3

        def click(self, *, force):
            calls.append(("click", force))

        def set_checked(self, *_args, **_kwargs):
            raise AssertionError("routed replay must not call set_checked")

        def evaluate(self, *_args, **_kwargs):
            raise AssertionError("routed replay must not mutate the DOM")

    state = replay.new_replay_state()
    state["locator_wrapper"] = object()
    target = Target()

    replay.set_checked(state, target, True)

    assert calls == [("click", True)]
    assert target.reads == 3


def test_checkbox_replay_preserves_the_playwright_failure(monkeypatch):
    calls = []

    class Target:
        def is_checked(self):
            return False

        def set_checked(self, checked, *, force):
            calls.append(("set_checked", checked, force))
            raise PlaywrightError("gesture was rejected")

        def click(self, **_kwargs):
            raise AssertionError("an ambiguous failed gesture must not repeat")

        def evaluate(self, *_args):
            raise AssertionError("replay must not mutate controlled DOM state")

    monkeypatch.setattr(replay, "_CHECKED_SETTLE_TIMEOUT", 0)
    with pytest.raises(RuntimeError, match="did not reach recorded state True") as exc:
        replay.set_checked(replay.new_replay_state(), Target(), True)

    assert isinstance(exc.value.__cause__, PlaywrightError)
    assert calls == [("set_checked", True, True)]


def test_routed_checkbox_fails_after_one_click_without_dom_fallback(monkeypatch):
    calls = []

    class Target:
        def is_checked(self):
            return False

        def click(self, *, force):
            calls.append(("click", force))

        def set_checked(self, *_args, **_kwargs):
            raise AssertionError("routed replay must not call set_checked")

        def evaluate(self, *_args):
            raise AssertionError("routed replay must not mutate the DOM")

    state = replay.new_replay_state()
    state["locator_wrapper"] = object()
    monkeypatch.setattr(replay, "_CHECKED_SETTLE_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="via routed input"):
        replay.set_checked(state, Target(), True)

    assert calls == [("click", True)]


class _BrowserBackedCodexActuator:
    """Exercise Codex's locator proxy with trusted Chromium mouse events."""

    def __init__(self):
        self.clicks = 0

    def click(self, _page, locator, *, position=None):
        assert position is None
        self.clicks += 1
        locator.click(force=True)

    def type_text(self, page, text):
        page.keyboard.type(text)

    def paste_text(self, page, text):
        page.keyboard.insert_text(text)


def _codex_routed_target(locator):
    from showAndTell.students.codex import CodexAdapter

    adapter = CodexAdapter()
    adapter.activate = lambda: None
    adapter.actuator = _BrowserBackedCodexActuator()
    hooks = adapter.demo_kwargs(SimpleNamespace(mode="replay"))["input_hooks"]
    state = replay.new_replay_state()
    state.update(hooks)
    return state, replay.action_target(state, locator), adapter.actuator


@pytest.mark.slow
def test_checkbox_replay_waits_for_delayed_aria_state_without_double_toggle():
    from playwright.sync_api import sync_playwright

    html = """
        <div id="custom" role="checkbox" tabindex="0"
             aria-checked="false">Include archived</div>
        <script>
          window.clicks = 0;
          custom.addEventListener('click', () => {
            window.clicks += 1;
            setTimeout(() => custom.setAttribute(
              'aria-checked',
              custom.getAttribute('aria-checked') === 'false' ? 'true' : 'false'
            ), 50);
          });
        </script>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for routed in (False, True):
            page = browser.new_page()
            page.set_content(html)
            target = page.locator("#custom")
            state = replay.new_replay_state()
            actuator = None
            if routed:
                state, target, actuator = _codex_routed_target(target)

            replay.set_checked(state, target, True)
            page.wait_for_timeout(100)

            assert target.is_checked() is True
            assert page.evaluate("window.clicks") == 1
            if actuator is not None:
                assert actuator.clicks == 1
            page.close()
        browser.close()


@pytest.mark.slow
def test_codex_routed_checkbox_survives_reactive_dom_replacement():
    from playwright.sync_api import sync_playwright

    html = """
        <div id="replace" role="checkbox" tabindex="0"
             aria-checked="false">Replace me</div>
        <script>
          window.clicks = 0;
          replace.addEventListener('click', () => {
            window.clicks += 1;
            setTimeout(() => replace.outerHTML =
              '<div id="replace" role="checkbox" aria-checked="true">Done</div>',
              50);
          });
        </script>
    """

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        state, target, actuator = _codex_routed_target(page.locator("#replace"))

        replay.set_checked(state, target, True)

        assert target.is_checked() is True
        assert target.inner_text() == "Done"
        assert page.evaluate("window.clicks") == 1
        assert actuator.clicks == 1
        browser.close()


@pytest.mark.slow
def test_rejected_checkbox_gesture_is_never_repeated(monkeypatch):
    from playwright.sync_api import sync_playwright

    html = """
        <input id="blocked" type="checkbox">
        <script>
          window.clicks = 0;
          blocked.addEventListener('click', event => {
            window.clicks += 1;
            event.preventDefault();
          });
        </script>
    """
    monkeypatch.setattr(replay, "_CHECKED_SETTLE_TIMEOUT", 0)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for routed in (False, True):
            page = browser.new_page()
            page.set_content(html)
            target = page.locator("#blocked")
            state = replay.new_replay_state()
            actuator = None
            if routed:
                state, target, actuator = _codex_routed_target(target)

            with pytest.raises(RuntimeError, match="did not reach recorded state True"):
                replay.set_checked(state, target, True)

            assert target.is_checked() is False
            assert page.evaluate("window.clicks") == 1
            if actuator is not None:
                assert actuator.clicks == 1
            page.close()
        browser.close()


@pytest.mark.slow
def test_checkbox_replay_is_idempotent_for_native_checked_state():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<input id="native" type="checkbox">'
            '<script>window.clicks = 0; native.addEventListener('
            "'click', () => window.clicks += 1)</script>"
        )
        target = page.locator("#native")
        state = replay.new_replay_state()

        replay.set_checked(state, target, True)
        replay.set_checked(state, target, True)
        replay.set_checked(state, target, False)

        assert target.is_checked() is False
        assert page.evaluate("window.clicks") == 2
        browser.close()


@pytest.mark.slow
def test_radio_replay_selects_once_and_refuses_synthetic_uncheck(monkeypatch):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<input id="standard" type="radio" name="shipping" checked>'
            '<input id="express" type="radio" name="shipping">'
            '<script>window.clicks = 0; express.addEventListener('
            "'click', () => window.clicks += 1)</script>"
        )
        standard = page.locator("#standard")
        express = page.locator("#express")
        state = replay.new_replay_state()

        replay.set_checked(state, express, True)
        replay.set_checked(state, express, True)

        assert standard.is_checked() is False
        assert express.is_checked() is True
        assert page.evaluate("window.clicks") == 1

        monkeypatch.setattr(replay, "_CHECKED_SETTLE_TIMEOUT", 0)
        with pytest.raises(RuntimeError, match="did not reach recorded state False"):
            replay.set_checked(state, express, False)
        assert express.is_checked() is True
        assert page.evaluate("window.clicks") == 1
        browser.close()


def test_secondary_seed_application_gets_a_dedicated_page():
    surfaces = [SURFACES[0], {
        "id": "onlyoffice", "application": "onlyoffice",
        "url": "http://127.0.0.1:8081/onlyoffice/editor/task-setup",
        "credentials": {},
    }]
    source = render_demonstrate([], surfaces, [{
        "type": "goto", "page": "page2",
        "url": "http://127.0.0.1:8081/onlyoffice/editor/task-setup",
    }])
    assert "pages['page2'] = page.context.new_page()" in source
    assert "context.pages[-1]" not in source


def test_type_events_compile_to_routable_keyboard_typing():
    """IME-recorded keystrokes replay through the page keyboard into whatever
    the preceding canvas click focused — never as fill() on the hidden
    textarea, which the editor treats as garbage."""
    events = [
        {"type": "click", "page": "page", "selectors": ["#grid"],
         "selector": "#grid", "frame_url": None,
         "target": {"tag": "canvas", "role": "canvas", "name": ""},
         "position": {"x": 10, "y": 20}, "at_ms": 100},
        {"type": "type", "page": "page", "text": "=SUM(E2:E4)", "at_ms": 400,
         "frame_url": None, "target": {"tag": "textarea", "role": "textbox",
                                       "name": ""}},
    ]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "_type_text(current, '=SUM(E2:E4)')" in source
    assert "def _paste_text" not in source
    assert "on_step('action-002'" in source


def test_paste_input_compiles_to_atomic_browser_text_insertion():
    events = [{
        "type": "type",
        "page": "page",
        "text": "a captured paste",
        "input_source": "paste",
        "at_ms": 100,
        "frame_url": None,
        "target": {"tag": "div", "role": "textbox", "name": ""},
    }]

    source = render_demonstrate(events, SURFACES)

    ast.parse(source)
    assert "_paste_text(current, 'a captured paste')" in source
    assert "_type_text(current, 'a captured paste')" not in source


def test_paste_text_uses_a_real_browser_paste_without_an_input_hook(monkeypatch):
    calls = []

    class Context:
        def grant_permissions(self, permissions, *, origin):
            calls.append(("permissions", permissions, origin))

    class Keyboard:
        def press(self, shortcut):
            calls.append(("press", shortcut))

    class Page:
        url = "http://127.0.0.1:8081/editor/task-setup"
        context = Context()
        keyboard = Keyboard()

        def evaluate(self, expression, value):
            calls.append(("clipboard", expression, value))

        def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    monkeypatch.setattr(replay.sys, "platform", "darwin")
    replay.paste_text(replay.new_replay_state(), Page(), "a captured paste")

    assert calls == [
        ("permissions", ["clipboard-read", "clipboard-write"],
         "http://127.0.0.1:8081"),
        ("clipboard", "value => navigator.clipboard.writeText(value)",
         "a captured paste"),
        ("press", "Meta+V"),
        ("wait", 1500),
    ]


def test_paste_text_stays_on_the_installed_os_input_plane():
    calls = []

    class Keyboard:
        def press(self, _value):
            raise AssertionError("browser input must not bypass the hook")

    class Page:
        keyboard = Keyboard()

        def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    state = replay.new_replay_state()
    state["type_text"] = lambda page, value: calls.append((page, value))
    page = Page()

    replay.paste_text(state, page, "a captured paste")

    assert calls == [
        (page, "a captured paste"),
        ("wait", 1500),
    ]


def test_paste_text_prefers_the_installed_os_paste_hook():
    calls = []

    class Page:
        def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    state = replay.new_replay_state()
    state["type_text"] = lambda _page, _value: calls.append("typed")
    state["paste_text"] = lambda page, value: calls.append((page, value))
    page = Page()

    replay.paste_text(state, page, "a captured paste")

    assert calls == [
        (page, "a captured paste"),
        ("wait", 1500),
    ]


def test_demonstrate_skips_a_stuck_action_and_reports_it():
    """A momentarily disabled toolbar button must not kill a three-minute
    teach at ninety percent; the failure is voiced through on_step and the
    replay only raises when a large share of the demo was skipped."""
    namespace = {}
    exec(render_demonstrate(_events(), SURFACES), namespace)

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def count(self):
            return 1

        def is_visible(self):
            return True

        def fill(self, value):
            pass

        def click(self, **kwargs):
            raise TimeoutError("element is disabled")

    class Page:
        url = "about:blank"
        frames = []

        def goto(self, url, **kwargs):
            self.url = url

        def bring_to_front(self):
            pass

        def locator(self, selector):
            return Locator(selector)

    steps = []
    namespace["demonstrate"](
        Page(), "http://fixture.test", {"email": "op", "password": "pw"},
        lambda key, _page, description: steps.append((key, description)),
    )
    skipped = [d for _k, d in steps if d.startswith("skipped")]
    assert len(skipped) == 1 and "element is disabled" in skipped[0]
    # The two fills succeeded; only the click was skipped, so no raise.
    assert [key for key, _ in steps] == ["action-001", "action-002", "action-003"]


def test_demonstrate_raises_when_most_actions_were_skipped():
    namespace = {}
    exec(render_demonstrate(_events(), SURFACES), namespace)
    # This stub page never produces the targets, and the patience a real
    # rendering page needs would only be spent waiting here.
    namespace["_REPLAY"]["resolve_timeout"] = 0.0

    class Locator:
        def count(self):
            return 0

    class Page:
        url = "about:blank"
        frames = []

        def goto(self, url, **kwargs):
            pass

        def bring_to_front(self):
            pass

        def locator(self, selector):
            return Locator()

        def get_by_role(self, role, name=None, exact=False):
            return Locator()

    import pytest
    with pytest.raises(RuntimeError, match="skipped"):
        namespace["demonstrate"](
            Page(), "http://fixture.test", {}, lambda *args: None)


def test_resolve_never_hands_a_form_field_action_its_own_label():
    """A form field's captured name is its aria-label or placeholder — text
    that also sits visibly on the label BESIDE the field — so matching by
    text can only ever return that label, and filling a label throws. The
    field's own recovery is the focus rung: the click before the fill put
    the caret in it."""

    class Locator:
        def __init__(self, kind, matches, tag="input"):
            self.kind = kind
            self.matches = matches
            self.tag = tag

        def count(self):
            return self.matches

        def is_visible(self):
            return True

        def evaluate(self, _script):
            return self.tag

        def locator(self, _selector):
            return Locator("child", 0)

    class Root:
        def locator(self, selector):
            if selector == ":focus":
                return Locator("field", 1)
            return Locator("stale", 0)

        def get_by_role(self, role, name=None, exact=False):
            return Locator("role", 0)

        def get_by_text(self, text, exact=False):
            return Locator("label", 1)

    target = {"tag": "input", "role": "input", "name": "Due Date"}
    resolved = replay.resolve(Root(), ["#stale"], target, True)
    assert resolved.kind == "field"


def test_resolve_rejects_a_focused_non_field_for_a_fill():
    """A forced click can leave a reactive grid wrapper focused while its
    input is being replaced. The focus fallback must not hand fill() that
    wrapper merely because it is the document's sole focused element."""

    class Locator:
        def __init__(self, tag, matches):
            self.tag = tag
            self.matches = matches

        def count(self):
            return self.matches

        def is_visible(self):
            return True

        def evaluate(self, _script):
            return self.tag

        def locator(self, _selector):
            return Locator("input", 0)

    class Root:
        def locator(self, selector):
            return Locator("div", 1) if selector == ":focus" else Locator("input", 0)

        def get_by_role(self, role, name=None, exact=False):
            return Locator("input", 0)

        def get_by_text(self, text, exact=False):
            return Locator("label", 1)

    target = {"tag": "input", "role": "combobox", "name": "Warehouse"}
    assert replay.resolve(Root(), ["#stale"], target, True) is None


def test_landed_judges_a_recovery_by_its_own_applications_dead_page():
    """Each application phrases its 'nothing to show' page its own way; a
    recovery navigation is judged against the phrasing of the application it
    landed on, and one with no known phrasing is taken at its word."""

    class Page:
        def locator(self, selector):
            return SimpleNamespace(inner_text=lambda: (
                "Sorry! I could not find what you were looking for"))

    assert replay.landed(Page(), "erpnext") is False
    assert replay.landed(Page(), "gitlab") is True
    assert replay.landed(Page(), None) is True


def test_canvas_position_clicks_are_forced_past_idle_tooltips():
    """A replay's cursor sits still, so hover tooltips linger over the canvas
    and Playwright's actionability check would block every later cell click
    ("subtree intercepts pointer events"). Position clicks target the app's
    own drawing surface — force them."""
    events = [{
        "type": "click", "page": "page", "selectors": ["#ws-canvas"],
        "selector": "#ws-canvas", "frame_url": None,
        "target": {"tag": "canvas", "role": "canvas", "name": ""},
        "position": {"x": 312, "y": 64}, "at_ms": 100,
    }]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "target.click(position={'x': 312, 'y': 64}, force=True)" in source


def test_visible_form_control_clicks_are_forced_past_reactive_form_overlays():
    """The locator ladder has already selected one visible captured field.
    Reactive grids may briefly cover that field while committing its sibling's
    value, so waiting for pointer actionability can strand the correct click."""
    events = [{
        "type": "click", "page": "page",
        "selectors": ['role=combobox[name*="Warehouse"]'],
        "selector": 'role=combobox[name*="Warehouse"]', "frame_url": None,
        "target": {"tag": "input", "role": "combobox", "name": "Warehouse"},
        "at_ms": 100,
    }]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "target.click(force=True)" in source

    # Links and buttons still keep Playwright's full actionability checking.
    plain = render_demonstrate([{
        "type": "click", "page": "page", "selectors": ["#next"],
        "selector": "#next", "frame_url": None,
        "target": {"tag": "button", "role": "button", "name": "Next"},
        "at_ms": 100,
    }], SURFACES)
    assert "target.click()" in plain
    assert "force=True" not in plain


def test_boolean_control_replay_sets_recorded_state_without_losing_force():
    """Boolean controls are state assignments, not relative toggles.

    Keeping force preserves the reactive-overlay fix; set_checked makes the
    action idempotent when the replay fixture already has the recorded state.
    """
    checkbox = {
        "type": "click", "page": "page", "selectors": ["#variants"],
        "selector": "#variants", "frame_url": None,
        "target": {"tag": "input", "role": "textbox",
                   "input_type": "checkbox", "name": "Has Variants"},
        "checked_before": False, "checked_after": True, "at_ms": 100,
    }
    source = render_demonstrate([checkbox], SURFACES)
    ast.parse(source)
    assert "_replay.set_checked(_REPLAY, target, True)" in source
    assert "target.click(force=True)" not in source

    radio = {
        **checkbox,
        "target": {"tag": "div", "role": "radio", "name": "Express"},
        "checked_after": False,
    }
    source = render_demonstrate([radio], SURFACES)
    ast.parse(source)
    assert "_replay.set_checked(_REPLAY, target, False)" in source


def test_legacy_boolean_click_without_state_keeps_compatible_forced_click():
    """Do not reinterpret immutable events.jsonl files captured before state."""
    legacy = {
        "type": "click", "page": "page", "selectors": ["#variants"],
        "selector": "#variants", "frame_url": None,
        "target": {"tag": "input", "role": "textbox",
                   "input_type": "checkbox", "name": "Has Variants"},
        "at_ms": 100,
    }
    source = render_demonstrate([legacy], SURFACES)
    ast.parse(source)
    assert "target.click(force=True)" in source
    assert "target.set_checked(" not in source


def test_render_upgrades_stored_onlyoffice_echo_pairs():
    """Old draft JSONL files retain the duplicate pair, so rendering—not only
    live capture finalization—must normalize it."""
    frame = {
        "type": "click", "page": "page", "at_ms": 100,
        "frame_url": "http://document-server/editor",
        "selectors": ["#ws-canvas-graphic-overlay"],
        "selector": "#ws-canvas-graphic-overlay",
        "position": {"x": 25, "y": 30},
        "target": {"tag": "canvas", "name": ""},
    }
    host = {
        "type": "click", "page": "page", "at_ms": 101,
        "frame_url": None, "selectors": ['[aria-label="Cell A1"]'],
        "selector": '[aria-label="Cell A1"]',
        "position": {"x": 10065, "y": 206},
        "target": {"tag": "canvas", "name": "Cell A1"},
    }

    source = render_demonstrate([frame, host], SURFACES)

    assert "_select_onlyoffice_cell(current, 'A1')" in source
    assert "target.click(position=" not in source
    assert "10065" not in source


def test_onlyoffice_cell_echo_replays_by_reference_not_captured_zoom_position():
    """A workbook can reopen a worksheet at a different zoom.

    The iframe observer owns the replayable canvas, while the host observer
    owns the stable cell identity. Their merged action must use both pieces of
    evidence to select the demonstrated cell independent of zoom and scroll.
    """
    frame = {
        "type": "click", "page": "page2", "at_ms": 100,
        "frame_url": "http://document-server/editor",
        "selectors": ["#ws-canvas-graphic-overlay"],
        "selector": "#ws-canvas-graphic-overlay",
        "position": {"x": 733, "y": 137},
        "target": {"tag": "canvas", "name": ""},
    }
    host = {
        "type": "click", "page": "page2", "at_ms": 101,
        "frame_url": None,
        "selectors": ['[aria-label="Cell D4"]'],
        "selector": '[aria-label="Cell D4"]',
        "position": {"x": 10773, "y": 313},
        "target": {"tag": "canvas", "name": "Cell D4"},
    }

    source = render_demonstrate([frame, host], SURFACES)

    assert "from showAndTell.applications.onlyoffice.browser import select_cell" in source
    assert "_select_onlyoffice_cell(current, 'D4')" in source
    assert "733" not in source
    assert "10773" not in source


def test_onlyoffice_frame_matching_ignores_runtime_host_and_build_stamp():
    """A VM capture must find the same editor inside a local task runtime."""
    captured = (
        "http://203.0.113.10:8081/"
        "9.4.0-83f2992736c997eaf416f3c00412d5e6/"
        "web-apps/apps/spreadsheeteditor/main/index.html"
        "?_dc=9.4.0-129&parentOrigin=http://203.0.113.10:8081"
    )
    live = SimpleNamespace(
        url=(
            "http://127.0.0.1:8081/"
            "9.4.0-408f0974d260b2027ed9a74c1b5c3ac4/"
            "web-apps/apps/spreadsheeteditor/main/index.html"
            "?_dc=9.4.0-129&parentOrigin=http://127.0.0.1:8081"
        )
    )
    main = SimpleNamespace(url="http://127.0.0.1:8081/editor")
    page = SimpleNamespace(url=main.url, main_frame=main,
                           frames=[main, live])

    assert replay.root(page, captured) is live


def test_locator_prefers_the_single_visible_match():
    """An SPA keeps hidden twins of dialog controls in the DOM; a selector
    matching {hidden form field, visible dialog field} must resolve to the
    visible one instead of failing the replay."""

    class Candidate:
        def __init__(self, visible):
            self.visible = visible

        def is_visible(self):
            return self.visible

    class Locator:
        def __init__(self):
            self.matches = [Candidate(False), Candidate(True)]

        def count(self):
            return len(self.matches)

        def nth(self, index):
            return self.matches[index]

    class Root:
        def locator(self, selector):
            return Locator()

    resolved = replay.locator(Root(), ["#twinned"], {"tag": "input"})
    assert isinstance(resolved, Candidate) and resolved.visible


def test_codegen_blurs_for_a_commit_only_background_click():
    event = {
        "type": "click", "page": "page", "at_ms": 10,
        "selectors": ["div.main-section"],
        "target": {"tag": "div", "role": "div", "name": "Form"},
        "commit_only": True,
    }

    source = render_demonstrate([event], SURFACES)

    assert "_replay.commit_active_field(_replay.root(current, None))" in source
    assert "target.click(position=" not in source


def test_locator_waits_for_submit_instead_of_accepting_stale_save_button():
    """A toolbar reuses its primary-button class while Save becomes Submit."""

    class Candidate:
        def __init__(self, label):
            self.label = label

        def is_visible(self):
            return True

        def evaluate(self, _script):
            return "button"

        def get_attribute(self, attribute):
            return self.label if attribute == "data-label" else None

        def inner_text(self):
            return self.label

    class Locator:
        def __init__(self, matches):
            self.matches = matches

        def count(self):
            return len(self.matches)

        def nth(self, index):
            return self.matches[index]

        def is_visible(self):
            return len(self.matches) == 1 and self.matches[0].is_visible()

        def evaluate(self, script):
            return self.matches[0].evaluate(script)

        def get_attribute(self, attribute):
            return self.matches[0].get_attribute(attribute)

        def inner_text(self):
            return self.matches[0].inner_text()

    class Root:
        def __init__(self):
            self.semantic_checks = 0

        def locator(self, selector):
            if selector.startswith("role=button"):
                self.semantic_checks += 1
                if self.semantic_checks >= 3:
                    return Locator([Candidate("Submit")])
                return Locator([])
            return Locator([Candidate("Save")])

        def get_by_role(self, *_args, **_kwargs):
            return Locator([])

        def get_by_text(self, *_args, **_kwargs):
            return Locator([])

    root = Root()
    resolved = replay.locator(
        root,
        ['role=button[name*="Submit"]', "button.btn.btn-primary"],
        {"tag": "button", "role": "button", "name": "Submit"},
        timeout=1.0,
    )

    assert resolved.inner_text() == "Submit"
    assert root.semantic_checks >= 3


def test_tooltip_identity_rejects_a_differently_named_css_match():
    class Candidate:
        def get_attribute(self, attribute):
            if attribute == "data-original-title":
                return "Previous Document"
            return None

        def inner_text(self):
            return ""

    target = {
        "tag": "button", "role": "button", "name": "Next Document",
        "name_source": "tooltip",
    }
    assert replay.named_control_matches(Candidate(), target) is False


def test_named_div_rejects_a_structural_selector_that_drifted_to_a_menu():
    class Candidate:
        def get_attribute(self, _attribute):
            return None

        def inner_text(self):
            return "Filter\nSort\nMove left\nMove right\nHide"

    target = {
        "tag": "div", "role": "div", "aria_role": "",
        "name": "Assignee", "name_source": "accessible",
        "identity_kind": "text",
    }

    assert replay.named_control_matches(Candidate(), target) is False


def test_named_element_identity_collapses_visible_whitespace():
    class Candidate:
        def get_attribute(self, _attribute):
            return None

        def inner_text(self):
            return "Assigned\nTo"

    target = {
        "tag": "div", "role": "div", "aria_role": "",
        "name": "Assigned To", "name_source": "accessible",
        "identity_kind": "text",
    }

    assert replay.named_control_matches(Candidate(), target) is True


def test_text_input_value_is_never_mistaken_for_its_placeholder_identity():
    class Candidate:
        def get_attribute(self, attribute):
            return {"placeholder": "Subject", "value": "Quarterly update"}.get(attribute)

        def inner_text(self):
            return ""

    target = {
        "tag": "input", "role": "textbox", "aria_role": "textbox",
        "name": "Subject", "name_source": "accessible",
        "identity_kind": "placeholder",
    }

    assert replay.named_control_matches(Candidate(), target) is True


def test_selector_provenance_overrides_legacy_string_classification():
    target = {"selector_kinds": ["semantic", "structural"]}

    assert replay.selector_kind("button.looks-like-css", target, 0) == "semantic"
    assert replay.selector_kind("#looks-stable", target, 1) == "structural"
    assert replay.selector_kind("div.card") == "weak"
    assert replay.selector_kind("div:nth-of-type(2) > div") == "structural"


def test_codegen_carries_selector_provenance_into_the_runtime_target():
    event = {
        "type": "click", "page": "page", "at_ms": 10,
        "selectors": ["div:nth-of-type(2) > div"],
        "target": {
            "tag": "div", "role": "div", "aria_role": "",
            "name": "Assignee", "name_source": "accessible",
            "identity_kind": "text", "selector_kinds": ["structural"],
            "selector_scores": [1000000],
        },
    }

    source = render_demonstrate([event], SURFACES)

    assert "'identity_kind': 'text'" in source
    assert "'selector_kinds': ['structural']" in source
    assert "'selector_scores': [1000000]" in source


def test_tooltip_identity_is_not_used_as_an_aria_role_fallback():
    class Empty:
        def count(self):
            return 0

    class Root:
        def locator(self, _selector):
            return Empty()

        def get_by_role(self, *_args, **_kwargs):
            raise AssertionError("tooltip identity is not an accessible name")

        def get_by_text(self, *_args, **_kwargs):
            raise AssertionError("tooltip identity is not visible text")

    target = {
        "tag": "button", "role": "button", "name": "Next Document",
        "name_source": "tooltip",
    }
    assert replay.resolve(Root(), ["button.next-doc"], target, False) is None


def test_raw_name_attribute_identity_does_not_reject_the_control_it_named():
    """A form control's `name=` attribute is machine identity, not a label.
    Holding a candidate's visible label up against it rejects the very
    element the operator clicked, so the named-control guard sits out."""
    class Candidate:
        def get_attribute(self, attribute):
            return "Approve" if attribute == "value" else None

        def inner_text(self):
            return "Approve"

    target = {
        "tag": "button", "role": "button", "name": "wf_action",
        "name_source": "attribute",
    }
    assert replay.named_control_matches(Candidate(), target) is True


def test_named_button_uses_click_sized_default_resolution_window(monkeypatch):
    """With no explicit timeout, a named button keeps resolving for the full
    click-sized window (30s), not the 8s ladder default — Save-to-Submit
    transitions can outlast the default while a recorder observes the page."""
    clock = {"now": 0.0}
    monkeypatch.setattr(replay, "time", SimpleNamespace(
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    ))

    class Empty:
        def count(self):
            return 0

    class Candidate:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def evaluate(self, _script):
            return "button"

        def get_attribute(self, attribute):
            return "Submit" if attribute == "data-label" else None

        def inner_text(self):
            return "Submit"

    class Root:
        def locator(self, _selector):
            return Candidate() if clock["now"] > 20.0 else Empty()

        def get_by_role(self, *_args, **_kwargs):
            return Empty()

        def get_by_text(self, *_args, **_kwargs):
            return Empty()

    resolved = replay.locator(
        Root(), ["button.btn-primary"],
        {"tag": "button", "role": "button", "name": "Submit"})

    assert resolved.inner_text() == "Submit"
    assert clock["now"] > 8.0


def test_root_matches_a_frame_whose_query_string_changed():
    """Embedded editors mint per-session tokens into their iframe URL; the
    exact-URL frame match must fall back to ignoring query and fragment."""

    class Frame:
        url = "http://ds.test/editor?token=NEW"

    class Page:
        url = "http://connector.test/doc"
        main_frame = SimpleNamespace(url=url)
        frames = [main_frame, Frame()]

    resolved = replay.root(Page(), "http://ds.test/editor?token=OLD")
    assert isinstance(resolved, Frame)


def test_root_does_not_confuse_a_root_path_child_frame_with_the_page():
    """A preview iframe and its host page may both use ``/`` while volatile
    query parameters carry the message identity.  The relaxed frame match must
    search child frames only, never return the top-level document."""

    main = SimpleNamespace(url="http://127.0.0.1:8082/?_task=mail&_mbox=INBOX")
    preview = SimpleNamespace(
        url=(
            "http://127.0.0.1:8082/?_task=mail&_uid=238&_mbox=INBOX"
            "&_framed=1&_action=preview"
        )
    )
    page = SimpleNamespace(url=main.url, main_frame=main,
                           frames=[main, preview])
    captured = (
        "http://192.0.2.10:8082/?_task=mail&_uid=337&_mbox=INBOX"
        "&_framed=1&_action=preview"
    )

    assert replay.root(page, captured) is preview


def test_recording_clock_starts_when_the_recorder_did():
    """at_ms zero must match the recording's own timeline, not "after the
    grace sleep" — otherwise the narration audio leads every action and a
    replay plays the voice seconds behind the gestures it describes."""
    import time as _time

    from showAndTell.capture import screenrec
    from showAndTell.capture.runtime import _clock_start

    assert _clock_start(SimpleNamespace(start_epoch_ms=1234)) == 1234
    before = int(_time.time() * 1000)
    assert _clock_start(screenrec.NullRecorder()) >= before


def test_erpnext_offline_hint_is_directly_runnable():
    assert "docker compose -p showandtell-erpnext" in _START_HINTS["erpnext"]
    assert "applications/erpnext/compose.yaml up -d" in _START_HINTS["erpnext"]


def test_positioned_container_click_replays_at_the_recorded_offset():
    """A large anchorless container records the raw click offset; the driver
    must replay it as a positioned force click, not a center click."""
    events = [
        {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/x",
         "at_ms": 0, "description": "open"},
        {"type": "click", "page": "page", "selectors": ["#dead-zone"],
         "selector": "#dead-zone", "frame_url": None,
         "position": {"x": 250, "y": 80},
         "target": {"tag": "div", "role": "div", "name": ""}, "at_ms": 400},
    ]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "target.click(position={'x': 250, 'y': 80}, force=True)" in source


def test_role_engine_selectors_round_trip_into_the_driver():
    """Playwright-ladder selectors (role=/text= engines) are plain locator()
    strings and must reach the driver's ranked list verbatim, replayed with
    an ordinary un-positioned click."""
    events = [
        {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/x",
         "at_ms": 0, "description": "open"},
        {"type": "click", "page": "page",
         "selectors": ['role=link[name*="test"]', 'text=test',
                       'div.list-row a'],
         "selector": 'role=link[name*="test"]', "frame_url": None,
         "target": {"tag": "a", "role": "link", "name": "test"}, "at_ms": 300},
    ]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "'role=link[name*=\"test\"]'" in source
    assert "target.click()" in source
    assert "position=" not in source


def test_selectorless_div_click_is_still_dropped_even_with_position():
    """A selector-less container cannot be located on replay at all — a
    position without a locatable element is noise either way."""
    events = [
        {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/x",
         "at_ms": 0, "description": "open"},
        {"type": "click", "page": "page", "selectors": [], "selector": None,
         "frame_url": None, "position": {"x": 10, "y": 10},
         "target": {"tag": "div", "role": "div", "name": ""}, "at_ms": 200},
    ]
    source = render_demonstrate(events, SURFACES)
    ast.parse(source)
    assert "click" not in source.split("def demonstrate")[1]


def test_replay_enters_the_spreadsheet_through_the_connector_not_the_host_page():
    """The bridge host page is not a replayable address.

    It reads its signed editor config from the URL fragment and strips it
    immediately, so the recorder only ever sees the stripped URL. Replaying
    that lands on "missing or invalid editor configuration"; the connector's
    editor route mints a fresh signed redirect instead.
    """
    surfaces = [
        {"id": "erpnext", "application": "erpnext", "label": "ERPNext",
         "url": "http://10.0.0.1:8080/login", "credentials": {}},
        {"id": "onlyoffice", "application": "onlyoffice",
         "label": "ONLYOFFICE spreadsheet",
         "url": "http://10.0.0.1:8085/onlyoffice/editor/task-setup",
         "credentials": {}},
    ]
    events = [
        {"type": "goto", "page": "page2", "at_ms": 0,
         "url": "http://10.0.0.1:8081/web-apps/brackett-host/"
                "editor.html?doc=task-setup&v=f82329ae5de6"},
        {"type": "click", "page": "page2", "selectors": ["#ws-canvas-graphic-overlay"],
         "selector": "#ws-canvas-graphic-overlay", "frame_url": None,
         "target": {"name": ""}, "at_ms": 100},
    ]

    source = render_demonstrate(events, surfaces)

    ast.parse(source)
    assert "brackett-host/editor.html" not in source
    assert (
        "pages['page2'].goto(_runtime_application_url(creds, 'onlyoffice', "
        "'http://10.0.0.1:8085/onlyoffice/editor/task-setup', "
        "_CAPTURED_SURFACES['onlyoffice']['url'])"
    ) in source


def test_supporting_application_urls_follow_the_live_runtime_context():
    credentials = {
        "_showAndTell_applications": {
            "onlyoffice": {
                "url": "http://127.0.0.1:8081",
                "browser_url": (
                    "http://127.0.0.1:8086/onlyoffice/editor/task-setup"
                ),
            },
            "roundcube": {
                "url": "http://127.0.0.1:8082",
                "browser_url": "http://127.0.0.1:8082/",
            },
        },
    }

    assert runtime_application_url(
        credentials,
        "onlyoffice",
        "http://203.0.113.10:8086/onlyoffice/editor/task-setup",
        "http://203.0.113.10:8086/onlyoffice/editor/task-setup",
    ) == "http://127.0.0.1:8086/onlyoffice/editor/task-setup"
    assert runtime_application_url(
        credentials,
        "roundcube",
        "http://203.0.113.10:8082/?_task=mail&_mbox=INBOX",
        "http://203.0.113.10:8082/",
    ) == "http://127.0.0.1:8082/?_task=mail&_mbox=INBOX"
    assert runtime_application_url(
        credentials,
        "roundcube",
        "http://203.0.113.10:8382/?_task=mail&_action=compose&_id=old",
        "http://203.0.113.10:8082/",
    ) == "http://127.0.0.1:8082/?_task=mail&_action=compose"
    assert runtime_application_url(
        {
            "_showAndTell_applications": {
                "orders": {
                    "url": "http://127.0.0.1:8088",
                    "browser_url": "http://127.0.0.1:8088/orders",
                },
            },
        },
        "orders",
        "http://203.0.113.10:8088/reports/open",
        "http://203.0.113.10:8088/orders",
    ) == "http://127.0.0.1:8088/reports/open"


def test_recovery_relocates_a_worker_port_to_the_primary_runtime():
    surfaces = [{
        "id": "erpnext", "application": "erpnext", "label": "ERPNext",
        "url": "http://203.0.113.10:8080/login", "credentials": {},
    }]
    events = [
        {
            "type": "tab_switch", "page": "page", "initial": True,
            "title": "Report", "description": "switch to Report", "at_ms": 0,
            "url": "http://203.0.113.10:8380/desk/job-opening/view/report",
        },
        {
            "type": "click", "page": "page", "at_ms": 100,
            "selectors": ['[data-name="HR-OPN-2026-0016"]'],
            "selector": '[data-name="HR-OPN-2026-0016"]',
            "target": {"tag": "a", "role": "link", "name": "HR-OPN-2026-0016"},
            "url": "http://203.0.113.10:8380/desk/job-opening/HR-OPN-2026-0016",
        },
    ]

    source = render_demonstrate(events, surfaces)

    assert (
        "_replay.recover(pages['page'], app_url.rstrip('/') + "
        "'/desk/job-opening/HR-OPN-2026-0016', 'erpnext'"
    ) in source
    assert "_replay.recover(pages['page'], 'http://203.0.113.10:8380" not in source


def test_recovery_keeps_a_non_web_landing_verbatim():
    surfaces = [{
        "id": "erpnext", "application": "erpnext", "label": "ERPNext",
        "url": "http://203.0.113.10:8080/login", "credentials": {},
    }]
    events = [
        {
            "type": "tab_switch", "page": "page", "initial": True,
            "title": "Desk", "description": "switch to Desk", "at_ms": 0,
            "url": "http://203.0.113.10:8380/desk",
        },
        {
            "type": "click", "page": "page", "at_ms": 100,
            "selectors": ["a.export"], "selector": "a.export",
            "target": {"tag": "a", "role": "link", "name": "Export"},
            "url": "about:blank",
        },
    ]

    source = render_demonstrate(events, surfaces)

    assert "_replay.recover(pages['page'], 'about:blank', None" in source
    assert "app_url.rstrip('/') + 'ank'" not in source


def test_recovery_keeps_a_foreign_host_landing_verbatim():
    surfaces = [{
        "id": "erpnext", "application": "erpnext", "label": "ERPNext",
        "url": "http://203.0.113.10:8080/login", "credentials": {},
    }]
    events = [
        {
            "type": "tab_switch", "page": "page", "initial": True,
            "title": "Desk", "description": "switch to Desk", "at_ms": 0,
            "url": "http://203.0.113.10:8380/desk",
        },
        {
            "type": "click", "page": "page", "at_ms": 100,
            "selectors": ["a.careers"], "selector": "a.careers",
            "target": {"tag": "a", "role": "link", "name": "Careers"},
            "url": "https://example.com/careers/apply",
        },
    ]

    source = render_demonstrate(events, surfaces)

    assert ("_replay.recover(pages['page'], "
            "'https://example.com/careers/apply', None") in source
    assert "app_url.rstrip('/') + '/careers/apply'" not in source


def test_primary_application_compose_landings_are_normalized_at_generation():
    surfaces = [{
        "id": "roundcube", "application": "roundcube", "label": "Mail",
        "url": "http://203.0.113.10:8082/", "credentials": {},
    }]
    events = [
        {
            "type": "tab_switch", "page": "page", "initial": True,
            "title": "Inbox", "description": "switch to Inbox", "at_ms": 0,
            "url": "http://203.0.113.10:8382/?_task=mail&_mbox=INBOX",
        },
        {
            "type": "click", "page": "page", "at_ms": 100,
            "selectors": ["a.compose"], "selector": "a.compose",
            "target": {"tag": "a", "role": "link", "name": "Compose"},
            "url": "http://203.0.113.10:8382/?_task=mail&_action=compose&_id=stale",
        },
    ]

    source = render_demonstrate(events, surfaces)

    assert (
        "_replay.recover(pages['page'], app_url.rstrip('/') + "
        "'/?_task=mail&_action=compose', 'roundcube'"
    ) in source
    assert "_id=stale" not in source


def test_supporting_goto_embeds_a_normalized_captured_url():
    surfaces = [
        {
            "id": "erpnext", "application": "erpnext", "label": "ERPNext",
            "url": "http://203.0.113.10:8080/login", "credentials": {},
        },
        {
            "id": "mail", "application": "roundcube", "label": "Mail",
            "url": "http://203.0.113.10:8082/", "credentials": {},
        },
    ]
    events = [{
        "type": "goto", "page": "page2", "at_ms": 0,
        "url": "http://203.0.113.10:8382/?_task=mail&_action=compose&_id=stale",
    }]

    source = render_demonstrate(events, surfaces)

    assert (
        "_runtime_application_url(creds, 'roundcube', "
        "'http://203.0.113.10:8382/?_task=mail&_action=compose', "
        "_CAPTURED_SURFACES['mail']['url'])"
    ) in source
    assert "_id=stale" not in source


def test_supporting_application_url_keeps_captured_fallback_without_runtime():
    captured = "http://203.0.113.10:8082/?_task=mail"

    assert runtime_application_url(
        {}, "roundcube", captured, "http://203.0.113.10:8082/"
    ) == captured


def test_in_place_spreadsheet_key_failure_does_not_reload_the_workbook():
    """A press that leaves the captured URL unchanged failed in place.

    In particular, an unsupported OS-input Tab must be reported as skipped;
    reopening ONLYOFFICE's connector route discards the active canvas state.
    """
    surfaces = [{
        "id": "onlyoffice", "application": "onlyoffice",
        "label": "ONLYOFFICE spreadsheet",
        "url": "http://10.0.0.1:8085/onlyoffice/editor/task-setup",
        "credentials": {},
    }]
    captured = (
        "http://10.0.0.1:8081/web-apps/brackett-host/"
        "editor.html?doc=task-setup&v=abc"
    )
    events = [
        {"type": "goto", "page": "page", "url": captured, "at_ms": 0},
        {"type": "press", "page": "page", "url": captured,
         "selectors": ["#area_id"], "frame_url": None, "key": "Tab",
         "target": {"tag": "textarea", "role": "textbox", "name": ""},
         "description": "press textbox", "at_ms": 100},
    ]

    source = render_demonstrate(events, surfaces)

    ast.parse(source)
    assert "_replay.recover(pages['page'], '', None, 'press textbox'" in source


def test_reviewed_startup_goto_lands_after_its_existing_login_event():
    surfaces = [
        {
            "id": "erpnext", "application": "erpnext",
            "url": "http://capture.test:8080/login", "credentials": {},
        },
        {
            "id": "roundcube", "application": "roundcube",
            "url": "http://capture.test:8082/", "credentials": {},
        },
    ]
    events = [
        {
            "type": "goto", "page": "page2", "at_ms": 0,
            "url": "http://capture.test:8082/?_task=mail&_mbox=Drafts",
            "replay_url": "http://capture.test:8082/?_task=mail&_mbox=INBOX",
        },
        {
            "type": "tab_switch", "page": "page2", "at_ms": 100,
            "url": "http://capture.test:8082/?_task=mail&_mbox=INBOX",
        },
    ]
    setup = [{
        "type": "login", "page": "page2", "surface_id": "roundcube",
    }]
    source = render_demonstrate(events, surfaces, setup)
    demonstrate = source.split("def demonstrate(", 1)[1]
    ast.parse(source)
    assert demonstrate.index("_auto_login(pages['page2']") < demonstrate.index(
        "pages['page2'].goto(_runtime_application_url(creds, 'roundcube'")
    assert demonstrate.count(".goto(") == 1
    assert "_mbox=INBOX" in demonstrate
    assert "_mbox=Drafts" not in demonstrate
    assert demonstrate.index(".goto(") < demonstrate.index("_pace(100)")


def test_audited_place_treats_query_filters_as_part_of_the_outcome():
    assert replay.same_place(
        "http://app.test/orders?customer=Good",
        "http://app.test/orders?customer=Bottom",
    ) is True
    assert replay.same_audited_place(
        "http://app.test/orders?customer=Good",
        "http://app.test/orders?customer=Bottom",
    ) is False
    assert replay.same_audited_place(
        "http://app.test/orders?b=2&a=1",
        "http://app.test/orders?a=1&b=2",
    ) is True


def test_compiler_requires_an_exact_audited_landing_without_recovery():
    event = {
        "type": "click",
        "page": "page",
        "url": "http://127.0.0.1:8023/orders?customer=Good",
        "replay_landed_url": (
            "http://127.0.0.1:8023/orders?customer=Bottom"
        ),
        "selector": "button.filter",
        "selectors": ["button.filter"],
        "target": {"tag": "button", "role": "button", "name": "Bottom"},
    }

    source = render_demonstrate([event], SURFACES)

    assert (
        "_replay.require_place(current, "
        "app_url.rstrip('/') + '/orders?customer=Bottom')"
    ) in source
    assert "target.click()" in source
    assert "raise RuntimeError('audited navigation failed: click Bottom')" in source
    assert "_replay.recover(" not in source
    assert "exact_place=True" not in source


def test_audited_noop_cannot_replace_a_navigation_with_a_direct_landing():
    event = {
        "type": "click",
        "page": "page",
        "url": "http://127.0.0.1:8023/orders",
        "replay_landed_url": "http://127.0.0.1:8023/orders/42",
        "replay_noop_reason": "unstable result row replaced by its landing",
        "target": {"tag": "div", "role": "div", "name": "Order 42"},
    }

    with pytest.raises(ValueError, match="replay the navigation gesture"):
        render_demonstrate([event], SURFACES)


def test_audited_noop_can_suppress_non_navigation_recorder_noise():
    event = {
        "type": "click",
        "page": "page",
        "url": "http://127.0.0.1:8023/orders",
        "replay_noop_reason": "duplicate layout click",
        "target": {"tag": "div", "role": "div", "name": "Orders"},
    }

    source = render_demonstrate([event], SURFACES)
    demonstrate = source.split("def demonstrate", 1)[1]

    assert "pass  # audited no-op" in demonstrate
    assert "current.goto(" not in demonstrate
    assert "_locator(" not in demonstrate


def test_auto_login_has_no_per_application_special_cases(monkeypatch):
    """Landing somewhere usable is the browser plane's job, not the caller's.

    ERPNext's bare Desk route oscillates -- verified against Frappe v16 on the
    fixture host -- and the fix belongs in the only module that knows which of
    its own routes is stable.
    """
    from showAndTell.applications.registry import Registry

    calls = []
    adapter = SimpleNamespace(
        login=lambda page, url, credentials: calls.append(("login", url)))
    monkeypatch.setattr(Registry, "browser", lambda self, name: adapter)

    class Page:
        def goto(self, url, *, wait_until):
            calls.append(("goto", url))

    assert auto_login(Page(), {
        "id": "erpnext", "application": "erpnext",
        "url": "http://erp.test/login",
        "credentials": {"email": "Administrator", "password": "secret"},
    }) is True
    assert calls == [("login", "http://erp.test")]


def test_the_unified_erpnext_browser_plane_lands_on_neutral_desk():
    """A new ERP or HR task must not inherit one module's list page."""
    from showAndTell.applications.registry import default_registry

    plane = default_registry().browser("erpnext")
    assert plane.LANDING == "/desk"


def test_erpnext_login_keeps_an_already_ready_desk_page():
    """Generated login markers must not reload the viewer's signed-in page."""
    from showAndTell.applications.erpnext import browser as plane

    calls = []

    class Page:
        url = "http://erp.test/app/item/view/list"

        def wait_for_selector(self, selector, timeout):
            calls.append(("ready", selector, timeout))

        def goto(self, *_args, **_kwargs):
            raise AssertionError("an already ready Desk page must not navigate")

    plane.login(Page(), "http://erp.test", {
        "email": "Administrator", "password": "secret",
    })

    assert calls == [("ready", plane.DESK_READY, 1_000)]


def test_settled_click_evidence_never_changes_generated_code():
    """Two-phase capture adds evidence to the persisted click; the driver
    compiled from it must stay byte-identical to the pre-evidence output."""
    legacy = {
        "type": "click", "page": "page", "selectors": ["#variants"],
        "selector": "#variants", "frame_url": None,
        "target": {"tag": "input", "role": "textbox",
                   "input_type": "checkbox", "name": "Has Variants"},
        "checked_before": False, "checked_after": True, "at_ms": 100,
    }
    settled = {
        **legacy,
        "action_id": "page:main:doc1:7",
        "capture_sequence": 3,
        "url_before": "http://127.0.0.1:8023/explore",
        "settlement": {"status": "settled", "default_prevented": False,
                       "url_after": "http://127.0.0.1:8023/explore",
                       "target_connected": True},
        "outcomes": [{"class": "browser_confirmed", "kind": "frame_navigation",
                      "destination": "http://127.0.0.1:8023/explore"}],
    }

    assert (render_demonstrate([settled], SURFACES)
            == render_demonstrate([legacy], SURFACES))


@pytest.mark.slow
def test_managed_capture_merges_two_phase_clicks_end_to_end(monkeypatch, tmp_path):
    """The whole capture loop, against a real Chromium: recorder messages
    flow through the transaction log and persist as merged, settled clicks.

    ManagedCapture normally owns a headful managed Chrome; the test
    substitutes a headless Chromium on a test-only CDP port and drives the
    operator's gestures through a second CDP client, exactly as a human
    would drive the real browser.
    """
    import contextlib
    import http.server
    import socket
    import subprocess
    import threading

    import pytest
    from playwright.sync_api import sync_playwright

    from showAndTell.capture import runtime as managed_capture, screenrec
    from showAndTell.core.chrome import wait_for_cdp
    from showAndTell.capture.runtime import ManagedCapture

    pytest.importorskip("playwright")

    page_html = (b"<html><title>Fixture</title><body>"
                 b'<input id="variants" type="checkbox" aria-label="Has Variants">'
                 b'<a id="spa" href="/list" onclick="event.preventDefault();'
                 b" history.pushState({}, '', '/list')\">Open list</a>"
                 b"</body></html>")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page_html)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    http_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        cdp_port = probe.getsockname()[1]

    procs: list[subprocess.Popen] = []
    with sync_playwright() as pw:
        chromium = pw.chromium.executable_path

        def fake_launch(port, extra_args=None, profile_root=None):
            proc = subprocess.Popen(
                [chromium, f"--remote-debugging-port={port}", "--headless=new",
                 f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run",
                 "--no-default-browser-check", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(proc)
            assert wait_for_cdp(port), "test chromium did not come up"
            return proc.pid

        def fake_kill(_profile_root=None):
            while procs:
                proc = procs.pop()
                proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)

        monkeypatch.setattr(managed_capture, "CAPTURE_CDP_PORT", cdp_port)
        monkeypatch.setattr(managed_capture, "launch_managed_chrome", fake_launch)
        monkeypatch.setattr(managed_capture, "kill_managed_chrome", fake_kill)
        monkeypatch.setattr(managed_capture, "_front_chrome_tab", lambda: None)
        monkeypatch.setattr(screenrec, "start",
                            lambda directory, out: screenrec.NullRecorder())

        capture = ManagedCapture(tmp_path, [{
            "id": "fixture", "label": "Fixture", "application": "fixture",
            "url": f"http://127.0.0.1:{http_port}/edit", "credentials": {},
        }])
        try:
            capture.start()
            capture.begin_recording()
            driver = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{cdp_port}")
            page = next(p for p in driver.contexts[0].pages
                        if f":{http_port}" in p.url)
            page.click("#variants")
            page.click("#spa")
            page.wait_for_timeout(1500)  # loop tick + paint delay + settle
            result = capture.stop()
        finally:
            capture.stop_requested.set()
            server.shutdown()
            fake_kill()

    events = result["events"]
    assert not [e for e in events
                if e["type"] in {"click_begin", "click_settled",
                                 "click_outcome", "document_ready"}]
    clicks = [e for e in events if e["type"] == "click"]
    assert len(clicks) == 2, [e["type"] for e in events]

    checkbox, spa = clicks
    assert checkbox["checked_before"] is False
    assert checkbox["checked_after"] is True
    assert checkbox["settlement"]["status"] == "settled"
    assert checkbox["action_id"].startswith("page:main:")
    assert checkbox.get("screenshot"), checkbox.get("snapshot_error")

    assert spa["settlement"]["default_prevented"] is True
    assert spa["settlement"]["url_after"].endswith("/list")
    # Post-action url keeps destination semantics for the dedupe rules.
    assert spa["url"].endswith("/list")
    outcome_classes = {o["class"] for o in spa["outcomes"]}
    assert "navigation_intent" in outcome_classes
