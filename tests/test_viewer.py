"""showAndTell.viewer.generate — scanning, warnings, and single-file assembly."""
from __future__ import annotations

import json
import hashlib

import pytest

from tests._viewer_fixture import load_generate, write_cache, write_task

gen = load_generate()


def build(root, **kwargs):
    return gen.build(
        tasks_dir=root / "tasks", cache_dir=root / "runs" / ".cache", **kwargs)


def test_build_shape(tmp_path):
    write_task(tmp_path, "alpha-one",
        questions=[{"id": "q1", "type": "closed", "question": "Q?",
                    "answer_aliases": ["yes"]}],
        narration=[{"key": "intro", "text": "hi"}])
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00", score=0.9)
    data, warnings = build(tmp_path)
    assert warnings == []
    assert [p["id"] for p in data["products"]] == ["claude-teach", "brackett-teach", "codex-record"]
    (t,) = data["tasks"]
    assert t["name"] == "alpha-one"
    assert t["title"] == "Alpha One"
    assert t["applications"] == ["erpnext"]
    assert t["primaryApplication"] == "erpnext"
    assert t["questions"][0]["id"] == "q1"
    assert t["questionTask"] == "alpha-one"
    assert t["questionsHash"] == hashlib.sha256(
        (tmp_path / "tasks/alpha-one/quiz/questions.json").read_bytes()).hexdigest()
    assert t["narration"][0]["key"] == "intro"
    assert t["demonstrate"].startswith("#")
    assert t["results"]["claude-teach"]["score"] == 0.9
    assert t["results"]["claude-teach"]["per_question"][0]["ok"] is True
    assert t["results"]["claude-teach"]["runs"][0]["deletion"] == {
        "kind": "cache",
        "id": "claude-teach__alpha-one__20260702T100000.json",
    }


def test_task_title_prefers_task_metadata_then_capture_title(tmp_path):
    task = write_task(tmp_path, "capture-title")
    (task / "testcase.json").write_text(json.dumps({"title": "Captured Task Title"}))
    data, warnings = build(tmp_path)
    assert warnings == []
    assert data["tasks"][0]["title"] == "Captured Task Title"

    task_toml = task / "task.toml"
    task_toml.write_text(task_toml.read_text().replace(
        'name = "capture-title"',
        'name = "capture-title"\ntitle = "Curated Task Title"',
    ))
    data, warnings = build(tmp_path)
    assert warnings == []
    assert data["tasks"][0]["title"] == "Curated Task Title"


def test_freshest_result_wins(tmp_path):
    write_task(tmp_path, "alpha-one")
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-01T10:00:00", score=0.2)
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00", score=0.9)
    data, _ = build(tmp_path)
    result = data["tasks"][0]["results"]["claude-teach"]
    assert result["score"] == pytest.approx(0.55)
    assert result["run_count"] == 2
    assert [r["score"] for r in result["runs"]] == [0.9, 0.2]


def test_promoted_ungraded_replay_is_visible_and_keeps_last_valid_score(tmp_path):
    write_task(tmp_path, "alpha-one")
    write_cache(
        tmp_path, "brackett-teach", "alpha-one",
        "2026-08-16T10:00:00", score=0.75,
        per_question=[{
            "id": "q1", "type": "closed", "ok": True, "answer": "yes",
        }],
    )
    run = (tmp_path / "runs" /
           "20260817-153101-brackett-teach-alpha-one")
    run.mkdir(parents=True)
    (run / "task-runtime.json").write_text(json.dumps({"task": "alpha-one"}))
    (run / "comprehend-response.txt").write_text("A1: yes")
    (run / "handoff.txt").write_text("saved handoff")
    (run / "screen.mov").write_bytes(b"video")
    (run / "comprehend.json").write_text(json.dumps({
        "status": "incomplete",
        "score": None,
        "judge_failures": 1,
        "closed_correct": 1,
        "closed_total": 1,
        "judge": {
            "backend": "anthropic-api",
            "model": "claude-sonnet-4-6",
        },
        "per_question": [{
            "id": "q1",
            "type": "closed",
            "answer": "yes",
            "ok": False,
            "judge": {
                "status": "error",
                "errors": [{
                    "type": "JudgeUnavailable",
                    "message": "ANTHROPIC_API_KEY is not set",
                }],
            },
        }],
    }))

    data, warnings = build(tmp_path)

    assert warnings == []
    result = data["tasks"][0]["results"]["brackett-teach"]
    assert result["score"] == 0.75
    assert result["run_count"] == 2
    assert result["graded_run_count"] == 1
    assert result["per_question"][0]["ok"] is True
    assert result["runs"][0]["score"] is None
    assert result["runs"][0]["status"] == "complete"
    assert result["runs"][0]["recording"].endswith(
        "/runs/20260817-153101-brackett-teach-alpha-one/screen.mov")
    assert {item["label"] for item in result["runs"][0]["artifacts"]} >= {
        "grading result", "complete quiz response", "agent handoff",
    }
    grading = result["grading"]
    assert grading["status"] == "incomplete"
    assert grading["error"] == "ANTHROPIC_API_KEY is not set"
    assert grading["credential"] == "ANTHROPIC_API_KEY"
    assert grading["task"].endswith("tasks/alpha-one")
    assert grading["run"] == str(run)
    assert "export ANTHROPIC_API_KEY='your-key'" in grading["recovery_command"]
    assert "tasks/alpha-one" in grading["recovery_command"]
    assert str(run) in grading["recovery_command"]


