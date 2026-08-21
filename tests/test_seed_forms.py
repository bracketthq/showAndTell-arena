"""Authored seed data: the form an application declares, and applying it.

The viewer renders whatever an application declares without knowing what a
message or a ticket is. These pin that boundary in both directions.
"""
import pytest

from showAndTell.applications.authoring.seedform import Field, SeedForm, describe
from tests._app_fixtures import app_manifest


def _form(**kwargs):
    return SeedForm(title="Inbox", row_noun="message", fields=(
        Field("sender", "From", required=True),
        Field("subject", "Subject", required=True),
        Field("body", "Message", kind="textarea"),
        Field("unread", "Leave unread", kind="checkbox", default=True),
    ), **kwargs)


# -- the schema ------------------------------------------------------------

def test_a_field_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown field kind"):
        Field("x", "X", kind="colour-picker")


def test_a_select_without_options_is_refused():
    with pytest.raises(ValueError, match="options"):
        Field("x", "X", kind="select")


def test_duplicate_field_names_are_refused():
    """Two fields with one name silently drop a value on the way through."""
    with pytest.raises(ValueError, match="duplicate"):
        SeedForm(title="t", row_noun="row",
                 fields=(Field("a", "A"), Field("a", "Again")))


def test_a_blank_row_carries_each_field_default():
    assert _form().blank_row() == {"sender": "", "subject": "", "body": "",
                                   "unread": True}


# -- validation ------------------------------------------------------------

def test_a_row_the_author_started_and_abandoned_is_dropped_not_refused():
    assert _form().validate([{"sender": "", "subject": "", "body": ""}]) == []


def test_a_row_missing_a_required_field_says_which_row_and_which_field():
    with pytest.raises(ValueError, match="message 1 is missing Subject"):
        _form().validate([{"sender": "Ops <ops@x.test>", "subject": ""}])


def test_an_unknown_field_is_refused_rather_than_silently_dropped():
    with pytest.raises(ValueError, match="unknown fields"):
        _form().validate([{"sender": "a", "subject": "b", "nope": "c"}])


def test_validation_normalises_whitespace_and_checkbox_types():
    rows = _form().validate([{"sender": "  Ops  ", "subject": "S", "unread": "yes"}])
    assert rows == [{"sender": "Ops", "subject": "S", "body": "", "unread": True}]


def test_a_select_value_outside_its_options_is_refused():
    form = SeedForm(title="t", row_noun="row", fields=(
        Field("status", "Status", kind="select", options=("open", "closed")),))
    with pytest.raises(ValueError, match="must be one of"):
        form.validate([{"status": "pending"}])


def test_a_checkbox_only_row_still_counts_as_blank():
    """Ticking a default box is not the author entering anything."""
    assert _form().validate([{"unread": True}]) == []


# -- opting in -------------------------------------------------------------

def test_an_application_declaring_no_form_is_simply_absent():
    class State:
        pass

    assert describe(State()) is None


def test_declaring_a_form_without_form_block_is_a_loud_error():
    """Only the application can turn its own rows into a seed block."""
    class State:
        SEED_FORM = _form()

    with pytest.raises(TypeError, match="form_block"):
        describe(State())


def test_a_form_of_the_wrong_type_is_a_loud_error():
    class State:
        SEED_FORM = {"title": "not a SeedForm"}

    with pytest.raises(TypeError, match="SeedForm"):
        describe(State())


def test_the_json_shape_is_everything_a_renderer_needs():
    payload = describe(type("S", (), {
        "SEED_FORM": _form(help="hi"), "form_block": lambda self, ctx, rows: {}})())
    assert payload["title"] == "Inbox"
    assert payload["row_noun"] == "message"
    assert payload["help"] == "hi"
    assert [f["name"] for f in payload["fields"]] == [
        "sender", "subject", "body", "unread"]
    assert payload["fields"][3]["kind"] == "checkbox"
    assert payload["blank_row"]["unread"] is True


# -- roundcube's own form --------------------------------------------------

def _roundcube_state():
    from showAndTell.applications.roundcube.state import State

    return State(app_manifest("roundcube"))


class _Ctx:
    credentials = {"email": "agent@showAndTell.test", "password": "showAndTell-mail"}


def test_every_declared_form_is_well_formed_and_roundcube_declares_one():
    """A form is opt-in, so most applications declare none.

    describe() raises on a form that is the wrong type or missing form_block,
    which is the point of walking every application rather than just one.
    """
    from showAndTell.applications.registry import default_registry

    registry = default_registry()
    declared = {name for name in registry.names()
                if registry.has_state(name)
                and describe(registry.state(name)) is not None}
    assert "roundcube" in declared


