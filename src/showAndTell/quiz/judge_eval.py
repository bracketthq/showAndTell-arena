"""Human-labeled regression suite for evaluating the live judge itself."""
from __future__ import annotations

import json
from pathlib import Path

from showAndTell.core import llm

from .judge import (closed_equivalent_detailed, judge_rubric_detailed,
                    release_manifest, require_ready)


DEFAULT_CASES = Path(__file__).with_name("judge_eval_cases.jsonl")


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError(f"{path}:{line_no}: case needs a non-empty id")
        cases.append(case)
    ids = [case["id"] for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError(f"{path}: cases must be non-empty with unique ids")
    return cases


def evaluate(cases: list[dict], *, preflight: bool = True) -> dict:
    if preflight:
        if llm.mode() == "stub":
            raise RuntimeError(
                "judge eval requires a live judge; unset SHOWANDTELL_LLM=stub")
        require_ready(live=True)
    rows = []
    for case in cases:
        kind = case.get("kind")
        try:
            if kind == "rubric":
                decision = judge_rubric_detailed(
                    case["question"], case["rubric"], case["answer"])
                score = decision["score"]
                passed = case["min_score"] <= score <= case["max_score"]
                observed = score
            elif kind == "closed":
                decision = closed_equivalent_detailed(
                    case["question"], case["answer"], case["aliases"])
                observed = decision["equivalent"]
                passed = observed is case["expected_equivalent"]
            else:
                raise ValueError(f"unsupported case kind {kind!r}")
            rows.append({
                "id": case["id"], "kind": kind,
                "adversarial": bool(case.get("adversarial")),
                "passed": passed, "observed": observed,
                "reason": decision["reason"], "judge": decision["judge"],
            })
        except Exception as exc:
            rows.append({
                "id": case["id"], "kind": kind,
                "adversarial": bool(case.get("adversarial")),
                "passed": False, "error": {
                    "type": type(exc).__name__, "message": str(exc),
                },
            })
    adversarial = [row for row in rows if row["adversarial"]]
    passed = sum(row["passed"] for row in rows)
    attack_failures = sum(not row["passed"] for row in adversarial)
    return {
        "status": "pass" if passed == len(rows) else "fail",
        "judge": release_manifest(),
        "summary": {
            "passed": passed,
            "total": len(rows),
            "adversarial_passed": len(adversarial) - attack_failures,
            "adversarial_total": len(adversarial),
            "attack_success_rate": (
                attack_failures / len(adversarial) if adversarial else 0.0),
        },
        "cases": rows,
    }
