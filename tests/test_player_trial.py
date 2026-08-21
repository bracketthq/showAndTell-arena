"""Generated Claude/Brackett/Codex trial support for viewer-captured drafts."""
from __future__ import annotations

import json
import inspect
import time

import pytest

from showAndTell.core import audio, tts
from showAndTell.demonstration import narration
from showAndTell.player import replay as player_replay
from showAndTell.player import trial as capture_trial
from showAndTell import tasks as taskload
from showAndTell.teacher import run as teach_run
from showAndTell.students import brackett as brackett_student

from tests._tasks import make_draft as _draft


def test_load_capture_validates_and_returns_surfaces(tmp_path):
    capture = capture_trial.load_capture(_draft(tmp_path))
    assert [row["label"] for row in capture["surfaces"]] == ["GitLab", "Zulip"]


def test_load_capture_rejects_non_capture_and_bad_url(tmp_path):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["status"] = "other"
    (draft / "testcase.json").write_text(json.dumps(payload))
    with pytest.raises(capture_trial.CaptureTrialError, match="not a captured draft"):
        capture_trial.load_capture(draft)

    payload["status"] = "captured-draft"
    payload["surfaces"] = [{"url": "file:///etc/passwd"}]
    (draft / "testcase.json").write_text(json.dumps(payload))
    with pytest.raises(capture_trial.CaptureTrialError, match="no valid application"):
        capture_trial.load_capture(draft)


def test_generated_replay_runs_demonstrate_with_origin_credentials_and_narration(
        tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True, "demonstrate": "demonstrate.py"}
    payload["surfaces"][0]["credentials"] = {"email": "operator", "password": "secret"}
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text("# generated")
    calls = []

    def demonstrate(page, app_url, creds, on_step):
        calls.append((page, app_url, creds))
        on_step("action-001", page, "Open the issue")

    loaded = []
    monkeypatch.setattr(
        taskload, "load_demonstrate_module",
        lambda _draft, **kwargs: loaded.append(kwargs) or
        type("Driver", (), {"demonstrate": staticmethod(demonstrate)})(),
    )
    monkeypatch.setattr(tts, "voice_args", lambda: None)
    monkeypatch.setattr(
        narration, "make_narrator",
        lambda _draft, voice, _out: lambda key, _page, description:
        calls.append((key, voice, description)),
    )
    capture = capture_trial.load_capture(draft)
    page = object()
    capture_trial.replay_generated(page, draft, capture)
    assert calls == [
        (page, "http://127.0.0.1:8023", {"email": "operator", "password": "secret"}),
        ("action-001", None, "Open the issue"),
    ]
    assert loaded == [{"url_replacements": {}, "source_override": None}]


def test_generated_replay_installs_os_input_hooks_for_codex(tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True}
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text("# generated")
    calls = []

    class Driver:
        _REPLAY = {
            "start": None, "on_start": None,
            "locator_wrapper": None, "type_text": None,
        }

        @staticmethod
        def demonstrate(_page, _app_url, _creds, _on_step):
            calls.append(Driver._REPLAY["locator_wrapper"]("locator"))
            Driver._REPLAY["type_text"]("page", "hello")

    monkeypatch.setattr(capture_trial, "_load_replay_module", lambda *_args: Driver)
    monkeypatch.setattr(tts, "voice_args", lambda: None)
    monkeypatch.setattr(narration, "make_narrator", lambda *_args: None)
    capture_trial.replay_generated(
        object(), draft, capture_trial.load_capture(draft),
        input_hooks={
            "locator_wrapper": lambda locator: f"os:{locator}",
            "type_text": lambda page, value: calls.append((page, value)),
        },
    )

    assert calls == ["os:locator", ("page", "hello")]


