from __future__ import annotations

import json

import pytest

from showAndTell.quiz import leaderboard


def _run(root, name, *, fingerprint, verified=False):
    path = root / name
    path.mkdir(parents=True)
    (path / "comprehend.json").write_text(json.dumps({
        "status": "complete", "score": 0.5,
        "closed_correct": 1, "closed_total": 1,
        "judge": {
            "prompt_fingerprint": fingerprint, "verified": verified,
        },
    }))
    return path


def test_leaderboard_rejects_mixed_judge_protocols(tmp_path):
    _run(tmp_path, "20260101-000000-claude-teach-task-a", fingerprint="a")
    _run(tmp_path, "20260101-000001-brackett-teach-task-a", fingerprint="b")
    with pytest.raises(ValueError, match="different judge protocols"):
        leaderboard.aggregate(leaderboard.load_runs(tmp_path))


def test_leaderboard_skips_incomplete_results_and_labels_local_runs(tmp_path):
    _run(tmp_path, "20260101-000000-claude-teach-task-a", fingerprint="a")
    failed = tmp_path / "20260101-000001-brackett-teach-task-a"
    failed.mkdir()
    (failed / "comprehend.json").write_text(json.dumps({
        "status": "incomplete", "score": None,
    }))

    aggregated = leaderboard.aggregate(leaderboard.load_runs(tmp_path))

    assert aggregated["n_runs"] == 1
    assert aggregated["official"] is False
    assert "local/unverified" in leaderboard.render_markdown(aggregated, "latest")


def test_leaderboard_only_labels_attested_results_official(tmp_path):
    _run(
        tmp_path, "20260101-000000-claude-teach-task-a",
        fingerprint="a", verified=True)
    aggregated = leaderboard.aggregate(leaderboard.load_runs(tmp_path))
    assert aggregated["official"] is True
