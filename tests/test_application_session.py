"""AppSession: one lease, one context per application, no environment handoff."""
import hashlib

import pytest

from showAndTell.applications.session import AppContext, AppSession
from tests._app_fixtures import app_manifest
from showAndTell.applications.host.client import FixtureHostUnsupported


class _State:
    """A data plane that records what it was asked to do."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.application = manifest.name
        self.prepared = 0
        self.resets = 0
        self.seeded: list = []
        self.captured: list = []
        self.block: dict = {}

    def prepare(self, ctx):
        self.prepared += 1

    def reset(self, ctx):
        self.resets += 1

    def seed(self, ctx, block):
        self.seeded.append((ctx, block))

    def export(self, ctx):
        return {"seen": len(self.seeded)}

    def capture(self, ctx, directory):
        self.captured.append(directory)
        return dict(self.block)


class _Registry:
    """A registry over real manifests but fake, inspectable state planes."""

    def __init__(self, names, *, without_state=()):
        from showAndTell.applications.registry import default_registry

        self._real = default_registry()
        self._names = tuple(names)
        self._without = set(without_state)
        self.states: dict[str, _State] = {}

    def names(self):
        return self._names

    def manifest(self, name):
        return self._real.manifest(name)

    def has_state(self, name):
        return name not in self._without

    def state(self, name):
        return _State(self.manifest(name))


class _Client:
    """The fixture host agent's surface, in memory."""

    host = "agent.test"

    def __init__(self, *, unhealthy=(), no_snapshots=()):
        self.calls: list[tuple] = []
        self.leases: list[tuple] = []
        self.released: list[str] = []
        self.force_released: list[list[str]] = []
        self._unhealthy = set(unhealthy)
        self._no_snapshots = set(no_snapshots)
        self.pulled: dict[str, str] = {}

    def force_release(self, apps):
        self.force_released.append(list(apps))
        return []

    def acquire_lease(self, apps, holder, ttl_s=900.0):
        self.leases.append((tuple(apps), holder))
        return "lease-1"

    def heartbeat(self, lease_id):
        pass

    def release(self, lease_id):
        self.released.append(lease_id)

    def status(self, app):
        return {"state": "unreachable" if app in self._unhealthy else "healthy",
                "ports": {"app": 8080}}

    def start(self, app, wait=True):
        self.calls.append(("start", app))

    def reset(self, app, lease_id=None):
        self.calls.append(("reset", app))

    def secrets(self, app):
        return {"token": f"{app}-secret"}

    def app_url(self, app):
        return f"http://agent.test/{app}"

    def snapshot(self, app, name, lease_id=None):
        if app in self._no_snapshots:
            raise FixtureHostUnsupported(app, "snapshot")
        self.calls.append(("snapshot", app, name))

    def pull_snapshot(self, app, name, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"tar")
        self.pulled[app] = name

    def push_snapshot(self, app, name, source, lease_id=None):
        self.calls.append(("push", app, name))

    def restore_snapshot(self, app, name, lease_id=None):
        self.calls.append(("restore", app, name))


def _session(names=("roundcube",), *, client=None, registry=None, **kwargs):
    return AppSession(list(names), client=client or _Client(),
                      registry=registry or _Registry(names), **kwargs)


# -- lifecycle -------------------------------------------------------------

def test_one_lease_covers_every_application():
    """Two leases would let a second capture take one app out from under this."""
    client = _Client()
    _session(("roundcube", "erpnext"), client=client).start()
    assert client.leases == [(("roundcube", "erpnext"), "showAndTell")]


def test_taking_a_lease_never_starts_an_application_by_itself():
    """A shared agent may be serving other work from the state it holds.

    Cold-starting one mid-session has to be the caller's decision, not a side
    effect of taking a lease.
    """
    client = _Client(unhealthy={"erpnext"})
    _session(("roundcube", "erpnext"), client=client).start()
    assert not [c for c in client.calls if c[0] == "start"]


