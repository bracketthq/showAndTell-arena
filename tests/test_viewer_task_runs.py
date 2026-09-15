"""Process and HTTP tests for the live viewer's product run buttons."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from showAndTell.core import chrome
from tests._viewer_fixture import load_serve, write_task


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="task-run", timeout=timeout)
        return self.returncode


def _post(url: str, path: str, token: str, payload: object):
    request = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-ShowAndTell-Edit-Token": token,
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


def _store(tmp_path: Path, *, profile_launcher=None):
    serve = load_serve()
    write_task(tmp_path, "alpha-one")
    managed_profile = tmp_path / "chrome-profile"
    (managed_profile / "Default").mkdir(parents=True)
    extension = (managed_profile / "Default" / "Extensions" /
                 chrome.BRACKETT_EXTENSION_ID / "1.0.0_0")
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text("{}")
    calls = []
    processes = []

    class Client:
        assignment = {"erpnext": "erpnext_2"}

    class Session:
        lease_id = "parent-lease"
        client = Client()

    class Manager:
        def clear_stale(self, _applications):
            pass

    class Execution:
        session = Session()
        manager = Manager()

        def close(self):
            pass

    def allocate(_applications, _holder, _demo_root):
        return Execution()

    def popen(command, **kwargs):
        process = FakeProcess()
        calls.append((command, kwargs))
        processes.append(process)
        return process

    kwargs = {}
    if profile_launcher is not None:
        kwargs["profile_launcher"] = profile_launcher
    return serve.task_runs.TaskRunStore(
        tmp_path / "tasks", popen=popen, execution_allocator=allocate,
        managed_profile=managed_profile,
        port_allocator=lambda preferred, **_kwargs: preferred,
        **kwargs,
    ), calls, processes


def _draft_store(tmp_path: Path, slug: str = "captured-one"):
    serve = load_serve()
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    draft = tmp_path / "task-drafts" / slug
    (draft / "demo").mkdir(parents=True)
    (draft / "task.toml").write_text(
        f'[task]\nname = "{slug}"\napplications = ["gitlab"]\n'
        'primary_application = "gitlab"\nsummary = "Captured"\n')
    (draft / "demo" / "seed.json").write_text('{"gitlab": {}}\n')
    (draft / "demonstrate.py").write_text("# generated replay\n")
    (draft / "testcase.json").write_text(json.dumps({
        "status": "captured-draft", "name": slug,
        "applications": ["gitlab"], "primary_application": "gitlab",
        "surfaces": [{
            "id": "gitlab", "application": "gitlab", "label": "GitLab",
            "url": "http://127.0.0.1:8023/explore",
        }],
        "replay": {"generated": True, "action_count": 1},
    }))
    calls = []
    processes = []
    workspaces = []

    class Client:
        assignment = {"gitlab": "gitlab_1"}

    class Session:
        lease_id = "draft-lease"
        client = Client()

    class Execution:
        session = Session()

        def annotate(self, **details):
            self.details = details

        def close(self):
            pass

    class Workspace:
        def __init__(self, metadata):
            self.metadata = metadata
            self.execution = Execution()
            self.restores = []
            self.closed = False
            workspaces.append(self)

        def start(self):
            self.metadata["surfaces"][0]["url"] = (
                "http://127.0.0.1:49123/explore")

        def restore(self, **options):
            self.restores.append(options)

        def close(self):
            self.closed = True

    def popen(command, **kwargs):
        process = FakeProcess()
        calls.append((command, kwargs))
        processes.append(process)
        return process

    profile = tmp_path / "chrome-profile"
    return serve.task_runs.TaskRunStore(
        tasks, popen=popen, draft_workspace_factory=Workspace,
        execution_allocator=lambda *_args: Execution(),
        managed_profile=profile,
        port_allocator=lambda preferred, **_kwargs: preferred,
    ), draft, calls, processes, workspaces


def test_task_run_store_honors_managed_profile_environment(tmp_path, monkeypatch):
    serve = load_serve()
    profile = tmp_path / "custom-profile"
    monkeypatch.setenv("SHOWANDTELL_CHROME_PROFILE", str(profile))

    store = serve.task_runs.TaskRunStore(tmp_path / "tasks")

    assert store.managed_profile == profile


@pytest.mark.parametrize(
    ("adapter", "label"),
    [
        ("claude-teach", "Claude"),
        ("brackett-teach", "Brackett"),
        ("codex-record", "Codex"),
    ],
)
def test_task_run_store_launches_fresh_cli_run(tmp_path, adapter, label):
    store, calls, processes = _store(tmp_path)

    result = store.start("alpha-one", adapter)

    command, kwargs = calls[0]
    assert command[:7] == [
        sys.executable,
        "-m",
        "showAndTell.cli",
        adapter,
        "--task",
        str((tmp_path / "tasks/alpha-one").resolve()),
        "--no-cache",
    ]
    assert command[7:9] == (["--chrome-cdp-port", "9223"]
                            if adapter == "codex-record"
                            else ["--cdp-port", "9223"])
    if adapter == "codex-record":
        assert command[9:11] == ["--cdp-port", "9333"]
    assert "--worker" not in command
    assert "--display" not in command
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["env"]["SHOWANDTELL_FIXTURE_HOST_LEASE"] == "parent-lease"
    assert kwargs["env"]["SHOWANDTELL_EXECUTION_MANAGER_CHILD"] == "1"
    assert kwargs["env"]["SHOWANDTELL_HEAR_NARRATION"] == "0"
    assert json.loads(kwargs["env"]["SHOWANDTELL_FIXTURE_HOST_ASSIGNMENT"]) == {
        "erpnext": "erpnext_2"}
    assert kwargs["start_new_session"] is True
    assert result["status"] == "running"
    assert result["label"] == label
    assert result["hear_narration"] is False

    processes[0].returncode = 0
    assert store.status(result["id"])["status"] == "complete"
    processes[0].returncode = 7
    failed = store.status(result["id"])
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 7


@pytest.mark.parametrize(
    ("adapter", "product"),
    [("claude-teach", "claude"), ("brackett-teach", "brackett"),
     ("codex-record", "codex")],
)
def test_captured_task_uses_the_shared_run_contract(tmp_path, adapter, product):
    store, draft, calls, processes, workspaces = _draft_store(tmp_path)

    started = store.start(
        "captured-one", adapter, source="draft", hear_narration=True)

    command, options = calls[0]
    assert command[1:4] == ["-m", "showAndTell.cli", "capture-trial"]
    assert command[command.index("--draft") + 1] == str(draft.resolve())
    assert command[command.index("--product") + 1] == product
    assert started["source"] == "draft"
    assert started["status"] == "running"
    assert started["hear_narration"] is True
    assert options["env"]["SHOWANDTELL_FIXTURE_HOST_LEASE"] == "draft-lease"
    assert options["env"]["SHOWANDTELL_HEAR_NARRATION"] == "1"
    assert workspaces[0].restores == [{
        "use_export": True, "state_dir": draft / "demo",
        "snapshot_only": True,
    }]
    refreshed = json.loads((draft / "testcase.json").read_text())
    assert refreshed["surfaces"][0]["url"] == (
        "http://127.0.0.1:49123/explore")
    assert refreshed["surfaces"][0]["captured_url"] == (
        "http://127.0.0.1:8023/explore")

    processes[0].returncode = 0
    assert store.status(started["id"])["status"] == "complete"
    assert workspaces[0].closed is True


def test_captured_and_ordinary_tasks_share_one_run_slot(tmp_path):
    store, _draft, _calls, processes, _workspaces = _draft_store(tmp_path)
    write_task(tmp_path, "alpha-one")

    draft_run = store.start("captured-one", "claude-teach", source="draft")
    with pytest.raises(ValueError, match="another task is already running") as exc:
        store.start("alpha-one", "brackett-teach")

    assert exc.value.details["active_run"]["source"] == "draft"
    replacement = store.start(
        "alpha-one", "brackett-teach", replace=True)
    assert processes[0].returncode == -15
    assert store.status(draft_run["id"])["status"] == "failed"
    assert replacement["source"] == "task"


def test_captured_task_requires_a_generated_replay(tmp_path):
    store, draft, calls, _processes, _workspaces = _draft_store(tmp_path)
    (draft / "demonstrate.py").unlink()

    with pytest.raises(ValueError, match="no generated replay") as exc:
        store.start("captured-one", "claude-teach", source="draft")

    assert exc.value.status == 409
    assert calls == []


def test_active_captured_task_cannot_be_promoted(tmp_path):
    store, _draft, _calls, processes, _workspaces = _draft_store(tmp_path)

    class CaptureStore:
        def __init__(self):
            self.promoted = []

        def promote(self, slug):
            self.promoted.append(slug)
            return {"ok": True, "slug": slug, "promoted": True}

    capture_store = CaptureStore()
    store.start("captured-one", "claude-teach", source="draft")

    with pytest.raises(ValueError, match="finish the active run") as exc:
        store.promote_draft(capture_store, "captured-one")

    assert exc.value.status == 409
    assert capture_store.promoted == []
    processes[0].returncode = 0
    assert store.promote_draft(capture_store, "captured-one")["promoted"] is True


def test_task_run_store_rejects_bad_inputs_and_parallel_run(tmp_path):
    store, _calls, processes = _store(tmp_path)

    with pytest.raises(ValueError, match="adapter"):
        store.start("alpha-one", ["claude-teach"])
    with pytest.raises(ValueError, match="task name"):
        store.start("../alpha-one", "claude-teach")
    with pytest.raises(ValueError, match="no seed"):
        store.start("missing", "claude-teach")
    with pytest.raises(ValueError, match="hear_narration"):
        store.start("alpha-one", "claude-teach", hear_narration="yes")

    store.start("alpha-one", "claude-teach")
    with pytest.raises(ValueError, match="another task is already running") as exc:
        store.start("alpha-one", "codex-record")
    assert exc.value.status == 409
    assert exc.value.details["code"] == "task_run_busy"
    assert exc.value.details["active_run"]["task"] == "alpha-one"

    processes[0].returncode = 0
    assert store.start("alpha-one", "codex-record")["adapter"] == "codex-record"


def test_task_run_store_can_enable_speaker_monitor(tmp_path):
    store, calls, _processes = _store(tmp_path)

    result = store.start(
        "alpha-one", "brackett-teach", hear_narration=True)

    assert calls[0][1]["env"]["SHOWANDTELL_HEAR_NARRATION"] == "1"
    control = Path(
        calls[0][1]["env"]["SHOWANDTELL_HEAR_NARRATION_CONTROL"])
    assert control.read_text() == "1"
    assert result["hear_narration"] is True

    updated = store.set_hear_narration(result["id"], False)

    assert control.read_text() == "0"
    assert updated["hear_narration"] is False

    updated = store.set_hear_narration(result["id"], True)
    assert control.read_text() == "1"
    assert updated["hear_narration"] is True


def test_task_run_store_can_stop_and_replace_the_active_run(tmp_path, monkeypatch):
    store, calls, processes = _store(tmp_path)
    closed_profiles = []
    monkeypatch.setattr(
        chrome, "kill_all_managed_chrome",
        lambda profile: closed_profiles.append(profile))

    first = store.start("alpha-one", "claude-teach")
    replacement = store.start(
        "alpha-one", "brackett-teach", replace=True)

    assert processes[0].returncode == -15
    assert store.status(first["id"])["status"] == "failed"
    assert replacement["status"] == "running"
    assert replacement["adapter"] == "brackett-teach"
    assert len(calls) == 2
    assert closed_profiles == [store.managed_profile]


def test_task_run_store_can_cancel_an_active_run(tmp_path, monkeypatch):
    store, _calls, processes = _store(tmp_path)
    closed_profiles = []
    monkeypatch.setattr(
        chrome, "kill_all_managed_chrome",
        lambda profile: closed_profiles.append(profile))
    started = store.start("alpha-one", "brackett-teach")

    cancelled = store.cancel(started["id"])

    assert processes[0].returncode == -15
    assert cancelled["status"] == "cancelled"
    assert cancelled["exit_code"] == -15
    assert closed_profiles == [store.managed_profile]
    # Repeated cancellation is harmless and does not close the profile twice.
    assert store.cancel(started["id"])["status"] == "cancelled"
    assert closed_profiles == [store.managed_profile]


def test_task_run_store_creates_missing_managed_chrome_profile(tmp_path):
    store, calls, _processes = _store(tmp_path)
    store.managed_profile = tmp_path / "missing-profile"

    store.start("alpha-one", "codex-record")

    assert (store.managed_profile / "Default").is_dir()
    assert len(calls) == 1
    assert calls[0][1]["env"]["SHOWANDTELL_CHROME_PROFILE"] == str(
        store.managed_profile)


def test_task_run_store_starts_brackett_without_the_extension(tmp_path):
    """A missing Brackett extension is the readiness gate's job now: the run
    starts and pauses in-run with Continue/Cancel instead of refusing."""
    store, calls, _processes = _store(tmp_path)
    extension_root = (store.managed_profile / "Default" / "Extensions" /
                      chrome.BRACKETT_EXTENSION_ID)
    shutil.rmtree(extension_root)

    result = store.start("alpha-one", "brackett-teach")

    assert result["status"] == "running"
    assert calls, "the run must spawn; the gate handles the extension"


def test_task_run_status_reads_only_the_recent_log_tail(tmp_path):
    store, _calls, _processes = _store(tmp_path)
    result = store.start("alpha-one", "claude-teach")
    log_path = tmp_path / "runs/.viewer" / f"{result['id']}.log"
    log_path.write_bytes(b"old-prefix\n" + b"x" * 17000 + b"\nrecent-line\n")

    status = store.status(result["id"])

    assert "old-prefix" not in status["log"]
    assert status["log"].endswith("recent-line\n")
    assert len(status["log"].encode()) <= 16000


def test_task_run_http_api_requires_token_and_reports_status(tmp_path):
    serve = load_serve()
    store, _calls, processes = _store(tmp_path)
    server = serve.make_server(0)
    server.task_run_store = store
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, result = _post(
            url, "/api/task-runs", "wrong",
            {"task": "alpha-one", "adapter": "claude-teach"},
        )
        assert status == 403
        assert "token" in result["error"]

        status, result = _post(
            url, "/api/task-runs", server.question_edit_token,
            {"task": "alpha-one", "adapter": "claude-teach"},
        )
        assert status == 202
        assert result["status"] == "running"
        assert result["status_endpoint"].endswith("/status")
        assert result["cancel_endpoint"].endswith("/cancel")
        assert result["audio_endpoint"].endswith("/audio")

        status, muted = _post(
            url, result["audio_endpoint"], server.question_edit_token,
            {"hear_narration": False},
        )
        assert status == 200
        assert muted["hear_narration"] is False

        status, audible = _post(
            url, result["audio_endpoint"], server.question_edit_token,
            {"hear_narration": True},
        )
        assert status == 200
        assert audible["hear_narration"] is True

        status, cancelled = _post(
            url, result["cancel_endpoint"], server.question_edit_token, {},
        )
        assert status == 200
        assert cancelled["status"] == "cancelled"
        assert cancelled["exit_code"] == -15

        status, result = _post(
            url, "/api/task-runs", server.question_edit_token,
            {"task": "alpha-one", "adapter": "claude-teach"},
        )
        assert status == 202

        processes[1].returncode = 0
        status, final = _post(
            url, result["status_endpoint"], server.question_edit_token, {},
        )
        assert status == 200
        assert final["status"] == "complete"
        assert final["exit_code"] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_task_run_gate_round_trip(tmp_path):
    """A child pausing at a readiness gate surfaces in status; Continue and
    Cancel drop the marker files the child's gate polls for."""
    from showAndTell.students import readiness

    serve = load_serve()
    store, calls, _processes = _store(tmp_path)
    result = store.start("alpha-one", "codex-record")
    run_id = result["id"]
    _command, kwargs = calls[0]
    gate_dir = Path(kwargs["env"][readiness.GATE_DIR_ENV])

    assert result["gate"] is None
    with pytest.raises(serve.task_runs.TaskRunError, match="not waiting"):
        store.gate_action(run_id, "continue")

    gate_dir.mkdir(parents=True)
    (gate_dir / readiness.GATE_FILE).write_text(json.dumps(
        {"problem": "the plugin is disabled",
         "instructions": "enable it in Settings"}))
    status = store.status(run_id)
    assert status["gate"] == {"problem": "the plugin is disabled",
                              "instructions": "enable it in Settings"}

    store.gate_action(run_id, "continue")
    assert (gate_dir / readiness.ACK_FILE).exists()
    store.gate_action(run_id, "cancel")
    assert (gate_dir / readiness.CANCEL_FILE).exists()
    with pytest.raises(serve.task_runs.TaskRunError, match="continue"):
        store.gate_action(run_id, "abort")