def test_uncached_completed_promoted_run_is_not_treated_as_current(tmp_path):
    write_task(tmp_path, "alpha-one")
    run = tmp_path / "runs/20260817-153101-brackett-teach-alpha-one"
    run.mkdir(parents=True)
    (run / "task-runtime.json").write_text(json.dumps({"task": "alpha-one"}))
    (run / "comprehend.json").write_text(json.dumps({
        "status": "complete", "score": 1.0, "per_question": [],
    }))

    data, warnings = build(tmp_path)

    assert warnings == []
    assert data["tasks"][0]["results"] == {}


def test_stale_fingerprinted_result_is_excluded(tmp_path):
    write_task(tmp_path, "alpha-one")
    path = write_cache(tmp_path, "claude-teach", "alpha-one",
                       "2026-07-02T10:00:00", score=0.9)
    payload = json.loads(path.read_text())
    payload["fingerprint"] = "0" * 64
    path.write_text(json.dumps(payload))

    data, warnings = build(tmp_path)

    assert warnings == []
    assert data["tasks"][0]["results"] == {}


def test_task_complexity_embedded(tmp_path):
    write_task(tmp_path, "alpha-one")
    data, warnings = build(tmp_path)
    assert warnings == []
    c = data["tasks"][0]["complexity"]
    assert set(c) == {"params", "dims", "tci", "tier"}
    assert c["tier"] in range(1, 6)


def test_missing_complexity_block_fails_build(tmp_path):
    write_task(tmp_path, "no-flags",
               toml='[task]\nname = "no-flags"\napplications = ["erpnext"]\nprimary_application = "erpnext"\n')
    with pytest.raises(gen.complexity.ComplexityError, match="no-flags"):
        build(tmp_path)


def test_malformed_toml_warns_and_skips(tmp_path):
    write_task(tmp_path, "alpha-one")
    write_task(tmp_path, "broken", toml="[task\nname =")
    data, warnings = build(tmp_path)
    assert [t["name"] for t in data["tasks"]] == ["alpha-one"]
    assert any("broken" in w and "TOML" in w for w in warnings)


def test_cache_entry_missing_keys_warns(tmp_path):
    write_task(tmp_path, "alpha-one")
    cache = tmp_path / "runs" / ".cache"
    cache.mkdir(parents=True)
    (cache / "bad.json").write_text(json.dumps({"adapter": "claude-teach", "task": "alpha-one"}))
    data, warnings = build(tmp_path)
    assert data["tasks"][0]["results"] == {}
    assert any("bad.json" in w for w in warnings)


def test_bad_narration_line_warns(tmp_path):
    d = write_task(tmp_path, "alpha-one", narration=[{"key": "intro", "text": "hi"}])
    nar = d / "demo" / "narration_script.jsonl"
    nar.write_text(nar.read_text() + "not json\n")
    data, warnings = build(tmp_path)
    assert len(data["tasks"][0]["narration"]) == 1
    assert any("narration_script" in w for w in warnings)


def test_non_object_questions_warns(tmp_path):
    d = write_task(tmp_path, "alpha-one")
    (d / "quiz" / "questions.json").write_text(json.dumps([1, 2]))
    data, warnings = build(tmp_path)
    (t,) = data["tasks"]
    assert t["questions"] == []
    assert any("questions.json" in w and "expected an object" in w for w in warnings)


