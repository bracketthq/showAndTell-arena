from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest

from showAndTell.tasks import load_demonstrate


def _task(
    root: Path,
    name: str,
    *,
    logic: str = "ORIGIN = 'default'\n",
    demonstration: str = (
        "from task_logic import ORIGIN\n"
        "def demonstrate():\n"
        "    return ORIGIN\n"
    ),
) -> Path:
    task_dir = root / name
    task_dir.mkdir()
    (task_dir / "task_logic.py").write_text(logic)
    (task_dir / "demonstrate.py").write_text(demonstration)
    return task_dir


def _task_modules() -> set[str]:
    return {name for name in sys.modules if name.startswith("_showAndTell_")}


def test_load_restores_import_state_and_supports_registered_dataclass(
    tmp_path, monkeypatch
):
    task_dir = _task(
        tmp_path,
        "dataclass-task",
        logic=(
            "import sys\n"
            "from dataclasses import dataclass\n"
            "assert sys.modules[__name__].__name__ == __name__\n"
            "@dataclass(frozen=True)\n"
            "class Record:\n"
            "    origin: str\n"
        ),
        demonstration=(
            "from task_logic import Record\n"
            "def demonstrate():\n"
            "    return Record('dataclass-task')\n"
        ),
    )
    monkeypatch.delitem(sys.modules, "task_logic", raising=False)
    original_path = list(sys.path)
    original_modules = _task_modules()

    record = load_demonstrate(task_dir)()

    assert record.origin == "dataclass-task"
    assert record.__class__.__module__.startswith(
        "_showAndTell_task_logic_dataclass_task_"
    )
    assert sys.path == original_path
    assert "task_logic" not in sys.modules
    assert _task_modules() == original_modules


def test_load_restores_preexisting_task_logic_alias(tmp_path, monkeypatch):
    task_dir = _task(tmp_path, "alias-task")
    previous = ModuleType("task_logic")
    previous.ORIGIN = "ambient"
    monkeypatch.setitem(sys.modules, "task_logic", previous)

    assert load_demonstrate(task_dir)() == "default"
    assert sys.modules["task_logic"] is previous


def test_logic_import_failure_restores_alias_path_and_modules(tmp_path, monkeypatch):
    task_dir = _task(
        tmp_path,
        "broken-logic",
        logic="raise RuntimeError('logic failed')\n",
    )
    previous = ModuleType("task_logic")
    monkeypatch.setitem(sys.modules, "task_logic", previous)
    original_path = list(sys.path)
    original_modules = _task_modules()

    with pytest.raises(RuntimeError, match="logic failed"):
        load_demonstrate(task_dir)

    assert sys.modules["task_logic"] is previous
    assert sys.path == original_path
    assert _task_modules() == original_modules


def test_demonstration_import_failure_restores_alias_path_and_modules(
    tmp_path, monkeypatch
):
    task_dir = _task(
        tmp_path,
        "broken-demonstration",
        demonstration=(
            "from task_logic import ORIGIN\n"
            "raise RuntimeError('demonstration failed')\n"
        ),
    )
    previous = ModuleType("task_logic")
    monkeypatch.setitem(sys.modules, "task_logic", previous)
    original_path = list(sys.path)
    original_modules = _task_modules()

    with pytest.raises(RuntimeError, match="demonstration failed"):
        load_demonstrate(task_dir)

    assert sys.modules["task_logic"] is previous
    assert sys.path == original_path
    assert _task_modules() == original_modules


def test_repeated_load_restores_repeated_preexisting_path_entries(tmp_path):
    task_dir = _task(tmp_path, "repeated-path")
    task_path = str(task_dir.resolve())
    original_path = list(sys.path)
    try:
        sys.path[1:1] = [task_path, task_path]
        expected_path = list(sys.path)

        assert load_demonstrate(task_dir)() == "default"
        assert sys.path == expected_path
        assert load_demonstrate(task_dir)() == "default"
        assert sys.path == expected_path
    finally:
        sys.path[:] = original_path


def test_concurrent_cross_task_loads_do_not_cross_bind(tmp_path):
    demonstration = (
        "import time\n"
        "time.sleep(0.05)\n"
        "from task_logic import ORIGIN\n"
        "def demonstrate():\n"
        "    return ORIGIN\n"
    )
    tasks = [
        _task(
            tmp_path,
            f"concurrent-{origin}",
            logic=f"ORIGIN = {origin!r}\n",
            demonstration=demonstration,
        )
        for origin in ("alpha", "bravo", "charlie", "delta")
    ]
    original_path = list(sys.path)
    original_alias = sys.modules.get("task_logic")
    had_alias = "task_logic" in sys.modules

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        drivers = list(executor.map(load_demonstrate, tasks))

    assert [driver() for driver in drivers] == [
        "alpha",
        "bravo",
        "charlie",
        "delta",
    ]
    assert sys.path == original_path
    if had_alias:
        assert sys.modules["task_logic"] is original_alias
    else:
        assert "task_logic" not in sys.modules
