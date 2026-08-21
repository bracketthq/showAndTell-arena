"""Background regrade store and HTTP boundaries for the local viewer."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from showAndTell.viewer import regrades
from tests._viewer_fixture import load_serve, write_task


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def _saved_run(root: Path, task: str = "alpha-one") -> Path:
    run = root / "runs/20260818-120000-brackett-teach-alpha-one"
    run.mkdir(parents=True)
    (run / "comprehend-response.txt").write_text("A1: saved answer")
    (run / "task-runtime.json").write_text(json.dumps({"task": task}))
    return run


def _post(url: str, path: str, token: str, payload: object):
    request = urllib.request.Request(
        url + path,
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


def test_regrade_store_launches_replace_without_replaying(tmp_path):
    task = write_task(tmp_path, "alpha-one")
    run = _saved_run(tmp_path)
    calls = []
    processes = []

    def popen(command, **kwargs):
        process = FakeProcess()
        calls.append((command, kwargs))
        processes.append(process)
        return process

    store = regrades.RegradeStore(tmp_path, popen=popen)
    result = store.start("tasks/alpha-one", str(run.relative_to(tmp_path)))

    command, kwargs = calls[0]
    assert command == [
        sys.executable, "-m", "showAndTell.cli", "regrade",
        "--task", str(task.resolve()), "--run", str(run.resolve()), "--replace",
    ]
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert result["status"] == "running"

    with pytest.raises(regrades.RegradeError, match="already being regraded"):
        store.start("tasks/alpha-one", str(run.relative_to(tmp_path)))

    processes[0].returncode = 0
    assert store.status(result["id"])["status"] == "complete"


def test_regrade_store_rejects_cross_task_and_arbitrary_paths(tmp_path):
    write_task(tmp_path, "alpha-one")
    write_task(tmp_path, "beta-one")
    run = _saved_run(tmp_path, task="alpha-one")
    store = regrades.RegradeStore(tmp_path, popen=lambda *_a, **_kw: FakeProcess())

    with pytest.raises(regrades.RegradeError, match="different task"):
        store.start("tasks/beta-one", str(run.relative_to(tmp_path)))
    with pytest.raises(regrades.RegradeError, match="saved run was not found"):
        store.start("tasks/alpha-one", "tasks/alpha-one/demo")


def test_regrade_store_accepts_only_own_draft_trial(tmp_path):
    task = write_task(tmp_path, "captured-one")
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    task.rename(drafts / task.name)
    trial = drafts / "captured-one/trials/20260818-120000-brackett"
    trial.mkdir(parents=True)
    (trial / "comprehend-response.txt").write_text("saved response")
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    store = regrades.RegradeStore(tmp_path, popen=popen)
    result = store.start(
        "task-drafts/captured-one",
        "task-drafts/captured-one/trials/20260818-120000-brackett",
    )

    assert result["status"] == "running"
    assert "regrade" in calls[0][0]


def test_regrade_http_api_requires_token_and_reports_status(tmp_path):
    serve = load_serve()
    write_task(tmp_path, "alpha-one")
    run = _saved_run(tmp_path)
    processes = []

    def popen(_command, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    store = regrades.RegradeStore(tmp_path, popen=popen)
    server = serve.make_server(0)
    server.regrade_store = store
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    payload = {
        "task": "tasks/alpha-one",
        "run": str(run.relative_to(tmp_path)),
    }
    try:
        status, denied = _post(url, "/api/regrades", "wrong", payload)
        assert status == 403 and "token" in denied["error"]

        status, started = _post(
            url, "/api/regrades", server.question_edit_token, payload)
        assert status == 202
        assert started["status"] == "running"
        assert started["status_endpoint"].endswith("/status")

        processes[0].returncode = 0
        status, finished = _post(
            url, started["status_endpoint"], server.question_edit_token, {})
        assert status == 200
        assert finished["status"] == "complete"
    finally:
        server.shutdown()
        server.server_close()



def test_regrade_resolves_a_dataset_task_from_the_hub_cache(
        tmp_path, monkeypatch):
    """Published tasks live in the huggingface cache, outside the repo;
    their saved runs under runs/ must still be regradable."""
    published = tmp_path / "hub-cache" / "abcdef1234567890"
    task = write_task(published, "published-one")
    run = _saved_run(tmp_path, task="published-one")
    run_dir = tmp_path / "runs/20260818-120000-brackett-teach-published-one"
    run.rename(run_dir)
    monkeypatch.setattr(regrades.hub, "cached_dataset_root",
                        lambda **kwargs: published / "tasks")
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        return FakeProcess()

    store = regrades.RegradeStore(tmp_path, popen=popen)
    result = store.start(str(task.resolve()), str(run_dir.relative_to(tmp_path)))

    assert result["status"] == "running"
    assert "--task" in calls[0] and str(task.resolve()) in calls[0]
