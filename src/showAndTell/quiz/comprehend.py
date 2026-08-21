"""Shared COMPREHEND phase: quiz whatever system just learned the workflow and
grade the answers with the benchmark's grader. Adapter-agnostic — each adapter
supplies an `ask(message) -> response_text` for its own chat surface."""
from __future__ import annotations

import json
import re
from pathlib import Path

from showAndTell.core import llm
from showAndTell.core.llm import reply_has_answer

from . import protocol
from .judge import (closed_equivalent_detailed, grade_closed,
                    grade_multiple_choice, judge_rubric_detailed,
                    release_manifest)


RESPONSE_ARTIFACT_NAME = "comprehend-response.txt"


def question_count(task) -> int:
    """Number of quiz questions this task carries — for run-progress labels."""
    return len(json.loads((task.dir / "quiz" / "questions.json").read_text())["questions"])


def build_message(task, intro: str | None = None) -> tuple[str, list[dict]]:
    quiz = json.loads((task.dir / "quiz" / "questions.json").read_text())["questions"]
    n = len(quiz)

    def rendered(q):
        if q["type"] == "closed":
            return f"[EXACT] {q['question']}"
        if q["type"] == "multiple_choice":
            choices = " ".join(
                f"{choice['id']}) {choice['text']}" for choice in q["options"])
            return f"[CHOICE] {q['question']} Choices: {choices}"
        return f"[EXPLAIN] {q['question']}"

    inline = "  ".join(f"({i + 1}) {rendered(q)}" for i, q in enumerate(quiz))
    fmt = " ;; ".join(f"A{i + 1}: <ans>" for i in range(n))
    lead = intro or (f"Based ONLY on the {task.name} workflow you just learned, "
                     "answer these comprehension questions.")
    instructions = (
        "For [EXACT], reply with only the requested value or tuple and no "
        "explanation. For [EXPLAIN], answer concisely in your own words. "
        "For [CHOICE], reply with only the option identifier. "
    )
    msg = (f"{lead} {instructions}Reply on ONE line, exactly this format: {fmt} . "
           f"Questions: {inline}")
    return msg, quiz


def grade(task, quiz: list[dict], response: str) -> dict:
    idx = response.rfind("A1:")
    ans = response[idx:] if idx >= 0 else ""
    parsed = {}
    n = len(quiz)
    for i in range(1, n + 1):
        # Interior answers are bounded by the next 'A<i+1>:' marker even across
        # wrapped lines — chat surfaces break one logical line freely. Only when
        # no next marker exists (the final answer, or a truncated reply) does a
        # newline end the answer: adapters return whole-page innerText, so an
        # unbounded final capture would swallow the UI chrome after the reply.
        marker = rf"(?:\s*;;\s*|\s+)A{i + 1}:"
        if i < n and re.search(marker, ans):
            m = re.search(rf"A{i}:\s*(.*?){marker}", ans, re.S)
        else:
            m = re.search(rf"A{i}:\s*(.*?)(?:\s*\n|\s*$)", ans, re.S)
        if m:
            parsed[i] = m.group(1).strip()

    # Three question types coexist across task generations: "multiple_choice"
    # (options + correct_option), "closed" (exact alias match, then a semantic
    # equivalence judge so paraphrase isn't scored as ignorance), and the
    # LLM-judged fallthrough ("rubric"/"llm_judge", scored 0..1).
    per, closed_ok, closed_total, mcq_ok, mcq_total = [], 0, 0, 0, 0
    judge_failures = 0
    judge_config = protocol.load()
    for i, q in enumerate(quiz, 1):
        a = parsed.get(i, "")
        if q["type"] == "multiple_choice":
            mcq_total += 1
            ok = grade_multiple_choice(a, q["correct_option"])
            mcq_ok += ok
            per.append({"id": q["id"], "type": "multiple_choice",
                        "answer": a, "ok": bool(ok)})
        elif q["type"] == "closed":
            closed_total += 1
            ok = grade_closed(a, q["answer_aliases"])
            # "grader" records which mechanism decided, keeping semantic calls
            # auditable. Stub mode stays exact-only so offline runs are
            # deterministic; a judge that errors out counts as a miss.
            grader = "exact"
            judge_info = None
            if not ok and a and llm.mode() != "stub":
                grader = "semantic"
                decision = None
                errors = []
                for _ in range(judge_config.max_attempts):
                    try:
                        decision = closed_equivalent_detailed(
                            q.get("question", ""), a, q["answer_aliases"])
                        break
                    except Exception as exc:
                        errors.append({
                            "type": type(exc).__name__, "message": str(exc),
                        })
                if decision:
                    ok = bool(decision["equivalent"])
                    judge_info = {
                        **decision["judge"],
                        "reason": decision["reason"],
                        "attempts": len(errors) + 1,
                        "errors": errors,
                    }
                else:
                    ok = False
                    judge_failures += 1
                    judge_info = {
                        **release_manifest(judge_config),
                        "status": "error",
                        "attempts": len(errors),
                        "errors": errors,
                    }
            closed_ok += ok
            per.append({"id": q["id"], "type": "closed",
                        "answer": a, "ok": bool(ok), "grader": grader,
                        "judge": judge_info})
        else:
            # One retry: a transient judge failure must stay distinguishable
            # from a wrong answer, not silently score 0 on the first hiccup.
            decision = None
            errors = []
            for _ in range(judge_config.max_attempts):
                try:
                    decision = judge_rubric_detailed(
                        q["question"], q["rubric"], a)
                    break
                except Exception as exc:
                    errors.append({
                        "type": type(exc).__name__, "message": str(exc),
                    })
                    continue
            sc = decision["score"] if decision else None
            if decision:
                judge_info = {
                    **decision["judge"],
                    "reason": decision["reason"],
                    "attempts": len(errors) + 1,
                    "errors": errors,
                }
            else:
                judge_failures += 1
                judge_info = {
                    **release_manifest(judge_config),
                    "status": "error",
                    "attempts": len(errors),
                    "errors": errors,
                }
            per.append({"id": q["id"], "type": q["type"],
                        "answer": a, "score": sc,
                        "ok": bool(sc is not None and sc >= 0.7),
                        "judge": judge_info})
    # Headline: mean over every question. Exact-graded kinds (multiple choice,
    # closed) -> 1/0; judged kinds -> the judge's 0..1. Infrastructure failure
    # makes the whole result explicitly incomplete instead of changing the
    # measured product score.
    points = [(1.0 if p["ok"] else 0.0) if p["type"] in ("multiple_choice", "closed")
              else p["score"] for p in per if p.get("score") is not None
              or p["type"] in ("multiple_choice", "closed")]
    complete = judge_failures == 0
    score = (round(sum(points) / len(per), 4) if complete and per
             else (0.0 if complete else None))
    return {"per_question": per, "score": score,
            "status": "complete" if complete else "incomplete",
            "judge": release_manifest(judge_config),
            "judge_failures": judge_failures,
            "closed_correct": closed_ok, "closed_total": closed_total,
            "multiple_choice_correct": mcq_ok, "multiple_choice_total": mcq_total}


