"""Load task artifacts: config, demonstration driver."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


_LOAD_LOCK = threading.RLock()
_MISSING = object()


@dataclass
class TaskConfig:
    name: str
    dir: Path
    # Real products used by the task. This is the whole declaration for a task
    # captured through the application architecture; the runtime composes a
    # session from it.
    applications: tuple[str, ...] = ()
    summary: str = ""
    primary_application: str = ""
    # "working" runs end-to-end against the deployed sites; "blocked" means the
    # task needs data the organic (WebArena-way, no-seed) sites don't provide.
    status: str = "working"
    status_reason: str = ""


def load_task(task_dir: Path) -> TaskConfig:
    cfg = tomllib.loads((task_dir / "task.toml").read_text())
    status = cfg.get("status", {})
    applications = tuple(cfg["task"].get("applications", ()))
    primary_application = cfg["task"].get("primary_application", "")
    if len(applications) != len(set(applications)):
        raise ValueError(f"{task_dir}: duplicate application names")
    if primary_application and primary_application not in applications:
        raise ValueError(
            f"{task_dir}: primary_application {primary_application!r} is not in applications"
        )
    if not applications:
        raise ValueError(f"{task_dir}: task must declare applications")
    if not primary_application:
        raise ValueError(f"{task_dir}: applications requires primary_application")
    return TaskConfig(
        name=cfg["task"]["name"],
        dir=task_dir,
        summary=cfg["task"].get("summary", ""),
        applications=applications,
        primary_application=primary_application,
        status=status.get("state", "working"),
        status_reason=status.get("reason", ""),
    )


def _module_name(kind: str, task_dir: Path) -> str:
    """Return an import-safe name unique to one task directory."""

    slug = task_dir.name.replace("-", "_")
    path_hash = hashlib.sha256(str(task_dir).encode()).hexdigest()[:16]
    return f"_showAndTell_{kind}_{slug}_{path_hash}"


def _restore_module(name: str, previous: object) -> None:
    if previous is _MISSING:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous  # type: ignore[assignment]


@contextmanager
def _loaded_module(
    name: str, path: Path, *, source: str | None = None
) -> Iterator[ModuleType]:
    """Execute one file as a registered module, then restore its old binding.

    Registering before execution is required by dataclasses and other code that
    resolves the module currently being initialized through ``sys.modules``.

    ``source`` executes supplied text in place of the file's own bytes while
    keeping the file's path as the code object's filename, so a traceback still
    points at the demonstrated driver rather than at a synthetic string.
    """

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name, _MISSING)
    sys.modules[name] = module
    try:
        if source is None:
            spec.loader.exec_module(module)
        else:
            exec(compile(source, str(path), "exec"), module.__dict__)
        yield module
    finally:
        _restore_module(name, previous)


@contextmanager
def load_task_logic(task_dir: Path) -> Iterator[ModuleType]:
    """Execute one task's ``task_logic.py`` in isolation and yield it.

    Registers the module under the same ``sys.modules`` key
    ``load_demonstrate_module`` uses, so the whole load-use-restore span is
    serialized behind the shared task-load lock: a concurrent task load can
    neither evict this module mid-use nor leak a stale binding.
    """

    task_dir = Path(task_dir).resolve()
    with _LOAD_LOCK:
        with _loaded_module(
            _module_name("task_logic", task_dir), task_dir / "task_logic.py"
        ) as module:
            yield module


def _remapped_source(
    path: Path,
    url_replacements: dict[str, str] | None,
    source_override: str | None,
) -> str | None:
    """Return the driver text to execute, or None to execute the file itself."""

    if not url_replacements and source_override is None:
        return None
    source = (
        source_override
        if source_override is not None
        else path.read_text(encoding="utf-8")
    )
    # Captured drivers are trusted repository code. Replace only complete
    # origins supplied by the capture-trial remapper; paths and selectors
    # remain byte-for-byte the demonstrated workflow. Longest origin first, so
    # one origin that prefixes another cannot partially rewrite it.
    for captured, live in sorted(
        (url_replacements or {}).items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        source = source.replace(captured, live)
    return source


def load_demonstrate_module(
    task_dir: Path,
    *,
    url_replacements: dict[str, str] | None = None,
    source_override: str | None = None,
) -> ModuleType:
    """Load a task driver module without leaking its import state.

    ``url_replacements`` and ``source_override`` let a capture trial run the
    demonstrated workflow against relocated origins; the remapped text is
    executed through the same isolation as the file itself, so a remapped load
    leaves no more behind than an ordinary one.
    """

    task_dir = task_dir.resolve()
    task_path = str(task_dir)
    logic_name = _module_name("task_logic", task_dir)
    demonstrate_name = _module_name("demonstrate", task_dir)
    demonstrate_path = task_dir / "demonstrate.py"
    source = _remapped_source(demonstrate_path, url_replacements, source_override)

    # The task modules use the intentionally shared bare name ``task_logic``.
    # Serialize the small import transaction so two task loads cannot cross-bind
    # that alias. Both the alias and path are restored even when either module
    # raises during import.
    with _LOAD_LOCK:
        previous_path = list(sys.path)
        previous_alias = sys.modules.get("task_logic", _MISSING)
        try:
            sys.path[:] = [
                task_path,
                *(entry for entry in sys.path if entry != task_path),
            ]
            with _loaded_module(
                logic_name, task_dir / "task_logic.py"
            ) as logic:
                sys.modules["task_logic"] = logic
                with _loaded_module(
                    demonstrate_name, demonstrate_path, source=source
                ) as demonstration:
                    module = demonstration
        finally:
            _restore_module("task_logic", previous_alias)
            sys.path[:] = previous_path
    return module


def load_demonstrate(
    task_dir: Path, *, url_replacements: dict[str, str] | None = None
):
    """Load a task driver without leaking its import state into the process."""

    return load_demonstrate_module(
        task_dir, url_replacements=url_replacements
    ).demonstrate