def test_a_caller_that_owns_the_agent_is_handed_the_lease_to_bootstrap_with():
    client = _Client(unhealthy={"erpnext"})
    seen: list = []
    _session(("roundcube", "erpnext"), client=client).start(
        ensure_started=lambda apps, lease_id: seen.append((apps, lease_id)))
    assert seen == [(["roundcube", "erpnext"], "lease-1")]


def test_bootstrapping_happens_before_contexts_are_resolved():
    """A URL read before the app is up would be resolved against nothing."""
    order: list[str] = []

    class Client(_Client):
        def app_url(self, app):
            order.append("app_url")
            return super().app_url(app)

    session = _session(("roundcube",), client=Client())
    session.start(ensure_started=lambda apps, lease_id: order.append("bootstrap"))
    assert order[0] == "bootstrap"


def test_force_breaks_a_stale_lease_before_acquiring():
    client = _Client()
    _session(("roundcube",), client=client, force=True).start()
    assert client.force_released == [["roundcube"]]


def test_close_releases_the_lease():
    client = _Client()
    session = _session(client=client)
    session.start()
    session.close()
    assert client.released == ["lease-1"]


def test_child_adopts_manager_lease_without_acquiring_or_releasing(monkeypatch):
    from showAndTell.applications.host.client import LEASE_ENV

    client = _Client()
    monkeypatch.setenv(LEASE_ENV, "manager-lease")
    monkeypatch.setenv("SHOWANDTELL_EXECUTION_MANAGER_CHILD", "1")
    session = _session(("roundcube", "erpnext"), client=client)
    session.start()
    assert session.lease_id == "manager-lease"
    assert client.leases == []
    session.close()
    assert client.released == []


def test_context_failure_releases_the_lease():
    class Client(_Client):
        def secrets(self, app):
            raise RuntimeError("secrets unavailable")

    client = Client()
    with pytest.raises(RuntimeError, match="secrets unavailable"):
        _session(("roundcube",), client=client).start()
    assert client.released == ["lease-1"]


def test_an_unknown_application_fails_before_any_container_is_touched():
    client = _Client()
    with pytest.raises(Exception, match="nope"):
        AppSession(["nope"], client=client)
    assert client.leases == []


def test_duplicate_applications_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        _session(("roundcube", "roundcube"))


# -- context ---------------------------------------------------------------

def test_each_application_gets_its_url_credentials_and_driver_secrets():
    """This is what replaced the SHOWANDTELL_<FAMILY>_<APP>_* environment handoff."""
    session = _session(("roundcube",))
    session.start()
    ctx = session.context("roundcube")
    assert ctx.url == "http://agent.test/roundcube"
    assert ctx.credentials["email"] == "agent@showAndTell.test"
    assert ctx.secrets == {"token": "roundcube-secret"}
    assert ctx.host == "agent.test"


def test_the_surface_url_appends_the_manifests_entry_path():
    session = _session(("erpnext",))
    session.start()
    assert session.surface_url("erpnext") == "http://agent.test/erpnext/login"


def test_runtime_url_prefers_an_explicit_state_browser_entry():
    session = _session(("roundcube",))
    session.start()
    state = session.state("roundcube")
    state.browser_metadata = lambda _ctx: {
        "browser_url": "http://connector.test/editor/task-setup"
    }

    assert session.runtime_url("roundcube") == "http://connector.test"


def test_runtime_url_keeps_the_application_origin_without_an_explicit_entry():
    session = _session(("erpnext",))
    session.start()

    assert session.runtime_url("erpnext") == "http://agent.test/erpnext"


