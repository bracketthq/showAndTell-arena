"""Serve the ShowAndTell-Bench viewer with live data: rebuilds the page from
tasks/, graded cache entries, and ungraded run history on every request, so a
browser refresh always shows the current state with no regenerate step.
Install the project dependencies first.

    python -m showAndTell.viewer.serve            # http://localhost:8000
    python -m showAndTell.viewer.serve 9001       # custom port

showAndTell.viewer.generate remains the way to produce a shareable snapshot file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import webbrowser
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from showAndTell.bundles import hub

from . import (
    _util,
    application_assets,
    capture,
    executions,
    generate,
    regrades,
    result_deletions,
    settings,
    task_runs,
)

_VIDEO_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}
_CHUNK = 64 * 1024
_MAX_QUESTION_BODY = 2 * 1024 * 1024
# Authored seed rows are typed by a person; generous, but bounded.
_MAX_SEED_BODY = 1024 * 1024
_QUESTION_TYPES = {"multiple_choice", "closed", "llm_judge", "rubric"}


def _parse_range(header: str | None, size: int):
    """'bytes=s-e' -> (start, end) inclusive; None -> send the full body;
    'bad' -> unsatisfiable/malformed (416). Multi-range gets the full body
    (browsers don't send it for video)."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    start_s, _, end_s = header[len("bytes="):].partition("-")
    try:
        if start_s == "":                      # suffix form: bytes=-N
            n = int(end_s)
            return (max(size - n, 0), size - 1) if n > 0 and size > 0 else "bad"
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return "bad"
    if start >= size or end < start:
        return "bad"
    return (start, min(end, size - 1))


class QuestionWriteError(_util.ViewerError):
    def __init__(self, message: str, status: int = 400, *,
                 current_hash: str | None = None) -> None:
        super().__init__(message, status,
                         details={"current_hash": current_hash} if current_hash else None)
        self.current_hash = current_hash


def _required_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionWriteError(f"{label} must be a non-empty string")
    return value


def _validate_questions(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise QuestionWriteError("questions must be a non-empty array")
    seen: set[str] = set()
    for index, question in enumerate(value):
        label = f"questions[{index}]"
        if not isinstance(question, dict):
            raise QuestionWriteError(f"{label} must be an object")
        question_id = _required_text(question.get("id"), f"{label}.id")
        if question_id in seen:
            raise QuestionWriteError(f"duplicate question id {question_id!r}")
        seen.add(question_id)
        kind = question.get("type")
        if kind not in _QUESTION_TYPES:
            raise QuestionWriteError(
                f"{label}.type must be one of {sorted(_QUESTION_TYPES)}")
        _required_text(question.get("question"), f"{label}.question")
        evidence = question.get("evidence")
        if evidence is not None and (
                not isinstance(evidence, list)
                or any(not isinstance(item, dict) for item in evidence)):
            raise QuestionWriteError(f"{label}.evidence must be an array of objects")
        if kind == "multiple_choice":
            if "rubric" in question or "answer_aliases" in question:
                raise QuestionWriteError(
                    f"{label} has fields incompatible with multiple_choice")
            options = question.get("options")
            if not isinstance(options, list) or len(options) < 2:
                raise QuestionWriteError(f"{label}.options must contain at least two options")
            option_ids: set[str] = set()
            for option_index, option in enumerate(options):
                option_label = f"{label}.options[{option_index}]"
                if not isinstance(option, dict):
                    raise QuestionWriteError(f"{option_label} must be an object")
                option_id = _required_text(option.get("id"), f"{option_label}.id")
                _required_text(option.get("text"), f"{option_label}.text")
                if option_id in option_ids:
                    raise QuestionWriteError(
                        f"{label}.options has duplicate id {option_id!r}")
                option_ids.add(option_id)
            if not any(option["text"] in {"I’m not sure", "I'm not sure"}
                       for option in options):
                raise QuestionWriteError(
                    f"{label}.options must include an “I’m not sure” choice")
            if question.get("correct_option") not in option_ids:
                raise QuestionWriteError(
                    f"{label}.correct_option must match an option id")
        elif kind == "closed":
            if any(field in question for field in ("options", "correct_option", "rubric")):
                raise QuestionWriteError(f"{label} has fields incompatible with closed")
            aliases = question.get("answer_aliases")
            if (not isinstance(aliases, list) or not aliases
                    or any(not isinstance(alias, str) or not alias.strip()
                           for alias in aliases)):
                raise QuestionWriteError(
                    f"{label}.answer_aliases must be a non-empty array of strings")
        else:
            if any(field in question for field in (
                    "options", "correct_option", "answer_aliases")):
                raise QuestionWriteError(
                    f"{label} has fields incompatible with {kind}")
            _required_text(question.get("rubric"), f"{label}.rubric")
    return value


def _question_path(task: str, source: object = "task") -> Path:
    if not isinstance(task, str) or not _util.TASK_NAME.fullmatch(task):
        raise QuestionWriteError("task name is invalid")
    if source not in ("task", "draft"):
        raise QuestionWriteError("question source must be 'task' or 'draft'")
    root = generate.TASKS_DIR.resolve()
    if source == "draft":
        root = root.parent / "task-drafts"
    target = (root / task / "quiz" / "questions.json").resolve()
    if (not target.is_relative_to(root)
            or target.parent.parent.parent != root
            or not (target.parent.parent / "task.toml").is_file()
            or not target.is_file()):
        raise QuestionWriteError("task questions file does not exist", 404)
    return target


def _save_questions(payload) -> dict:
    if not isinstance(payload, dict):
        raise QuestionWriteError("request body must be an object")
    target = _question_path(payload.get("task"), payload.get("source", "task"))
    expected_hash = payload.get("base_hash")
    if (not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise QuestionWriteError("base_hash must be a lowercase SHA-256 hex digest")
    current_bytes = target.read_bytes()
    current_hash = hashlib.sha256(current_bytes).hexdigest()
    if not hmac.compare_digest(expected_hash, current_hash):
        raise QuestionWriteError(
            "questions changed on disk; reload before saving", 409,
            current_hash=current_hash)
    questions = _validate_questions(payload.get("questions"))
    try:
        current = json.loads(current_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuestionWriteError("current questions file is not valid JSON", 409,
                                 current_hash=current_hash) from exc
    if not isinstance(current, dict):
        raise QuestionWriteError("current questions file must contain an object", 409,
                                 current_hash=current_hash)
    # Replace only the owned list. Notes and future top-level metadata remain
    # byte-for-byte equivalent as JSON values in the rewritten object.
    current["questions"] = questions
    encoded = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, raw_temp = tempfile.mkstemp(prefix="questions-", suffix=".json", dir=target.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, target.stat().st_mode & 0o777)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    new_hash = hashlib.sha256(encoded).hexdigest()
    return {"ok": True, "hash": new_hash, "questions": questions}


class ViewerHandler(BaseHTTPRequestHandler):
    def _question_editing_enabled(self) -> bool:
        bound = self.server.server_address[0]
        peer = self.client_address[0]
        return (
            bound in {"127.0.0.1", "::1", "localhost"}
            and peer in {"127.0.0.1", "::1"}
            and isinstance(getattr(self.server, "question_edit_token", None), str)
        )

    def _send_body(self, status: int, ctype: str, body: bytes, *,
                   cache: str = "no-store", extra: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path, ctype: str, *, extra: tuple = ()) -> None:
        """One immutable capture artifact, revalidated instead of re-sent."""
        modified = formatdate(target.stat().st_mtime, usegmt=True)
        if self.headers.get("If-Modified-Since") == modified:
            self.send_response(304)
            self.send_header("Last-Modified", modified)
            self.end_headers()
            return
        self._send_body(200, ctype, target.read_bytes(), cache="no-cache",
                        extra=(("Last-Modified", modified), *extra))

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_body(status, "application/json; charset=utf-8", body)

    def _local_write_allowed(self) -> bool:
        if not self._question_editing_enabled():
            self._json_response(403, {"error": "writes are loopback serve-mode only"})
            return False
        expected = getattr(self.server, "question_edit_token", "")
        presented = self.headers.get("X-ShowAndTell-Edit-Token", "")
        if not presented or not hmac.compare_digest(presented, expected):
            self._json_response(403, {"error": "invalid edit token"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            server_port = self.server.server_address[1]
            if (parsed.scheme != "http"
                    or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                    or parsed.port != server_port):
                self._json_response(403, {"error": "cross-origin writes are refused"})
                return False
        return True

    def _read_json_body(self, maximum: int) -> object | None:
        if self.headers.get_content_type() != "application/json":
            self._json_response(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self._json_response(411, {"error": "Content-Length is required"})
            return None
        if length > maximum:
            self._json_response(413, {"error": "request payload is too large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "request body must be valid JSON"})
            return None

    def _store_call(self, call, *, maximum: int = 1024, status: int = 200,
                    require_object: bool = False, decorate=None) -> None:
        """Guard, read the JSON body, run one store call, answer JSON.

        ``call`` receives the parsed body; a raised ``ViewerError`` becomes
        its HTTP status with the message plus any structured details.
        """
        if not self._local_write_allowed():
            return
        payload = self._read_json_body(maximum)
        if payload is None:
            return
        if require_object and not isinstance(payload, dict):
            self._json_response(400, {"error": "request body must be an object"})
            return
        try:
            result = call(payload)
        except _util.ViewerError as exc:
            self._json_response(exc.status, {"error": str(exc), **exc.details})
            return
        if decorate is not None:
            decorate(result)
        self._json_response(status, result)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        asset_job = re.fullmatch(
            r"/api/application-assets/([0-9a-f]{32})/status", path)
        if asset_job:
            if not self._local_write_allowed():
                return
            try:
                self._json_response(
                    200, self.server.application_asset_store.status(asset_job.group(1)))
            except application_assets.AssetError as exc:
                self._json_response(exc.status, {"error": str(exc), **exc.details})
            return
        if path == "/api/settings":
            if not self._local_write_allowed():
                return
            self._json_response(200, self.server.settings_store.public())
            return
        if path == "/api/executions":
            if not self._local_write_allowed():
                return
            try:
                running = self.server.execution_registry.list_running()
            except Exception as exc:
                self._json_response(502, {
                    "error": f"fixture host executions are unavailable: {exc}"
                })
                return
            self._json_response(200, {"executions": running})
            return
        if path.startswith("/runs/"):
            self._serve_run_artifact(path)
            return
        if path.startswith("/tasks/"):
            self._serve_task_asset(path, generate.TASKS_DIR, "/tasks/")
            return
        if path.startswith("/dataset-tasks/"):
            dataset_root = hub.cached_dataset_root()
            if dataset_root is None:
                self.send_error(404, "no cached dataset")
                return
            self._serve_task_asset(path, dataset_root, "/dataset-tasks/")
            return
        if path.startswith("/task-drafts/"):
            self._serve_task_asset(
                path, generate.TASKS_DIR.resolve().parent / "task-drafts",
                "/task-drafts/")
            return
        seed_page = re.fullmatch(r"/captures/([0-9a-f]{32})/seed", path)
        if seed_page:
            self._serve_seed_editor(seed_page.group(1))
            return
        seed_api = re.fullmatch(r"/api/captures/([0-9a-f]{32})/seed", path)
        if seed_api:
            self._capture_seed_forms(seed_api.group(1))
            return
        if path not in ("/", "/index.html"):
            # ASCII only: this string lands in the HTTP status line (latin-1)
            self.send_error(404, "the viewer is a single page - request /")
            return
        enabled = self._question_editing_enabled()
        # Captured tasks and all write controls are local serve-mode features.
        # Keep the authenticated/public viewer on the original read-only build
        # path so local captures can never be exposed there.
        data, warnings = (
            generate.build(include_drafts=True) if enabled else generate.build()
        )
        token = getattr(self.server, "question_edit_token", None)

        def feature(endpoint: str, **extra) -> dict:
            return {
                "enabled": enabled,
                "endpoint": endpoint if enabled else None,
                "token": token if enabled else None,
                **extra,
            }

        data["questionEditing"] = feature("/api/questions")
        data["taskCapture"] = feature(
            "/api/captures",
            mode="managed",
            slugPattern=capture.SLUG_PATTERN,
            assetsEndpoint="/api/application-assets",
            applications=[
                {key: value for key, value in surface.items() if key != "credentials"}
                for surface in capture.CAPTURE_SURFACES
            ],
        )
        data["taskRuns"] = feature("/api/task-runs", adapters=[
            {"id": adapter, "label": label}
            for adapter, label in task_runs.ADAPTERS.items()
        ])
        data["draftPromotion"] = feature("/api/drafts/promote")
        data["executions"] = feature("/api/executions")
        data["settings"] = feature("/api/settings")
        data["regrades"] = feature("/api/regrades")
        data["resultDeletion"] = feature("/api/results/delete")
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        self._send_body(200, "text/html; charset=utf-8",
                        generate.assemble(data).encode("utf-8"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/application-assets/preflight":
            self._store_call(
                lambda payload: self.server.application_asset_store.preflight(
                    payload.get("applications")),
                maximum=16 * 1024, require_object=True)
            return
        if path == "/api/application-assets/install":
            self._store_call(
                lambda payload: self.server.application_asset_store.start(
                    payload.get("applications")),
                maximum=16 * 1024, require_object=True, status=202,
                decorate=self._application_asset_endpoint)
            return
        asset_cancel = re.fullmatch(
            r"/api/application-assets/([0-9a-f]{32})/cancel", path)
        if asset_cancel:
            self._store_call(
                lambda _payload: self.server.application_asset_store.cancel(
                    asset_cancel.group(1)),
                decorate=self._application_asset_endpoint)
            return
        if path == "/api/settings":
            self._update_settings()
            return
        if path == "/api/settings/test-host":
            self._store_call(
                lambda _payload: self.server.settings_store.test_host())
            return
        if path == "/api/settings/test-judge":
            self._store_call(
                lambda _payload: self.server.settings_store.test_judge())
            return
        if path == "/api/regrades":
            self._start_regrade()
            return
        if path == "/api/results/delete":
            self._store_call(
                lambda payload: result_deletions.delete_result(
                    generate.CACHE_DIR, generate.TASKS_DIR, payload),
                maximum=4096, require_object=True)
            return
        regrade_match = re.fullmatch(r"/api/regrades/([0-9a-f]{32})/status", path)
        if regrade_match:
            self._regrade_status(regrade_match.group(1))
            return
        if path == "/api/task-runs":
            self._start_task_run()
            return
        run_match = re.fullmatch(r"/api/task-runs/([0-9a-f]{32})/status", path)
        if run_match:
            self._task_run_status(run_match.group(1))
            return
        run_cancel = re.fullmatch(r"/api/task-runs/([0-9a-f]{32})/cancel", path)
        if run_cancel:
            self._task_run_cancel(run_cancel.group(1))
            return
        run_audio = re.fullmatch(r"/api/task-runs/([0-9a-f]{32})/audio", path)
        if run_audio:
            self._task_run_audio(run_audio.group(1))
            return
        gate_match = re.fullmatch(r"/api/task-runs/([0-9a-f]{32})/gate", path)
        if gate_match:
            self._task_run_gate(gate_match.group(1))
            return
        if path == "/api/drafts/promote":
            self._promote_draft()
            return
        execution_match = re.fullmatch(
            r"/api/executions/([0-9a-f]{16})/force-release", path)
        if execution_match:
            self._force_release_execution(execution_match.group(1))
            return
        if path == "/api/captures":
            self._start_capture()
            return
        pending_capture = re.fullmatch(
            r"/api/captures/pending/([0-9a-f]{32})/cancel", path)
        if pending_capture:
            self._cancel_pending_capture(pending_capture.group(1))
            return
        seed_match = re.fullmatch(r"/api/captures/([0-9a-f]{32})/seed", path)
        if seed_match:
            self._apply_capture_seed(seed_match.group(1))
            return
        capture_match = re.fullmatch(
            r"/api/captures/([0-9a-f]{32})/(record|chunks|finish|cancel)", path)
        if capture_match:
            session_id, action = capture_match.groups()
            if action == "record":
                self._begin_capture_recording(session_id)
            elif action == "chunks":
                self._append_capture_chunk(session_id)
            elif action == "cancel":
                self._cancel_capture(session_id)
            else:
                self._finish_capture(session_id)
            return
        if path != "/api/questions":
            self._json_response(404, {"error": "no such endpoint"})
            return
        self._store_call(_save_questions, maximum=_MAX_QUESTION_BODY)

    def _update_settings(self) -> None:
        def update(payload):
            if self.server.settings_store.would_change_host(payload):
                try:
                    running = self.server.execution_registry.list_running()
                except Exception:
                    running = []
                if running:
                    raise settings.SettingsError(
                        "fixture host cannot be changed while executions are active", 409)
            return self.server.settings_store.update(payload)

        self._store_call(update, maximum=16 * 1024, require_object=True)

    def _promote_draft(self) -> None:
        self._store_call(
            lambda payload: self.server.task_run_store.promote_draft(
                self.server.capture_store, payload.get("slug")),
            maximum=capture.MAX_JSON_BYTES, require_object=True)

    def _start_task_run(self) -> None:
        self._store_call(
            lambda payload: self.server.task_run_store.start(
                payload.get("task"), payload.get("adapter"),
                replace=payload.get("replace", False),
                hear_narration=payload.get("hear_narration", False),
                source=payload.get("source", "task")),
            maximum=task_runs.MAX_JSON_BYTES, require_object=True, status=202,
            decorate=self._task_run_endpoint)

    def _start_regrade(self) -> None:
        self._store_call(
            lambda payload: self.server.regrade_store.start(
                payload.get("task"), payload.get("run")),
            maximum=regrades.MAX_JSON_BYTES, require_object=True, status=202,
            decorate=self._regrade_endpoint)

    def _regrade_status(self, regrade_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.regrade_store.status(regrade_id),
            decorate=self._regrade_endpoint)

    @staticmethod
    def _regrade_endpoint(result: dict) -> None:
        result["status_endpoint"] = f"/api/regrades/{result['id']}/status"

    def _task_run_gate(self, run_id: str) -> None:
        self._store_call(
            lambda payload: self.server.task_run_store.gate_action(
                run_id, (payload or {}).get("action")),
            decorate=self._task_run_endpoint)

    def _task_run_status(self, run_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.task_run_store.status(run_id),
            decorate=self._task_run_endpoint)

    def _task_run_cancel(self, run_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.task_run_store.cancel(run_id),
            decorate=self._task_run_endpoint)

    def _task_run_audio(self, run_id: str) -> None:
        def update(payload):
            enabled = payload.get("hear_narration")
            if not isinstance(enabled, bool):
                raise task_runs.TaskRunError(
                    "hear_narration must be true or false")
            return self.server.task_run_store.set_hear_narration(
                run_id, enabled)

        self._store_call(
            update,
            maximum=task_runs.MAX_JSON_BYTES, require_object=True,
            decorate=self._task_run_endpoint)

    @staticmethod
    def _task_run_endpoint(result: dict) -> None:
        result["status_endpoint"] = f"/api/task-runs/{result['id']}/status"
        result["cancel_endpoint"] = f"/api/task-runs/{result['id']}/cancel"
        result["audio_endpoint"] = f"/api/task-runs/{result['id']}/audio"

    def _start_capture(self) -> None:
        def decorate(result: dict) -> None:
            base = f"/api/captures/{result['id']}"
            result["chunk_endpoint"] = f"{base}/chunks"
            result["record_endpoint"] = f"{base}/record"
            result["finish_endpoint"] = f"{base}/finish"
            result["cancel_endpoint"] = f"{base}/cancel"

        self._store_call(self.server.capture_store.start,
                         maximum=capture.MAX_JSON_BYTES, status=201,
                         decorate=decorate)

    def _cancel_pending_capture(self, launch_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.capture_store.cancel_pending(launch_id))

    @staticmethod
    def _application_asset_endpoint(result: dict) -> None:
        job_id = result.get("id")
        if job_id:
            base = f"/api/application-assets/{job_id}"
            result["status_endpoint"] = f"{base}/status"
            result["cancel_endpoint"] = f"{base}/cancel"

    # -- seed editor -------------------------------------------------------
    def _serve_seed_editor(self, session_id: str) -> None:
        """The authoring page managed Chrome opens beside the showAndTell.applications."""
        if self.server.capture_store is None:
            self.send_error(404, "capture is not enabled")
            return
        token = getattr(self.server, "question_edit_token", "") or ""
        body = (capture.SEED_EDITOR_HTML
                .replace("__SESSION_ID__", session_id)
                .replace("__EDIT_TOKEN__", token)).encode("utf-8")
        self._send_body(200, "text/html; charset=utf-8", body)

    def _capture_seed_forms(self, session_id: str) -> None:
        if self.server.capture_store is None:
            self._json_response(404, {"error": "capture is not enabled"})
            return
        try:
            self._json_response(
                200, self.server.capture_store.seed_forms(session_id))
        except capture.CaptureError as exc:
            self._json_response(exc.status, {"error": str(exc)})

    def _apply_capture_seed(self, session_id: str) -> None:
        self._store_call(
            lambda payload: self.server.capture_store.apply_seed(
                session_id, payload),
            maximum=_MAX_SEED_BODY)

    def _begin_capture_recording(self, session_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.capture_store.begin_recording(session_id))

    def _cancel_capture(self, session_id: str) -> None:
        self._store_call(
            lambda _payload: self.server.capture_store.cancel(session_id))

    def _append_capture_chunk(self, session_id: str) -> None:
        if not self._local_write_allowed():
            return
        if self.headers.get_content_type() not in {"video/webm", "application/octet-stream"}:
            self._json_response(415, {"error": "recording chunks must be video/webm"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            sequence = int(self.headers.get("X-ShowAndTell-Chunk", ""))
        except ValueError:
            self._json_response(400, {"error": "valid chunk length and sequence are required"})
            return
        try:
            result = self.server.capture_store.append_chunk(
                session_id, sequence, self.rfile, length)
        except capture.CaptureError as exc:
            self._json_response(exc.status, {"error": str(exc)})
            return
        self._json_response(200, result)

    def _finish_capture(self, session_id: str) -> None:
        self._store_call(
            lambda payload: self.server.capture_store.finish(session_id, payload),
            maximum=capture.MAX_JSON_BYTES)

    def _force_release_execution(self, execution_id: str) -> None:
        def call(_payload):
            try:
                return self.server.execution_registry.force_release(execution_id)
            except _util.ViewerError:
                raise
            except Exception as exc:
                raise _util.ViewerError(
                    f"fixture lease could not be released: {exc}", 502) from exc

        self._store_call(call)

    def _serve_task_asset(self, path: str, root: Path, prefix: str) -> None:
        """Serve confined task media and known captured-trial artifacts."""
        from urllib.parse import unquote
        tasks_dir = root.resolve()
        # Confinement checks the request path, not the resolved file: hub
        # cache bundles are symlink farms into the content-addressed blobs/
        # store, so resolving the target would escape the tasks root.
        try:
            relative = Path(unquote(path[len(prefix):]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe task-asset path")
            target = tasks_dir / relative
        except (ValueError, OSError):
            self.send_error(404, "no such evidence asset")
            return
        if not target.is_file():
            self.send_error(404, "no such evidence asset")
            return
        if (len(relative.parts) == 3 and relative.parts[1] == "demo"
                and target.name in generate.DEMO_RECORDING_NAMES):
            self._serve_media(target, _VIDEO_TYPES[target.suffix.lower()])
            return
        if (prefix == "/task-drafts/" and len(relative.parts) == 4
                and relative.parts[1] == "trials"
                and target.name in generate.SCREEN_RECORDING_NAMES):
            self._serve_media(target, _VIDEO_TYPES[target.suffix.lower()])
            return
        if (prefix == "/task-drafts/" and len(relative.parts) == 4
                and relative.parts[1] == "trials"
                and target.name in generate.TRIAL_ARTIFACTS):
            self._send_file(target, generate.TRIAL_ARTIFACTS[target.name][1],
                            extra=(("X-Content-Type-Options", "nosniff"),))
            return
        if target.suffix.lower() != ".png":
            self.send_error(404, "no such evidence asset")
            return
        self._send_file(target, "image/png")

    def _serve_run_artifact(self, path: str) -> None:
        from urllib.parse import unquote
        runs_dir = generate.CACHE_DIR.parent.resolve()
        try:
            relative = Path(unquote(path[len("/runs/"):]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe run-artifact path")
            target = (runs_dir / relative).resolve()
        except (ValueError, OSError):
            # embedded null bytes (and other exotic paths) raise before the
            # guards below get a chance to run — treat them the same way
            self.send_error(404, "no such recording")
            return
        if not target.is_relative_to(runs_dir) or not target.is_file():
            self.send_error(404, "no such recording")
            return
        ctype = _VIDEO_TYPES.get(target.suffix)
        if ctype is not None and target.name in generate.SCREEN_RECORDING_NAMES:
            self._serve_media(target, ctype)
            return
        artifact = generate.TRIAL_ARTIFACTS.get(target.name)
        if artifact is not None:
            self._send_file(
                target, artifact[1],
                extra=(("X-Content-Type-Options", "nosniff"),),
            )
            return
        self.send_error(404, "no such run artifact")

    def _serve_media(self, target: Path, ctype: str) -> None:
        size = target.stat().st_size
        rng = _parse_range(self.headers.get("Range"), size)
        if rng == "bad":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        start, end = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with target.open("rb") as f:       # stream: never the whole file in memory
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(_CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                               # the player scrubbed away mid-send

    def log_message(self, format, *args):  # keep the terminal quiet
        pass


class ViewerServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        supervisor = getattr(self, "execution_supervisor", None)
        if supervisor is not None:
            supervisor.close()
        store = getattr(self, "capture_store", None)
        if store is not None:
            store.close()
        settings_store = getattr(self, "settings_store", None)
        if settings_store is not None:
            settings_store.close()
        regrade_store = getattr(self, "regrade_store", None)
        if regrade_store is not None:
            regrade_store.close()
        asset_store = getattr(self, "application_asset_store", None)
        if asset_store is not None:
            asset_store.close()
        super().server_close()


def make_server(port: int, *, settings_path: Path | str | None = None,
                settings_environment=None) -> ThreadingHTTPServer:
    # 127.0.0.1: a local dev tool should not listen on the LAN
    server = ViewerServer(("127.0.0.1", port), ViewerHandler)
    server.question_edit_token = secrets.token_urlsafe(32)
    server.settings_store = settings.SettingsStore(
        settings_path, environment=settings_environment)
    server.execution_supervisor = executions.ExecutionSupervisor()
    server.execution_registry = executions.HostExecutionRegistry()
    server.application_asset_store = application_assets.ApplicationAssetStore()
    # The bound port, not the requested one: port=0 picks a free port.
    server.capture_store = capture.CaptureStore(
        generate.TASKS_DIR,
        seed_editor_base=f"http://127.0.0.1:{server.server_address[1]}",
        supervisor=server.execution_supervisor)
    server.task_run_store = task_runs.TaskRunStore(
        generate.TASKS_DIR, supervisor=server.execution_supervisor)
    server.regrade_store = regrades.RegradeStore(generate.TASKS_DIR.parent)
    return server


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = make_server(port)
    url = f"http://localhost:{server.server_address[1]}"
    print(f"serving the task inspector on {url} "
          f"(rebuilds on every refresh; Ctrl-C to stop)")
    webbrowser.open(url)  # no-op on headless machines
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
