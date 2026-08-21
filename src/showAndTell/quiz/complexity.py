"""Task Complexity Index (TCI): how hard a task is to learn from its demo.

Six summed dimensions. Pass 1 parameters are counted from the task's own
files; Pass 2 flags are author-declared in task.toml's [complexity] block and
are REQUIRED — a missing or invalid block is a hard error, so every task
declares how it should be scored (rubric: docs/COMPLEXITY.md).

    D1 rule       = log2(1+branches) + 0.5*(outcomes-1) + precedence
    D2 evidence   = 1.5*(hops-1) + (systems-1)
    D3 plan       = 1.5*(plan-1) + chained + state
    D4 precision  = arithmetic + optimize
    D5 inference  = never_rules
    D6 signal     = 0.25*steps + words/100 + binding
    TCI           = D1+D2+D3+D4+D5+D6      tiers: <8.5, <10.5, <12.5, <15, rest

Stdlib-only on purpose: the viewer and auditing tools can use this module
without pulling in runtime dependencies.
"""
from __future__ import annotations

import ast
import json
import math
import tomllib
from pathlib import Path

# Author-declared flags: key -> (lo, hi) for ints, or bool for true/false.
_FLAGS: dict[str, tuple[int, int] | type] = {
    "hops": (1, 3),          # lookups/records consulted per decision
    "systems": (1, 3),       # distinct applications spanned
    "plan": (1, 3),          # actions per processed item
    "chained": bool,         # later step depends on an earlier step's result
    "state": bool,           # mutable state carries across items
    "precedence": (0, 2),    # precedence levels among competing rules
    "optimize": bool,        # pick-best over candidates with tie-break
    "never_rules": bool,     # a "never do X" rule must be inferred
    "binding": (1, 2),       # 1 = values on screen only; 2 = matched across screens
}
_TIER_EDGES = (8.5, 10.5, 12.5, 15.0)


class ComplexityError(Exception):
    """A task's complexity inputs are missing or invalid."""


def _read_text(task_dir: Path, rel: str) -> str:
    try:
        return (task_dir / rel).read_text(encoding="utf-8")
    except OSError as e:
        raise ComplexityError(f"{task_dir.name}/{rel}: unreadable ({e})") from e


def _branches(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.While)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
    return n


def _outcomes(tree: ast.AST) -> int:
    return len({ast.dump(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Return) and n.value is not None})


def _arithmetic(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            return True
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Gt, ast.Lt, ast.GtE, ast.LtE)) for op in node.ops):
            return True
    return False


def auto_metrics(task_dir: Path) -> dict:
    """Pass 1: parameters counted from the task's own files."""
    try:
        logic = ast.parse(_read_text(task_dir, "task_logic.py"))
    except SyntaxError as e:
        raise ComplexityError(f"{task_dir.name}/task_logic.py: {e}") from e
    steps = _read_text(task_dir, "demonstrate.py").count("on_step(")
    words = 0
    for line in _read_text(task_dir, "demo/narration_script.jsonl").splitlines():
        if not line.strip():
            continue
        try:
            words += len(json.loads(line).get("text", "").split())
        except json.JSONDecodeError:
            continue  # the viewer warns-and-skips bad lines; count what parses
    return {"branches": _branches(logic), "outcomes": max(1, _outcomes(logic)),
            "arithmetic": _arithmetic(logic),
            "steps": steps, "words": words}


def read_flags(task_dir: Path) -> dict:
    """Pass 2: the author-declared [complexity] block, validated. Hard error
    when absent or malformed — every task must declare its flags."""
    where = f"{task_dir.name}/task.toml"
    try:
        cfg = tomllib.loads(_read_text(task_dir, "task.toml"))
    except tomllib.TOMLDecodeError as e:
        raise ComplexityError(f"{where}: bad TOML ({e})") from e
    block = cfg.get("complexity")
    if not isinstance(block, dict):
        raise ComplexityError(f"{where}: missing [complexity] block — declare the "
                              f"task's complexity flags (see docs/COMPLEXITY.md)")
    block = dict(block)
    # Captures authored by a viewer process started before planted-gap support
    # was removed can still contain this retired flag. It no longer contributes
    # to the score, but it should not make those otherwise valid drafts unloadable.
    block.pop("boundary_gap", None)
    if unknown := sorted(set(block) - set(_FLAGS)):
        raise ComplexityError(f"{where}: unknown [complexity] key(s): {', '.join(unknown)}")
    if missing := sorted(set(_FLAGS) - set(block)):
        raise ComplexityError(f"{where}: missing [complexity] key(s): {', '.join(missing)}")
    for key, spec in _FLAGS.items():
        v = block[key]
        if spec is bool:
            if not isinstance(v, bool):
                raise ComplexityError(f"{where}: {key} must be true/false, got {v!r}")
        # bool is an int subclass — reject it explicitly for the int flags
        elif isinstance(v, bool) or not isinstance(v, int) or not spec[0] <= v <= spec[1]:
            raise ComplexityError(
                f"{where}: {key} must be an integer {spec[0]}..{spec[1]}, got {v!r}")
    return block


def score_task(task_dir: Path) -> dict:
    """Score one task -> {"params", "dims", "tci", "tier"}."""
    task_dir = Path(task_dir)
    m = auto_metrics(task_dir)
    f = read_flags(task_dir)
    dims = {
        "rule": math.log2(1 + m["branches"]) + 0.5 * (m["outcomes"] - 1) + f["precedence"],
        "evidence": 1.5 * (f["hops"] - 1) + (f["systems"] - 1),
        "plan": 1.5 * (f["plan"] - 1) + f["chained"] + f["state"],
        "precision": m["arithmetic"] + f["optimize"],
        "inference": f["never_rules"],
        "signal": 0.25 * m["steps"] + m["words"] / 100 + f["binding"],
    }
    tci = sum(dims.values())
    return {"params": {**m, **f},
            "dims": {k: round(v, 2) for k, v in dims.items()},
            "tci": round(tci, 2),
            "tier": 1 + sum(tci >= edge for edge in _TIER_EDGES)}