def test_non_table_task_section_warns_and_skips(tmp_path):
    write_task(tmp_path, "alpha-one")
    write_task(tmp_path, "broken", toml='task = "x"\n')
    data, warnings = build(tmp_path)
    assert [t["name"] for t in data["tasks"]] == ["alpha-one"]
    assert any("broken" in w and "not a table" in w for w in warnings)


def test_cache_result_not_object_warns(tmp_path):
    write_task(tmp_path, "alpha-one")
    cache = tmp_path / "runs" / ".cache"
    cache.mkdir(parents=True)
    (cache / "strres.json").write_text(json.dumps(
        {"adapter": "claude-teach", "task": "alpha-one",
         "cached_at": "2026-07-02T10:00:00", "result": "oops"}))
    data, warnings = build(tmp_path)
    assert data["tasks"][0]["results"] == {}
    assert any("strres.json" in w for w in warnings)


def test_assemble_is_self_contained(tmp_path):
    write_task(tmp_path, "alpha-one", narration=[{"key": "k", "text": "</script>sneaky"}])
    data, _ = build(tmp_path)
    page = gen.assemble(data)
    assert "<!-- @" not in page                      # every marker replaced
    assert 'id="showAndTell-data"' in page
    assert "</script>sneaky" not in page             # island can't be closed early
    assert "\\u003c/script>sneaky" in page           # every "<" JSON-escaped
    assert '<script type="module">' in page
    assert "<style>" in page
    assert "--pass" in page                          # tokens really inlined
    assert "replay completed — regrading required" in page
    assert "Regrading does not replay the product." in page


def test_assemble_rejects_broken_skeleton(tmp_path, monkeypatch):
    src = tmp_path / "src"
    for rel in gen.CSS_MANIFEST + gen.JS_MANIFEST:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    (src / "skeleton.html").write_text("<html><body></body></html>")
    monkeypatch.setattr(gen, "SRC", src)
    import pytest
    with pytest.raises(SystemExit):
        gen.assemble({"tasks": []})


def _serving(tmp_path):
    """Start showAndTell.viewer.serve on an ephemeral port against a temp repo;
    returns (server, base_url, serve_module)."""
    import threading

    from tests._viewer_fixture import load_serve

    serve = load_serve()
    server = serve.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", serve


def test_serve_rebuilds_per_request(tmp_path, monkeypatch):
    import urllib.request

    write_task(tmp_path, "alpha-one")
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(serve.generate, "CACHE_DIR", tmp_path / "runs" / ".cache")
    try:
        body = urllib.request.urlopen(f"{url}/").read().decode("utf-8")
        assert 'id="showAndTell-data"' in body
        assert "alpha-one" in body
        # a task added after startup must appear on the very next refresh
        write_task(tmp_path, "alpha-two")
        body = urllib.request.urlopen(f"{url}/").read().decode("utf-8")
        assert "alpha-two" in body
    finally:
        server.shutdown()


def test_serve_404_on_other_paths(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request

    import pytest as _pytest

    write_task(tmp_path, "alpha-one")
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(serve.generate, "CACHE_DIR", tmp_path / "runs" / ".cache")
    try:
        with _pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{url}/favicon.ico")
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_serve_main_opens_browser(monkeypatch):
    from tests._viewer_fixture import load_serve

    serve = load_serve()
    opened = []
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(serve.ThreadingHTTPServer, "serve_forever", lambda self: None)
    monkeypatch.setattr(serve.sys, "argv", ["serve.py", "0"])
    serve.main()
    assert len(opened) == 1
    assert opened[0].startswith("http://localhost:")
    assert not opened[0].endswith(":0")  # real bound port, not the requested 0


def _post_questions(url, token, payload, *, origin=None):
    import urllib.error
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "X-ShowAndTell-Edit-Token": token,
    }
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"{url}/api/questions", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


