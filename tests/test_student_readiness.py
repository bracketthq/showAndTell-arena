"""The readiness gate: pause, instruct, re-check; Continue/Cancel channels."""
from __future__ import annotations

import json
import threading
import time

import pytest

from showAndTell.students import readiness


def test_ensure_is_silent_when_the_check_passes():
    out = []
    readiness.ensure(lambda: None, "irrelevant", out.append)
    assert out == []


def test_non_interactive_gate_fails_with_the_instructions(monkeypatch):
    monkeypatch.delenv(readiness.GATE_DIR_ENV, raising=False)
    monkeypatch.setattr(readiness.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="not signed in"):
        readiness.ensure(lambda: "not signed in", "sign in first", print)


def test_viewer_gate_continue_rechecks_before_proceeding(monkeypatch, tmp_path):
    monkeypatch.setenv(readiness.GATE_DIR_ENV, str(tmp_path))
    states = iter(["broken", "still broken", None])

    def press_continue_twice():
        for _ in range(2):
            deadline = time.time() + 5
            while not (tmp_path / readiness.GATE_FILE).exists():
                assert time.time() < deadline
                time.sleep(0.05)
            (tmp_path / readiness.ACK_FILE).touch()
            # wait until the gate consumed this press before pressing again —
            # two touches inside one round collapse into a single unlink
            while (tmp_path / readiness.ACK_FILE).exists():
                assert time.time() < deadline
                time.sleep(0.05)

    thread = threading.Thread(target=press_continue_twice, daemon=True)
    thread.start()
    out = []
    readiness.ensure(lambda: next(states, None), "fix it", out.append)
    thread.join(timeout=5)
    # first Continue re-checked and found it still broken; the second passed
    assert sum("✋" in line for line in out) == 2
    assert any("verified" in line for line in out)
    assert not (tmp_path / readiness.GATE_FILE).exists()


def test_viewer_gate_cancel_aborts(monkeypatch, tmp_path):
    monkeypatch.setenv(readiness.GATE_DIR_ENV, str(tmp_path))

    def press_cancel():
        deadline = time.time() + 5
        while not (tmp_path / readiness.GATE_FILE).exists():
            assert time.time() < deadline
            time.sleep(0.05)
        (tmp_path / readiness.CANCEL_FILE).touch()

    threading.Thread(target=press_cancel, daemon=True).start()
    with pytest.raises(readiness.ReadinessCancelled, match="cancelled"):
        readiness.ensure(lambda: "broken", "fix it", lambda _m: None)


def test_viewer_gate_live_pass_needs_no_continue(monkeypatch, tmp_path):
    monkeypatch.setenv(readiness.GATE_DIR_ENV, str(tmp_path))
    fixed = time.time() + 0.3
    out = []
    readiness.ensure(
        lambda: None if time.time() >= fixed else "waiting for sign-in",
        "sign in in the opened browser", out.append, live=True)
    assert any("verified" in line for line in out)
    assert not (tmp_path / readiness.ACK_FILE).exists()


def test_gate_publication_exposes_managed_browser_destination(monkeypatch, tmp_path):
    monkeypatch.setenv(readiness.GATE_DIR_ENV, str(tmp_path))

    with readiness.GatePublication(
            "sign in", "complete login", open_url="https://agent.example/login",
            open_label="Open login"):
        payload = json.loads((tmp_path / readiness.GATE_FILE).read_text())

    assert payload == {
        "problem": "sign in",
        "instructions": "complete login",
        "open_browser": True,
        "open_url": "https://agent.example/login",
        "open_label": "Open login",
    }
    assert not (tmp_path / readiness.GATE_FILE).exists()


def test_gate_publication_reports_continue_without_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv(readiness.GATE_DIR_ENV, str(tmp_path))
    publication = readiness.GatePublication("sign in", "complete login")

    with publication:
        (tmp_path / readiness.ACK_FILE).touch()
        assert not publication.cancelled()
        assert (tmp_path / readiness.ACK_FILE).exists()
        assert publication.continued()
        assert not publication.continued()

    assert not (tmp_path / readiness.ACK_FILE).exists()