def test_codex_replay_rejects_a_driver_without_os_input_hooks(tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True}
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text("# generated")
    monkeypatch.setattr(
        capture_trial, "_load_replay_module",
        lambda *_args: type("OldDriver", (), {"_REPLAY": {}}),
    )

    with pytest.raises(capture_trial.CaptureTrialError, match="OS-input replay"):
        capture_trial.replay_generated(
            object(), draft, capture_trial.load_capture(draft),
            input_hooks={"locator_wrapper": object(), "type_text": object()},
        )


def test_generated_replay_remaps_all_capture_time_origins(tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True}
    payload["surfaces"][0]["url"] = "http://127.0.0.1:49123/explore"
    payload["surfaces"][1]["url"] = "http://127.0.0.1:49124/login"
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {"
        "'gitlab': {'url': 'http://127.0.0.1:8023/explore'}, "
        "'zulip': {'url': 'http://127.0.0.1:8083/login'}}\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )
    captured = capture_trial.load_capture(draft)
    assert capture_trial._surface_origins(draft, captured) == {
        "http://127.0.0.1:8023": "http://127.0.0.1:49123",
        "http://127.0.0.1:8083": "http://127.0.0.1:49124",
    }

    loaded = []
    monkeypatch.setattr(
        taskload, "load_demonstrate_module",
        lambda _draft, **kwargs: loaded.append(kwargs) or type(
            "Driver", (), {"demonstrate": staticmethod(
                lambda _page, _app_url, _creds, _on_step: None)})(),
    )
    monkeypatch.setattr(tts, "voice_args", lambda: None)
    monkeypatch.setattr(narration, "make_narrator", lambda *_args: lambda *_a: None)
    capture_trial.replay_generated(object(), draft, captured)
    assert loaded[0]["url_replacements"] == {
        "http://127.0.0.1:8023": "http://127.0.0.1:49123",
        "http://127.0.0.1:8083": "http://127.0.0.1:49124",
    }


def test_generated_replay_remaps_cohosted_replica_ports(tmp_path):
    """ONLYOFFICE's visible connector and editor iframe use different ports.

    A replica moves both by the same stride; frame actions must follow the
    Document Server port even though only the connector is a declared surface.
    """
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["surfaces"] = [
        {"id": "erpnext", "url": "http://fixture.test:8180/login"},
        {"id": "onlyoffice", "url": "http://fixture.test:8186/editor/task"},
    ]
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {"
        "'erpnext': {'url': 'http://fixture.test:8080/login'}, "
        "'onlyoffice': {'url': 'http://fixture.test:8086/editor/task'}}\n"
        "FRAME = 'http://fixture.test:8081/web-apps/editor/index.html'\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )

    captured = capture_trial.load_capture(draft)

    assert capture_trial._surface_origins(draft, captured) == {
        "http://fixture.test:8080": "http://fixture.test:8180",
        "http://fixture.test:8081": "http://fixture.test:8181",
        "http://fixture.test:8086": "http://fixture.test:8186",
    }


def test_generated_replay_keeps_a_cohosted_neighbours_own_scheme(tmp_path):
    """Only surfaces are recorded, so nothing says a neighbour's scheme
    followed the surface's; an https service beside http surfaces must not be
    downgraded to an unreachable http origin."""
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["surfaces"] = [
        {"id": "erpnext", "url": "http://fixture.test:8180/login"},
        {"id": "onlyoffice", "url": "http://fixture.test:8186/editor/task"},
    ]
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {"
        "'erpnext': {'url': 'http://fixture.test:8080/login'}, "
        "'onlyoffice': {'url': 'http://fixture.test:8086/editor/task'}}\n"
        "SECURE = 'https://fixture.test:8443/webhooks'\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )

    captured = capture_trial.load_capture(draft)

    assert capture_trial._surface_origins(draft, captured) == {
        "http://fixture.test:8080": "http://fixture.test:8180",
        "http://fixture.test:8086": "http://fixture.test:8186",
        "https://fixture.test:8443": "https://fixture.test:8543",
    }


def test_generated_replay_leaves_neighbours_of_ambiguous_relocations_alone(tmp_path):
    """Surfaces on one capture host moving by different strides: the neighbour
    stays unmapped rather than following the wrong application."""
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["surfaces"] = [
        {"id": "erpnext", "url": "http://fixture.test:8180/login"},
        {"id": "onlyoffice", "url": "http://fixture.test:8286/editor/task"},
    ]
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {"
        "'erpnext': {'url': 'http://fixture.test:8080/login'}, "
        "'onlyoffice': {'url': 'http://fixture.test:8086/editor/task'}}\n"
        "FRAME = 'http://fixture.test:8081/web-apps/editor/index.html'\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )

    captured = capture_trial.load_capture(draft)

    assert capture_trial._surface_origins(draft, captured) == {
        "http://fixture.test:8080": "http://fixture.test:8180",
        "http://fixture.test:8086": "http://fixture.test:8286",
    }


def test_generated_replay_does_not_follow_portless_surface_moves(tmp_path):
    """Fixture surfaces always publish explicit ports, so a portless surface
    relocation remaps only its own origin, never its ported neighbours."""
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["surfaces"] = [
        {"id": "erpnext", "url": "http://replica.test/login"},
    ]
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {"
        "'erpnext': {'url': 'http://capture.test/login'}}\n"
        "API = 'http://capture.test:8081/api'\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )

    captured = capture_trial.load_capture(draft)

    assert capture_trial._surface_origins(draft, captured) == {
        "http://capture.test": "http://replica.test",
    }


def test_seed_stage_runs_automatically_before_product_recording(tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    capture = capture_trial.load_capture(draft)
    calls = []

    class Driver:
        @staticmethod
        def seed(page, app_url, creds, on_step):
            calls.append(("seed", page, app_url, creds))

    monkeypatch.setattr(capture_trial, "_load_replay_module", lambda *_args: Driver())
    page = object()
    capture_trial.restore_generated_seed(page, draft, capture, calls.append)
    assert calls[0] == "  replaying setup gestures (no state snapshot in this draft)…"
    assert calls[1] == ("seed", page, "http://127.0.0.1:8023", {})


def test_brackett_routes_audio_and_groups_tabs_before_replay():
    """The shared runner routes audio before product launch; Brackett then
    adopts the application tabs before replay and re-verifies live URLs."""
    from showAndTell.students import brackett as brackett_chrome
    from showAndTell.teacher import run as teacher_run

    shared = inspect.getsource(teacher_run._run)
    assert shared.index('session.narration("virtual-cable")') < shared.index(
        "sync_playwright()")
    arm = inspect.getsource(brackett_chrome.BrackettAdapter._arm_trial)
    assert arm.index("_start_show_and_tell(") < arm.index("_adopt_application_tabs(")
    kwargs = inspect.getsource(brackett_chrome.BrackettAdapter.demo_kwargs)
    assert "page.url for page in session.ctx.pages" in kwargs
    assert '"before_start": verify_live_application_tabs' in kwargs


def test_legacy_combined_driver_is_split_in_memory(tmp_path):
    draft = _draft(tmp_path)
    (draft / "task_logic.py").write_text("# generated\n")
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {'gitlab': {'id': 'gitlab', "
        "'url': 'http://127.0.0.1:8023'}}\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )
    (draft / "demo").mkdir()
    (draft / "demo/seed_events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/seed"}) + "\n")
    (draft / "events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/record"}) + "\n")
    upgraded = capture_trial._driver_source(draft)
    assert upgraded is not None
    assert "def seed(" in upgraded and "'/seed'" in upgraded
    assert "def demonstrate(" in upgraded and "'/record'" in upgraded


def test_pre_tab_focus_driver_is_rerendered_in_memory(tmp_path):
    """Drivers generated before tab activation existed replay multi-app
    captures with the wrong tab visible; they are upgraded from their events
    the same way pre-pacing drivers are."""
    draft = _draft(tmp_path)
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {'gitlab': {'id': 'gitlab', "
        "'url': 'http://127.0.0.1:8023'}}\n"
        "_REPLAY = {'start': None}\n"
        "def _pace(at_ms): pass\n"
        "def seed(page, app_url, creds, on_step): pass\n"
        "def demonstrate(page, app_url, creds, on_step): pass\n"
    )
    (draft / "demo").mkdir()
    (draft / "demo/seed_events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/seed"}) + "\n")
    (draft / "events.jsonl").write_text(json.dumps({
        "type": "click", "page": "page", "selectors": ["#go"], "selector": "#go",
        "frame_url": None, "target": {"name": "Go"}, "at_ms": 50}) + "\n")
    upgraded = capture_trial._driver_source(draft)
    assert upgraded is not None
    assert "bring_to_front()" in upgraded

    # A driver that already activates pages and waits for its application is
    # left untouched.
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {}\n"
        "def _pace(at_ms): pass\n"
        "def seed(page, app_url, creds, on_step): pass\n"
        "def demonstrate(page, app_url, creds, on_step):\n"
        "    pages = {'page': page}\n"
        "    _wait_ready(pages['page'], 'gitlab')\n"
        "    pages['page'].bring_to_front()\n"
    )
    assert capture_trial._driver_source(draft) is None


def test_pre_readiness_driver_is_rerendered_in_memory(tmp_path):
    """Drivers generated before the readiness gate existed fire their first
    gesture into an application that is still starting, which loses it
    silently; they are upgraded from their events like pre-pacing drivers."""
    draft = _draft(tmp_path)
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {'gitlab': {'id': 'gitlab', 'application': "
        "'gitlab', 'url': 'http://127.0.0.1:8023'}}\n"
        "_REPLAY = {'start': None}\n"
        "def _pace(at_ms): pass\n"
        "def seed(page, app_url, creds, on_step): pass\n"
        "def demonstrate(page, app_url, creds, on_step):\n"
        "    pages = {'page': page}\n"
        "    pages['page'].bring_to_front()\n"
    )
    (draft / "demo").mkdir()
    (draft / "demo/seed_events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/seed"}) + "\n")
    (draft / "events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/explore", "at_ms": 0}) + "\n")
    upgraded = capture_trial._driver_source(draft)
    assert upgraded is not None
    assert "_replay.wait_ready(pages['page'], 'gitlab')" in upgraded


