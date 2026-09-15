"""Result caching for teach runs: fingerprint a task's inputs, reuse a cached
result on an unchanged task, force a fresh run with no_cache. The CLI wrapper is
tested with a fake run_fn so no browser/product is involved."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from showAndTell.core import cache


def _make_task(root: Path, name: str = "demo-task") -> Path:
    """A minimal but load_task-able task dir with every fingerprinted file."""
    d = root / "tasks" / name
    (d / "quiz").mkdir(parents=True)
    (d / "demo").mkdir()
    (d / "task.toml").write_text(
        f'[task]\nname = "{name}"\napplications = ["erpnext"]\n'
        'primary_application = "erpnext"\nsummary = "x"\n')
    (d / "task_logic.py").write_text("def decide(x):\n    return 'ok'\n")
    (d / "demonstrate.py").write_text("def demonstrate(*a, **k):\n    pass\n")
    (d / "quiz" / "questions.json").write_text(
        '{"questions": [{"id": "q1", "type": "closed",'
        ' "question": "?", "answer_aliases": ["yes"]}]}\n')
    (d / "demo" / "narration_script.jsonl").write_text('{"key": "a", "text": "hi"}\n')
    (d / "demo" / "seed.json").write_text('{"users": []}\n')
    return d


RESULT = {
    "per_question": [
        {"id": "q1", "type": "closed", "answer": "yes", "ok": True},
        {"id": "q2", "type": "rubric", "answer": "the rule", "score": 0.5},
    ],
    "score": 0.75,
    "closed_correct": 1,
    "closed_total": 1,
}


# --- fingerprint -----------------------------------------------------------

def test_fingerprint_is_stable_when_nothing_changes(tmp_path):
    d = _make_task(tmp_path)
    assert cache.fingerprint(d) == cache.fingerprint(d)


def test_fingerprint_changes_when_the_quiz_changes(tmp_path):
    d = _make_task(tmp_path)
    before = cache.fingerprint(d)
    (d / "quiz" / "questions.json").write_text(
        '{"questions": [{"id": "q1", "type": "closed",'
        ' "question": "?", "answer_aliases": ["yes"]},'
        ' {"id": "q2", "type": "closed",'
        ' "question": "?", "answer_aliases": ["no"]}]}\n')
    assert cache.fingerprint(d) != before


def test_fingerprint_ignores_complexity_block(tmp_path):
    d = _make_task(tmp_path)
    base = cache.fingerprint(d)
    toml = (d / "task.toml").read_text()
    (d / "task.toml").write_text(toml + "\n[complexity]\nhops = 1\nbinding = 2\n")
    assert cache.fingerprint(d) == base          # flags don't change what a result means
    (d / "task.toml").write_text(
        toml.replace("[task]", "[task]\n# edited") + "\n[complexity]\nhops = 1\n")
    assert cache.fingerprint(d) != base          # real edits still invalidate


# --- load / save -----------------------------------------------------------

def test_load_is_none_when_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    assert cache.load("claude-teach", d) is None


def test_save_then_load_roundtrips_the_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    f = cache.save("claude-teach", d, RESULT, run_dir=tmp_path / "runs" / "r1")
    assert f.exists()
    assert f.parent == Path("runs") / ".cache"     # lives under gitignored runs/
    got = cache.load("claude-teach", d)
    assert got is not None
    assert got["result"] == RESULT
    assert got["adapter"] == "claude-teach"
    assert got["task"] == "demo-task"


def test_repeated_saves_retain_history_and_load_latest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    first = {**RESULT, "score": 0.25}
    f1 = cache.save("claude-teach", d, first, run_dir=Path("runs") / "r1")
    f2 = cache.save("claude-teach", d, RESULT, run_dir=Path("runs") / "r2")

    assert f1 != f2
    assert f1.exists() and f2.exists()
    assert cache.load("claude-teach", d)["result"] == RESULT
    assert len(list((Path("runs") / ".cache").glob("*.json"))) == 2


def test_load_misses_after_the_task_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    cache.save("claude-teach", d, RESULT)
    (d / "task_logic.py").write_text("def decide(x):\n    return 'CHANGED'\n")
    assert cache.load("claude-teach", d) is None   # stale -> miss


def test_cache_is_scoped_per_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    cache.save("claude-teach", d, RESULT)
    assert cache.load("claude-teach", d) is not None
    assert cache.load("codex-record", d) is None   # different adapter -> miss


def test_cache_is_scoped_per_judge_protocol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    cache.save("claude-teach", d, RESULT, judge_fingerprint="a" * 64)

    assert cache.load(
        "claude-teach", d, judge_fingerprint="a" * 64) is not None
    assert cache.load(
        "claude-teach", d, judge_fingerprint="b" * 64) is None
    assert cache.load("claude-teach", d) is None


@pytest.mark.parametrize("adapter", cache.TEACH_ADAPTERS)
def test_adapter_from_standard_run_directory(adapter):
    run = Path(f"runs/20260817-153101-{adapter}-demo-task")
    assert cache.adapter_from_run_dir(run) == adapter


def test_adapter_from_run_directory_rejects_arbitrary_paths():
    assert cache.adapter_from_run_dir(Path("runs/not-a-run-brackett-teach-x")) is None


# --- CLI wrapper -----------------------------------------------------------

def _fake_run(run_name: str, *, write_result: bool = True):
    """Return a run_fn that mimics an adapter: makes runs/<name>/ and (usually)
    writes comprehend.json, recording each call."""
    calls = []

    def run_fn():
        calls.append(1)
        rd = Path("runs") / run_name
        rd.mkdir(parents=True, exist_ok=True)
        if write_result:
            (rd / "comprehend.json").write_text(json.dumps(RESULT))
        return rd

    run_fn.calls = calls
    return run_fn


def test_wrapper_miss_runs_then_caches(tmp_path, monkeypatch):
    from showAndTell import cli
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    run_fn = _fake_run("20260101-000000-claude-teach-demo-task")
    cli._run_with_cache("claude-teach", d, no_cache=False, run_fn=run_fn, out=lambda *_: None)
    assert run_fn.calls == [1]                      # ran once
    assert cache.load("claude-teach", d)["result"] == RESULT   # and cached it


def test_wrapper_hit_skips_the_run(tmp_path, monkeypatch):
    from showAndTell import cli
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    cache.save("claude-teach", d, RESULT, run_dir=Path("runs") / "old")
    run_fn = _fake_run("should-not-run")
    lines = []
    cli._run_with_cache("claude-teach", d, no_cache=False, run_fn=run_fn, out=lines.append)
    assert run_fn.calls == []                        # never ran
    assert any("cache" in ln.lower() for ln in lines)   # told the user it was cached


def test_wrapper_no_cache_forces_run_and_refreshes(tmp_path, monkeypatch):
    from showAndTell import cli
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    stale = {**RESULT, "score": 0.1}
    cache.save("claude-teach", d, stale, run_dir=Path("runs") / "old")
    run_fn = _fake_run("20260101-000000-claude-teach-demo-task")
    cli._run_with_cache("claude-teach", d, no_cache=True, run_fn=run_fn, out=lambda *_: None)
    assert run_fn.calls == [1]                        # ran despite the cache
    assert cache.load("claude-teach", d)["result"]["score"] == 0.75   # refreshed


def test_wrapper_does_not_cache_a_failed_run(tmp_path, monkeypatch):
    from showAndTell import cli
    monkeypatch.chdir(tmp_path)
    d = _make_task(tmp_path)
    run_fn = _fake_run("20260101-000000-claude-teach-demo-task", write_result=False)
    cli._run_with_cache("claude-teach", d, no_cache=False, run_fn=run_fn, out=lambda *_: None)
    assert run_fn.calls == [1]
    assert cache.load("claude-teach", d) is None      # no comprehend.json -> not cached


def test_regrade_replace_promotes_completed_result_to_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from showAndTell import cli
    from showAndTell.quiz import comprehend, judge

    monkeypatch.chdir(tmp_path)
    task = _make_task(tmp_path)
    run = tmp_path / "runs/20260817-153101-brackett-teach-demo-task"
    run.mkdir(parents=True)
    (run / comprehend.RESPONSE_ARTIFACT_NAME).write_text("A1: yes")
    regraded = {**RESULT, "status": "complete"}
    monkeypatch.setattr(cli, "_grade_saved_response", lambda *_args: regraded)

    cli._cmd_regrade(SimpleNamespace(
        task=str(task), run=str(run), replace=True))

    hit = cache.load(
        "brackett-teach", task,
        judge_fingerprint=judge.protocol_fingerprint())
    assert hit is not None
    assert hit["result"] == regraded
    assert Path(hit["run_dir"]) == run
