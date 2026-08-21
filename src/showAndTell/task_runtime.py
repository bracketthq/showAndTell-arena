"""Central task-to-application lifecycle.

Every product adapter must enter through :func:`running_task`.  A run owns one
task-specific seed and, when the fixture supports it, an isolated application
namespace (containers, network, writable volumes, and host ports).  Immutable
image layers and explicitly read-only datasets remain shared by Docker.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from showAndTell.applications.session import AppSession

from showAndTell.tasks import TaskConfig, load_task
_RUN_COMPONENT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class TaskSeed:
    """A validated seed owned by exactly one task directory."""

    path: Path
    data: dict[str, Any]
    sha256: str


def load_task_seed(task: TaskConfig) -> TaskSeed:
    """Load and validate one application-keyed task seed."""
    path = task.dir / "demo" / "seed.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"task {task.name!r} has no demo/seed.json") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: task seed must be a JSON object")
    declared = set(task.applications)
    present = set(value)
    if present != declared:
        missing = sorted(declared - present)
        unknown = sorted(present - declared)
        raise ValueError(
            f"{path}: seed application blocks do not match task.toml; "
            f"missing={missing}, unknown={unknown}"
        )
    invalid = sorted(name for name, block in value.items()
                     if not isinstance(block, dict))
    if invalid:
        raise ValueError(f"{path}: application blocks must be objects: {invalid}")
    return TaskSeed(path=path, data=value, sha256=hashlib.sha256(raw).hexdigest())


def new_run_id(task_name: str) -> str:
    """Return a Docker/hostname-safe identity for one task execution."""

    task = _RUN_COMPONENT.sub("-", task_name.lower()).strip("-")[:36] or "task"
    return f"{task}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class TaskRuntime:
    """A prepared application instance held for the duration of one task run."""

    task: TaskConfig
    seed: TaskSeed
    run_id: str
    app_url: str
    credentials: dict[str, Any]
    _session: AppSession
    _closed: bool = False

    def manifest(self) -> dict[str, Any]:
        """Return auditable, secret-free provenance for the prepared run."""
        return {
            "task": self.task.name,
            "applications": list(self.task.applications),
            "primary_application": self.task.primary_application,
            "run_id": self.run_id,
            "seed": {
                "path": str(self.seed.path),
                "sha256": self.seed.sha256,
            },
            "app_url": self.app_url,
        }

    def write_manifest(self, directory: Path) -> Path:
        path = Path(directory) / "task-runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.manifest(), indent=2) + "\n")
        return path

    def close(self) -> None:
        if self._closed:
            return
        self._session.close()
        self._closed = True


def declared_snapshots(document: Mapping[str, Any]) -> dict[str, str]:
    """Return application snapshots the prepared seed requires."""
    declared: dict[str, str] = {}
    for application, block in document.items():
        if not isinstance(block, Mapping):
            continue
        snapshot = block.get("snapshot")
        if isinstance(snapshot, str) and snapshot:
            declared[str(application)] = snapshot
    return declared


def start_task(
    task_dir: Path,
    *,
    run_id: str | None = None,
    task_loader: Callable[[Path], TaskConfig] = load_task,
    session_factory: Callable[..., AppSession] = AppSession,
    host_client_factory: Callable[[], Any] | None = None,
) -> TaskRuntime:
    """Start and deterministically prepare the applications for one task."""

    task_dir = Path(task_dir).resolve()
    task = task_loader(task_dir)
    seed = load_task_seed(task)
    identity = run_id or new_run_id(task.name)
    if not task.applications:
        raise ValueError(
            f"task {task.name!r} declares no applications")
    from showAndTell.applications.host.client import local_agent_client

    ordered = (task.primary_application,) + tuple(
        name for name in task.applications if name != task.primary_application)
    client = (host_client_factory or local_agent_client)()
    session = session_factory(
        ordered, client=client, holder=f"task-{identity}",
        demo_root=task_dir / "demo")
    try:
        def ensure_started(applications, _lease_id) -> None:
            for name in applications:
                client.start(name, wait=True)

        session.start(ensure_started=ensure_started)
        session.reset()
        restored = set(session.restore_snapshots(task_dir / "demo"))
        required = declared_snapshots(seed.data)
        missing = set(required) - restored
        if missing:
            details = ", ".join(
                f"{name} ({required[name]})" for name in sorted(missing)
            )
            raise RuntimeError(
                f"task {task.name!r} did not restore declared snapshots: {details}"
            )
        session.seed(seed.data)
        # Demonstration adapters append their own native routes. Passing the
        # manifest's surface entry here would turn /login into /login/login.
        runtime_url = getattr(session, "runtime_url", None)
        app_url = (
            runtime_url(task.primary_application)
            if callable(runtime_url)
            else session.urls[task.primary_application]
        )
        return TaskRuntime(
            task=task, seed=seed, run_id=identity,
            app_url=app_url,
            credentials=session.browser_credentials(task.primary_application),
            _session=session,
        )
    except BaseException:
        session.close()
        raise


@contextmanager
def running_task(task_dir: Path, **kwargs: Any) -> Iterator[TaskRuntime]:
    runtime = start_task(task_dir, **kwargs)
    try:
        yield runtime
    finally:
        runtime.close()


@contextmanager
def booted_task(task_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield the seeded application URL and credentials for local tooling."""
    with running_task(Path(task_dir)) as runtime:
        yield runtime.app_url, runtime.credentials