def test_a_form_row_becomes_a_message_addressed_to_the_manifest_mailbox():
    state = _roundcube_state()
    block = state.form_block(_Ctx(), state.SEED_FORM.validate([{
        "sender": "Ops <ops@acme.test>", "subject": "PO 4471 needs approval",
        "body": "Waiting on you.", "unread": True}]))
    message = block["messages"][0]
    assert block["mailboxes"][0]["email"] == "agent@showAndTell.test"
    assert message["headers"] == {"From": "Ops <ops@acme.test>",
                                  "To": "agent@showAndTell.test",
                                  "Subject": "PO 4471 needs approval"}
    assert message["body_text"] == "Waiting on you."
    # "Leave unread" ticked means the message is not seen.
    assert message["seen"] is False


def test_a_date_is_only_set_when_the_author_gives_one():
    state = _roundcube_state()
    rows = state.SEED_FORM.validate([
        {"sender": "a@x", "subject": "s", "sent_at": "2026-06-23T09:12"},
        {"sender": "b@x", "subject": "t"}])
    block = state.form_block(_Ctx(), rows)
    assert "Date" not in block["messages"][1]["headers"]
    # The picker hands over an instant; a mail Date is RFC 2822, and turning
    # one into the other is this application's business, not the form's.
    assert block["messages"][0]["headers"]["Date"] == \
        "Tue, 23 Jun 2026 09:12:00 +0000"


def test_a_picked_time_without_a_zone_is_read_as_utc():
    """datetime-local submits wall-clock text, so two machines would disagree."""
    form = SeedForm(title="t", row_noun="row",
                    fields=(Field("when", "When", kind="datetime"),))
    assert form.validate([{"when": "2026-06-23T09:12"}]) == [
        {"when": "2026-06-23T09:12:00+00:00"}]


def test_a_picked_time_that_states_its_zone_is_converted_not_ignored():
    form = SeedForm(title="t", row_noun="row",
                    fields=(Field("when", "When", kind="datetime"),))
    assert form.validate([{"when": "2026-06-23T11:12:00+02:00"}]) == [
        {"when": "2026-06-23T09:12:00+00:00"}]


def test_something_that_is_not_a_date_names_the_row_and_the_field():
    form = SeedForm(title="t", row_noun="row",
                    fields=(Field("when", "When", kind="datetime"),))
    with pytest.raises(ValueError, match="row 1: When must be a date and time"):
        form.validate([{"when": "next tuesday"}])


def test_row_ids_are_positional_so_reapplying_replaces():
    """Seeding is delete-by-id then write; stable ids are what make it idempotent."""
    state = _roundcube_state()
    rows = state.SEED_FORM.validate([{"sender": "a@x", "subject": "one"},
                                     {"sender": "b@x", "subject": "two"}])
    ids = [m["external_id"] for m in state.form_block(_Ctx(), rows)["messages"]]
    assert ids == ["authored-0001", "authored-0002"]
    again = state.form_block(_Ctx(), rows)
    assert [m["external_id"] for m in again["messages"]] == ids


# -- the authoring tab -----------------------------------------------------

def test_the_authoring_tab_is_offered_only_when_an_application_needs_one(tmp_path):
    """A capture whose apps set themselves up gets no extra tab."""
    from tests._viewer_fixture import load_capture

    store = load_capture().CaptureStore(
        tmp_path, seed_editor_base="http://127.0.0.1:8099/")

    class Workspace:
        def __init__(self, forms):
            self.session = type("S", (), {"seed_forms": lambda _self: forms})()

    assert store._seed_editor_tool("a" * 32, Workspace({})) == []
    assert store._seed_editor_tool("a" * 32, None) == []
    offered = store._seed_editor_tool("a" * 32, Workspace({"roundcube": {}}))
    assert offered == [{"label": "Starting data",
                        "url": "http://127.0.0.1:8099/captures/"
                               + "a" * 32 + "/seed"}]


def test_a_capture_with_no_viewer_address_offers_no_tab(tmp_path):
    from tests._viewer_fixture import load_capture

    store = load_capture().CaptureStore(tmp_path)

    class Workspace:
        session = type("S", (), {"seed_forms": lambda _self: {"roundcube": {}}})()

    assert store._seed_editor_tool("a" * 32, Workspace()) == []