def test_pre_credential_routing_driver_is_rerendered_in_memory(tmp_path):
    """Drivers generated just before per-surface credential routing carry
    every older marker, yet still sign supporting pages in with the primary's
    credentials and replay stale host/iframe echo pairs.  The upgrade gate
    must key on something only the current renderer emits."""
    draft = _draft(tmp_path)
    (draft / "demonstrate.py").write_text(
        "_CAPTURED_SURFACES = {'gitlab': {'id': 'gitlab', 'application': "
        "'gitlab', 'url': 'http://127.0.0.1:8023'}}\n"
        "_REPLAY = {'start': None}\n"
        "_DEAD_PAGE_MARKERS = ()\n"
        "def _pace(at_ms): pass\n"
        "def _frame_key(page): pass\n"
        "def _recover(page): pass\n"
        "def _resolve(page): pass\n"
        "def _same_place(a, b): pass\n"
        "def _retype(el): pass\n"
        "def seed(page, app_url, creds, on_step): pass\n"
        "def demonstrate(page, app_url, creds, on_step):\n"
        "    pages = {'page': page}\n"
        "    _wait_ready(pages['page'], 'gitlab')\n"
        "    pages['page'].bring_to_front()\n"
        "    pages['page'].get_by_text(name, exact=True)\n"
    )
    (draft / "demo").mkdir()
    (draft / "demo/seed_events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/seed"}) + "\n")
    (draft / "events.jsonl").write_text(json.dumps({
        "type": "goto", "page": "page",
        "url": "http://127.0.0.1:8023/explore", "at_ms": 0}) + "\n")
    upgraded = capture_trial._driver_source(draft)
    assert upgraded is not None
    assert "_REPLAY = _replay.new_replay_state()" in upgraded