def test_question_save_preserves_top_level_metadata_and_evidence(tmp_path, monkeypatch):
    d = write_task(tmp_path, "alpha-one", questions=[{
        "id": "q1", "type": "closed", "question": "Old?",
        "answer_aliases": ["old"],
        "scope": "keep-me", "evidence": [{"type": "action", "step": "one"}],
    }])
    path = d / "quiz/questions.json"
    source = json.loads(path.read_text())
    source["_scoping_note"] = "preserve this top-level key"
    path.write_text(json.dumps(source))
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    try:
        base_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        questions = [
            {"id": "q1", "type": "closed", "question": "New?",
             "answer_aliases": ["new", "yes"],
             "scope": "keep-me", "evidence": [{"type": "action", "step": "one"}]},
            {"id": "q2", "type": "multiple_choice", "question": "Choose?",
             "options": [{"id": "A", "text": "One"},
                         {"id": "B", "text": "I’m not sure"}],
             "correct_option": "A"},
            {"id": "q3", "type": "llm_judge", "question": "Explain?",
             "rubric": "Mentions the reason."},
        ]
        status, result = _post_questions(
            url, server.question_edit_token,
            {"task": "alpha-one", "base_hash": base_hash, "questions": questions},
            origin=url)
        assert status == 200
        assert result["hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
        saved = json.loads(path.read_text())
        assert saved["_scoping_note"] == "preserve this top-level key"
        assert saved["questions"] == questions
        assert saved["questions"][0]["evidence"] == source["questions"][0]["evidence"]
    finally:
        server.shutdown()


def test_question_save_supports_captured_task_drafts(tmp_path, monkeypatch):
    draft = write_task(tmp_path, "captured-one", questions=[])
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    draft = draft.rename(drafts / draft.name)
    path = draft / "quiz/questions.json"
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    questions = [{
        "id": "q1", "type": "closed", "question": "What happened?",
        "answer_aliases": ["The expected outcome"],
    }]
    try:
        status, result = _post_questions(
            url, server.question_edit_token,
            {"task": "captured-one", "source": "draft",
             "base_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
             "questions": questions})

        assert status == 200
        assert result["questions"] == questions
        assert json.loads(path.read_text())["questions"] == questions
        assert not (tmp_path / "tasks/captured-one").exists()
    finally:
        server.shutdown()


def test_question_save_rejects_stale_hash_schema_and_traversal(tmp_path, monkeypatch):
    d = write_task(tmp_path, "alpha-one", questions=[{
        "id": "q1", "type": "closed", "question": "Old?",
        "answer_aliases": ["old"],
    }])
    path = d / "quiz/questions.json"
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    valid = [{"id": "q1", "type": "closed", "question": "New?",
              "answer_aliases": ["new"]}]
    try:
        original = path.read_bytes()
        status, result = _post_questions(
            url, server.question_edit_token,
            {"task": "alpha-one", "base_hash": "0" * 64, "questions": valid})
        assert status == 409 and "changed on disk" in result["error"]
        assert path.read_bytes() == original

        base_hash = hashlib.sha256(original).hexdigest()
        bad = [{"id": "q1", "type": "multiple_choice", "question": "Bad?",
                "options": [{"id": "A", "text": "Only"}],
                "correct_option": "Z"}]
        status, result = _post_questions(
            url, server.question_edit_token,
            {"task": "alpha-one", "base_hash": base_hash, "questions": bad})
        assert status == 400 and "options" in result["error"]
        assert path.read_bytes() == original

        status, result = _post_questions(
            url, server.question_edit_token,
            {"task": "../alpha-one", "base_hash": base_hash, "questions": valid})
        assert status == 400 and "task name" in result["error"]
        assert path.read_bytes() == original
    finally:
        server.shutdown()


def test_question_save_requires_token_and_same_origin(tmp_path, monkeypatch):
    d = write_task(tmp_path, "alpha-one", questions=[{
        "id": "q1", "type": "closed", "question": "Old?",
        "answer_aliases": ["old"],
    }])
    path = d / "quiz/questions.json"
    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    payload = {"task": "alpha-one",
               "base_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
               "questions": json.loads(path.read_text())["questions"]}
    try:
        status, result = _post_questions(url, "wrong", payload)
        assert status == 403 and "token" in result["error"]
        status, result = _post_questions(
            url, server.question_edit_token, payload, origin="http://evil.test")
        assert status == 403 and "cross-origin" in result["error"]
    finally:
        server.shutdown()


def test_canonical_task_recording_is_discovered_and_range_served(tmp_path, monkeypatch):
    import urllib.request

    d = write_task(tmp_path, "alpha-one")
    recording = d / "demo/recording.webm"
    recording.write_bytes(b"0123456789")
    data, warnings = build(tmp_path)
    assert warnings == []
    assert data["tasks"][0]["demoRecording"] == "tasks/alpha-one/demo/recording.webm"

    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    try:
        request = urllib.request.Request(
            f"{url}/tasks/alpha-one/demo/recording.webm",
            headers={"Range": "bytes=2-5"})
        response = urllib.request.urlopen(request)
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.headers["Content-Type"] == "video/webm"
        assert response.read() == b"2345"
    finally:
        server.shutdown()


def test_live_build_discovers_captured_drafts_without_changing_snapshot(tmp_path):
    write_task(tmp_path, "alpha-one")
    captured = write_task(tmp_path, "captured-one", application="erpnext")
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    captured.rename(drafts / captured.name)
    (drafts / "captured-one/demo/seed.json").write_text(json.dumps({
        "erpnext": {"items": [{"name": "AB01"}]},
    }))
    (drafts / "captured-one/demo/seed_events.jsonl").write_text(
        json.dumps({"type": "fill", "page": "page", "description": "fill Item Code",
                    "value": "AB01", "selector": "#ignored"}) + "\n"
    )
    (drafts / "captured-one/testcase.json").write_text(json.dumps({
        "seed": {"source": "fixture-export", "exported": True},
    }))

    snapshot, warnings = build(tmp_path)
    assert warnings == [] and snapshot["drafts"] == []

    live, warnings = build(tmp_path, include_drafts=True)
    assert warnings == []
    assert [task["name"] for task in live["tasks"]] == ["alpha-one"]
    assert [(task["name"], task["draft"]) for task in live["drafts"]] == [
        ("captured-one", True)]
    assert live["drafts"][0]["demoRecording"] is None
    assert live["drafts"][0]["seed"] == {
        "erpnext": {"items": [{"name": "AB01"}]}}
    assert live["drafts"][0]["seedEvents"] == [{
        "type": "fill", "page": "page", "description": "fill Item Code",
        "value": "AB01",
    }]
    assert live["drafts"][0]["seedInfo"]["source"] == "fixture-export"


def test_live_build_normalizes_graded_and_legacy_draft_trials(tmp_path):
    write_task(tmp_path, "alpha-one")
    captured = write_task(tmp_path, "captured-results")
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    captured.rename(drafts / captured.name)
    trial_root = drafts / "captured-results/trials"

    brackett = trial_root / "20260803-121353-brackett"
    brackett.mkdir(parents=True)
    graded = {
        "score": 0.75,
        "closed_correct": 1,
        "closed_total": 1,
        "multiple_choice_correct": 2,
        "multiple_choice_total": 3,
        "per_question": [{"id": "q1", "type": "closed", "ok": True,
                          "answer": "yes"}],
    }
    (brackett / "comprehend.json").write_text(json.dumps(graded))
    (brackett / "result.json").write_text(json.dumps({
        **graded, "product": "brackett", "status": "complete",
        "agent_url": "https://example.test/agents/1",
    }))
    (brackett / "handoff.txt").write_text("handoff")
    (brackett / "comprehend-response.txt").write_text("complete response")
    (brackett / "screen.mov").write_bytes(b"trial video")

    claude = trial_root / "20260802-101112-claude"
    claude.mkdir()
    ungraded = {
        "status": "incomplete", "score": None, "judge_failures": 2,
        "closed_correct": 1, "closed_total": 1,
        "multiple_choice_correct": 0, "multiple_choice_total": 0,
        "judge": {"backend": "anthropic-api", "model": "claude-sonnet-4-6"},
        "per_question": [{
            "id": "q2", "type": "llm_judge", "answer": "because", "score": None,
            "ok": False, "judge": {"status": "error", "errors": [{
                "type": "JudgeUnavailable",
                "message": "ANTHROPIC_API_KEY is not set",
            }]},
        }],
    }
    (claude / "comprehend.json").write_text(json.dumps(ungraded))
    (claude / "result.json").write_text(json.dumps({
        **ungraded, "product": "claude", "status": "complete",
        "grading_status": "incomplete",
    }))

    legacy = trial_root / "20260801-091011-claude"
    legacy.mkdir()
    (legacy / "result.json").write_text(json.dumps({
        "product": "claude", "status": "complete", "learned": "legacy",
        "comprehend_score": 0.4,
    }))
    (claude / "learned.txt").write_text("legacy")

    # A crashed historical trial had no result manifest at all. It should be
    # visible as incomplete, not silently discarded.
    (trial_root / "20260731-091011-codex").mkdir()

    data, warnings = build(tmp_path, include_drafts=True)

    assert warnings == []
    results = data["drafts"][0]["results"]
    brackett_result = results["brackett-teach"]
    assert brackett_result["score"] == 0.75
    assert brackett_result["per_question"] == graded["per_question"]
    assert brackett_result["cached_at"] == "2026-08-03T12:13:53"
    assert brackett_result["recording"] == (
        "/task-drafts/captured-results/trials/"
        "20260803-121353-brackett/screen.mov")
    assert {item["label"] for item in brackett_result["artifacts"]} >= {
        "grading result", "complete quiz response", "trial manifest",
        "agent handoff", "open agent"}
    # Keep the previous valid score while surfacing recovery for the newest,
    # successfully completed but ungraded replay.
    assert results["claude-teach"]["score"] == 0.4
    assert results["claude-teach"]["status"] == "complete"
    grading = results["claude-teach"]["grading"]
    assert grading["status"] == "incomplete"
    assert grading["error"] == "ANTHROPIC_API_KEY is not set"
    assert grading["backend"] == "anthropic-api"
    assert grading["credential"] == "ANTHROPIC_API_KEY"
    assert grading["task"].endswith("task-drafts/captured-results")
    assert grading["run"] == str(claude)
    assert "export ANTHROPIC_API_KEY='your-key'" in grading["recovery_command"]
    assert "./showAndTell regrade" in grading["recovery_command"]
    assert "task-drafts/captured-results" in grading["recovery_command"]
    assert results["codex-record"]["score"] is None
    assert results["codex-record"]["status"] == "incomplete"


def test_captured_draft_recording_is_range_served(tmp_path, monkeypatch):
    import urllib.request

    write_task(tmp_path, "alpha-one")
    captured = write_task(tmp_path, "captured-one")
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    captured.rename(drafts / captured.name)
    recording = drafts / "captured-one/demo/recording.webm"
    recording.write_bytes(b"draft-video")

    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    try:
        response = urllib.request.urlopen(
            f"{url}/task-drafts/captured-one/demo/recording.webm")
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/webm"
        assert response.read() == b"draft-video"
    finally:
        server.shutdown()


def test_captured_draft_trial_artifacts_are_served_from_known_paths(
        tmp_path, monkeypatch):
    import urllib.error
    import urllib.request

    write_task(tmp_path, "alpha-one")
    captured = write_task(tmp_path, "captured-one")
    drafts = tmp_path / "task-drafts"
    drafts.mkdir()
    captured.rename(drafts / captured.name)
    trial = drafts / "captured-one/trials/20260803-121353-brackett"
    trial.mkdir(parents=True)
    comprehend = trial / "comprehend.json"
    comprehend.write_text('{"score": 0.75}\n')
    (trial / "comprehend-response.txt").write_text("complete response")
    (trial / "handoff.txt").write_text("saved handoff")
    (trial / "not-allowed.py").write_text("secret = True")
    (trial / "screen.mov").write_bytes(b"0123456789")

    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    try:
        response = urllib.request.urlopen(
            f"{url}/task-drafts/captured-one/trials/"
            "20260803-121353-brackett/comprehend.json")
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert json.loads(response.read()) == {"score": 0.75}

        response = urllib.request.urlopen(
            f"{url}/task-drafts/captured-one/trials/"
            "20260803-121353-brackett/handoff.txt")
        assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert response.read() == b"saved handoff"

        response = urllib.request.urlopen(
            f"{url}/task-drafts/captured-one/trials/"
            "20260803-121353-brackett/comprehend-response.txt")
        assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert response.read() == b"complete response"

        request = urllib.request.Request(
            f"{url}/task-drafts/captured-one/trials/"
            "20260803-121353-brackett/screen.mov",
            headers={"Range": "bytes=2-5"})
        response = urllib.request.urlopen(request)
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.read() == b"2345"

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"{url}/task-drafts/captured-one/trials/"
                "20260803-121353-brackett/not-allowed.py")
        assert exc.value.code == 404
    finally:
        server.shutdown()