def test_the_recorder_ignores_everything_on_a_tool_origin():
    """The author filling in starting data is setup for the setup.

    Recording it would replay as demonstrated actions and seed the data twice.
    """
    from showAndTell.capture.runtime import ManagedCapture

    capture = ManagedCapture(
        "/tmp", [{"id": "roundcube", "url": "http://127.0.0.1:8082/"}],
        tools=[{"label": "Starting data",
                "url": "http://127.0.0.1:8099/captures/x/seed"}])
    assert capture.tool_origins == {"http://127.0.0.1:8099"}


def test_no_tools_means_no_origins_to_filter():
    from showAndTell.capture.runtime import ManagedCapture

    assert ManagedCapture("/tmp", []).tool_origins == set()


def test_freezing_setup_closes_the_authoring_tabs_and_returns_focus():
    """The editor belongs to setup; it must not sit open through the demo.

    Closing it here also keeps its URL out of the replay driver, which records
    every page still open at the moment recording starts as one to reopen.
    """
    from showAndTell.capture.runtime import ManagedCapture

    class Page:
        def __init__(self, closed=False, explode=False):
            self.closed = closed
            self.fronted = False
            self._explode = explode

        def is_closed(self):
            return self.closed

        def close(self):
            if self._explode:
                raise RuntimeError("tab already gone")
            self.closed = True

        def bring_to_front(self):
            self.fronted = True

    editor, already_gone, app = Page(), Page(closed=True), Page()
    ManagedCapture._close_tool_pages([editor, already_gone], app)
    assert editor.closed is True
    assert app.fronted is True

    # A tab the author already closed is not an error, and not a reason to
    # steal focus back either.
    app = Page()
    ManagedCapture._close_tool_pages([Page(closed=True)], app)
    assert app.fronted is False

    # Nor is a tab that refuses to close a reason to abort the recording.
    app = Page()
    ManagedCapture._close_tool_pages([Page(explode=True)], app)
    assert app.fronted is False


def test_a_capture_with_no_authoring_tab_never_touches_focus():
    from showAndTell.capture.runtime import ManagedCapture

    class Page:
        fronted = False

        def bring_to_front(self):
            self.fronted = True

    app = Page()
    ManagedCapture._close_tool_pages([], app)
    assert app.fronted is False


# -- replay authentication -------------------------------------------------

class _Page:
    url = "http://app.test/login"


def _capture(tmp_path):
    from showAndTell.capture.runtime import ManagedCapture

    return ManagedCapture(tmp_path, [])


ROUNDCUBE = {"id": "roundcube", "application": "roundcube", "label": "Roundcube",
             "url": "http://app.test/",
             "credentials": {"email": "agent@showAndTell.test",
                             "password": "showAndTell-mail"}}


def test_a_replay_step_is_recorded_even_when_capture_login_fails(tmp_path, monkeypatch):
    """The bug: Roundcube replayed signed out.

    The step used to be appended only inside `if auto_login(...)`, so a
    surface whose adapter failed during capture produced a draft with no login
    step and every replay ran unauthenticated. Replay must authenticate
    because the surface requires it, not because one attempt worked.
    """
    from showAndTell.applications.browser import runtime as browser_runtime

    def explode(page, surface, **kwargs):
        raise RuntimeError("Connection to storage server failed")

    monkeypatch.setattr(browser_runtime, "auto_login", explode)
    capture = _capture(tmp_path)
    result = capture._authenticate(_Page(), ROUNDCUBE, "page2")

    assert result["status"] == "failed"
    assert "storage server" in result["error"]
    assert [e["surface_id"] for e in capture.setup_events] == ["roundcube"]
    assert capture.setup_events[0]["page"] == "page2"


def test_a_replay_step_is_recorded_when_the_operator_signs_in_by_hand(
        tmp_path, monkeypatch):
    """auto_login returning False means "I did not do it", not "not needed"."""
    from showAndTell.applications.browser import runtime as browser_runtime

    monkeypatch.setattr(browser_runtime, "auto_login",
                        lambda page, surface, **kwargs: False)
    capture = _capture(tmp_path)
    result = capture._authenticate(_Page(), ROUNDCUBE, "page")

    assert result["status"] == "manual"
    assert len(capture.setup_events) == 1