def test_browser_credentials_include_every_application_context():
    session = _session(("roundcube", "erpnext"))
    session.start()
    credentials = session.browser_credentials("roundcube")
    assert credentials["email"] == "agent@showAndTell.test"
    assert credentials["_showAndTell_applications"] == {
        "roundcube": {
            "url": "http://agent.test/roundcube",
            "browser_url": "http://agent.test/roundcube/",
            "credentials": {
                "email": "agent@showAndTell.test",
                "password": "showAndTell-mail",
            },
            "metadata": {},
        },
        "erpnext": {
            "url": "http://agent.test/erpnext",
            "browser_url": "http://agent.test/erpnext/login",
            "credentials": {
                "email": "Administrator",
                "password": "showAndTell-admin",
            },
            "metadata": {},
        },
    }


# -- data ------------------------------------------------------------------

def test_prepare_reaches_every_data_plane():
    registry = _Registry(("roundcube", "erpnext"))
    session = _session(("roundcube", "erpnext"), registry=registry)
    session.start()
    session.prepare()
    assert [session.state(n).prepared for n in ("roundcube", "erpnext")] == [1, 1]


def test_reset_runs_the_driver_first_then_the_data_plane():
    """The coarse driver reset is the guarantee nothing survives the last run."""
    client = _Client()
    session = _session(("roundcube",), client=client)
    session.start()
    session.reset()
    assert ("reset", "roundcube") in client.calls
    assert session.state("roundcube").resets == 1


def test_seed_routes_each_block_to_its_own_application():
    session = _session(("roundcube", "erpnext"))
    session.start()
    session.seed({"roundcube": {"a": 1}, "erpnext": {"b": 2}})
    assert session.state("roundcube").seeded[0][1] == {"a": 1}
    assert session.state("erpnext").seeded[0][1] == {"b": 2}


def test_seed_skips_an_application_the_document_does_not_mention():
    session = _session(("roundcube", "erpnext"))
    session.start()
    session.seed({"roundcube": {"a": 1}})
    assert session.state("erpnext").seeded == []


def test_seed_refuses_a_block_for_an_application_not_in_the_session():
    session = _session(("roundcube",))
    session.start()
    with pytest.raises(ValueError, match="erpnext"):
        session.seed({"erpnext": {}})


def test_export_is_keyed_by_application():
    session = _session(("roundcube", "erpnext"))
    session.start()
    assert session.export() == {"roundcube": {"seen": 0}, "erpnext": {"seen": 0}}


def test_an_application_with_no_data_plane_is_simply_absent():
    registry = _Registry(("roundcube", "erpnext"), without_state=("erpnext",))
    session = _session(("roundcube", "erpnext"), registry=registry)
    session.start()
    assert set(session.export()) == {"roundcube"}


# -- capture ---------------------------------------------------------------

def test_capture_collects_each_applications_own_block(tmp_path):
    registry = _Registry(("roundcube",))
    session = _session(("roundcube",), registry=registry)
    session.start()
    session.state("roundcube").block = {"messages": [{"external_id": "a"}]}
    captured = session.capture(tmp_path)
    assert captured["roundcube"]["messages"] == [{"external_id": "a"}]


def test_capture_takes_a_snapshot_where_the_manifest_declares_one(tmp_path):
    """ERPNext is a database: it cannot describe hand-made state as data."""
    client = _Client()
    session = _session(("erpnext",), client=client, holder="draft-x")
    session.start()
    captured = session.capture(tmp_path)
    assert ("snapshot", "erpnext", "draft-x") in client.calls
    assert captured["erpnext"]["snapshot"] == "erpnext-state.tar"
    assert (tmp_path / "erpnext-state.tar").is_file()


def test_no_snapshot_is_attempted_for_an_application_that_declares_none(tmp_path):
    client = _Client()
    session = _session(("roundcube",), client=client)
    session.start()
    session.capture(tmp_path)
    assert not [c for c in client.calls if c[0] == "snapshot"]


def test_a_driver_that_refuses_a_snapshot_wins_over_its_manifest(tmp_path):
    """The driver owns the containers, so a 501 is the authoritative answer."""
    client = _Client(no_snapshots={"erpnext"})
    session = _session(("erpnext",), client=client)
    session.start()
    assert session.capture(tmp_path) == {}


