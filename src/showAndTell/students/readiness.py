"""Product-readiness gate: pause, instruct, and re-check instead of failing.

Every product adapter has prerequisites the harness cannot create itself —
a signed-in account, an installed browser extension, an enabled plugin.
``await_user_fix`` turns each missing prerequisite into an explicit pause:
the run states the problem and the remedy, the user fixes it out of band
and *continues*, and the gate re-checks before proceeding — a continue is
never trusted on its own. Cancelling aborts the run cleanly.

Two front ends share the contract:

- a terminal run prompts on stdin (Enter re-checks, ``q`` cancels);
- a viewer run publishes the pause to ``$SHOWANDTELL_GATE_DIR/gate.json`` and
  waits for the viewer's Continue/Cancel buttons, which drop ``gate-ack``
  or ``gate-cancel`` marker files beside it.

With neither channel available the gate fails immediately with the
instructions, never hanging an unattended run.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

GATE_DIR_ENV = "SHOWANDTELL_GATE_DIR"
GATE_FILE = "gate.json"
ACK_FILE = "gate-ack"
CANCEL_FILE = "gate-cancel"


class ReadinessCancelled(SystemExit):
    """The user chose Cancel at a readiness gate."""


def _gate_dir() -> Path | None:
    raw = os.environ.get(GATE_DIR_ENV)
    return Path(raw) if raw else None


def _passes(check) -> bool:
    try:
        return check() is None
    except Exception:
        return False


def _gate_payload(problem: str, instructions: str, *,
                  open_url: str | None = None,
                  open_label: str | None = None) -> dict:
    payload = {"problem": problem, "instructions": instructions}
    if open_url or open_label:
        payload["open_browser"] = True
        if open_url:
            payload["open_url"] = open_url
        payload["open_label"] = open_label or "Open managed Chrome"
    return payload


def _wait_for_viewer(gate_dir: Path, problem: str, instructions: str,
                     out, recheck=None, *, open_url: str | None = None,
                     open_label: str | None = None) -> bool:
    """Publish the pause and block until Continue, Cancel, or a live pass.

    Returns True when ``recheck`` observed the prerequisite passing on its
    own (no Continue needed), False when the user pressed Continue.
    """
    gate_dir.mkdir(parents=True, exist_ok=True)
    ack, cancel = gate_dir / ACK_FILE, gate_dir / CANCEL_FILE
    ack.unlink(missing_ok=True)
    cancel.unlink(missing_ok=True)
    (gate_dir / GATE_FILE).write_text(json.dumps(_gate_payload(
        problem, instructions, open_url=open_url, open_label=open_label)), "utf-8")
    out("  waiting for Continue in the viewer…")
    try:
        while True:
            if cancel.exists():
                raise ReadinessCancelled(f"cancelled at readiness gate: {problem}")
            if ack.exists():
                ack.unlink(missing_ok=True)
                return False
            if recheck is not None and _passes(recheck):
                return True
            time.sleep(2 if recheck is not None else 0.5)
    finally:
        (gate_dir / GATE_FILE).unlink(missing_ok=True)


def _wait_for_terminal(problem: str, out, recheck=None) -> bool:
    """Prompt on stdin; with ``recheck`` also notice a live pass. Returns
    True on a live pass, False when the user asked to continue."""
    import select

    out("  press Enter to continue once done, or q + Enter to cancel.")
    while True:
        if recheck is not None:
            readable, _w, _x = select.select([sys.stdin], [], [], 2)
            if not readable:
                if _passes(recheck):
                    return True
                continue
        line = sys.stdin.readline()
        if not line or line.strip().lower() == "q":
            raise ReadinessCancelled(f"cancelled at readiness gate: {problem}")
        return False


def await_user_fix(check, problem: str, instructions: str, out=print, *,
                   live: bool = False, open_url: str | None = None,
                   open_label: str | None = None) -> None:
    """Block until ``check()`` passes, guided by the user.

    ``check`` returns None when satisfied, or a short current-state string.
    With ``live=True`` the gate additionally re-runs the check in the
    background — a prerequisite the harness can observe passing (a sign-in
    completing in the driven browser, a file appearing on disk) resolves the
    pause without a Continue. Leave it False when re-checking is intrusive.
    """
    gate_dir = _gate_dir()
    interactive = gate_dir is not None or sys.stdin.isatty()
    recheck = check if live else None
    state: str | None = problem
    while state is not None:
        out(f"✋ {state}")
        out(f"  {instructions}")
        if not interactive:
            raise SystemExit(f"{state}\n{instructions}")
        if gate_dir is not None:
            passed = _wait_for_viewer(
                gate_dir, state, instructions, out, recheck,
                open_url=open_url, open_label=open_label)
        else:
            passed = _wait_for_terminal(state, out, recheck)
        state = None if passed else check()
        if state is None:
            out("  ✓ verified — continuing.")


def ensure(check, instructions: str, out=print, *, live: bool = False,
           open_url: str | None = None, open_label: str | None = None) -> None:
    """Run ``check`` once; gate on its report when it fails."""
    state = check()
    if state is not None:
        await_user_fix(
            check, state, instructions, out, live=live,
            open_url=open_url, open_label=open_label)


class GatePublication:
    """Make a self-resolving wait visible to the viewer's gate UI.

    For waits that already watch their prerequisite live (Brackett's sign-in
    loop), this publishes the problem/instructions so the viewer shows them
    with Continue/Cancel, and exposes both actions for the loop to honor.
    """

    def __init__(self, problem: str, instructions: str, *,
                 open_url: str | None = None,
                 open_label: str | None = None) -> None:
        self.problem = problem
        self.instructions = instructions
        self.open_url = open_url
        self.open_label = open_label
        self._dir = _gate_dir()

    def __enter__(self) -> "GatePublication":
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / ACK_FILE).unlink(missing_ok=True)
            (self._dir / CANCEL_FILE).unlink(missing_ok=True)
            (self._dir / GATE_FILE).write_text(json.dumps(_gate_payload(
                self.problem, self.instructions, open_url=self.open_url,
                open_label=self.open_label)), "utf-8")
        return self

    def cancelled(self) -> bool:
        if self._dir is None:
            return False
        return (self._dir / CANCEL_FILE).exists()

    def continued(self) -> bool:
        """Consume and report one viewer Continue action."""
        if self._dir is None:
            return False
        acknowledgement = self._dir / ACK_FILE
        if not acknowledgement.exists():
            return False
        acknowledgement.unlink(missing_ok=True)
        return True

    def __exit__(self, *_exc) -> None:
        if self._dir is not None:
            (self._dir / GATE_FILE).unlink(missing_ok=True)
