import json
from dataclasses import fields
from pathlib import Path

import pytest

from showAndTell.task_runtime import TaskRuntime, start_task
from showAndTell.tasks import TaskConfig


class FakeSession:
    last = None

    def __init__(self, applications, **_kwargs):
        self.applications = tuple(applications)
        self.urls = {"alpha": "http://agent.test:8123"}
        self.seeded = None
        self.closed = False
        self.events = []
        FakeSession.last = self

    def start(self, *, ensure_started=None):
        if ensure_started is not None:
            ensure_started(self.applications, "lease-1")

    def reset(self):
        self.events.append("reset")

    def restore_snapshots(self, directory):
        self.events.append(("restore", Path(directory)))
        return []

    def seed(self, document):
        self.events.append("seed")
        self.seeded = document

    def browser_credentials(self, primary):
        assert primary == "alpha"
        return {
            "email": "agent@example.test",
            "password": "pw",
            "_showAndTell_applications": {
                "alpha": {"url": self.urls["alpha"]}
            },
        }

    def close(self):
        self.closed = True


class FakeHostClient:
    def __init__(self):
        self.started = []

    def start(self, application, *, wait=True):
        self.started.append((application, wait))


class SnapshotSession(FakeSession):
    def restore_snapshots(self, directory):
        self.events.append(("restore", Path(directory)))
        return ["alpha"]


class BrowserUrlSession(FakeSession):
    def runtime_url(self, primary):
        assert primary == "alpha"
        return "http://connector.test:8456"


def test_task_runtime_exposes_only_real_runtime_state() -> None:
    assert {field.name for field in fields(TaskRuntime)} == {
        "task", "seed", "run_id", "app_url", "credentials",
        "_session", "_closed",
    }


def test_start_task_uses_base_url_and_application_scoped_seed(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "demo").mkdir(parents=True)
    (task_dir / "demo/seed.json").write_text(json.dumps({
        "alpha": {"records": [1]},
    }))

    def load(_path):
        return TaskConfig(
            name="sample", dir=task_dir,
            applications=("alpha",), primary_application="alpha",
        )

    host_client = FakeHostClient()
    runtime = start_task(
        task_dir,
        task_loader=load,
        session_factory=FakeSession,
        host_client_factory=lambda: host_client,
    )

    assert runtime.app_url == "http://agent.test:8123"
    assert runtime.credentials["_showAndTell_applications"]["alpha"]["url"] == (
        runtime.app_url
    )
    assert FakeSession.last.seeded == {"alpha": {"records": [1]}}
    assert FakeSession.last.events == [
        "reset", ("restore", task_dir / "demo"), "seed"
    ]
    assert host_client.started == [("alpha", True)]
    runtime.close()
    assert FakeSession.last.closed is True


def test_start_task_uses_the_sessions_explicit_runtime_url(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "demo").mkdir(parents=True)
    (task_dir / "demo/seed.json").write_text(json.dumps({
        "alpha": {"records": [1]},
    }))

    def load(_path):
        return TaskConfig(
            name="sample", dir=task_dir,
            applications=("alpha",), primary_application="alpha",
        )

    runtime = start_task(
        task_dir,
        task_loader=load,
        session_factory=BrowserUrlSession,
        host_client_factory=FakeHostClient,
    )

    assert runtime.app_url == "http://connector.test:8456"
    runtime.close()


@pytest.mark.parametrize(
    "document,problem",
    (
        ({"records": [1]}, "missing=\\['alpha'\\], unknown=\\['records'\\]"),
        ({"alpha": [], "other": {}}, "unknown=\\['other'\\]"),
        ({"alpha": []}, "application blocks must be objects"),
    ),
)
def test_start_task_rejects_noncanonical_seed_documents(
    tmp_path: Path,
    document: dict,
    problem: str,
) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "demo").mkdir(parents=True)
    (task_dir / "demo/seed.json").write_text(json.dumps(document))

    def load(_path):
        return TaskConfig(
            name="sample", dir=task_dir,
            applications=("alpha",), primary_application="alpha",
        )

    with pytest.raises(ValueError, match=problem):
        start_task(
            task_dir,
            task_loader=load,
            session_factory=FakeSession,
            host_client_factory=FakeHostClient,
        )


def test_start_task_restores_declared_snapshot_before_structured_seed(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "demo").mkdir(parents=True)
    (task_dir / "demo/seed.json").write_text(json.dumps({
        "alpha": {"snapshot": "alpha-state.tar", "records": [1]},
    }))

    def load(_path):
        return TaskConfig(
            name="snapshot-task", dir=task_dir,
            applications=("alpha",), primary_application="alpha",
        )

    runtime = start_task(
        task_dir,
        task_loader=load,
        session_factory=SnapshotSession,
        host_client_factory=FakeHostClient,
    )

    assert SnapshotSession.last.events == [
        "reset", ("restore", task_dir / "demo"), "seed"
    ]
    assert SnapshotSession.last.seeded == {
        "alpha": {"snapshot": "alpha-state.tar", "records": [1]},
    }
    runtime.close()


def test_start_task_fails_when_declared_snapshot_was_not_restored(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "demo").mkdir(parents=True)
    (task_dir / "demo/seed.json").write_text(json.dumps({
        "alpha": {"snapshot": "alpha-state.tar"},
    }))

    def load(_path):
        return TaskConfig(
            name="snapshot-task", dir=task_dir,
            applications=("alpha",), primary_application="alpha",
        )

    with pytest.raises(
        RuntimeError,
        match=r"did not restore declared snapshots: alpha \(alpha-state.tar\)",
    ):
        start_task(
            task_dir,
            task_loader=load,
            session_factory=FakeSession,
            host_client_factory=FakeHostClient,
        )

    assert FakeSession.last.events == [
        "reset", ("restore", task_dir / "demo")
    ]
    assert FakeSession.last.seeded is None
    assert FakeSession.last.closed is True