def test_a_successful_login_records_the_same_step(tmp_path, monkeypatch):
    from showAndTell.applications.browser import runtime as browser_runtime

    monkeypatch.setattr(browser_runtime, "auto_login",
                        lambda page, surface, **kwargs: True)
    capture = _capture(tmp_path)
    assert capture._authenticate(_Page(), ROUNDCUBE, "page")["status"] == "signed-in"
    assert len(capture.setup_events) == 1


def test_a_demonstrated_login_stays_visible_for_recording(tmp_path, monkeypatch):
    from showAndTell.applications.browser import runtime as browser_runtime

    attempts = []
    monkeypatch.setattr(browser_runtime, "auto_login",
                        lambda *args, **kwargs: attempts.append((args, kwargs)))
    surface = dict(ROUNDCUBE, login_replay="demonstrated")
    capture = _capture(tmp_path)

    result = capture._authenticate(_Page(), surface, "page")

    assert result["status"] == "manual"
    assert attempts == []
    assert capture.setup_events[0]["replay_mode"] == "demonstrated"


def test_a_surface_with_no_credentials_records_no_login_step(tmp_path):
    """ONLYOFFICE authenticates by URL; a login step would be noise."""
    capture = _capture(tmp_path)
    result = capture._authenticate(_Page(), {
        "id": "onlyoffice", "label": "Docs", "url": "http://x/",
        "credentials": {"email": "", "password": ""}}, "page3")

    assert result == {"id": "onlyoffice", "required": False,
                      "status": "not-required"}
    assert capture.setup_events == []


def test_every_surface_signs_in_at_its_own_live_address(monkeypatch):
    """The bug behind "Roundcube does not auto-login on replay", part two.

    Only the primary was handed app_url and creds; a supporting application
    fell back to the address and credentials it had when the demonstration was
    captured. A task captured on the VM then tried to sign into the VM even
    when replayed locally.
    """
    from showAndTell.applications.browser import runtime as browser_runtime
    from showAndTell.applications.browser.context import CONTEXT_KEY

    seen = []
    monkeypatch.setattr(browser_runtime, "browser_plane", lambda application: type(
        "A", (), {"login": staticmethod(
            lambda page, base, creds: seen.append((base, creds)))})())

    runtime_creds = {
        "email": "Administrator", "password": "showAndTell-admin",
        CONTEXT_KEY: {
            "erpnext": {"url": "http://127.0.0.1:9080",
                        "credentials": {"email": "Administrator",
                                        "password": "showAndTell-admin"}},
            "roundcube": {"url": "http://127.0.0.1:9082",
                          "credentials": {"email": "agent@showAndTell.test",
                                          "password": "showAndTell-mail"}},
        },
    }
    captured = {"id": "roundcube", "application": "roundcube",
                "url": "http://203.0.113.10:8082/",
                "credentials": {"email": "stale@old.test", "password": "stale"}}

    assert browser_runtime.auto_login(
        object(), captured, app_url="http://127.0.0.1:9080",
        credentials=runtime_creds) is True

    base, creds = seen[-1]
    assert base == "http://127.0.0.1:9082"          # not the VM, not ERPNext's
    assert creds["email"] == "agent@showAndTell.test"  # not the primary's
    # The reserved context block is plumbing; an adapter must never see it.
    assert CONTEXT_KEY not in creds


def test_without_a_runtime_context_the_captured_surface_still_works(monkeypatch):
    """Older drafts and the capture-time call pass no context at all."""
    from showAndTell.applications.browser import runtime as browser_runtime

    seen = []
    monkeypatch.setattr(browser_runtime, "browser_plane", lambda application: type(
        "A", (), {"login": staticmethod(
            lambda page, base, creds: seen.append((base, creds)))})())

    assert browser_runtime.auto_login(object(), {
        "id": "roundcube", "application": "roundcube",
        "url": "http://127.0.0.1:8082/",
        "credentials": {"email": "agent@showAndTell.test",
                        "password": "showAndTell-mail"}}) is True
    assert seen[-1][0] == "http://127.0.0.1:8082"


def test_the_generated_driver_signs_every_surface_in_the_same_way():
    from showAndTell.demonstration.compiler import render_demonstrate

    surfaces = [
        {"id": "erpnext", "application": "erpnext", "url": "http://a/", "credentials": {}},
        {"id": "roundcube", "application": "roundcube", "url": "http://b/", "credentials": {}},
    ]
    source = render_demonstrate([], surfaces, [
        {"type": "login", "page": "page", "surface_id": "erpnext"},
        {"type": "login", "page": "page2", "surface_id": "roundcube"},
    ])
    logins = [line.strip() for line in source.splitlines() if "_auto_login(" in line]
    # seed() and demonstrate() both authenticate.  The latter creates fresh
    # supporting tabs, and it still has to work when seed() is skipped because
    # a state snapshot was restored.
    assert len(logins) == 4
    assert sum("app_url=app_url" in line for line in logins) == 2
    assert sum("app_url=None" in line for line in logins) == 2
    assert all("_login_credentials(creds," in line for line in logins), logins