def test_task_run_gate_opens_the_managed_browser_profile(tmp_path):
    from showAndTell.students import readiness

    opened = []
    store, calls, _processes = _store(
        tmp_path, profile_launcher=lambda profile, url, port:
        opened.append((profile, url, port)))
    result = store.start("alpha-one", "brackett-teach")
    gate_dir = Path(calls[0][1]["env"][readiness.GATE_DIR_ENV])
    gate_dir.mkdir(parents=True)
    gate_dir.joinpath(readiness.GATE_FILE).write_text(json.dumps({
        "problem": "Brackett is missing",
        "instructions": "Install it from https://store.example/brackett",
        "open_browser": True,
        "open_url": "https://store.example/brackett",
        "open_label": "Install Brackett",
    }))

    status = store.gate_action(result["id"], "open")

    assert opened == [(
        store.managed_profile, "https://store.example/brackett", 9223)]
    assert status["gate"]["open_label"] == "Install Brackett"


def test_task_run_login_gate_opens_the_single_managed_profile(tmp_path):
    from showAndTell.students import readiness

    opened = []
    store, calls, _processes = _store(
        tmp_path, profile_launcher=lambda profile, url, port:
        opened.append((profile, url, port)))
    result = store.start("alpha-one", "brackett-teach")
    gate_dir = Path(calls[0][1]["env"][readiness.GATE_DIR_ENV])
    gate_dir.mkdir(parents=True)
    gate_dir.joinpath(readiness.GATE_FILE).write_text(json.dumps({
        "problem": "Brackett is not signed in",
        "instructions": "Complete login",
        "open_browser": True,
        "open_url": "https://workspace.example/login",
        "open_label": "Open Brackett login",
    }))

    store.gate_action(result["id"], "open")

    assert opened == [(
        store.managed_profile, "https://workspace.example/login", 9223)]
    assert (store.managed_profile / "Default").is_dir()


