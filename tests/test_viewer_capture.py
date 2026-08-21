"""Task Inspector new-testcase capture storage and HTTP boundary."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import os
import threading
import urllib.error
import urllib.request

import pytest

from tests._viewer_fixture import load_serve, write_task
from tests._app_fixtures import stub_state_planes


@pytest.fixture(autouse=True)
def _no_live_data_planes(monkeypatch):
    """This module covers the viewer's orchestration, not the showAndTell.applications.

    The real data planes reach live containers over HTTP and IMAP, so leaving
    them in would make every test here a Docker test.
    """
    stub_state_planes(monkeypatch)


class DummyKeeper:
    """Stands in for LeaseKeeper: no-op, since these tests never call start()."""

    def stop(self):
        pass


class RecordingHostClient:
    """Records the snapshot/restore calls a fixture host agent client takes."""

    def __init__(self):
        self.calls: list[tuple] = []

    def snapshot(self, app, name, *, lease_id=None):
        self.calls.append(("snapshot", app, name))

    def pull_snapshot(self, app, name, dest):
        self.calls.append(("pull_snapshot", app, name, str(dest)))
        return dest

    def push_snapshot(self, app, name, source, *, lease_id=None):
        self.calls.append(("push_snapshot", app, name, str(source)))

    def restore_snapshot(self, app, name, *, lease_id=None):
        self.calls.append(("restore_snapshot", app, name))

    @contextlib.contextmanager
    def lease_scope(self, apps, holder):
        yield "lease-1"


def test_capture_workspace_starts_clean_app_and_exports_seed(monkeypatch):
    serve = load_serve()
    import showAndTell.applications.session as session_module

    calls = []

    class Session:
        applications = ("kiwix",)
        credentials = {"kiwix": {}}
        lease_id = "lease-test"
        registry = type("Registry", (), {"has_state": lambda self, name: False})()

        def __init__(self, applications, **kwargs):
            assert tuple(applications) == self.applications

        def start(self, *, ensure_started=None):
            calls.append(("start",))

        def reset(self, *, only=None, coarse=True) -> None:
            calls.append(("reset",))

        def prepare(self, *, only=None):
            calls.append(("prepare",))

        def export(self):
            calls.append(("export",))
            return {"kiwix": {"articles": [{"title": "Seeded"}]}}

        def restore_snapshots(self, state_dir):
            return []

        def seed(self, value):
            calls.append(("seed", value))

        def surface_url(self, name):
            return "http://127.0.0.1:43123/"

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(session_module, "AppSession", Session)
    metadata = serve.capture.validate_start({
        "slug": "clean-seed", "title": "Clean seed", "applications": ["kiwix"],
        "mode": "managed",
    })
    workspace = serve.capture.CaptureWorkspace(metadata)
    surfaces = workspace.start()
    assert surfaces[0]["url"] == "http://127.0.0.1:43123/"
    assert calls.index(("start",)) < calls.index(("reset",))
    seed, exported = workspace.export()
    assert exported is True and seed["kiwix"]["articles"][0]["title"] == "Seeded"
    workspace.restore(use_export=False)
    assert calls[-2:] == [("reset",), ("prepare",)]
    workspace.restore(use_export=True)
    assert calls[-1] == ("prepare",)
    workspace.close()
    assert calls[-1] == ("close",)


def test_capture_workspace_opens_onlyoffice_blank_workbook_editor(monkeypatch):
    """Docs is only editable once a workbook exists, and the surface must open it.

    The blank workbook is the ONLYOFFICE data plane's job; the viewer's job is
    to point the browser at the editor page that plane reports.
    """
    serve = load_serve()
    from showAndTell.applications.registry import Registry

    calls: list[tuple] = []

    class OnlyOfficeState:
        application = "onlyoffice"

        def __init__(self, manifest):
            self.manifest = manifest
            self.document_ids = ()

        def prepare(self, ctx):
            calls.append(("prepare", "onlyoffice"))
            self.document_ids = ("task-setup",)

        def reset(self, ctx):
            calls.append(("reset", "onlyoffice"))

        def seed(self, ctx, block):
            pass

        def export(self, ctx):
            return {"documents": []}

        def editor_url(self, ctx):
            if not self.document_ids:
                raise RuntimeError("no workbook registered")
            return f"http://127.0.0.1:49090/onlyoffice/editor/{self.document_ids[0]}"

    state = OnlyOfficeState(None)

    def _state(self, name, **kwargs):
        state.manifest = self.manifest(name)
        return state

    monkeypatch.setattr(Registry, "state", _state)

    class FakeHostClient:
        host = "127.0.0.1"

        def health(self):
            return {"apps": ["onlyoffice"]}

        def status(self, app):
            return {"state": "healthy", "ports": {"app": 1}}

        def acquire_lease(self, apps, holder, ttl_s=900.0):
            return "lease-blank-sheet"

        def heartbeat(self, lease_id):
            pass

        def release(self, lease_id):
            pass

        def app_url(self, app):
            return f"http://127.0.0.1:1/{app}"

        def secrets(self, app):
            return {"connector_token": "fake-connector-token"}

        def reset(self, app, *, lease_id=None):
            calls.append(("host-reset", app))

    monkeypatch.setattr(
        serve.capture.fixture_host, "client_for_capture",
        lambda: serve.capture.fixture_host.CaptureClient(
            FakeHostClient(), is_local=False))

    metadata = serve.capture.validate_start({
        "slug": "blank-sheet", "title": "Blank sheet",
        "applications": ["onlyoffice"], "mode": "managed",
    })
    workspace = serve.capture.CaptureWorkspace(metadata)
    surfaces = workspace.start()

    assert surfaces[0]["url"] == (
        "http://127.0.0.1:49090/onlyoffice/editor/task-setup")
    assert surfaces[0]["label"] == "ONLYOFFICE spreadsheet"
    # The coarse driver reset runs before the workbook is planted, or the
    # operator inherits the previous capture's sheet.
    assert calls.index(("host-reset", "onlyoffice")) < \
        calls.index(("prepare", "onlyoffice"))
    workspace.close()


def test_begin_recording_flushes_workbooks_into_the_seed_before_exporting(
        tmp_path, monkeypatch):
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    order: list[str] = []

    class FakeManagedCapture:
        login_results = []
        voice_recorded = False

        def __init__(self, directory, surfaces, **kwargs):
            pass

        def start(self):
            pass

        def prepare_freeze(self, timeout=0):
            order.append("browser-freeze")
            return [{"application": "onlyoffice", "url": "http://sheet.test"}]

        def begin_recording(self, timeout=0):
            return {"voice_recorded": False}

        def stop(self, timeout=0):
            return {"recording": None, "started_ms": 1,
                    "setup_events": [], "events": []}

    class FakeWorkspace:
        def __init__(self, metadata):
            self.metadata = metadata

        def start(self):
            return self.metadata["surfaces"]

        def capture_state(self, directory):
            order.append("capture")
            return {"onlyoffice": {"workbooks": [{
                "document_id": "task-setup", "title": "Task Setup.xlsx",
                "format": "xlsx", "asset": "onlyoffice/task-setup.xlsx",
                "sha256": "b" * 64,
            }]}}

        def validate_live_surfaces(self, surfaces):
            assert surfaces == [
                {"application": "onlyoffice", "url": "http://sheet.test"}
            ]
            order.append("validate-live")

        def export(self):
            order.append("export")
            return {"onlyoffice": {"documents": [{"document_id": "task-setup"}]}}, True

        def restore(self, *, use_export, state_dir=None, snapshot_only=False):
            pass

        def close(self):
            pass

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=FakeWorkspace)
    session = store.start({
        "slug": "sheet-draft", "title": "Sheet draft",
        "applications": ["onlyoffice"], "mode": "managed",
    })

    store.begin_recording(session["id"])

    # The flush has to land before the export, or the export fingerprints the
    # workbook as it was before the operator's edits were saved.
    assert order == ["browser-freeze", "validate-live", "capture", "export"]
    store.finish(session["id"], {
        "duration_ms": 10, "voice_recorded": False,
        "transcript_supported": False, "steps": [],
    })
    seed = json.loads(
        (tmp_path / "task-drafts/sheet-draft/demo/seed.json").read_text())
    assert seed["onlyoffice"]["workbooks"] == [{
        "document_id": "task-setup", "title": "Task Setup.xlsx",
        "format": "xlsx", "asset": "onlyoffice/task-setup.xlsx",
        "sha256": "b" * 64,
    }]


def test_capture_refuses_a_browser_on_another_onlyoffice_replica():
    serve = load_serve()
    workspace = serve.capture.CaptureWorkspace(
        {"slug": "sheet-draft", "surfaces": []})

    class State:
        @staticmethod
        def connector_url(ctx):
            return "http://fixture.test:8086"

    class Session:
        applications = ("onlyoffice",)

        @staticmethod
        def state(name):
            assert name == "onlyoffice"
            return State()

        @staticmethod
        def context(name):
            assert name == "onlyoffice"
            return type("Context", (), {"url": "http://fixture.test:8081"})()

    workspace.session = Session()
    workspace._onlyoffice_documents = {
        "task-setup": {"document_id": "task-setup", "sha256": "a" * 64},
    }

    with pytest.raises(RuntimeError, match="different fixture replica"):
        workspace.validate_live_surfaces([{
            "application": "onlyoffice",
            "url": ("http://fixture.test:8181/web-apps/brackett-host/"
                    "editor.html?doc=task-setup"),
        }])


def test_capture_allows_an_intentionally_empty_onlyoffice_workbook(tmp_path):
    serve = load_serve()
    workspace = serve.capture.CaptureWorkspace(
        {"slug": "empty-sheet", "surfaces": []})
    empty = {"onlyoffice": {"workbooks": [{
        "document_id": "task-setup",
        "asset": "onlyoffice/task-setup.xlsx",
        "sha256": "a" * 64,
    }]}}

    class State:
        @staticmethod
        def connector_url(ctx):
            return "http://fixture.test:8086"

    class Session:
        applications = ("onlyoffice",)

        @staticmethod
        def state(name):
            return State()

        @staticmethod
        def context(name):
            return type("Context", (), {"url": "http://fixture.test:8081"})()

        @staticmethod
        def capture(directory):
            return empty

    workspace.session = Session()
    workspace._onlyoffice_documents = {
        "task-setup": {"document_id": "task-setup", "sha256": "a" * 64},
    }
    workspace.validate_live_surfaces([{
        "application": "onlyoffice",
        "url": ("http://fixture.test:8081/web-apps/brackett-host/"
                "editor.html?doc=task-setup"),
        "title": "Task Setup.xlsx",
    }])

    assert workspace.capture_state(tmp_path / "demo") == empty


def _draft_demo_with_workbook(tmp_path, payload=b"PK\x03\x04captured", digest=None):
    demo = tmp_path / "demo"
    (demo / "onlyoffice").mkdir(parents=True, exist_ok=True)
    (demo / "onlyoffice" / "task-setup.xlsx").write_bytes(payload)
    (demo / "seed.json").write_text(json.dumps({
        "onlyoffice": {
            "documents": [{"document_id": "task-setup"}],
            "workbooks": [{
                "document_id": "task-setup", "title": "Task Setup.xlsx",
                "format": "xlsx", "asset": "onlyoffice/task-setup.xlsx",
                "sha256": digest or hashlib.sha256(payload).hexdigest(),
            }],
        },
    }), encoding="utf-8")
    return demo


class _WorkbookState:
    """Stands in for applications/onlyoffice/state.py, recording registrations."""

    application = "onlyoffice"

    def __init__(self, registered):
        self.registered = registered

    def prepare(self, ctx):
        # A rebuilt runtime re-creates the blank starter workbook; the captured
        # one has to land after it, which is what the ordering test asserts.
        self.registered.append({"workbooks": [{"document_id": "task-setup",
                                               "source": {}}]})

    def reset(self, ctx):
        pass

    def seed(self, ctx, block):
        self.registered.append({"workbooks": [
            {"document_id": row["document_id"],
             "filename": row.get("title") or f"{row['document_id']}.xlsx",
             "content": ctx.asset(row["asset"], row.get("sha256", ""))}
            if row.get("asset") else dict(row)
            for row in block.get("workbooks", [])]})

    def export(self, ctx):
        return {"documents": []}


class _WorkbookSession:
    """The slice of AppSession that CaptureWorkspace.restore actually drives."""

    def __init__(self, registered):
        self.applications = ("onlyoffice",)
        self.demo_root = None
        self.resets = 0
        self._state = _WorkbookState(registered)

    def state(self, name):
        return self._state

    def use_demo_root(self, root):
        self.demo_root = None if root is None else pathlib.Path(root)

    def _context(self):
        from showAndTell.applications.session import AppContext
        from showAndTell.applications.registry import default_registry

        return AppContext(manifest=default_registry().manifest("onlyoffice"),
                          url="http://127.0.0.1:49090", credentials={},
                          demo_root=self.demo_root)

    def restore_snapshots(self, state_dir):
        return []

    def reset(self, *, only=None, coarse=True):
        self.resets += 1

    def prepare(self, *, only=None):
        self._state.prepare(self._context())

    def seed(self, document):
        for name, block in document.items():
            if name in self.applications:
                self._state.seed(self._context(), block)

    def close(self):
        pass


def _workbook_workspace(serve, registered, *, surfaces=()):
    workspace = serve.capture.CaptureWorkspace(
        {"slug": "sheet-draft", "surfaces": list(surfaces)})
    workspace.session = _WorkbookSession(registered)
    return workspace


def test_restore_puts_the_captured_workbook_back_after_a_state_snapshot(
        tmp_path, monkeypatch):
    # The ERP snapshot covers only ERPNext; the sheet lives beside it as a file
    # and must come back even though the snapshot restore reported success.
    serve = load_serve()
    demo = _draft_demo_with_workbook(tmp_path)
    registered: list[dict] = []
    workspace = _workbook_workspace(serve, registered)
    monkeypatch.setattr(
        workspace.session, "restore_snapshots", lambda state_dir: ["erpnext"])

    workspace.restore(use_export=True, state_dir=demo)

    # ONLYOFFICE carries no snapshot of its own, so it is rebuilt -- blank
    # starter first -- and the captured sheet must land on top of that, last.
    assert registered[-1]["workbooks"][0]["content"] == b"PK\x03\x04captured"
    assert registered[-1]["workbooks"][0]["filename"] == "Task Setup.xlsx"


def test_restore_reapplies_the_captured_workbook_over_the_blank_starter(tmp_path):
    # Rebuilding a runtime re-creates the blank capture workbook, so the
    # captured one has to be written after it, not before.
    serve = load_serve()
    demo = _draft_demo_with_workbook(tmp_path)
    registered: list[dict] = []
    surfaces = [{"application": "onlyoffice", "url": "http://old.test"}]
    workspace = _workbook_workspace(serve, registered, surfaces=surfaces)

    workspace.restore(use_export=False, state_dir=demo)

    assert [sorted(block["workbooks"][0].keys() & {"source", "content"})
            for block in registered] == [["source"], ["content"]]


def test_restore_refuses_a_captured_workbook_whose_bytes_no_longer_match(tmp_path):
    serve = load_serve()
    demo = _draft_demo_with_workbook(tmp_path, digest="0" * 64)
    registered: list[dict] = []
    workspace = _workbook_workspace(serve, registered)

    with pytest.raises(ValueError, match="sha256"):
        workspace.restore(use_export=True, state_dir=demo)


def test_restore_refuses_a_workbook_asset_that_escapes_the_draft(tmp_path):
    # Assets are read from the draft the same way a task seed reads its own,
    # so they get the same containment guarantee.
    serve = load_serve()
    demo = _draft_demo_with_workbook(tmp_path)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"PK\x03\x04elsewhere")
    seed = json.loads((demo / "seed.json").read_text())
    seed["onlyoffice"]["workbooks"][0].update({
        "asset": "../outside.xlsx",
        "sha256": hashlib.sha256(b"PK\x03\x04elsewhere").hexdigest(),
    })
    (demo / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    registered: list[dict] = []
    workspace = _workbook_workspace(serve, registered)

    with pytest.raises(ValueError, match="escapes|invalid task asset path"):
        workspace.restore(use_export=True, state_dir=demo)

    # The blank starter is fine; the workbook pointing outside the draft is not.
    assert not any("content" in block["workbooks"][0] for block in registered)


def test_restore_without_captured_workbooks_registers_only_the_blank_starter(
        tmp_path):
    serve = load_serve()
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "seed.json").write_text(json.dumps({"erpnext": {"items": []}}))
    registered: list[dict] = []
    workspace = _workbook_workspace(serve, registered)

    workspace.restore(use_export=True, state_dir=demo)

    # The blank starter comes back so the surface stays editable; what must not
    # appear is a captured workbook, because the draft carries none.
    assert [sorted(block["workbooks"][0].keys() & {"source", "content"})
            for block in registered] == [["source"]]


def test_captured_workbook_descriptors_are_the_ones_restore_consumes(
        tmp_path, monkeypatch):
    """Capture writes the draft's workbook records; restore reads them back.

    Nothing else pins these two halves together, so a rename on either side
    would strand the sheet again without failing any single-sided test.
    """
    serve = load_serve()
    payload = b"PK\x03\x04operator-sheet"
    registered: list[dict] = []

    class CapturingSession(_WorkbookSession):
        def capture(self, directory):
            target = pathlib.Path(directory) / "onlyoffice"
            target.mkdir(parents=True, exist_ok=True)
            (target / "task-setup.xlsx").write_bytes(payload)
            return {"onlyoffice": {"workbooks": [{
                "document_id": "task-setup", "title": "Task Setup.xlsx",
                "format": "xlsx", "asset": "onlyoffice/task-setup.xlsx",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }]}}

    workspace = serve.capture.CaptureWorkspace(
        {"slug": "sheet-draft", "surfaces": []})
    workspace.session = CapturingSession(registered)
    demo = tmp_path / "demo"

    captured = workspace.capture_state(demo)
    # The draft on disk is all a later trial has: a fresh viewer process reads
    # these records from seed.json, not from a live session.
    demo.mkdir(parents=True, exist_ok=True)
    (demo / "seed.json").write_text(json.dumps(captured), encoding="utf-8")
    monkeypatch.setattr(
        workspace.session, "restore_snapshots", lambda state_dir: ["erpnext"])
    workspace.restore(use_export=True, state_dir=demo)

    assert registered[-1]["workbooks"][0]["content"] == payload


def test_force_start_breaks_stale_lease_before_acquiring(monkeypatch):
    """force:true must clear whatever lease blocks the stack, then acquire."""
    serve = load_serve()
    class ForcedHostClient:
        # AppSession reads this for services that are not addressed by URL.
        host = "127.0.0.1"

        def status(self, app):
            return {"state": "healthy", "ports": {"app": 1}, "snapshots": ["golden"]}


        def reset(self, app, *, lease_id=None):

            pass

        def __init__(self):
            self.calls: list[tuple] = []

        def force_release(self, apps):
            self.calls.append(("force_release", list(apps)))
            return [{"holder": "zombie", "apps": list(apps)}]

        def acquire_lease(self, apps, holder, ttl_s=900.0):
            self.calls.append(("acquire", list(apps)))
            return "lease-forced"

        def app_url(self, app):
            return f"http://127.0.0.1:1/{app}"

        def secrets(self, app):
            return {}

        def heartbeat(self, lease_id):
            pass

        def release(self, lease_id):
            pass

    fake_client = ForcedHostClient()
    monkeypatch.setattr(
        serve.capture.fixture_host, "client_for_capture",
        lambda: serve.capture.fixture_host.CaptureClient(
            fake_client, is_local=False))

    metadata = serve.capture.validate_start({
        "slug": "forced-start", "title": "Forced start",
        "applications": ["erpnext"], "mode": "managed", "force": True,
    })
    assert metadata["force"] is True
    workspace = serve.capture.CaptureWorkspace(metadata)
    try:
        workspace.start()
        assert fake_client.calls == [
            ("force_release", ["erpnext"]), ("acquire", ["erpnext"])]
    finally:
        workspace.close()


def test_host_busy_start_surfaces_structured_conflict(monkeypatch):
    """A held stack must fail as 409 host_busy naming the holder, not a 500."""
    serve = load_serve()
    from showAndTell.applications.host.client import FixtureHostBusy

    class BusyHostClient:
        # AppSession reads this for services that are not addressed by URL.
        host = "127.0.0.1"

        def status(self, app):
            return {"state": "healthy", "ports": {"app": 1}, "snapshots": ["golden"]}


        def reset(self, app, *, lease_id=None):

            pass

        def __init__(self):
            self.forced = False

        def force_release(self, apps):
            self.forced = True

        def acquire_lease(self, apps, holder, ttl_s=900.0):
            raise FixtureHostBusy("capture-sap-order", 1234.5)

    fake_client = BusyHostClient()
    monkeypatch.setattr(
        serve.capture.fixture_host, "client_for_capture",
        lambda: serve.capture.fixture_host.CaptureClient(
            fake_client, is_local=False))

    metadata = serve.capture.validate_start({
        "slug": "busy-start", "title": "Busy start",
        "applications": ["erpnext"], "mode": "managed",
    })
    assert metadata["force"] is False
    workspace = serve.capture.CaptureWorkspace(metadata)
    with pytest.raises(serve.capture.CaptureError) as excinfo:
        workspace.start()
    assert excinfo.value.status == 409
    assert excinfo.value.details == {
        "code": "host_busy", "held_by": "capture-sap-order", "since": 1234.5}
    assert "capture-sap-order" in str(excinfo.value)
    assert fake_client.forced is False  # force must stay opt-in


def test_start_conflict_reaches_the_browser_with_structured_fields(
        tmp_path, monkeypatch):
    serve = load_serve()
    write_task(tmp_path, "alpha-one")
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(serve.generate, "CACHE_DIR", tmp_path / "runs/.cache")
    server = serve.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    class BusyStore:
        def start(self, payload):
            raise serve.capture.CaptureError(
                "fixture host busy: held by capture-sap-order", 409,
                details={"code": "host_busy", "held_by": "capture-sap-order",
                         "since": 1234.5})

        def close(self):
            pass

    server.capture_store = BusyStore()
    try:
        status, body = _request(url, server.question_edit_token, "/api/captures", {
            "slug": "busy-http", "title": "Busy", "applications": ["gitlab"],
        })
        assert status == 409
        assert body["code"] == "host_busy"
        assert body["held_by"] == "capture-sap-order"
        assert body["since"] == 1234.5
        assert "busy" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


class BootstrappingHostClient:
    """Stands in for a fixture host agent client, tracking cold-start calls.

    ``state``/``snapshots`` model one app's (erpnext's) reported status;
    ``start``/``baseline``/``golden`` mutate them exactly as the real agent's
    driver would, so a test can assert on both the calls made and the
    resulting state.
    """
    # AppSession reads this for services that are not addressed by URL.
    host = "127.0.0.1"


    def __init__(self, *, state="unreachable", snapshots=()):
        self.calls: list[tuple] = []
        self._state = state
        self._snapshots = list(snapshots)

    def acquire_lease(self, apps, holder, ttl_s=900.0):
        self.calls.append(("acquire_lease", tuple(apps)))
        return "lease-cold-start"

    def heartbeat(self, lease_id):
        pass

    def release(self, lease_id):
        pass

    def app_url(self, app):
        return f"http://127.0.0.1:1/{app}"

    def connector_url(self):
        return "http://127.0.0.1:2"

    def secrets(self, app):
        return {"connector_token": "fake-connector-token"}

    def reset(self, app, *, lease_id=None):
        self.calls.append(("reset", app))

    def status(self, app):
        self.calls.append(("status", app))
        return {"state": self._state, "snapshots": list(self._snapshots)}

    def start(self, app, *, wait=True):
        self.calls.append(("start", app, wait))
        self._state = "healthy"

    def baseline(self, app, *, lease_id=None):
        self.calls.append(("baseline", app))

    def golden(self, app, *, lease_id=None):
        self.calls.append(("golden", app))
        self._snapshots.append("golden")


def _cold_start_workspace(monkeypatch, fake_client, *, is_local,
                          application="erpnext"):
    """A minimal snapshot-backed application capture wired to fake_client."""
    serve = load_serve()
    monkeypatch.setattr(
        serve.capture.fixture_host, "client_for_capture",
        lambda: serve.capture.fixture_host.CaptureClient(
            fake_client, is_local=is_local))
    metadata = serve.capture.validate_start({
        "slug": "cold-start", "title": "Cold start",
        "applications": [application], "mode": "managed",
    })
    return serve.capture.CaptureWorkspace(metadata)


def test_local_cold_start_starts_unhealthy_app_then_bakes_missing_golden(monkeypatch):
    """First local run: start(wait=True), then baseline+golden when golden is missing."""
    fake_client = BootstrappingHostClient(state="unreachable", snapshots=())
    workspace = _cold_start_workspace(monkeypatch, fake_client, is_local=True)
    try:
        workspace.start()
        assert ("start", "erpnext", True) in fake_client.calls
        assert ("baseline", "erpnext") in fake_client.calls
        assert ("golden", "erpnext") in fake_client.calls
        # start must precede baseline/golden: baking golden needs a live app.
        assert (fake_client.calls.index(("start", "erpnext", True))
                < fake_client.calls.index(("baseline", "erpnext"))
                < fake_client.calls.index(("golden", "erpnext")))
    finally:
        workspace.close()


def test_local_healthy_with_golden_skips_bootstrap_entirely(monkeypatch):
    """A local agent that is already healthy with a golden snapshot is a no-op."""
    fake_client = BootstrappingHostClient(state="healthy", snapshots=["golden"])
    workspace = _cold_start_workspace(monkeypatch, fake_client, is_local=True)
    try:
        workspace.start()
        bootstrap_calls = {call[0] for call in fake_client.calls
                           if call[0] in ("start", "baseline", "golden")}
        assert bootstrap_calls == set()
    finally:
        workspace.close()


def test_remote_agent_never_cold_bootstraps_even_when_unhealthy(monkeypatch):
    """A configured/shared agent must never be cold-started mid-capture.

    Loud failure is the correct behavior for a remote agent that isn't
    healthy yet -- it might be serving another capture or trial -- so
    bootstrap must not run at all, regardless of reported state.
    """
    fake_client = BootstrappingHostClient(state="unreachable", snapshots=())
    workspace = _cold_start_workspace(monkeypatch, fake_client, is_local=False)
    try:
        workspace.start()
        bootstrap_calls = {call[0] for call in fake_client.calls
                           if call[0] in ("start", "baseline", "golden")}
        assert bootstrap_calls == set()
    finally:
        workspace.close()


def test_capture_store_creates_structured_draft(tmp_path):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(
        tasks, port_allocator=lambda preferred, **_kwargs: preferred)
    session = store.start({
        "slug": "review-positive-items",
        "title": "Review positive items",
        "summary": "Approve only clearly positive reviews.",
        "applications": ["magento_admin"],
    })
    store.append_chunk(session["id"], 0, io.BytesIO(b"webm-a"), 6)
    store.append_chunk(session["id"], 1, io.BytesIO(b"-webm-b"), 7)
    result = store.finish(session["id"], {
        "duration_ms": 12_345,
        "voice_recorded": True,
        "transcript_supported": True,
        "steps": [
            {"at_ms": 800, "text": "Open the pending reviews."},
            {"at_ms": 6200, "text": "Approve this clearly positive review."},
        ],
    })

    draft = tmp_path / "task-drafts" / "review-positive-items"
    assert result["path"] == str(draft)
    assert (draft / "recording.webm").read_bytes() == b"webm-a-webm-b"
    testcase = json.loads((draft / "testcase.json").read_text())
    assert testcase["status"] == "captured-draft"
    assert testcase["applications"] == ["magento_admin"]
    assert testcase["surfaces"][0]["captured_url"] == testcase["surfaces"][0]["url"]
    assert testcase["expected_results"] == []
    assert testcase["preconditions"]
    assert testcase["steps"][1] == {
        "id": "step-002", "at_ms": 6200,
        "text": "Approve this clearly positive review.",
    }
    narration = [json.loads(line) for line in
                 (draft / "narration_script.jsonl").read_text().splitlines()]
    assert [row["key"] for row in narration] == ["step-001", "step-002"]


def test_pending_managed_launch_can_be_cancelled_before_start_returns(tmp_path):
    serve = load_serve()
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class BlockingWorkspace:
        def __init__(self, _metadata):
            pass

        def start(self):
            entered.set()
            assert release.wait(2)

        def close(self):
            closed.set()

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=BlockingWorkspace)
    launch_id = "d" * 32
    errors = []

    def start():
        try:
            store.start({
                "slug": "cancel-pending", "title": "Cancel pending",
                "applications": ["kiwix"], "mode": "managed",
                "launch_id": launch_id,
            })
        except Exception as exc:  # the canceled start is the expected result
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    assert entered.wait(2)

    result = store.cancel_pending(launch_id)
    assert closed.wait(1)
    release.set()
    thread.join(2)

    assert result["cancelled"] is True
    assert errors and "cancelled" in str(errors[0])
    assert store._sessions == {}


def test_pending_cancel_can_arrive_before_launch_request(tmp_path):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks)
    launch_id = "e" * 32

    store.cancel_pending(launch_id)

    with pytest.raises(serve.capture.CaptureError, match="launch was cancelled"):
        store.start({
            "slug": "cancel-before-start", "title": "Cancel before start",
            "applications": ["kiwix"], "mode": "managed",
            "launch_id": launch_id,
        })
    assert launch_id not in store._cancelled_launches


def test_capture_store_rejects_bad_apps_order_and_duplicate_name(tmp_path):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(
        tasks, port_allocator=lambda preferred, **_kwargs: preferred)
    with pytest.raises(serve.capture.CaptureError, match="unknown application"):
        store.start({"slug": "bad-app", "title": "Bad", "applications": ["nope"]})

    session = store.start({
        "slug": "valid-draft", "title": "Valid", "applications": ["gitlab"],
    })
    with pytest.raises(serve.capture.CaptureError, match="expected recording chunk 0"):
        store.append_chunk(session["id"], 2, io.BytesIO(b"x"), 1)
    store.append_chunk(session["id"], 0, io.BytesIO(b"x"), 1)
    store.finish(session["id"], {
        "duration_ms": 10, "steps": [], "voice_recorded": True,
        "transcript_supported": False,
    })
    with pytest.raises(serve.capture.CaptureError, match="already exists"):
        store.start({
            "slug": "valid-draft", "title": "Again", "applications": ["gitlab"],
        })


def _promotable_draft(root, slug="captured-task"):
    draft = root / "task-drafts" / slug
    (draft / "quiz").mkdir(parents=True)
    (draft / "task.toml").write_text(
        f'[task]\nname = "{slug}"\napplications = ["gitlab"]\n'
        'primary_application = "gitlab"\nsummary = "Captured"\n\n'
        '[status]\nstate = "draft"\nreason = "Review before promotion."\n'
    )
    (draft / "testcase.json").write_text(json.dumps({
        "status": "captured-draft", "name": slug, "title": "Captured task",
        "authoring_todos": ["Review selectors.", "Promote the reviewed draft into tasks/."],
    }))
    (draft / "quiz" / "questions.json").write_text('{"questions": []}\n')
    return draft


def test_capture_store_promotes_into_ordinary_tasks(tmp_path):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    draft = _promotable_draft(tmp_path)
    store = serve.capture.CaptureStore(tasks)

    result = store.promote("captured-task")

    task = tasks / "captured-task"
    assert result["path"] == str(task) and result["promoted"] is True
    assert not draft.exists() and task.is_dir()
    assert 'state = "working"' in (task / "task.toml").read_text()
    testcase = json.loads((task / "testcase.json").read_text())
    assert testcase["status"] == "captured-task"
    assert all("Promote" not in item for item in testcase["authoring_todos"])


def test_capture_store_rolls_back_metadata_when_promotion_move_fails(
        tmp_path, monkeypatch):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    draft = _promotable_draft(tmp_path)
    original_testcase = (draft / "testcase.json").read_text()
    original_toml = (draft / "task.toml").read_text()
    store = serve.capture.CaptureStore(tasks)

    def fail_replace(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(serve.capture.os, "replace", fail_replace)

    with pytest.raises(serve.capture.CaptureError, match="could not be promoted"):
        store.promote("captured-task")

    assert draft.is_dir()
    assert (draft / "testcase.json").read_text() == original_testcase
    assert (draft / "task.toml").read_text() == original_toml
    assert not (tasks / "captured-task").exists()


def _replayable_draft(draft, testcase=None):
    """Give a draft the generated replay required by the shared task runner."""
    if testcase is None:
        testcase = json.loads((draft / "testcase.json").read_text())
    applications = testcase.get("applications") or [
        row["application"] for row in testcase.get("surfaces", [])
        if row.get("application")]
    if not (draft / "task.toml").exists():
        primary = testcase.get("primary_application") or applications[0]
        (draft / "task.toml").write_text(
            f'[task]\nname = "{draft.name}"\n'
            f'applications = {json.dumps(applications)}\n'
            f'primary_application = "{primary}"\nsummary = "Captured"\n')
    (draft / "demo").mkdir(exist_ok=True)
    (draft / "demo" / "seed.json").write_text("{}\n")
    (draft / "demonstrate.py").write_text("# the generated replay driver\n")
    (draft / "testcase.json").write_text(json.dumps(
        {**testcase, "replay": {"generated": True, "action_count": 3}}))
    return draft


def _request(url, token, path, payload, *, content_type="application/json", extra=None):
    headers = {"Content-Type": content_type, "X-ShowAndTell-Edit-Token": token,
               **(extra or {})}
    data = json.dumps(payload).encode() if content_type == "application/json" else payload
    req = urllib.request.Request(url + path, data=data, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


def test_capture_http_round_trip_and_token_boundary(tmp_path, monkeypatch):
    serve = load_serve()
    write_task(tmp_path, "alpha-one")
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(serve.generate, "CACHE_DIR", tmp_path / "runs/.cache")
    server = serve.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(url).read().decode()
        assert '"taskCapture": {"enabled": true' in page
        assert "Magento Admin" in page

        status, denied = _request(url, "wrong", "/api/captures", {
            "slug": "http-draft", "title": "HTTP draft", "applications": ["gitlab"],
        })
        assert status == 403 and "token" in denied["error"]

        status, started = _request(url, server.question_edit_token, "/api/captures", {
            "slug": "http-draft", "title": "HTTP draft", "applications": ["gitlab"],
        })
        assert status == 201
        status, chunk = _request(
            url, server.question_edit_token, started["chunk_endpoint"], b"fake-webm",
            content_type="video/webm", extra={"X-ShowAndTell-Chunk": "0"})
        assert status == 200 and chunk["next_chunk"] == 1
        status, finished = _request(
            url, server.question_edit_token, started["finish_endpoint"],
            {"duration_ms": 500, "steps": [], "voice_recorded": True,
             "transcript_supported": False})
        assert status == 200 and finished["slug"] == "http-draft"
        assert (tmp_path / "task-drafts/http-draft/recording.webm").read_bytes() == b"fake-webm"
        _replayable_draft(tmp_path / "task-drafts/http-draft")

        launch_id = "e" * 32
        status, pending = _request(
            url, server.question_edit_token, "/api/captures", {
                "slug": "pending-http", "title": "Pending HTTP",
                "applications": ["kiwix"], "launch_id": launch_id,
            })
        assert status == 201 and pending["id"]
        status, cancelled = _request(
            url, server.question_edit_token,
            f"/api/captures/pending/{launch_id}/cancel", {})
        assert status == 200 and cancelled["cancelled"] is True

        class FakeProcess:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        class FakeWorkspace:
            execution = None

            def __init__(self, metadata):
                self.metadata = metadata

            def start(self):
                pass

            def restore(self, **_options):
                pass

            def close(self):
                pass

        server.task_run_store._popen = lambda *args, **kwargs: FakeProcess()
        server.task_run_store.draft_workspace_factory = FakeWorkspace
        status, trial = _request(
            url, server.question_edit_token, "/api/task-runs",
            {"task": "http-draft", "source": "draft",
             "adapter": "brackett-teach",
             "hear_narration": True})
        assert status == 202 and trial["status"] == "running"
        assert trial["source"] == "draft"
        assert trial["hear_narration"] is True
        status, muted = _request(
            url, server.question_edit_token, trial["audio_endpoint"],
            {"hear_narration": False})
        assert status == 200 and muted["hear_narration"] is False
        status, audible = _request(
            url, server.question_edit_token, trial["audio_endpoint"],
            {"hear_narration": True})
        assert status == 200 and audible["hear_narration"] is True
        status, cancelled = _request(
            url, server.question_edit_token, trial["cancel_endpoint"], {})
        assert status == 200 and cancelled["status"] == "cancelled"

        _promotable_draft(tmp_path, "http-promote")
        status, promoted = _request(
            url, server.question_edit_token, "/api/drafts/promote",
            {"slug": "http-promote"})
        assert status == 200 and promoted["promoted"] is True
        assert (tmp_path / "tasks/http-promote/task.toml").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_managed_capture_generates_demonstrate_and_bundle(tmp_path, monkeypatch):
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    class FakeManagedCapture:
        login_results = [{"id": "gitlab", "required": True,
                          "status": "signed-in"}]

        def __init__(self, directory, surfaces, **kwargs):
            self.directory = directory
            self.surfaces = surfaces

        def start(self):
            pass

        def begin_recording(self, timeout=0):
            return {"voice_recorded": True}

        voice_recorded = False

        def stop(self, timeout=0):
            return {"recording": None, "started_ms": 1, "setup_events": [], "events": [
                {"type": "goto", "page": "page", "url": "http://127.0.0.1:8023/explore",
                 "at_ms": 0, "description": "open GitLab"},
                {"type": "tab_switch", "page": "page", "title": "GitLab",
                 "at_ms": 200, "description": "switch to GitLab"},
                {"type": "drag", "page": "page", "selector": "#issue-card",
                 "selectors": ["#issue-card"], "frame_url": None,
                 "target": {"name": "Issue", "selectors": ["#issue-card"]},
                 "destination": {"name": "Doing", "selectors": ["#doing"]},
                 "source_position": {"x": 10, "y": 10},
                 "target_position": {"x": 30, "y": 20},
                 "path": [{"x": 10, "y": 10}, {"x": 100, "y": 20}],
                 "at_ms": 350, "description": "drag Issue to Doing"},
                {"type": "click", "page": "page", "selector": "#new-issue",
                 "selectors": ["#new-issue", "[data-testid=\"new-issue\"]"],
                 "frame_url": None, "target": {"name": "New issue"}, "at_ms": 500},
            ]}

    class FakeWorkspace:
        def __init__(self, metadata):
            self.metadata = metadata
            self.closed = False

        def start(self):
            return self.metadata["surfaces"]

        def export(self):
            return {"gitlab": {"projects": [{"name": "Seeded"}]}}, True

        def restore(self, *, use_export, state_dir=None, snapshot_only=False):
            self.restored_with_export = use_export
            self.restored_state_dir = state_dir
            self.restored_snapshot_only = snapshot_only

        def close(self):
            self.closed = True

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=FakeWorkspace)
    session = store.start({
        "slug": "managed-draft", "title": "Managed draft",
        "summary": "Open a new issue.", "applications": ["gitlab"],
        "mode": "managed",
    })
    assert session["managed"] is True
    assert session["login_results"] == [{
        "id": "gitlab", "required": True, "status": "signed-in"}]
    started = store.begin_recording(session["id"])
    assert started["seed_exported"] is True
    assert started["seed_keys"] == ["gitlab"]
    result = store.finish(session["id"], {
        "duration_ms": 900, "voice_recorded": True, "transcript_supported": True,
        "steps": [{"at_ms": 450, "text": "Open a new issue."}],
    })
    draft = tmp_path / "task-drafts/managed-draft"
    assert result["replay_generated"] is True and result["actions"] == 3
    assert result["voice_recorded"] is False
    assert "#new-issue" in (draft / "demonstrate.py").read_text()
    testcase = json.loads((draft / "testcase.json").read_text())
    assert testcase["replay"]["generated"] is True
    assert testcase["replay"]["bundle"] == "capture_bundle"
    assert testcase["replay"]["action_count"] == 3
    assert testcase["capture"]["voice_recorded"] is False
    assert testcase["seed"]["source"] == "fixture-export"
    assert json.loads((draft / "demo/seed.json").read_text()) == {
        "gitlab": {"projects": [{"name": "Seeded"}]}}

def test_finish_shifts_narration_onto_the_recording_clock(tmp_path, monkeypatch):
    """Browser narration timestamps start when /record returns; the action
    clock now starts when the recorder process spawned, several seconds
    earlier. Alignment and the saved narration script must use one clock or
    voice keys attach to the wrong actions."""
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    class FakeManagedCapture:
        login_results = []

        def __init__(self, directory, surfaces, **kwargs):
            self.directory = directory

        def start(self):
            pass

        def begin_recording(self, timeout=0):
            return {"voice_recorded": True}

        def stop(self, timeout=0):
            return {
                "recording": None, "started_ms": 1, "voice_recorded": True,
                "narration_lead_ms": 4000, "setup_events": [],
                "events": [
                    {"type": "click", "page": "page", "selector": "#new-issue",
                     "selectors": ["#new-issue"], "frame_url": None,
                     "target": {"name": "New issue"}, "at_ms": 4500},
                ],
            }

    class FakeWorkspace:
        def __init__(self, metadata):
            self.metadata = metadata

        def start(self):
            return self.metadata["surfaces"]

        def export(self):
            return {"gitlab": {}}, False

        def restore(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=FakeWorkspace)
    session = store.start({
        "slug": "shifted-draft", "title": "Shifted draft",
        "applications": ["gitlab"], "mode": "managed",
    })
    store.begin_recording(session["id"])
    store.finish(session["id"], {
        "duration_ms": 900, "voice_recorded": True, "transcript_supported": True,
        "steps": [{"at_ms": 450, "text": "Open a new issue."}],
    })
    draft = tmp_path / "task-drafts/shifted-draft"
    narration = [json.loads(line) for line in
                 (draft / "demo/narration_script.jsonl").read_text().splitlines()]
    # 450 on the browser clock + 4000ms recorder lead = 4450 beside the
    # click at 4500, and the spoken step wins the key for that action.
    assert narration == [{"key": "voice-001", "text": "Open a new issue.",
                          "at_ms": 4450}]
    # The operator-facing step list keeps the timestamps as spoken.
    testcase = json.loads((draft / "testcase.json").read_text())
    assert testcase["steps"][0]["at_ms"] == 450


def test_managed_capture_requires_record_boundary_and_can_cancel(tmp_path, monkeypatch):
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    calls = []

    class FakeManagedCapture:
        voice_recorded = False

        def __init__(self, directory, surfaces, **kwargs):
            calls.append(("capture", directory, surfaces))

        def start(self):
            calls.append(("capture-start",))

        def begin_recording(self, timeout=0):
            calls.append(("record",))
            return {"voice_recorded": True}

        def stop(self, timeout=0):
            calls.append(("stop",))
            return {"events": [], "setup_events": [], "recording": None,
                    "voice_recorded": False, "started_ms": None}

    class FakeWorkspace:
        def __init__(self, metadata):
            calls.append(("workspace", metadata["slug"]))

        def start(self):
            calls.append(("apps-clean",))

        def export(self):
            calls.append(("export",))
            return {"items": [{"id": 1}]}, True

        def close(self):
            calls.append(("apps-close",))

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=FakeWorkspace)
    session = store.start({
        "slug": "two-stage-draft", "title": "Two stage draft",
        "applications": ["kiwix"], "mode": "managed",
    })
    with pytest.raises(serve.capture.CaptureError, match="start recording"):
        store.finish(session["id"], {"duration_ms": 1, "steps": []})
    store.begin_recording(session["id"])
    assert calls.index(("export",)) < calls.index(("record",))
    result = store.cancel(session["id"])
    assert result["cancelled"] is True
    assert calls[-2:] == [("stop",), ("apps-close",)]


def test_force_start_replaces_active_managed_capture(tmp_path, monkeypatch):
    """The confirmed retry clears viewer session state, not only host leases."""
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    calls = []

    class FakeManagedCapture:
        def __init__(self, directory, surfaces, **kwargs):
            self.slug = json.loads(
                (directory / "session.json").read_text())["slug"]

        def start(self):
            calls.append(("capture-start", self.slug))

        def stop(self, timeout=0):
            calls.append(("capture-stop", self.slug))

    class FakeWorkspace:
        def __init__(self, metadata):
            self.slug = metadata["slug"]

        def start(self):
            calls.append(("workspace-start", self.slug))

        def close(self):
            calls.append(("workspace-close", self.slug))

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=FakeWorkspace)
    first = store.start({
        "slug": "first-managed", "title": "First managed",
        "applications": ["kiwix"], "mode": "managed",
    })

    with pytest.raises(serve.capture.CaptureError) as excinfo:
        store.start({
            "slug": "second-managed", "title": "Second managed",
            "applications": ["kiwix"], "mode": "managed",
        })
    assert excinfo.value.status == 409
    assert excinfo.value.details["code"] == "managed_capture_busy"
    assert excinfo.value.details["held_by"] == "capture-first-managed"
    assert set(store._sessions) == {first["id"]}
    # The rejected attempt must not leave its newly-created staging directory.
    assert len(list(store.output_root.glob(".*-capture-*"))) == 1

    second = store.start({
        "slug": "second-managed", "title": "Second managed",
        "applications": ["kiwix"], "mode": "managed", "force": True,
    })
    assert set(store._sessions) == {second["id"]}
    assert calls.index(("capture-stop", "first-managed")) < \
        calls.index(("capture-start", "second-managed"))
    assert calls.index(("workspace-close", "first-managed")) < \
        calls.index(("workspace-start", "second-managed"))
    store.cancel(second["id"])


def test_state_snapshot_lands_in_the_draft_and_is_recorded(monkeypatch, tmp_path):
    """A fixture that cannot export hand-made state contributes a snapshot.

    ERPNext's export only round-trips a seed it created itself, so without
    this a capture writes {"profile": null, "items": []} and silently loses
    everything the operator built in the UI.
    """
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    class FakeManagedCapture:
        def __init__(self, directory, surfaces, out=print, **kwargs):
            self.directory = directory

        def start(self, timeout=45):
            self.login_results = []

        def begin_recording(self, timeout=30):
            return {"voice_recorded": True}

        def stop(self, timeout=30):
            return {"events": [], "setup_events": [], "recording": None,
                    "voice_recorded": True, "started_ms": 0}

    class SnapshottingWorkspace:
        def __init__(self, metadata):
            self.metadata = metadata
            self.snapshot_calls = []

        def start(self):
            return self.metadata["surfaces"]

        def export(self):
            # Exactly what acmefix returns for operator-created state.
            return {"erpnext": {"profile": None, "items": []}}, False

        def capture_state(self, directory):
            self.snapshot_calls.append(directory)
            (directory / "erpnext-state.tar").write_bytes(b"tar-bytes")
            return {"erpnext": {"snapshot": "erpnext-state.tar"}}

        def restore(self, *, use_export, state_dir=None, snapshot_only=False):
            pass

        def close(self):
            pass

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(
        tasks, workspace_factory=SnapshottingWorkspace)
    session = store.start({
        "slug": "snapshot-draft", "title": "Snapshot draft",
        "applications": ["erpnext"], "mode": "managed",
    })

    began = store.begin_recording(session["id"])
    assert began["state_snapshots"] == ["erpnext-state.tar"]
    # The declarative export is honestly reported as empty, not as a seed.
    assert began["seed_exported"] is False

    result = store.finish(session["id"], {
        "duration_ms": 1000, "steps": [], "transcript_supported": True,
    })
    assert result["state_snapshots"] == ["erpnext-state.tar"]

    draft = tasks.parent / "task-drafts" / "snapshot-draft"
    assert (draft / "demo" / "erpnext-state.tar").read_bytes() == b"tar-bytes"
    testcase = json.loads((draft / "testcase.json").read_text())
    assert testcase["seed"]["source"] == "state-snapshot"
    assert testcase["seed"]["state_snapshots"] == ["demo/erpnext-state.tar"]


class _TrialSession:
    """The slice of AppSession a trial restore drives, over a real agent client."""

    def __init__(self, client, applications=("erpnext",), *, on_seed=None):
        self.applications = tuple(applications)
        self.client = client
        self.holder = "draft-restored"
        self.lease_id = "lease-1"
        self.demo_root = None
        self.resets = 0
        self.prepares = 0
        self._on_seed = on_seed

    def state(self, name):
        return None

    def use_demo_root(self, root):
        self.demo_root = root

    def restore_snapshots(self, state_dir):
        from showAndTell.applications.session import AppSession

        return AppSession.restore_snapshots(self, state_dir)

    def reset(self, *, only=None, coarse=True):
        self.resets += 1

    def prepare(self, *, only=None):
        self.prepares += 1

    def seed(self, document):
        if self._on_seed is not None:
            self._on_seed(document)

    def close(self):
        pass


def _trial_workspace(serve, session):
    workspace = serve.capture.CaptureWorkspace.__new__(serve.capture.CaptureWorkspace)
    workspace.metadata = {"slug": "restored-draft"}
    workspace.legacy = []
    workspace.session = session
    return workspace


def test_trial_restores_the_draft_state_snapshot_from_disk(tmp_path):
    """A trial must start from the task's own data, not the shared baseline.

    The in-memory snapshot handle belongs to the process that captured it, so
    after a viewer restart the draft on disk is the only record of what the
    demonstration started from.
    """
    serve = load_serve()
    recording_client = RecordingHostClient()
    session = _TrialSession(recording_client)
    workspace = _trial_workspace(serve, session)

    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "erpnext-state.tar").write_bytes(b"snapshot")

    workspace.restore(use_export=True, state_dir=demo)

    assert ("push_snapshot", "erpnext", "draft-restored",
            str(demo / "erpnext-state.tar")) in recording_client.calls
    assert ("restore_snapshot", "erpnext", "draft-restored") in recording_client.calls
    # A snapshot is the exact starting state; resetting after it would throw
    # the task's own data away and start from the shared baseline instead.
    assert session.resets == 0


def test_trial_restore_survives_the_draft_rename(tmp_path):
    """Promotion moves the draft, so restore must read the path it is given."""
    serve = load_serve()
    recording_client = RecordingHostClient()
    session = _TrialSession(recording_client)
    workspace = _trial_workspace(serve, session)

    original = tmp_path / "draft" / "demo"
    original.mkdir(parents=True)
    (original / "erpnext-state.tar").write_bytes(b"snapshot")
    promoted = tmp_path / "task" / "demo"
    promoted.parent.mkdir()
    original.parent.rename(promoted.parent)

    workspace.restore(use_export=True, state_dir=promoted)

    assert ("restore_snapshot", "erpnext", "draft-restored") in recording_client.calls


def test_trial_falls_back_when_a_draft_has_no_state_snapshot(tmp_path):
    """No snapshot means the applications are reset and prepared instead."""
    serve = load_serve()
    recording_client = RecordingHostClient()
    session = _TrialSession(recording_client)
    workspace = _trial_workspace(serve, session)

    demo = tmp_path / "demo"
    demo.mkdir()

    workspace.restore(use_export=True, state_dir=demo)

    assert not [c for c in recording_client.calls if c[0] == "restore_snapshot"]
    assert (session.resets, session.prepares) == (1, 1)


def test_trial_skips_setup_replay_when_the_draft_has_a_state_snapshot():
    """Replaying setup on top of a restored snapshot applies the work twice.

    The failure is not subtle: the recorded selectors assume the page looks as
    it did during capture, so a row that already exists sends :focus to the
    wrong element and fill() lands on a button.
    """
    from showAndTell.player.trial import has_state_snapshot

    assert has_state_snapshot(
        {"seed": {"state_snapshots": ["demo/acmefix-state.tar"]}}) is True
    # Drafts without one still need the gesture replay as their only fallback.
    assert has_state_snapshot({"seed": {"state_snapshots": []}}) is False
    assert has_state_snapshot({"seed": {}}) is False
    assert has_state_snapshot({}) is False


def test_finish_reports_the_recording_after_the_draft_is_moved(tmp_path):
    """finish() renames the staging directory, invalidating the recording path.

    Reading it afterwards reported recording_available=false for a draft whose
    testcase.json — written before the move — correctly recorded the file.
    """
    serve = load_serve()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks)
    session = store.start({
        "slug": "moved-draft", "title": "Moved draft", "applications": ["gitlab"],
    })
    store.append_chunk(session["id"], 0, io.BytesIO(b"webm-bytes"), 10)
    result = store.finish(session["id"], {
        "duration_ms": 1000, "steps": [], "voice_recorded": True,
        "transcript_supported": False,
    })

    draft = tmp_path / "task-drafts" / "moved-draft"
    testcase = json.loads((draft / "testcase.json").read_text())
    assert testcase["capture"]["recording"] == "recording.webm"
    # The API response must agree with what actually landed on disk.
    assert result["recording_available"] is True
    assert result["bytes"] == 10


def test_finish_names_which_applications_actually_exported_state(monkeypatch, tmp_path):
    """A single `exported` flag lets one application mask another's empty shell."""
    serve = load_serve()
    from showAndTell.capture import runtime as managed_capture

    class FakeManagedCapture:
        def __init__(self, directory, surfaces, out=print, **kwargs):
            pass

        def start(self, timeout=45):
            self.login_results = []

        def begin_recording(self, timeout=30):
            return {"voice_recorded": True}

        def stop(self, timeout=30):
            return {"events": [], "setup_events": [], "recording": None,
                    "voice_recorded": True, "started_ms": 0}

    class MixedWorkspace:
        def __init__(self, metadata):
            self.metadata = metadata

        def start(self):
            return self.metadata["surfaces"]

        def export(self):
            # ERPNext cannot describe hand-made state; ONLYOFFICE can.
            return {"erpnext": {"profile": None, "items": []},
                    "onlyoffice": {"documents": [{"document_id": "task-setup"}]}}, True

        def restore(self, *, use_export, state_dir=None, snapshot_only=False):
            pass

        def close(self):
            pass

    monkeypatch.setattr(managed_capture, "ManagedCapture", FakeManagedCapture)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    store = serve.capture.CaptureStore(tasks, workspace_factory=MixedWorkspace)
    session = store.start({
        "slug": "mixed-draft", "title": "Mixed draft",
        "applications": ["erpnext", "onlyoffice"], "mode": "managed",
    })
    store.begin_recording(session["id"])
    result = store.finish(session["id"], {
        "duration_ms": 1000, "steps": [], "transcript_supported": True,
    })

    assert result["exported_applications"] == ["onlyoffice"]
    testcase = json.loads(
        (tmp_path / "task-drafts" / "mixed-draft" / "testcase.json").read_text())
    assert testcase["seed"]["exported_applications"] == ["onlyoffice"]
    # The coarse flag stays true because ONLYOFFICE really did export.
    assert testcase["seed"]["exported"] is True