def test_task_loader_applies_trial_origin_replacements_in_memory(tmp_path):
    task = tmp_path / "captured"
    task.mkdir()
    (task / "task_logic.py").write_text("# generated\n")
    (task / "demonstrate.py").write_text(
        "CAPTURED = 'http://127.0.0.1:55773/onlyoffice/editor/task-setup'\n"
        "def demonstrate(page, app_url, creds, on_step): return CAPTURED\n"
    )
    driver = taskload.load_demonstrate(task, url_replacements={
        "http://127.0.0.1:55773": "http://127.0.0.1:60123",
    })
    assert driver(None, "", {}, None) == (
        "http://127.0.0.1:60123/onlyoffice/editor/task-setup")
    # The captured artifact remains stable; remapping is trial-local.
    assert "55773" in (task / "demonstrate.py").read_text()


def test_automatic_trial_opens_primary_app_before_starting_product(tmp_path):
    capture = capture_trial.load_capture(_draft(tmp_path))
    calls = []

    class Page:
        def goto(self, url, wait_until=None):
            calls.append(("goto", url, wait_until))

        def bring_to_front(self):
            calls.append(("front",))

    page = Page()

    class Context:
        def new_page(self):
            calls.append(("new",))
            return page

    assert capture_trial.open_trial_surfaces(Context(), capture) == [page]
    assert calls == [
        ("new",),
        ("goto", "http://127.0.0.1:8023", "commit"),
        ("front",),
    ]