def test_task_run_gate_can_refocus_the_run_browser_without_navigating(tmp_path):
    from showAndTell.students import readiness

    opened = []
    store, calls, _processes = _store(
        tmp_path, profile_launcher=lambda profile, url, port:
        opened.append((profile, url, port)))
    result = store.start("alpha-one", "claude-teach")
    gate_dir = Path(calls[0][1]["env"][readiness.GATE_DIR_ENV])
    gate_dir.mkdir(parents=True)
    gate_dir.joinpath(readiness.GATE_FILE).write_text(json.dumps({
        "problem": "Claude is not signed in",
        "instructions": "Use the open side panel",
        "open_browser": True,
        "open_label": "Open Claude sign-in",
    }))

    store.gate_action(result["id"], "open")

    assert opened == [(store.managed_profile, None, 9223)]


def test_task_run_gate_rejects_an_untrusted_browser_destination(tmp_path):
    from showAndTell.students import readiness

    store, calls, _processes = _store(tmp_path, profile_launcher=lambda *_: None)
    result = store.start("alpha-one", "brackett-teach")
    gate_dir = Path(calls[0][1]["env"][readiness.GATE_DIR_ENV])
    gate_dir.mkdir(parents=True)
    gate_dir.joinpath(readiness.GATE_FILE).write_text(json.dumps({
        "problem": "bad destination",
        "instructions": "do not open this",
        "open_browser": True,
        "open_url": "file:///tmp/not-allowed",
    }))

    with pytest.raises(ValueError, match="no browser destination"):
        store.gate_action(result["id"], "open")
