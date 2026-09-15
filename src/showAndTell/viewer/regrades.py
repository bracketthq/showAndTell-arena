"""Background regrading for saved responses in the local viewer."""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from showAndTell.bundles import hub
from showAndTell.quiz import comprehend

from . import _util


MAX_JSON_BYTES = 4096


class RegradeError(_util.ViewerError):
    pass


class RegradeStore:
    """Own regrade subprocesses launched by one local viewer server."""

    def __init__(
        self,
        repository_root: Path,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.log_root = self.repository_root / "runs" / ".viewer"
        self._popen = popen
        self._runs: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _resolve_task(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise RegradeError("task path is required")
        target = (self.repository_root / value).resolve()
        allowed_roots = [
            self.repository_root / "tasks",
            self.repository_root / "task-drafts",
        ]
        # a task shown from the published dataset regrades out of the hub
        # cache (regrading only rewrites the saved run under runs/)
        dataset_root = hub.cached_dataset_root()
        if dataset_root is not None:
            allowed_roots.append(dataset_root)
        if (
            not any(target.parent == root.resolve() for root in allowed_roots)
            or not (target / "task.toml").is_file()
        ):
            raise RegradeError("task was not found", 404)
        return target

    def _resolve_run(self, value: object, task_dir: Path) -> Path:
        if not isinstance(value, str) or not value:
            raise RegradeError("run path is required")
        target = (self.repository_root / value).resolve()
        runs_root = (self.repository_root / "runs").resolve()
        draft_trials = (task_dir / "trials").resolve()
        expected_parent = (
            draft_trials
            if task_dir.parent == (self.repository_root / "task-drafts").resolve()
            else runs_root
        )
        if target.parent != expected_parent:
            raise RegradeError("saved run was not found", 404)
        if not (target / comprehend.RESPONSE_ARTIFACT_NAME).is_file():
            raise RegradeError("saved response was not found", 404)
        if expected_parent == runs_root:
            runtime = target / "task-runtime.json"
            try:
                recorded_task = json.loads(runtime.read_text()).get("task")
            except (OSError, ValueError, AttributeError):
                recorded_task = None
            if recorded_task != task_dir.name:
                raise RegradeError("saved response belongs to a different task", 409)
        return target

    def start(self, task: object, run: object) -> dict:
        task_dir = self._resolve_task(task)
        run_dir = self._resolve_run(run, task_dir)
        with self._lock:
            if any(
                _util.process_active(item) and item["run_dir"] == run_dir
                for item in self._runs.values()
            ):
                raise RegradeError("this saved response is already being regraded", 409)
            regrade_id = secrets.token_hex(16)
            self.log_root.mkdir(parents=True, exist_ok=True)
            log_path = self.log_root / f"regrade-{regrade_id}.log"
            command = [
                sys.executable,
                "-m",
                "showAndTell.cli",
                "regrade",
                "--task",
                str(task_dir),
                "--run",
                str(run_dir),
                "--replace",
            ]
            try:
                with log_path.open("wb") as log:
                    process = self._popen(
                        command,
                        cwd=self.repository_root,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except Exception as exc:
                log_path.unlink(missing_ok=True)
                raise RegradeError(f"could not start regrading: {exc}", 500) from exc
            self._runs[regrade_id] = {
                "id": regrade_id,
                "task": str(task),
                "run": str(run),
                "run_dir": run_dir,
                "process": process,
                "log_path": log_path,
                "started_at": _util.utc_now(),
            }
        return self.status(regrade_id)

    def status(self, regrade_id: str) -> dict:
        with self._lock:
            item = _util.lookup_token(
                self._runs, regrade_id, "regrade", "regrade", RegradeError)
            exit_code = item["process"].poll()
            return {
                "id": item["id"],
                "task": item["task"],
                "run": item["run"],
                "status": _util.process_status(exit_code),
                "exit_code": exit_code,
                "started_at": item["started_at"],
                "log": _util.log_tail(item["log_path"]),
            }

    def close(self) -> None:
        with self._lock:
            processes = [item["process"] for item in self._runs.values()]
        for process in processes:
            if process.poll() is None:
                process.terminate()

