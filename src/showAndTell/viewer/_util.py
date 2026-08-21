"""Small helpers shared by the viewer's sibling modules."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
from pathlib import Path

# Promoted task directory names; shared by every store that resolves one.
TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ViewerError(ValueError):
    """A store-level failure carrying the HTTP status serve.py answers with.

    ``details`` is machine-readable fields merged into the JSON error payload,
    so the frontend can react to a code instead of parsing the message.
    """

    def __init__(self, message: str, status: int = 400,
                 *, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.details = dict(details or {})


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log_tail(path: Path, maximum: int = 16000) -> str:
    """Decode at most the final ``maximum`` bytes of a potentially large log."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(size - maximum, 0))
            return handle.read(maximum).decode("utf-8", "replace")
    except OSError:
        return ""


def process_status(exit_code: int | None) -> str:
    return ("running" if exit_code is None
            else "complete" if exit_code == 0 else "failed")


def process_active(item: dict) -> bool:
    """Whether a store's run/trial entry is still live.

    ``process`` is None while a reservation's slow startup runs outside the
    store lock; that placeholder counts as active so concurrent starts and
    promotions keep conflicting with it.
    """
    process = item.get("process")
    return process is None or process.poll() is None


def terminate_process_group(process, timeout: float = 5.0) -> None:
    """Stop a viewer-owned subprocess and every child in its new session."""
    if process.poll() is not None:
        return

    def send(sig, fallback: str) -> None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            try:
                os.killpg(pid, sig)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        try:
            getattr(process, fallback)()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM, "terminate")
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL, "kill")
        process.wait(timeout=timeout)


def lookup_token(table: dict, token: str, kind: str, missing: str,
                 error: type[ViewerError]) -> dict:
    """A 32-hex id's table entry, or the store's own 404."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise error(f"{kind} id is invalid", 404)
    try:
        return table[token]
    except KeyError as exc:
        raise error(f"{missing} was not found", 404) from exc


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