def test_restore_pushes_back_only_the_snapshots_the_draft_carries(tmp_path):
    client = _Client()
    session = _session(("roundcube", "erpnext"), client=client, holder="draft-x")
    session.start()
    (tmp_path / "erpnext-state.tar").write_bytes(b"tar")
    assert session.restore_snapshots(tmp_path) == ["erpnext"]
    assert ("restore", "erpnext", "draft-x") in client.calls


# -- assets ----------------------------------------------------------------

def test_an_asset_is_read_only_after_its_digest_matches(tmp_path):
    payload = b"message bytes"
    (tmp_path / "roundcube").mkdir()
    (tmp_path / "roundcube" / "0001.eml").write_bytes(payload)
    ctx = AppContext(manifest=app_manifest("roundcube"), url="",
                     credentials={}, demo_root=tmp_path)
    assert ctx.asset("roundcube/0001.eml",
                     hashlib.sha256(payload).hexdigest()) == payload


def test_an_edited_asset_is_a_loud_failure(tmp_path):
    (tmp_path / "a.eml").write_bytes(b"edited")
    ctx = AppContext(manifest=app_manifest("roundcube"), url="",
                     credentials={}, demo_root=tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        ctx.asset("a.eml", hashlib.sha256(b"original").hexdigest())


def test_a_hub_cache_symlinked_asset_is_readable(tmp_path):
    """Published bundles store files as symlinks into the hub cache's
    blobs/ store; the digest, not path resolution, is the integrity check."""
    payload = b"message bytes"
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "cafebabe").write_bytes(payload)
    demo = tmp_path / "snapshot" / "demo"
    (demo / "roundcube").mkdir(parents=True)
    (demo / "roundcube" / "0001.eml").symlink_to("../../../blobs/cafebabe")
    ctx = AppContext(manifest=app_manifest("roundcube"), url="",
                     credentials={}, demo_root=demo)
    assert ctx.asset("roundcube/0001.eml",
                     hashlib.sha256(payload).hexdigest()) == payload


def test_an_asset_may_not_escape_the_demo_directory(tmp_path):
    ctx = AppContext(manifest=app_manifest("roundcube"), url="",
                     credentials={}, demo_root=tmp_path)
    with pytest.raises(ValueError, match="traversal|escape|invalid"):
        ctx.asset("../secret", "ab" * 32)


def test_asking_for_an_asset_without_a_task_root_says_so():
    ctx = AppContext(manifest=app_manifest("roundcube"), url="", credentials={})
    with pytest.raises(RuntimeError, match="demo root"):
        ctx.asset("a.eml", "ab" * 32)


def test_use_demo_root_repoints_every_context(tmp_path):
    """A trial only learns which draft it is replaying at restore time."""
    session = _session(("roundcube",))
    session.start()
    assert session.context("roundcube").demo_root is None
    session.use_demo_root(tmp_path)
    assert session.context("roundcube").demo_root == tmp_path


def test_reset_and_prepare_can_target_a_subset():
    """A trial rebuilds only the applications no snapshot covered."""
    client = _Client()
    registry = _Registry(("roundcube", "erpnext"))
    session = _session(("roundcube", "erpnext"), client=client, registry=registry)
    session.start()
    session.reset(only=["roundcube"])
    session.prepare(only=["roundcube"])

    assert [c for c in client.calls if c[0] == "reset"] == [("reset", "roundcube")]
    assert session.state("roundcube").resets == 1
    assert session.state("erpnext").resets == 0
    assert session.state("erpnext").prepared == 0


def test_no_selector_still_means_every_application():
    client = _Client()
    session = _session(("roundcube", "erpnext"), client=client)
    session.start()
    session.reset()
    assert {c[1] for c in client.calls if c[0] == "reset"} == {"roundcube", "erpnext"}
