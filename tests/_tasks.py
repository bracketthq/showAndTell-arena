"""Shared helpers for per-task test files and the task contract suite.

Every helper takes the task directory (or a file inside it) explicitly, so
one copy serves the per-task files that used to each carry their own.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
TASKS_DIR = ROOT / "tasks"


def all_task_dirs() -> list[Path]:
    return sorted(p for p in TASKS_DIR.iterdir() if p.is_dir())


def task_dir(name: str) -> Path:
    return TASKS_DIR / name


def load_module(name: str, path: Path):
    # taskload's loader registers the module in sys.modules before executing
    # (required by dataclasses and anything else that resolves the module
    # being initialized) and restores the prior binding after.
    from showAndTell.tasks import _loaded_module

    with _loaded_module(name, Path(path)) as mod:
        return mod


def load_task_logic(task: Path):
    from showAndTell.tasks import _module_name

    return load_module(_module_name("logic", task), task / "task_logic.py")


def quiz(task: Path) -> list[dict]:
    return json.loads((task / "quiz" / "questions.json").read_text())["questions"]


def quiz_by_id(task: Path) -> dict:
    return {q["id"]: q for q in quiz(task)}


def gaps(task: Path) -> list[dict]:
    return json.loads((task / "teacher" / "planted_gaps.json").read_text())["gaps"]


def on_step_keys(demonstrate_py: Path) -> list[str]:
    tree = ast.parse(demonstrate_py.read_text())
    keys = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "on_step" and node.args
                and isinstance(node.args[0], ast.Constant)):
            keys.append(node.args[0].value)
    return keys


def narration_lines(jsonl: Path) -> list[dict]:
    return [json.loads(line)
            for line in jsonl.read_text().splitlines() if line.strip()]


def narration_keys(jsonl: Path) -> list[str]:
    return [line["key"] for line in narration_lines(jsonl)]


def module_constants(py: Path) -> dict:
    tree = ast.parse(py.read_text())
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)):
            out[node.targets[0].id] = node.value.value
    return out


def make_draft(tmp_path: Path) -> Path:
    """A minimal viewer-captured draft (testcase.json only) for trial tests."""
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / "testcase.json").write_text(json.dumps({
        "status": "captured-draft",
        "name": "captured-demo",
        "surfaces": [
            {"id": "gitlab", "label": "GitLab", "url": "http://127.0.0.1:8023"},
            {"id": "zulip", "label": "Zulip", "url": "http://127.0.0.1:8083"},
        ],
        "steps": [{"id": "step-001", "at_ms": 20, "text": "Open the issue."}],
    }))
    return draft