def test_recorded_stage_reauthenticates_supporting_pages_after_snapshot_restore():
    """A snapshot must not leave Roundcube's fresh replay tab signed out."""
    from showAndTell.demonstration.compiler import render_demonstrate

    surfaces = [
        {"id": "erpnext", "application": "erpnext", "url": "http://a/",
         "credentials": {"email": "admin", "password": "erp-pass"}},
        {"id": "roundcube", "application": "roundcube", "url": "http://b/",
         "credentials": {"email": "agent@example.test", "password": "mail-pass"}},
    ]
    setup = [
        {"type": "login", "page": "page", "surface_id": "erpnext"},
        {"type": "login", "page": "page2", "surface_id": "roundcube"},
    ]
    events = [{"type": "goto", "page": "page2", "url": "http://b/?_task=mail"}]

    source = render_demonstrate(events, surfaces, setup)
    demonstrate = source.split("def demonstrate(", 1)[1]

    login = "_auto_login(pages['page2'], _CAPTURED_SURFACES['roundcube']"
    goto = (
        "pages['page2'].goto(_runtime_application_url(creds, 'roundcube', "
        "'http://b/?_task=mail', _CAPTURED_SURFACES['roundcube']['url']), "
        "wait_until='domcontentloaded')"
    )
    assert login in demonstrate
    assert demonstrate.index(login) < demonstrate.index(goto)
    assert "_login_credentials(creds, 'page2')" in demonstrate


def test_one_applications_snapshot_does_not_suppress_rebuilding_the_others(
        tmp_path):
    """The bug behind "the seeded data was not there".

    A draft with an ERPNext tar restored that tar, and the truthy result then
    skipped reset+prepare for EVERY application — so Roundcube replayed with
    whatever the previous session had left in its mailbox.
    """
    from tests._viewer_fixture import load_capture

    calls = []

    class Session:
        applications = ("erpnext", "onlyoffice", "roundcube")
        registry = None

        def restore_snapshots(self, state_dir):
            return ["erpnext"]                      # only ERPNext carries one

        def reset(self, *, only=None, coarse=True):
            calls.append(("reset", tuple(only or ())))

        def prepare(self, *, only=None):
            calls.append(("prepare", tuple(only or ())))

        def use_demo_root(self, root):
            pass

        def seed(self, document):
            calls.append(("seed", tuple(sorted(document))))

    workspace = load_capture().CaptureWorkspace.__new__(
        load_capture().CaptureWorkspace)
    workspace.metadata = {"slug": "test-00"}
    workspace.legacy = []
    workspace.session = Session()
    workspace.restore(use_export=True, state_dir=tmp_path)

    rebuilt = dict((kind, apps) for kind, apps in calls if kind != "seed")
    assert rebuilt["reset"] == ("onlyoffice", "roundcube")
    assert rebuilt["prepare"] == ("onlyoffice", "roundcube")
    # ERPNext is not reset: its snapshot IS the starting state.
    assert "erpnext" not in rebuilt["reset"]


# -- roundcube login idempotence -------------------------------------------

class _Locator:
    def __init__(self, count=0, visible=False, on_click=None, text=""):
        self._count, self._visible = count, visible
        self._on_click, self._text = on_click, text
        self.first = self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def click(self):
        if self._on_click:
            self._on_click()

    def fill(self, value):
        pass

    def wait_for(self, **kwargs):
        pass

    def inner_text(self):
        return self._text


class _RoundcubePage:
    """A page that is already authenticated: no login form, a logout link."""

    def __init__(self, form_visible):
        self.form_visible = form_visible
        self.logged_out = False
        self.waited = []

    def goto(self, url, **kwargs):
        pass

    def locator(self, selector):
        from showAndTell.applications.roundcube import browser as plane

        if selector == plane.LOGIN_USER:
            return _Locator(count=1 if self.form_visible else 0,
                            visible=self.form_visible)
        if selector == plane.LOGOUT:
            def out():
                self.logged_out = True
            return _Locator(count=1, visible=True, on_click=out)
        if selector == plane.ACCOUNT:
            return _Locator(count=1, text="agent@showAndTell.test")
        return _Locator(count=1, visible=True)

    def wait_for_load_state(self, state):
        self.waited.append(state)

    def get_by_role(self, role, **kwargs):
        return _Locator(count=1, visible=True)