# --- recordings ------------------------------------------------------------

def test_result_carries_recording_path(tmp_path):
    write_task(tmp_path, "alpha-one")
    rd = tmp_path / "runs" / "20260717-x-claude-teach-alpha-one"
    rd.mkdir(parents=True)
    (rd / "screen.mov").write_bytes(b"fake video")
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00",
                run_dir="runs/20260717-x-claude-teach-alpha-one")
    data, warnings = build(tmp_path)
    assert warnings == []
    assert data["tasks"][0]["results"]["claude-teach"]["recording"] == \
        "../../../runs/20260717-x-claude-teach-alpha-one/screen.mov"


def test_recording_prefers_mov_but_takes_mkv(tmp_path):
    write_task(tmp_path, "alpha-one")
    rd = tmp_path / "runs" / "r1"
    rd.mkdir(parents=True)
    (rd / "screen.mkv").write_bytes(b"x")
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00",
                run_dir="runs/r1")
    data, _ = build(tmp_path)
    assert data["tasks"][0]["results"]["claude-teach"]["recording"] == "../../../runs/r1/screen.mkv"


def test_recording_none_when_absent(tmp_path):
    write_task(tmp_path, "alpha-one")
    # run_dir points at a dir with no screen.* file (the default "runs/demo"
    # doesn't even exist) — no warning, recording is None
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00")
    data, warnings = build(tmp_path)
    assert warnings == []
    assert data["tasks"][0]["results"]["claude-teach"]["recording"] is None


