"""Shared builders: a miniature tasks/ + runs/.cache tree for the viewer tests."""
from __future__ import annotations

import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_generate():
    """Return the viewer's data-generation module."""
    from showAndTell.viewer import generate

    return generate


def load_serve():
    """Return the viewer's local HTTP server module."""
    from showAndTell.viewer import serve

    return serve


def load_capture():
    """Return the viewer's capture orchestration module."""
    from showAndTell.viewer import capture

    return capture


# Neutral flag values for application tasks; real tasks declare their own.
COMPLEXITY_BLOCK = """
[complexity]
hops = 1
systems = 1
plan = 1
chained = false
state = false
precedence = 0
optimize = false
never_rules = false
binding = 1
"""


def write_task(root: Path, name: str, application: str = "erpnext", *,
               questions: list | None = None,
               narration: list | None = None, toml: str | None = None) -> Path:
    d = root / "tasks" / name
    (d / "quiz").mkdir(parents=True)
    (d / "demo").mkdir()
    (d / "task.toml").write_text(
        toml if toml is not None
        else f'[task]\nname = "{name}"\napplications = ["{application}"]\nprimary_application = "{application}"\nsummary = "Summary of {name}."\n'
             + COMPLEXITY_BLOCK)
    (d / "quiz" / "questions.json").write_text(json.dumps({"questions": questions or []}))
    (d / "demo" / "narration_script.jsonl").write_text(
        "".join(json.dumps(n) + "\n" for n in (narration or [])))
    (d / "demo" / "seed.json").write_text("{}\n")
    (d / "demonstrate.py").write_text("# the demonstrated workflow\n")
    (d / "task_logic.py").write_text("# the decision rule\n")
    return d


def write_cache(root: Path, adapter: str, task: str, cached_at: str, *,
                score: float = 0.5, closed_correct: int = 1, closed_total: int = 2,
                per_question: list | None = None,
                run_dir: str = "runs/demo") -> Path:
    cache = root / "runs" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    payload = {
        "adapter": adapter, "task": task, "cached_at": cached_at,
        "run_dir": run_dir, "fingerprint": "f" * 12,
        "result": {
            "score": score, "closed_correct": closed_correct, "closed_total": closed_total,
            "per_question": per_question if per_question is not None else
                [{"id": "q1", "type": "closed", "ok": True, "answer": "yes"}],
        },
    }
    f = cache / f"{adapter}__{task}__{cached_at.replace(':', '').replace('-', '')}.json"
    f.write_text(json.dumps(payload))
    return f


def mini_repo(root: Path) -> None:
    """Three deterministic tasks across two applications; alpha-one has two product
    runs, alpha-two one, beta-one none. Browser tests key off these values."""
    write_task(root, "alpha-one", application="erpnext",
        questions=[
            {"id": "q1", "type": "multiple_choice", "question": "Close it?",
             "options": [{"id": "A", "text": "Yes"}, {"id": "B", "text": "No"},
                         {"id": "C", "text": "I’m not sure"}],
             "correct_option": "A",
             "evidence": [
                 {"type": "narration", "key": "intro", "text": "Watch me."},
                 {"type": "action", "step": "close", "description": "Closes the issue."},
                 {"type": "page", "page": "issue", "description": "Issue state is visible."},
                 {"type": "screenshot", "step": "close", "line": 2,
                  "path": "tasks/alpha-one/evidence/close.png", "sha256": "fixture",
                  "description": "Captured close action."},
             ]},
            {"id": "q2", "type": "llm_judge", "question": "Why?",
             "rubric": "Mentions the [dup] marker."},
        ],
        narration=[{"key": "intro", "text": "Watch me."}, {"key": "wrap", "text": "Done."}])
    evidence_dir = root / "tasks" / "alpha-one" / "evidence"
    evidence_dir.mkdir()
    evidence_dir.joinpath("close.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
    write_task(root, "alpha-two", application="erpnext",
        questions=[{"id": "q1", "type": "closed", "question": "Ship it?",
                    "answer_aliases": ["no"]}])
    write_task(root, "beta-one", application="gitlab",
        questions=[{"id": "q1", "type": "closed", "question": "Label it?",
                    "answer_aliases": ["bug"]}])
    rec = root / "runs" / "rec-demo"
    rec.mkdir(parents=True)
    (rec / "screen.mov").write_bytes(b"not really a movie")
    write_cache(root, "claude-teach", "alpha-one", "2026-07-02T10:00:00", score=0.9,
        per_question=[
            {"id": "q1", "type": "multiple_choice", "ok": True, "answer": "A"},
            {"id": "q2", "type": "llm_judge", "score": 0.8, "answer": "Because of the marker"}],
        run_dir="runs/rec-demo")
    write_cache(root, "brackett-teach", "alpha-one", "2026-07-02T11:00:00", score=0.6,
        per_question=[
            {"id": "q1", "type": "multiple_choice", "ok": False, "answer": "B"},
            {"id": "q2", "type": "llm_judge", "score": 0.7, "answer": "Marker"}])
    write_cache(root, "claude-teach", "alpha-two", "2026-07-02T12:00:00", score=0.3,
        per_question=[{"id": "q1", "type": "closed", "ok": False, "answer": "yes"}])
