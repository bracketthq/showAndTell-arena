"""Result cache for teach runs.

A teach run drives a real, external product end to end — minutes of work — and
writes its graded result to runs/<ts>-<adapter>-<task>/comprehend.json. This
module lets a repeat invocation reuse that result instead of paying for it
again, keyed by the adapter, a fingerprint of the task's inputs, and the live
judge protocol fingerprint. Edit the task or change the judge model/prompt and
the cache misses rather than returning an incomparable score.

The cache lives under runs/.cache/ (runs/ is gitignored) so it never enters git.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

TEACH_ADAPTERS = ("claude-teach", "brackett-teach", "codex-record")

# The files that define what a task *is*: its inputs, its demonstration, and
# what it grades. Any change invalidates a cached result, and a bundle records
# them as the run's provenance — so both planes read this one list.
TASK_ARTIFACT_FILES = (
    "task.toml",
    "task_logic.py",
    "demonstrate.py",
    "quiz/questions.json",
    "demo/narration_script.jsonl",
    "demo/seed.json",
)


def cache_dir() -> Path:
    """Cache root, relative to cwd — same base the adapters write runs under."""
    return Path("runs") / ".cache"


def adapter_from_run_dir(run_dir: Path) -> str | None:
    """Return the teach adapter encoded in a standard run-directory name.

    Run directories are durable user-facing artifacts, so keep this parser
    strict: an arbitrary directory passed to ``regrade`` must not be
    mislabeled and inserted into the reusable result cache.
    """
    name = Path(run_dir).name
    try:
        datetime.datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    suffix = name[15:]
    return next(
        (adapter for adapter in TEACH_ADAPTERS
         if suffix.startswith(f"-{adapter}-")),
        None,
    )


def _meaning_bytes(rel: str, p: Path) -> bytes:
    """File bytes as they count toward the fingerprint. task.toml contributes
    its bytes MINUS the [complexity] block: those flags describe how hard the
    task is, not what a result means, so declaring or tuning them must not
    invalidate cached teach results. The strip is textual so a task.toml
    without the block hashes exactly as before this feature existed."""
    if not p.exists():
        return b"\0__absent__"
    data = p.read_bytes()
    if rel != "task.toml":
        return data
    out, skipping = [], False
    for line in data.splitlines(keepends=True):
        s = line.strip()
        if s.startswith(b"["):
            skipping = s == b"[complexity]"
            if skipping:  # the blank separator above the block belongs to it
                while out and not out[-1].strip():
                    out.pop()
        if not skipping:
            out.append(line)
    return b"".join(out)


def fingerprint(task_dir: Path) -> str:
    """A stable hash over the task's meaning-defining inputs. Missing files are
    folded in as absent (still hashed), so adding/removing one shifts the hash."""
    h = hashlib.sha256()
    for rel in TASK_ARTIFACT_FILES:
        p = Path(task_dir) / rel
        h.update(rel.encode())
        h.update(b"\0")
        h.update(_meaning_bytes(rel, p))
        h.update(b"\0")
    return h.hexdigest()


def _cache_key(adapter: str, task_dir: Path,
               judge_fingerprint: str | None = None) -> str:
    fp = fingerprint(task_dir)
    judge = f"__judge-{judge_fingerprint[:12]}" if judge_fingerprint else ""
    return f"{adapter}__{Path(task_dir).name}__{fp[:12]}{judge}"


def _cache_files(adapter: str, task_dir: Path,
                 judge_fingerprint: str | None = None) -> list[Path]:
    """All saved results for the current task fingerprint, oldest first.

    The un-suffixed name is the legacy single-entry cache format. New entries
    carry a timestamp suffix so forced runs remain available for aggregation.
    """
    root = cache_dir()
    key = _cache_key(adapter, task_dir, judge_fingerprint)
    if not root.exists():
        return []
    files = sorted(root.glob(f"{key}*.json"))
    if judge_fingerprint is None:
        # The legacy prefix is also a prefix of judge-qualified keys. Never let
        # an unqualified lookup select an arbitrary judge protocol.
        files = [path for path in files if "__judge-" not in path.name]
    return files


def load(adapter: str, task_dir: Path,
         judge_fingerprint: str | None = None) -> dict | None:
    """The cached payload for this adapter + current task fingerprint, or None on
    a miss (never cached, task changed since, or an unreadable cache file)."""
    hits = []
    for f in _cache_files(adapter, task_dir, judge_fingerprint):
        try:
            payload = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            hits.append(payload)
    return max(hits, key=lambda p: p.get("cached_at", ""), default=None)


def save(adapter: str, task_dir: Path, result: dict,
         run_dir: Path | None = None,
         judge_fingerprint: str | None = None) -> Path:
    """Append `result` to this adapter/task's history; returns the new path."""
    root = cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    # Microseconds make back-to-back saves distinct without relying on run-dir
    # naming. The sortable suffix also makes the on-disk history readable.
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    f = root / f"{_cache_key(adapter, task_dir, judge_fingerprint)}__{stamp}.json"
    payload = {
        "adapter": adapter,
        "task": Path(task_dir).name,
        "fingerprint": fingerprint(task_dir),
        "judge_fingerprint": judge_fingerprint,
        "cached_at": now.isoformat(timespec="microseconds"),
        "run_dir": str(run_dir) if run_dir is not None else None,
        "result": result,
    }
    f.write_text(json.dumps(payload, indent=2) + "\n")
    return f