def test_recording_none_for_absolute_run_dir(tmp_path):
    write_task(tmp_path, "alpha-one")
    rd = tmp_path / "elsewhere"
    rd.mkdir()
    (rd / "screen.mov").write_bytes(b"x")
    write_cache(tmp_path, "claude-teach", "alpha-one", "2026-07-02T10:00:00",
                run_dir=str(rd))   # absolute: no valid ../ URL exists for it
    data, _ = build(tmp_path)
    assert data["tasks"][0]["results"]["claude-teach"]["recording"] is None


def _rec_repo(tmp_path, serve, monkeypatch):
    """Point the live server's generate module at a temp repo that has one
    recording on disk."""
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(serve.generate, "CACHE_DIR", tmp_path / "runs" / ".cache")
    rd = tmp_path / "runs" / "r1"
    rd.mkdir(parents=True)
    (rd / "screen.mov").write_bytes(b"0123456789")
    (rd / "comprehend.json").write_text("{}")
    return rd


def test_serve_recording_full_and_ranges(tmp_path, monkeypatch):
    import urllib.request
    from urllib.error import HTTPError

    write_task(tmp_path, "alpha-one")
    server, url, serve = _serving(tmp_path)
    _rec_repo(tmp_path, serve, monkeypatch)
    try:
        # full fetch: 200, correct type, advertises ranges
        resp = urllib.request.urlopen(f"{url}/runs/r1/screen.mov")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/quicktime"
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.read() == b"0123456789"
        # Saved grading/answer artifacts are available, but the endpoint still
        # rejects every filename outside the explicit allowlist.
        resp = urllib.request.urlopen(f"{url}/runs/r1/comprehend.json")
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert json.loads(resp.read()) == {}
        # single range: 206 with the exact slice
        req = urllib.request.Request(f"{url}/runs/r1/screen.mov",
                                     headers={"Range": "bytes=2-5"})
        resp = urllib.request.urlopen(req)
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 2-5/10"
        assert resp.read() == b"2345"
        # open-ended range
        req = urllib.request.Request(f"{url}/runs/r1/screen.mov",
                                     headers={"Range": "bytes=7-"})
        resp = urllib.request.urlopen(req)
        assert resp.status == 206 and resp.read() == b"789"
        # unsatisfiable range -> 416
        req = urllib.request.Request(f"{url}/runs/r1/screen.mov",
                                     headers={"Range": "bytes=99-"})
        with pytest.raises(HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 416
    finally:
        server.shutdown()


def test_parse_range_forms():
    from tests._viewer_fixture import load_serve
    pr = load_serve()._parse_range
    assert pr(None, 10) is None                       # no header -> full body
    assert pr("bytes=2-5", 10) == (2, 5)
    assert pr("bytes=7-", 10) == (7, 9)               # open-ended
    assert pr("bytes=-3", 10) == (7, 9)               # suffix
    assert pr("bytes=-999", 10) == (0, 9)             # suffix larger than file
    assert pr("bytes=0-999", 10) == (0, 9)            # end clamped
    assert pr("bytes=1-2,4-5", 10) is None            # multi-range -> full body
    assert pr("bytes=abc-", 10) == "bad"
    assert pr("bytes=5-2", 10) == "bad"               # end < start
    assert pr("bytes=10-", 10) == "bad"               # start == size
    assert pr("bytes=-0", 10) == "bad"
    assert pr("bytes=-5", 0) == "bad"                 # zero-byte file: unsatisfiable


def test_serve_run_artifact_refuses_unallowlisted_files_and_traversal(
        tmp_path, monkeypatch):
    import urllib.request
    from urllib.error import HTTPError

    write_task(tmp_path, "alpha-one")
    server, url, serve = _serving(tmp_path)
    _rec_repo(tmp_path, serve, monkeypatch)
    try:
        for bad in ("/runs/r1/task-runtime.json",    # not an allowlisted artifact
                    "/runs/missing/screen.mov",      # no such file
                    "/runs/../tasks/alpha-one/task.toml",   # traversal
                    "/runs/r1/%2e%2e/r1/comprehend.json"):  # encoded traversal
            with pytest.raises(HTTPError) as e:
                urllib.request.urlopen(f"{url}{bad}")
            assert e.value.code == 404, bad
    finally:
        server.shutdown()


def test_serve_recording_survives_null_byte(tmp_path, monkeypatch):
    import urllib.request
    from urllib.error import HTTPError

    write_task(tmp_path, "alpha-one")
    server, url, serve = _serving(tmp_path)
    _rec_repo(tmp_path, serve, monkeypatch)
    try:
        with pytest.raises(HTTPError) as e:
            urllib.request.urlopen(f"{url}/runs/r1/%00screen.mov")
        assert e.value.code == 404
    finally:
        server.shutdown()


def test_dataset_tasks_join_the_build_read_only(tmp_path, monkeypatch):
    from showAndTell.bundles import hub

    write_task(tmp_path, "alpha-one")
    published = tmp_path / "hub-cache" / "abcdef1234567890"
    write_task(published, "published-one", application="roundcube")
    write_task(published, "alpha-one")  # name collision: local wins
    (published / "tasks" / "published-one" / "demo" / "recording.mp4"
     ).write_bytes(b"x")
    monkeypatch.setattr(hub, "cached_dataset_info", lambda: {
        "repo": "acme/showAndTellarena", "revision": "abcdef123456",
        "task_dirs": sorted((published / "tasks").iterdir())})

    data, warnings = build(tmp_path)

    assert warnings == []
    names = [t["name"] for t in data["tasks"]]
    assert names.count("alpha-one") == 1
    row = next(t for t in data["tasks"] if t["name"] == "published-one")
    assert row["dataset"] is True
    assert (row["demoRecording"]
            == "dataset-tasks/published-one/demo/recording.mp4")
    assert next(t for t in data["tasks"]
                if t["name"] == "alpha-one")["dataset"] is False
    assert data["dataset"] == {"repo": "acme/showAndTellarena",
                               "revision": "abcdef123456", "count": 1}


def test_dataset_task_recording_is_served_from_the_hub_cache(
        tmp_path, monkeypatch):
    import urllib.request

    write_task(tmp_path, "alpha-one")
    published = tmp_path / "hub-cache" / "abcdef1234567890"
    write_task(published, "published-one")
    (published / "tasks" / "published-one" / "demo" / "recording.webm"
     ).write_bytes(b"0123456789")

    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.hub, "cached_dataset_root",
                        lambda **kwargs: published / "tasks")
    try:
        response = urllib.request.urlopen(
            f"{url}/dataset-tasks/published-one/demo/recording.webm")
        assert response.status == 200
        assert response.read() == b"0123456789"
    finally:
        server.shutdown()