def test_opening_a_surface_signs_in_before_the_replay_starts():
    """Skipping seed() must not also skip its login.

    Generated seed() opens with _auto_login, so gating seed() on a state
    snapshot silently left the page on /login — every recorded selector then
    failed to resolve against the login form.
    """
    calls = []

    class Page:
        url = "http://vm:8080/login"

        def goto(self, url, **kwargs):
            calls.append(("goto", url))

        def bring_to_front(self):
            pass

    class Ctx:
        def new_page(self):
            return Page()

    capture = {"surfaces": [{
        "id": "erpnext", "label": "ERPNext", "url": "http://vm:8080/login",
        "credentials": {"email": "Administrator", "password": "pw"},
    }]}

    original = capture_trial.auto_login
    capture_trial.auto_login = lambda page, surface, **kw: calls.append(
        ("login", surface["id"], kw["app_url"])) or True
    try:
        capture_trial.open_trial_surfaces(Ctx(), capture, lambda *_: None)
    finally:
        capture_trial.auto_login = original

    assert ("login", "erpnext", "http://vm:8080") in calls
    assert calls.index(("goto", "http://vm:8080/login")) < calls.index(
        ("login", "erpnext", "http://vm:8080"))


def test_opening_a_demonstrated_login_surface_leaves_it_signed_out():
    calls = []

    original = capture_trial.auto_login
    capture_trial.auto_login = lambda *a, **k: calls.append("login") or True
    try:
        capture_trial._sign_in(object(), {
            "id": "erpnext",
            "url": "http://vm:8080/login",
            "credentials": {"email": "Administrator", "password": "pw"},
            "login_replay": "demonstrated",
        }, lambda *_: None)
    finally:
        capture_trial.auto_login = original

    assert calls == []


def test_a_failed_sign_in_is_reported_instead_of_a_selector_error():
    class Page:
        def goto(self, url, **kwargs):
            pass

    capture = {"surfaces": [{
        "id": "erpnext", "label": "ERPNext", "url": "http://vm:8080/login",
        "credentials": {"email": "Administrator", "password": "pw"},
    }]}

    original = capture_trial.auto_login

    def boom(page, surface, **kw):
        raise RuntimeError("password rejected")

    capture_trial.auto_login = boom
    try:
        with pytest.raises(capture_trial.CaptureTrialError, match="sign-in to ERPNext failed"):
            capture_trial._sign_in(Page(), capture["surfaces"][0], lambda *_: None)
    finally:
        capture_trial.auto_login = original


