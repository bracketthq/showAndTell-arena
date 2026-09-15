import json

import pytest

from showAndTell.core import llm


def test_complete_raises_clear_error_when_cli_omits_result(monkeypatch):
    """The claude CLI exits 0 on error subtypes (error_max_turns,
    error_during_execution) whose JSON has no 'result' key; that must surface
    as a clear error, not a bare KeyError."""
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")

    class Proc:
        stdout = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})

    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(RuntimeError, match="error_max_turns"):
        llm.complete("hello")
