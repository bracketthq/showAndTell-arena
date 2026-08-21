"""Confined deletion of individual saved viewer results and their artifacts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import _util


class ResultDeletionError(_util.ViewerError):
    pass


def _child(root: Path, name: object, label: str) -> Path:
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or Path(name).name != name):
        raise ResultDeletionError(f"{label} is invalid")
    root = root.resolve()
    target = root / name
    if target.parent.resolve() != root:
        raise ResultDeletionError(f"{label} is invalid")
    return target


def _read_cache(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResultDeletionError("saved result was not found", 404) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultDeletionError("saved result could not be read", 409) from exc
    if not isinstance(payload, dict):
        raise ResultDeletionError("saved result is not valid", 409)
    return payload


def _promoted_run(cache_dir: Path, value: object) -> Path | None:
    """Resolve only ordinary runs/<one-child> directories; never external paths."""
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    runs_root = cache_dir.resolve().parent
    relative = Path(value)
    if (len(relative.parts) != 2 or relative.parts[0] != runs_root.name
            or relative.parts[1].startswith(".")):
        return None
    candidate = runs_root / relative.parts[1]
    if candidate.is_symlink():
        return None
    return candidate.resolve()


def _cache_still_references(cache_dir: Path, run_dir: Path) -> bool:
    for path in cache_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(payload, dict)
                and _promoted_run(cache_dir, payload.get("run_dir")) == run_dir):
            return True
    return False


def _remove_directory(target: Path, label: str) -> bool:
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        raise ResultDeletionError(f"{label} is not a removable result directory", 409)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ResultDeletionError(f"{label} could not be deleted", 409) from exc
    return True


def delete_result(cache_dir: Path, tasks_dir: Path, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ResultDeletionError("request body must be an object")
    kind = payload.get("kind")
    result_id = payload.get("id")

    if kind == "cache":
        target = _child(cache_dir, result_id, "saved result id")
        if target.suffix != ".json" or target.is_symlink():
            raise ResultDeletionError("saved result id is invalid")
        cached = _read_cache(target)
        run_dir = _promoted_run(cache_dir, cached.get("run_dir"))
        try:
            target.unlink()
        except FileNotFoundError as exc:
            raise ResultDeletionError("saved result was not found", 404) from exc
        except OSError as exc:
            raise ResultDeletionError("saved result could not be deleted", 409) from exc
        artifacts_deleted = bool(
            run_dir and not _cache_still_references(cache_dir, run_dir)
            and _remove_directory(run_dir, "saved run"))
        return {"ok": True, "artifacts_deleted": artifacts_deleted}

    if kind == "run":
        target = _child(cache_dir.parent, result_id, "saved run id")
        if target.name.startswith("."):
            raise ResultDeletionError("saved run id is invalid")
        if not target.exists():
            raise ResultDeletionError("saved run was not found", 404)
        if not (target / "task-runtime.json").is_file():
            raise ResultDeletionError("saved run is not a recognized result", 409)
        _remove_directory(target, "saved run")
        return {"ok": True, "artifacts_deleted": True}

    if kind == "draft":
        task = payload.get("task")
        if not isinstance(task, str) or not _util.TASK_NAME.fullmatch(task):
            raise ResultDeletionError("draft task name is invalid")
        drafts_root = tasks_dir.resolve().parent / "task-drafts"
        task_dir = _child(drafts_root, task, "draft task name")
        if (task_dir.is_symlink()
                or task_dir.resolve().parent != drafts_root.resolve()
                or not (task_dir / "task.toml").is_file()):
            raise ResultDeletionError("draft task was not found", 404)
        trials_dir = task_dir / "trials"
        if trials_dir.is_symlink():
            raise ResultDeletionError("draft trials directory is invalid", 409)
        target = _child(trials_dir, result_id, "draft trial id")
        if not target.exists():
            raise ResultDeletionError("saved trial was not found", 404)
        _remove_directory(target, "saved trial")
        return {"ok": True, "artifacts_deleted": True}

    raise ResultDeletionError("result kind is invalid")