def test_dataset_recording_serves_through_hub_cache_symlinks(
        tmp_path, monkeypatch):
    """The hub cache stores files as symlinks into its blobs/ store; the
    asset route must not resolve them out of the confinement root."""
    import urllib.request

    write_task(tmp_path, "alpha-one")
    published = tmp_path / "hub-cache" / "abcdef1234567890"
    write_task(published, "published-one")
    blobs = tmp_path / "hub-cache" / "blobs"
    blobs.mkdir()
    (blobs / "cafebabe").write_bytes(b"0123456789")
    link = published / "tasks" / "published-one" / "demo" / "recording.mp4"
    link.symlink_to("../../../../blobs/cafebabe")

    server, url, serve = _serving(tmp_path)
    monkeypatch.setattr(serve.hub, "cached_dataset_root",
                        lambda **kwargs: published / "tasks")
    try:
        response = urllib.request.urlopen(
            f"{url}/dataset-tasks/published-one/demo/recording.mp4")
        assert response.status == 200
        assert response.read() == b"0123456789"
    finally:
        server.shutdown()


def test_dataset_assets_404_before_first_pull(tmp_path):
    import urllib.error
    import urllib.request

    write_task(tmp_path, "alpha-one")
    server, url, _serve = _serving(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(
                f"{url}/dataset-tasks/x/demo/recording.webm")
        assert err.value.code == 404
    finally:
        server.shutdown()
