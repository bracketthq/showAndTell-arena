"""Loopback viewer process manager for full benchmark task runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from urllib.parse import urlsplit

from showAndTell.bundles import hub
from showAndTell.core import chrome
from showAndTell.students import readiness
from . import _util, executions, fixture_host, generate


MAX_JSON_BYTES = 4096
ADAPTERS = {adapter: label for _, adapter, label in generate.PRODUCT_ROWS}
PRODUCTS = {adapter: product for product, adapter, _ in generate.PRODUCT_ROWS}


class TaskRunError(_util.ViewerError):
    pass


class TaskRunStore:
    """Own subprocesses launched from one local viewer server."""

    def __init__(
        self,
        tasks_dir: Path,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        supervisor: executions.ExecutionSupervisor | None = None,
        execution_allocator=None,
        draft_workspace_factory=None,
        drafts_dir: Path | None = None,
        managed_profile: Path | None = None,
        profile_launcher: Callable[[Path, str | None, int], object] | None = None,
        port_allocator=None,
    ) -> None:
        self.tasks_dir = Path(tasks_dir).resolve()
        self.repository_root = self.tasks_dir.parent
        self.drafts_dir = Path(
            drafts_dir or self.repository_root / "task-drafts").resolve()
        self.log_root = self.repository_root / "runs" / ".viewer"
        self._popen = popen
        self.supervisor = supervisor or executions.ExecutionSupervisor()
        self.execution_allocator = execution_allocator or self._acquire_execution
        if draft_workspace_factory is None:
            from .capture import CaptureWorkspace
            draft_workspace_factory = CaptureWorkspace
        self.draft_workspace_factory = draft_workspace_factory
        self.managed_profile = Path(
            managed_profile
            or os.environ.get("SHOWANDTELL_CHROME_PROFILE", "")
            or Path.home() / ".showAndTell/chrome-profile"
        ).expanduser()
        self.profile_launcher = profile_launcher or self._launch_profile_url
        self.port_allocator = port_allocator or chrome.available_cdp_port
        self._runs: dict[str, dict] = {}
        self._lock = threading.RLock()
        # Starting can include a deliberate stop-and-replace. Serialize that
        # transition without blocking ordinary status polls on ``_lock``.
        self._start_lock = threading.Lock()

    @staticmethod
    def _launch_profile_url(
        profile_root: Path, url: str | None, cdp_port: int,
    ) -> object:
        process = chrome.launch(
            user_data_dir=profile_root, port=cdp_port, url=url)
        # A URL handed to an already-running profile selects its window. On
        # macOS, explicitly activate Chrome as well so the managed window is
        # not left behind the viewer after the automatic handoff.
        if (sys.platform == "darwin"
                and "Google Chrome.app" in chrome.chrome_bin()):
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application "Google Chrome" to activate'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return process

    def _task_dir(self, task: object, source: str = "task") -> Path:
        if not isinstance(task, str) or not _util.TASK_NAME.fullmatch(task):
            raise TaskRunError("task name is invalid")
        if source == "draft":
            target = (self.drafts_dir / task).resolve()
            if (not target.is_relative_to(self.drafts_dir)
                    or target.parent != self.drafts_dir
                    or not (target / "task.toml").is_file()):
                raise TaskRunError("captured task was not found", 404)
            try:
                testcase = json.loads(
                    (target / "testcase.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TaskRunError("captured task metadata is invalid", 409) from exc
            if not ((testcase.get("replay") or {}).get("generated")
                    and (target / "demonstrate.py").is_file()):
                raise TaskRunError(
                    "this captured task has no generated replay; "
                    "re-record it in managed mode", 409)
            return target
        target = (self.tasks_dir / task).resolve()
        if (target.is_relative_to(self.tasks_dir)
                and target.parent == self.tasks_dir
                and (target / "task.toml").is_file()
                and (target / "demo" / "seed.json").is_file()):
            return target
        # a task shown from the published dataset runs out of the hub cache
        dataset_root = hub.cached_dataset_root()
        if dataset_root is not None:
            cached = dataset_root / task
            if ((cached / "task.toml").is_file()
                    and (cached / "demo" / "seed.json").is_file()):
                return cached
        raise TaskRunError("task was not found or has no seed", 404)

    @staticmethod
    def _acquire_execution(applications: list[str], holder: str, demo_root: Path):
        return fixture_host.acquire_execution(
            applications, holder, demo_root=demo_root)

    def start(
        self, task: object, adapter: object, *, replace: object = False,
        hear_narration: object = False, source: object = "task",
    ) -> dict:
        if not isinstance(replace, bool):
            raise TaskRunError("replace must be true or false")
        if not isinstance(hear_narration, bool):
            raise TaskRunError("hear_narration must be true or false")
        if source not in {"task", "draft"}:
            raise TaskRunError("run source must be 'task' or 'draft'")
        with self._start_lock:
            return self._start_serialized(
                task, adapter, replace=replace,
                hear_narration=hear_narration, source=str(source))

    @staticmethod
    def _active_run(item: dict) -> dict:
        adapter = item.get("adapter")
        return {
            "id": item.get("id"),
            "task": item.get("task") or "task starting",
            "adapter": adapter,
            "source": item.get("source", "task"),
            "label": ADAPTERS.get(adapter, adapter or "Product"),
            "started_at": item.get("started_at"),
        }

    @staticmethod
    def _terminate_process(process) -> None:
        """Stop the viewer-owned runner and every child in its process group."""
        try:
            _util.terminate_process_group(process)
        except subprocess.TimeoutExpired as exc:
            raise TaskRunError(
                "the current task could not be stopped", 500) from exc

    def _stop_active_runs(self, active: list[tuple[str, dict]]) -> None:
        for run_id, item in active:
            process = item.get("process")
            if process is None:
                raise TaskRunError(
                    "the current task is still starting; try again shortly", 409,
                    details={"code": "task_run_stopping",
                             "active_run": self._active_run(item)})
            self._terminate_process(process)
            self.supervisor.complete(run_id)
        # The adapters normally close Chrome in their cleanup paths. A forced
        # process-group stop cannot run Python finally blocks, so explicitly
        # clear only ShowAndTell's managed profile before the replacement starts.
        chrome.kill_all_managed_chrome(self.managed_profile)

    def _ordinary_execution(
        self, task: str, task_dir: Path, adapter: str,
        applications: list[str], run_id: str,
        chrome_cdp_port: int, codex_cdp_port: int,
    ) -> tuple[list[str], object, Callable[[], None]]:
        command = [
            sys.executable, "-m", "showAndTell.cli", adapter,
            "--task", str(task_dir), "--no-cache",
        ]
        if adapter == "codex-record":
            command.extend([
                "--chrome-cdp-port", str(chrome_cdp_port),
                "--cdp-port", str(codex_cdp_port),
            ])
        else:
            command.extend(["--cdp-port", str(chrome_cdp_port)])
        try:
            execution = self.execution_allocator(
                applications, f"task-{task}-{run_id}", task_dir / "demo")
        except Exception as exc:
            raise TaskRunError(
                f"could not acquire task applications: {exc}", 409) from exc
        return command, execution, execution.close

    def _draft_execution(
        self, task: str, task_dir: Path, adapter: str,
        applications: list[str], run_id: str,
        chrome_cdp_port: int, codex_cdp_port: int,
    ) -> tuple[list[str], object, Callable[[], None]]:
        try:
            testcase = json.loads(
                (task_dir / "testcase.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskRunError("captured task metadata is invalid", 409) from exc
        original_surfaces = [dict(row) for row in testcase.get("surfaces", [])]
        workspace = None
        try:
            workspace = self.draft_workspace_factory({
                "slug": task,
                "_execution_kind": "run",
                "_execution_product": ADAPTERS[adapter],
                "applications": applications,
                "primary_application": testcase.get("primary_application")
                or (applications[0] if applications else ""),
                "surfaces": [dict(row) for row in original_surfaces],
                "force": False,
            })
            workspace.start()
            captured_by_id = {
                row.get("id"): row.get("captured_url", row.get("url"))
                for row in original_surfaces
            }
            testcase["surfaces"] = [
                {**row, "captured_url": captured_by_id.get(
                    row.get("id"), row["url"])}
                for row in workspace.metadata["surfaces"]
            ]
            _util.write_json(task_dir / "testcase.json", testcase)
            workspace.restore(
                use_export=True, state_dir=task_dir / "demo",
                snapshot_only=True)
        except Exception as exc:
            if workspace is not None:
                workspace.close()
            status = getattr(exc, "status", 500)
            details = getattr(exc, "details", {})
            raise TaskRunError(
                f"could not prepare captured task applications: {exc}",
                status, details=details) from exc
        command = [
            sys.executable, "-m", "showAndTell.cli", "capture-trial",
            "--draft", str(task_dir), "--product", PRODUCTS[adapter],
            "--cdp-port", str(chrome_cdp_port),
            "--codex-cdp-port", str(codex_cdp_port),
        ]
        execution = getattr(workspace, "execution", None)
        return command, execution, workspace.close

    def _start_serialized(
        self, task: object, adapter: object, *, replace: bool,
        hear_narration: bool, source: str,
    ) -> dict:
        task_dir = self._task_dir(task, source)
        if not isinstance(adapter, str) or adapter not in ADAPTERS:
            raise TaskRunError(f"adapter must be one of {sorted(ADAPTERS)}")
        cleanup = None
        try:
            (self.managed_profile / "Default").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TaskRunError(
                f"could not create the managed Chrome profile at "
                f"{self.managed_profile}: {exc}", 500) from exc
        # A missing Brackett extension is handled by the run's readiness
        # gate (pause + instructions + Continue/Cancel in the run panel),
        # not by refusing to start.
        config = tomllib.loads((task_dir / "task.toml").read_text())
        applications = list(config.get("task", {}).get("applications", ()))
        if not applications:
            raise TaskRunError("task declares no applications")
        chrome_cdp_port = self.port_allocator(chrome.DEFAULT_CDP_PORT)
        codex_cdp_port = self.port_allocator(
            9333, unavailable={chrome_cdp_port})
        run_id = secrets.token_hex(16)
        with self._lock:
            active = [(item_id, item) for item_id, item in self._runs.items()
                      if _util.process_active(item)]
        if active and not replace:
            raise TaskRunError(
                "another task is already running", 409,
                details={"code": "task_run_busy",
                         "active_run": self._active_run(active[0][1])})
        if active:
            self._stop_active_runs(active)
        with self._lock:
            # Reserve the single run slot while the slower execution allocation
            # below proceeds with the lock released, so status polls stay live.
            self._runs[run_id] = {
                "id": run_id,
                "task": str(task),
                "adapter": str(adapter),
                "source": source,
                "started_at": _util.utc_now(),
                "applications": applications,
                "process": None,
                "hear_narration": hear_narration,
            }
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
            log_path = self.log_root / f"{run_id}.log"
            narration_control = self.log_root / f"{run_id}-hear-narration"
            narration_control.write_text(
                "1" if hear_narration else "0", encoding="utf-8")
            if source == "draft":
                command, execution, cleanup = self._draft_execution(
                    str(task), task_dir, str(adapter), applications, run_id,
                    chrome_cdp_port, codex_cdp_port)
            else:
                command, execution, cleanup = self._ordinary_execution(
                    str(task), task_dir, str(adapter), applications, run_id,
                    chrome_cdp_port, codex_cdp_port)
            annotate = getattr(execution, "annotate", None)
            if callable(annotate):
                annotate(
                    task=str(task), kind="run",
                    product=ADAPTERS[str(adapter)],
                )
            child_env = executions.child_environment(execution)
            child_env["SHOWANDTELL_CHROME_PROFILE"] = str(self.managed_profile)
            child_env["SHOWANDTELL_HEAR_NARRATION"] = (
                "1" if hear_narration else "0")
            child_env["SHOWANDTELL_HEAR_NARRATION_CONTROL"] = str(
                narration_control)
            gate_dir = self.log_root / f"{run_id}-gate"
            child_env[readiness.GATE_DIR_ENV] = str(gate_dir)
            try:
                with log_path.open("wb") as log:
                    process = self._popen(
                        command,
                        cwd=self.repository_root,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=child_env,
                        start_new_session=True,
                    )
            except Exception as exc:
                raise TaskRunError(
                    f"could not start {ADAPTERS[str(adapter)]}: {exc}", 500
                ) from exc
        except BaseException:
            if cleanup is not None:
                cleanup()
            if "log_path" in locals():
                log_path.unlink(missing_ok=True)
            with self._lock:
                self._runs.pop(run_id, None)
            raise
        with self._lock:
            self._runs[run_id] = {
                "id": run_id,
                "task": str(task),
                "adapter": str(adapter),
                "source": source,
                "process": process,
                "log_path": log_path,
                "started_at": _util.utc_now(),
                "applications": applications,
                "chrome_cdp_port": chrome_cdp_port,
                "execution": execution,
                "gate_dir": gate_dir,
                "narration_control": narration_control,
                "hear_narration": hear_narration,
            }
        self.supervisor.register(run_id, process=process, cleanup=cleanup)
        return self.status(run_id)

    def gate_action(self, run_id: str, action: object) -> dict:
        """Answer or reopen a readiness gate from the viewer."""
        if action not in ("open", "continue", "cancel"):
            raise TaskRunError("gate action must be 'open', 'continue', or 'cancel'")
        with self._lock:
            item = self._run(run_id)
            gate_dir = item.get("gate_dir")
            gate = _read_gate(gate_dir)
        if gate is None:
            raise TaskRunError("this run is not waiting at a readiness gate", 409)
        if action == "open":
            url = gate.get("open_url")
            if not gate.get("open_browser"):
                raise TaskRunError("this readiness gate has no browser destination", 409)
            try:
                self.profile_launcher(
                    self.managed_profile, url, item["chrome_cdp_port"])
            except Exception as exc:
                raise TaskRunError(
                    f"could not open managed Chrome: {exc}", 500) from exc
            return self.status(run_id)
        marker = (readiness.ACK_FILE if action == "continue"
                  else readiness.CANCEL_FILE)
        (gate_dir / marker).touch()
        return self.status(run_id)

    def cancel(self, run_id: str) -> dict:
        """Stop one active viewer-owned run and release its resources."""
        with self._lock:
            item = self._run(run_id)
            process = item.get("process")
            if process is None:
                raise TaskRunError(
                    "this task is still starting; try cancellation again shortly", 409)
        if process.poll() is None:
            self._terminate_process(process)
            with self._lock:
                item["cancelled"] = True
            try:
                self.supervisor.complete(run_id)
            finally:
                chrome.kill_all_managed_chrome(self.managed_profile)
        return self.status(run_id)

    def set_hear_narration(self, run_id: str, enabled: bool) -> dict:
        """Change the speaker copy without interrupting the virtual mic feed."""
        if not isinstance(enabled, bool):
            raise TaskRunError("hear_narration must be true or false")
        with self._lock:
            item = self._run(run_id)
            process = item.get("process")
            if process is None or process.poll() is not None:
                raise TaskRunError("this task is no longer running", 409)
            control = item.get("narration_control")
            if control is None:
                raise TaskRunError(
                    "live narration control is unavailable for this run", 409)
            control.write_text("1" if enabled else "0", encoding="utf-8")
            item["hear_narration"] = enabled
        return self.status(run_id)

    def _run(self, run_id: str) -> dict:
        return _util.lookup_token(self._runs, run_id, "run", "task run",
                                  TaskRunError)

    def promote_draft(self, capture_store, slug: object) -> dict:
        """Promote only while the draft is outside the shared run slot."""
        with self._start_lock:
            with self._lock:
                active = [item for item in self._runs.values()
                          if _util.process_active(item)
                          and item.get("source") == "draft"
                          and item.get("task") == slug]
            if active:
                raise TaskRunError(
                    "finish the active run before adding this task", 409)
            return capture_store.promote(slug)

    def status(self, run_id: str) -> dict:
        with self._lock:
            item = self._run(run_id)
            exit_code = item["process"].poll()
            result = {
                "id": item["id"],
                "task": item["task"],
                "adapter": item["adapter"],
                "source": item.get("source", "task"),
                "label": ADAPTERS[item["adapter"]],
                "status": ("cancelled" if item.get("cancelled")
                           else _util.process_status(exit_code)),
                "exit_code": exit_code,
                "started_at": item["started_at"],
                "applications": list(item["applications"]),
                "hear_narration": item.get("hear_narration", False),
                "log": _util.log_tail(item["log_path"]),
                "gate": _read_gate(item.get("gate_dir")),
            }
        if exit_code is not None:
            self.supervisor.complete(run_id)
        return result

    def close(self) -> None:
        self.supervisor.close()


def _read_gate(gate_dir) -> dict | None:
    """The child's published readiness pause, if it is waiting at one."""
    if not gate_dir:
        return None
    try:
        raw = (gate_dir / readiness.GATE_FILE).read_text("utf-8")
        gate = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(gate, dict):
        return None
    result = {"problem": str(gate.get("problem", "")),
              "instructions": str(gate.get("instructions", ""))}
    open_url = str(gate.get("open_url", ""))
    parsed = urlsplit(open_url)
    trusted_url = parsed.scheme in ("http", "https") and parsed.netloc
    if trusted_url or (not open_url and gate.get("open_browser") is True):
        result["open_browser"] = True
        if trusted_url:
            result["open_url"] = open_url
        result["open_label"] = str(
            gate.get("open_label") or "Open managed Chrome")
    return result