def zero_result(task, reason: str) -> dict:
    """Build a completed zero without contacting a grader or product chat.

    This represents a measured product outcome, such as Claude returning an
    empty generated shortcut, rather than an infrastructure or judge failure.
    """
    _message, quiz = build_message(task)
    judge_manifest = release_manifest(protocol.load())
    per = []
    closed_total = 0
    multiple_choice_total = 0
    for question in quiz:
        item = {
            "id": question["id"], "type": question["type"],
            "answer": "", "ok": False,
        }
        if question["type"] == "multiple_choice":
            multiple_choice_total += 1
        elif question["type"] == "closed":
            closed_total += 1
            item.update({"grader": "not-run", "judge": None})
        else:
            item.update({
                "score": 0.0,
                "judge": {**judge_manifest, "status": "not-run",
                          "reason": reason},
            })
        per.append(item)
    return {
        "per_question": per,
        "score": 0.0,
        "status": "complete",
        "judge": judge_manifest,
        "judge_failures": 0,
        "closed_correct": 0,
        "closed_total": closed_total,
        "multiple_choice_correct": 0,
        "multiple_choice_total": multiple_choice_total,
        "completion_reason": reason,
    }


def run(task, ask, out=print, intro: str | None = None,
        response_artifact: Path | None = None) -> dict:
    """Quiz via ``ask(message) -> response`` and grade the complete response.

    When ``response_artifact`` is supplied, the exact text returned by the
    adapter is persisted before any prompt-echo removal or parsing. This keeps
    grading reproducible even when an adapter returns surrounding page chrome.
    """
    msg, quiz = build_message(task, intro=intro)
    # Drafts are replayable before their comprehension questions have been
    # authored.  Treat that as a completed, empty measurement instead of
    # opening the product chat with an answer template that contains no
    # questions.  Some product UIs never acknowledge that malformed prompt,
    # which used to leave an otherwise successful replay hanging forever.
    if not quiz:
        if response_artifact is not None:
            Path(response_artifact).write_text("", encoding="utf-8")
        return grade(task, quiz, "")
    resp = ask(msg)
    if response_artifact is not None:
        Path(response_artifact).write_text(resp, encoding="utf-8")
    # The chat echoes our prompt (which contains the 'A1: <ans>' template); grade
    # only what follows the first echo so we read the answer, not the template.
    # (Splitting on the last occurrence would discard the reply whenever the model
    # quotes the final question back.)
    tail = msg[-45:]
    if tail in resp:
        resp = resp.split(tail, 1)[-1]
    return grade(task, quiz, resp)


def print_results(task, r: dict, label: str, out=print) -> None:
    out("")
    out(f"══════ COMPREHEND — {task.name} ({label}) ══════")
    for p in r["per_question"]:
        if p["type"] in {"multiple_choice", "closed"}:
            out(f"  {p['id']:4} {'✓' if p['ok'] else '✗'}  {p['answer'][:66]}")
        else:
            sc = p["score"]
            out(f"  {p['id']:4} LLM {sc if sc is not None else '?'}  {p['answer'][:58]}")
    exact_ok = r.get("multiple_choice_correct", 0) + r.get("closed_correct", 0)
    exact_total = r.get("multiple_choice_total", 0) + r.get("closed_total", 0)
    out(f"  ── exact (multiple choice + closed): {exact_ok}/{exact_total}")
    if r.get("status", "complete") == "complete":
        out(f"  ── score: {r['score']}")
    else:
        out(f"  ── score: ungraded ({r.get('judge_failures', 0)} judge failures)")