def test_capture_keeps_replica_assignment_on_its_managed_session(monkeypatch):
    from showAndTell.applications.host.client import ASSIGNMENT_ENV, LEASE_ENV

    monkeypatch.delenv(ASSIGNMENT_ENV, raising=False)
    monkeypatch.delenv(LEASE_ENV, raising=False)

    class AssignedClient(BootstrappingHostClient):
        assignment = {"erpnext": "erpnext_3"}

    fake_client = AssignedClient(state="healthy", snapshots=["golden"])
    workspace = _cold_start_workspace(monkeypatch, fake_client, is_local=True)
    try:
        workspace.start()
        assert workspace.session.client.assignment == {"erpnext": "erpnext_3"}
        # Concurrent captures must not publish process-global routing. The
        # manager passes this execution's lease and assignment only to the
        # child process it launches.
        assert ASSIGNMENT_ENV not in os.environ
        assert LEASE_ENV not in os.environ
    finally:
        workspace.close()
    assert ASSIGNMENT_ENV not in os.environ
    assert LEASE_ENV not in os.environ


def test_capture_without_assignment_sets_no_assignment_env(monkeypatch):
    import os

    from showAndTell.applications.host.client import ASSIGNMENT_ENV

    monkeypatch.delenv(ASSIGNMENT_ENV, raising=False)

    fake_client = BootstrappingHostClient(state="healthy", snapshots=["golden"])
    workspace = _cold_start_workspace(monkeypatch, fake_client, is_local=True)
    try:
        workspace.start()
        assert ASSIGNMENT_ENV not in os.environ
    finally:
        workspace.close()