def test_surfaces_without_credentials_are_not_signed_in():
    calls = []

    original = capture_trial.auto_login
    capture_trial.auto_login = lambda *a, **k: calls.append("login") or True
    try:
        capture_trial._sign_in(
            object(), {"id": "kiwix", "url": "http://vm:8888/", "credentials": {}},
            lambda *_: None)
    finally:
        capture_trial.auto_login = original
    assert calls == []


def _paced_module(tmp_path, at_ms=(0, 500)):
    from showAndTell.demonstration.compiler import render_demonstrate

    surfaces = [{"id": "erpnext", "label": "ERPNext",
                 "url": "http://vm:8080/login", "credentials": {}}]
    events = [{"type": "click", "page": "page", "at_ms": ms,
               "selector": f"#b{i}", "selectors": [f"#b{i}"],
               "target": {"tag": "button", "role": "button", "name": f"b{i}"}}
              for i, ms in enumerate(at_ms)]
    source = render_demonstrate(events, surfaces, [])
    path = tmp_path / "demonstrate.py"
    path.write_text(source, encoding="utf-8")
    import importlib.util
    spec = importlib.util.spec_from_file_location("paced_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, source


class _ReplayClock:
    """Deterministic monotonic clock for replay timing tests."""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def test_recorded_timing_is_emitted_only_for_the_recorded_stage(tmp_path):
    """Setup is a silent prelude; only the demonstration carries real pacing."""
    from showAndTell.demonstration.compiler import render_demonstrate

    surfaces = [{"id": "erpnext", "label": "ERPNext",
                 "url": "http://vm:8080/login", "credentials": {}}]
    action = {"type": "click", "page": "page", "selector": "#go",
              "selectors": ["#go"],
              "target": {"tag": "button", "role": "button", "name": "go"}}
    source = render_demonstrate(
        [{**action, "at_ms": 4200}], surfaces, [{**action, "at_ms": 900}])

    seed_body = source.split("def seed(")[1].split("def demonstrate(")[0]
    demonstrate_body = source.split("def demonstrate(")[1]
    assert "_pace(4200)" in demonstrate_body
    assert "_pace(" not in seed_body, "setup replay must not be paced"


def test_pace_waits_until_the_actions_recorded_moment(tmp_path, monkeypatch):
    module, _ = _paced_module(tmp_path)
    clock = _ReplayClock(now=100.0)
    monkeypatch.setattr(player_replay, "time", clock)
    module._REPLAY["start"] = clock.monotonic()
    module._pace(300)
    assert clock.sleeps == pytest.approx([0.3])
    assert clock.monotonic() == pytest.approx(100.3)


def test_pace_does_nothing_without_a_clock(tmp_path):
    """An unpaced caller (a plain task run) must not be slowed down."""
    module, _ = _paced_module(tmp_path)
    module._REPLAY["start"] = None
    began = time.monotonic()
    module._pace(5000)
    assert time.monotonic() - began < 0.1


def test_pace_starts_the_trial_clock_only_after_setup(tmp_path):
    """The first paced action, not demonstrate() entry, starts narration.

    Generated demonstrate() performs login, opens supporting tabs, and waits
    for them before reaching _pace(). The hook therefore keeps setup silent.
    """
    module, _ = _paced_module(tmp_path, at_ms=(0,))
    calls = []

    def start():
        calls.append("audio")
        module._REPLAY["start"] = time.monotonic()

    module._REPLAY.update({"start": None, "on_start": start})
    module._pace(0)
    module._pace(0)

    assert calls == ["audio"]


def test_recorded_audio_starts_after_generated_setup(tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True}
    payload["capture"] = {"recording": "demo/recording.mov"}
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text("# generated")
    (draft / "demo").mkdir()
    (draft / "demo/recording.mov").write_bytes(b"recording")
    calls = []

    class Player:
        def poll(self):
            return 0

    class Driver:
        _REPLAY = {"start": None, "on_start": None}

        @staticmethod
        def demonstrate(page, app_url, creds, on_step):
            calls.append("setup complete")
            Driver._REPLAY["on_start"]()
            calls.append("first action")

    monkeypatch.setattr(capture_trial, "_load_replay_module", lambda *_args: Driver)
    monkeypatch.setattr(
        audio, "start_recorded_narration",
        lambda _path, _out: calls.append("audio") or Player(),
    )

    capture_trial.replay_generated(
        object(), draft, capture_trial.load_capture(draft),
        lambda *_: None,
    )

    assert calls == ["setup complete", "audio", "first action"]


def test_pre_start_gate_runs_after_setup_but_before_audio_and_first_action(
        tmp_path, monkeypatch):
    draft = _draft(tmp_path)
    payload = json.loads((draft / "testcase.json").read_text())
    payload["replay"] = {"generated": True}
    payload["capture"] = {"recording": "demo/recording.mov"}
    (draft / "testcase.json").write_text(json.dumps(payload))
    (draft / "demonstrate.py").write_text("# generated")
    (draft / "demo").mkdir()
    (draft / "demo/recording.mov").write_bytes(b"recording")
    calls = []

    class Player:
        def poll(self):
            return 0

    class Driver:
        _REPLAY = {"start": None, "on_start": None}

        @staticmethod
        def demonstrate(page, app_url, creds, on_step):
            calls.append("setup complete")
            Driver._REPLAY["on_start"]()
            calls.append("first action")

    monkeypatch.setattr(capture_trial, "_load_replay_module", lambda *_args: Driver)
    monkeypatch.setattr(
        audio, "start_recorded_narration",
        lambda _path, _out: calls.append("audio") or Player(),
    )

    capture_trial.replay_generated(
        object(), draft, capture_trial.load_capture(draft),
        lambda *_: None,
        before_start=lambda: calls.append("group verified"),
    )

    assert calls == ["setup complete", "group verified", "audio", "first action"]


def test_pace_preserves_long_recorded_gaps_for_narration_sync(
        tmp_path, monkeypatch):
    """Audio runs continuously, so actions must not truncate quiet gaps."""
    module, _ = _paced_module(tmp_path, at_ms=(5_549, 82_732))
    clock = _ReplayClock(now=100.0)
    monkeypatch.setattr(player_replay, "time", clock)
    module._REPLAY["start"] = clock.monotonic()

    module._pace(5_549)
    module._pace(82_732)

    assert clock.sleeps == pytest.approx([5.549, 77.183])
    assert clock.monotonic() == pytest.approx(182.732)


def test_pace_does_not_compound_a_late_action(tmp_path, monkeypatch):
    module, _ = _paced_module(tmp_path, at_ms=(60_000,))
    clock = _ReplayClock(now=170.0)
    monkeypatch.setattr(player_replay, "time", clock)
    module._REPLAY["start"] = 100.0

    module._pace(60_000)

    assert clock.sleeps == []
    assert clock.monotonic() == 170.0


def test_pace_honours_a_speed_multiplier(tmp_path, monkeypatch):
    module, _ = _paced_module(tmp_path)
    clock = _ReplayClock(now=100.0)
    monkeypatch.setattr(player_replay, "time", clock)
    module._REPLAY.update({"start": clock.monotonic(), "speed": 10.0})

    module._pace(2000)

    assert clock.sleeps == pytest.approx([0.2])


def test_recording_path_resolves_only_when_the_file_exists(tmp_path):
    assert capture_trial._recording_path(tmp_path, {}) is None
    assert capture_trial._recording_path(
        tmp_path, {"capture": {"recording": "demo/recording.mov"}}) is None
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "recording.mov").write_bytes(b"x")
    assert capture_trial._recording_path(
        tmp_path, {"capture": {"recording": "demo/recording.mov"}}) is not None