CREDS = {"email": "agent@showAndTell.test", "password": "showAndTell-mail"}


def test_roundcube_replay_url_discards_only_transient_compose_session_ids():
    from showAndTell.applications.roundcube import browser as plane

    assert plane.normalize_replay_url(
        "http://rc.test/?_task=mail&_action=compose&_id=stale&draft=1#editor"
    ) == "http://rc.test/?_task=mail&_action=compose&draft=1#editor"
    assert plane.normalize_replay_url(
        "http://rc.test/?_task=mail&_action=show&_id=message"
    ) == "http://rc.test/?_task=mail&_action=show&_id=message"


def test_roundcube_replay_url_without_a_session_id_is_a_byte_identical_no_op():
    from showAndTell.applications.roundcube import browser as plane

    url = "http://rc.test/?_task=mail&_action=compose&_to=a@b.com&_subject=hello%20world"
    assert plane.normalize_replay_url(url) == url


def test_signing_in_twice_does_not_sign_out_in_between():
    """A trial calls login when it opens the surface and again for setup.

    Treating "no login form" as "somebody else is signed in" made the second
    call log out and then wait for a form that never came — a 30s timeout that
    failed the whole replay.
    """
    from showAndTell.applications.roundcube import browser as plane

    page = _RoundcubePage(form_visible=False)
    plane.login(page, "http://rc.test", CREDS)
    assert page.logged_out is False


def test_a_visible_form_is_still_filled_in():
    from showAndTell.applications.roundcube import browser as plane

    page = _RoundcubePage(form_visible=True)
    plane.login(page, "http://rc.test", CREDS)
    assert page.logged_out is False


def test_waiting_for_mail_settles_the_folder_rather_than_its_visibility():
    """Both traps at once: hidden when empty, attached before it has loaded."""
    from showAndTell.applications.roundcube import browser as plane

    page = _RoundcubePage(form_visible=False)
    plane.login(page, "http://rc.test", CREDS)
    # networkidle, not a visibility wait -- an empty mailbox is never visible,
    # and an attached-only wait returns before any row exists.
    assert page.waited == ["networkidle"]


def test_roundcube_ready_waits_for_the_wired_compose_recipient_widget():
    """A compose URL is driveable only once compose JS has built its widgets.

    Every other compose control sits in the server-rendered document long
    before the page can be driven; the recipient tokenizer input is the one
    control that compose init itself creates, so it marks the init finished.
    """
    from showAndTell.applications.roundcube import browser as plane

    calls = []

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def wait_for(self, *, state, timeout):
            calls.append((self.selector, state, timeout))

    class Page:
        url = "http://rc.test/?_task=mail&_action=compose&_id=fresh"

        def locator(self, selector):
            return Locator(selector)

    plane.ready(Page())

    assert calls == [(plane.COMPOSE_RECIPIENT_INPUT, "visible", 60_000)]


def test_roundcube_ready_settles_folder_views_and_only_folder_views():
    """Mail folder routes settle like login does; a message opened as its own
    page has no folder list to wait on, so no gate may run there."""
    from showAndTell.applications.roundcube import browser as plane

    class Page(_RoundcubePage):
        url = "http://rc.test/?_task=mail&_mbox=INBOX"

    page = Page(form_visible=False)
    plane.ready(page)
    assert page.waited == ["networkidle"]

    class ShowPage(_RoundcubePage):
        url = "http://rc.test/?_task=mail&_action=show&_uid=7&_mbox=INBOX"

        def locator(self, selector):
            raise AssertionError("a full-page message has no folder gate")

    plane.ready(ShowPage(form_visible=False))


def test_roundcube_ready_leaves_non_mail_tasks_alone():
    """Settings and addressbook render no #messagelist; waiting on the mail
    gate there is a 30s hang followed by a hard replay failure."""
    from showAndTell.applications.roundcube import browser as plane

    class Page:
        url = "http://rc.test/?_task=settings&_action=preferences"

        def locator(self, selector):
            raise AssertionError("no selector wait belongs on a settings view")

        def wait_for_load_state(self, state):
            raise AssertionError("no load-state wait belongs on a settings view")

    plane.ready(Page())
