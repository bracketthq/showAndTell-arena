"""Loopback viewer support for recording a human workflow as a task draft.

Managed mode owns Chrome, records Playwright/CDP actions and accessibility
evidence, and authors a replay driver. Browser mode remains as a WebM fallback.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import _util, application_assets, executions, fixture_host


# The authoring page managed Chrome opens beside the showAndTell.applications. Read once:
# the viewer is long-lived and the file never changes under it.
SEED_EDITOR_HTML = (Path(__file__).resolve().parent / "seed_editor.html").read_text(
    encoding="utf-8")

MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_RECORDING_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
# Also shipped to the capture form, so client and server validate slugs alike.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"
_SLUG = re.compile(SLUG_PATTERN)


# A surface is what the user opens. Every application describes its own
# surface in app.toml.
def _application_surfaces() -> tuple[dict, ...]:
    """One selectable surface per application folder, straight from its manifest."""
    from showAndTell.applications.registry import default_registry

    registry = default_registry()
    surfaces = []
    for name in registry.names():
        manifest = registry.manifest(name)
        surfaces.append({
            "id": manifest.surface_id,
            "application": name,
            "label": manifest.label,
            "familiar_label": manifest.familiar_label,
            "space_required_bytes": application_assets.fresh_install_required_bytes(name),
            "space_required_estimated":
                application_assets.fresh_install_space_is_estimate(name),
            "url": (f"http://127.0.0.1:{manifest.app_port.host}"
                    f"{manifest.surface_entry}"),
            "credentials": dict(manifest.credentials),
        })
    return tuple(sorted(
        surfaces,
        key=lambda surface: (surface["space_required_bytes"],
                             surface["familiar_label"].casefold()),
    ))


CAPTURE_SURFACES = _application_surfaces()
_SURFACES_BY_ID = {surface["id"]: surface for surface in CAPTURE_SURFACES}


class CaptureWorkspace:
    """Clean application runtimes held across setup and recording.

    Capture authoring is deliberately two-phase: applications start in a reset
    state, the operator edits that state, and ``capture_state`` freezes it
    immediately before the demonstration recorder starts.

    Selected applications are held by one ``AppSession``.
    """

    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
        self.session = None
        self.execution = None
        self._host = None
        self._cancelled = threading.Event()
        self._onlyoffice_documents: dict[str, dict] = {}
        self._live_surfaces: dict[str, dict] = {}

    # -- start -------------------------------------------------------------
    def _applications(self) -> list[str]:
        """Unique applications selected for this capture, in UI order."""
        applications: list[str] = []
        for surface in self.metadata["surfaces"]:
            name = surface["application"]
            if name not in applications:
                applications.append(name)
        return applications

    def _start_session(self, applications: list[str]) -> None:
        from showAndTell.applications.host.client import FixtureHostBusy

        try:
            execution = fixture_host.acquire_execution(
                applications,
                f"capture-{self.metadata['slug']}",
                clear_stale=bool(self.metadata.get("force")),
                on_session=self._claim_starting_session,
            )
            if self._cancelled.is_set():
                execution.close()
                raise CaptureError("managed capture was cancelled", 409)
            self.execution = execution
            self.execution.annotate(
                task=self.metadata["slug"],
                kind=str(self.metadata.get("_execution_kind") or "capture"),
                product=str(self.metadata.get("_execution_product")
                            or "Viewer capture"),
            )
            self.session = self.execution.session
        except FixtureHostBusy as exc:
            raise CaptureError(str(exc), 409, details={
                "code": "host_busy", "held_by": exc.held_by, "since": exc.since,
            }) from exc
        session = self.session
        # Reset before prepare: the operator must not inherit the previous
        # capture's mail or workbooks.
        session.reset()
        session.prepare()
        if "onlyoffice" in session.applications:
            state = session.state("onlyoffice")
            block = state.export(session.context("onlyoffice"))
            self._onlyoffice_documents = {
                str(row.get("document_id")): dict(row)
                for row in block.get("documents", [])
                if isinstance(row, dict) and row.get("document_id")
            }
        for surface in self.metadata["surfaces"]:
            name = surface["application"]
            if name not in session.applications:
                continue
            surface["url"] = self._surface_url_for(session, name)
            surface["credentials"] = dict(session.credentials[name])

    def _claim_starting_session(self, session) -> None:
        """Expose the lease early so Cancel can release a booting application."""
        self.session = session
        if self._cancelled.is_set():
            session.close()
            raise CaptureError("managed capture was cancelled", 409)

    @staticmethod
    def _surface_url_for(session, name: str) -> str:
        """Where the browser opens this application.

        Most applications answer with their own URL. ONLYOFFICE is the
        exception: its editable surface is the connector's editor page for a
        specific document, which only the data plane knows.
        """
        state = session.state(name) if session.registry.has_state(name) else None
        editor_url = getattr(state, "editor_url", None)
        if callable(editor_url):
            try:
                return editor_url(session.context(name))
            except RuntimeError:
                pass
        return session.surface_url(name)

    def start(self) -> list[dict]:
        applications = self._applications()
        try:
            self._start_session(applications)
            return self.metadata["surfaces"]
        except BaseException:
            self.close()
            raise

    # -- freeze ------------------------------------------------------------
    @staticmethod
    def _has_content(value) -> bool:
        """Whether an exported block holds actual state rather than a shell.

        An application that cannot describe operator-created state still
        returns its block shape — ERPNext returns ``{"profile": None,
        "items": []}``. Treating that as a successful export makes the draft
        claim a seed it does not have, and hides the fact that replay depends
        entirely on the captured setup actions.
        """
        if isinstance(value, dict):
            return any(CaptureWorkspace._has_content(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(CaptureWorkspace._has_content(item) for item in value)
        if value is None or value == "":
            return False
        return True

    def export(self) -> tuple[dict, bool]:
        """Return merged application state and whether it is a real export."""
        merged: dict = {}
        if self.session is not None:
            merged.update(self.session.export())
        return merged, self._has_content(merged)

    def capture_state(self, directory: Path) -> dict:
        """Ask every application for the state the operator built by hand.

        A workbook exists only inside the editing session until it is told to
        save; mail exists only inside Dovecot; an ERPNext site is a database
        that no declarative export can describe. Each application answers in
        whichever of those forms it actually has.
        """
        if self.session is None:
            return {}
        return self.session.capture(Path(directory))

    @staticmethod
    def _origin(value: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(value)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port

    def validate_live_surfaces(self, surfaces: list[dict]) -> None:
        """Prove that browser and data plane belong to the same app lease.

        The connector URL redirects to Document Server, so both the leased
        connector origin and the leased application origin are valid.  Any
        other replica is unsafe to export: its visible workbook is not the one
        this workspace owns.
        """
        self._live_surfaces = {
            str(row.get("application")): dict(row)
            for row in surfaces if isinstance(row, dict) and row.get("application")
        }
        if self.session is None or "onlyoffice" not in self.session.applications:
            return
        live = self._live_surfaces.get("onlyoffice")
        if not live or not isinstance(live.get("url"), str):
            raise RuntimeError("ONLYOFFICE browser page could not be identified before export")
        state = self.session.state("onlyoffice")
        ctx = self.session.context("onlyoffice")
        app_origin = self._origin(ctx.url)
        connector_origin = self._origin(state.connector_url(ctx))
        live_url = str(live["url"])
        live_origin = self._origin(live_url)
        if live_origin not in {app_origin, connector_origin}:
            raise RuntimeError(
                "ONLYOFFICE browser is on a different fixture replica than "
                "the capture workspace; the setup is still open and was not exported"
            )
        parsed = urlsplit(live_url)
        document_ids = set(self._onlyoffice_documents)
        if live_origin == app_origin:
            live_document = (parse_qs(parsed.query).get("doc") or [""])[0]
        else:
            live_document = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if document_ids and live_document not in document_ids:
            raise RuntimeError(
                f"ONLYOFFICE browser shows document {live_document!r}, but the "
                "capture workspace owns a different workbook"
            )

    # -- replay ------------------------------------------------------------
    @staticmethod
    def _draft_seed(state_dir: Path | None) -> dict:
        """The application-keyed seed a draft carries on disk."""
        if state_dir is None:
            return {}
        try:
            seed = json.loads((Path(state_dir) / "seed.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return seed if isinstance(seed, dict) else {}

    def restore(self, *, use_export: bool, state_dir: Path | None = None,
                snapshot_only: bool = False) -> None:
        """Return applications to a captured task's starting state for a run.

        A captured snapshot is authoritative wherever one exists: it is the
        exact state the demonstration began from, which a declarative export
        cannot describe. ``snapshot_only`` skips the fallbacks for a workspace
        that has just started clean, so it is not reset twice.
        """
        if self.session is not None:
            restored = (self.session.restore_snapshots(state_dir)
                        if state_dir is not None else [])
            # Per application, not globally. One application carrying a
            # snapshot used to suppress the reset for every other one, so a
            # task with an ERPNext tar replayed with whatever the previous
            # session had left in its mailbox and its editor.
            rebuilt = [name for name in self.session.applications
                       if name not in restored]
            if not snapshot_only and rebuilt:
                self.session.reset(only=rebuilt)
                self.session.prepare(only=rebuilt)
            # Assets are restored regardless of the snapshot: the application
            # that can produce a snapshot is not the one holding the sheet or
            # the mailbox, and a rebuild would otherwise leave the blank
            # workbook and empty inbox the capture opened with.
            seed = self._draft_seed(state_dir)
            blocks = {name: block for name, block in seed.items()
                      if name in self.session.applications
                      and isinstance(block, dict)}
            if blocks:
                # The draft is where its own .eml and .xlsx assets live.
                self.session.use_demo_root(state_dir)
                self.session.seed(blocks)

    # -- teardown ----------------------------------------------------------
    def close(self) -> None:
        self._cancelled.set()
        if self.execution is not None:
            self.execution.close()
            self.execution = None
            self.session = None
        elif self.session is not None:
            # Lightweight/custom workspaces used by integrations may provide
            # only an application session. The production path always owns an
            # ExecutionLease, but this remains an idempotent compatibility
            # boundary for those callers.
            self.session.close()
            self.session = None

class CaptureError(_util.ViewerError):
    pass


def _text(value, label: str, *, required: bool = True, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise CaptureError(f"{label} must be a string")
    value = value.strip()
    if required and not value:
        raise CaptureError(f"{label} is required")
    if len(value) > limit:
        raise CaptureError(f"{label} is too long")
    return value


def validate_start(payload) -> dict:
    if not isinstance(payload, dict):
        raise CaptureError("request body must be an object")
    slug = _text(payload.get("slug"), "slug", limit=64).lower()
    if not _SLUG.fullmatch(slug):
        raise CaptureError("slug must be 3-64 lowercase letters, numbers, or hyphens")
    title = _text(payload.get("title", slug), "title", limit=160)
    summary = _text(payload.get("summary", ""), "summary", required=False)
    ids = payload.get("applications")
    if not isinstance(ids, list) or not ids or len(ids) > 8:
        raise CaptureError("applications must select between 1 and 8 apps")
    if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
        raise CaptureError("applications must contain unique app ids")
    try:
        surfaces = [dict(_SURFACES_BY_ID[value]) for value in ids]
    except KeyError as exc:
        raise CaptureError(f"unknown application {exc.args[0]!r}") from exc
    applications = list(dict.fromkeys(surface["application"] for surface in surfaces))
    mode = payload.get("mode", "browser")
    if mode not in {"browser", "managed"}:
        raise CaptureError("mode must be browser or managed")
    launch_id = payload.get("launch_id", "")
    if launch_id and (not isinstance(launch_id, str)
                      or not re.fullmatch(r"[0-9a-f]{32}", launch_id)):
        raise CaptureError("launch_id must be 32 lowercase hexadecimal characters")
    return {
        "force": bool(payload.get("force", False)),
        "slug": slug,
        "title": title,
        "summary": summary,
        "surfaces": surfaces,
        "applications": applications,
        "primary_application": applications[0],
        "mode": mode,
        "launch_id": launch_id,
    }


def validate_finish(payload) -> dict:
    if not isinstance(payload, dict):
        raise CaptureError("request body must be an object")
    duration = payload.get("duration_ms")
    if not isinstance(duration, (int, float)) or duration < 0 or duration > 24 * 60 * 60 * 1000:
        raise CaptureError("duration_ms must be between 0 and 24 hours")
    raw_steps = payload.get("steps", [])
    if not isinstance(raw_steps, list) or len(raw_steps) > 10000:
        raise CaptureError("steps must be an array of at most 10000 items")
    steps = []
    for index, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            raise CaptureError(f"steps[{index - 1}] must be an object")
        at_ms = raw.get("at_ms")
        if not isinstance(at_ms, (int, float)) or at_ms < 0 or at_ms > duration + 60_000:
            raise CaptureError(f"steps[{index - 1}].at_ms is invalid")
        text = _text(raw.get("text"), f"steps[{index - 1}].text", limit=2000)
        steps.append({"id": f"step-{index:03d}", "at_ms": round(at_ms), "text": text})
    return {
        "duration_ms": round(duration),
        "steps": steps,
        "transcript_supported": bool(payload.get("transcript_supported")),
        "voice_recorded": bool(payload.get("voice_recorded", True)),
    }


class CaptureStore:
    """Thread-safe state for capture sessions owned by one viewer server."""

    def __init__(self, tasks_dir: Path, *, workspace_factory=CaptureWorkspace,
                 seed_editor_base: str | None = None,
                 port_allocator=None,
                 supervisor: executions.ExecutionSupervisor | None = None) -> None:
        # Where managed Chrome should reach this viewer. None means the
        # authoring tab is not offered -- the store still serves the page.
        self.seed_editor_base = seed_editor_base.rstrip("/") if seed_editor_base else None
        self.tasks_root = Path(tasks_dir).resolve()
        self.output_root = self.tasks_root.parent / "task-drafts"
        self.workspace_factory = workspace_factory
        # Retained as an ignored keyword for callers upgrading from the
        # draft-specific runner. Run ports now belong to TaskRunStore.
        del port_allocator
        self.supervisor = supervisor or executions.ExecutionSupervisor()
        self._sessions: dict[str, dict] = {}
        self._cancelled_launches: set[str] = set()
        self._lock = threading.RLock()

    def _draft_path(self, slug: str) -> Path:
        """The confined on-disk draft, or the 404 the caller answers with.

        Promotion needs the authored ``task.toml`` produced by managed mode;
        browser-only captures remain evidence drafts until re-recorded.
        """
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise CaptureError("draft name is invalid")
        draft = (self.output_root / slug).resolve()
        if (not draft.is_relative_to(self.output_root.resolve())
                or not (draft / "testcase.json").is_file()
                or not (draft / "task.toml").is_file()):
            raise CaptureError("captured draft was not found", 404)
        return draft

    def promote(self, slug: str) -> dict:
        """Make a captured draft an ordinary runnable task."""
        with self._lock:
            draft = self._draft_path(slug)
            try:
                self.tasks_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CaptureError("captured task could not be promoted", 409) from exc
            target = (self.tasks_root / slug).resolve()
            if not target.is_relative_to(self.tasks_root):
                raise CaptureError("task name is invalid")
            if target.exists():
                raise CaptureError(f"a task named {slug!r} already exists", 409)
            testcase_path = draft / "testcase.json"
            try:
                original_testcase = testcase_path.read_text(encoding="utf-8")
                testcase = json.loads(original_testcase)
            except (OSError, json.JSONDecodeError) as exc:
                raise CaptureError("captured draft metadata is invalid", 409) from exc
            if testcase.get("status") != "captured-draft":
                raise CaptureError("captured draft metadata has an invalid status", 409)
            testcase["status"] = "captured-task"
            testcase["promoted_at"] = _util.utc_now()
            testcase["authoring_todos"] = [
                item for item in testcase.get("authoring_todos", [])
                if "Promote" not in str(item)
            ]
            task_toml = draft / "task.toml"
            try:
                original_toml = task_toml.read_text(encoding="utf-8")
            except OSError as exc:
                raise CaptureError("captured task status could not be promoted", 409) from exc
            toml, replacements = re.subn(
                r'(?ms)^\[status\]\nstate = "draft"\nreason = "[^"]*"\n',
                '[status]\nstate = "working"\n'
                'reason = "Captured and promoted through the Task Inspector."\n',
                original_toml,
                count=1,
            )
            if replacements != 1:
                raise CaptureError("captured task status could not be promoted", 409)
            try:
                _util.write_json(testcase_path, testcase)
                task_toml.write_text(toml, encoding="utf-8")
                os.replace(draft, target)
            except OSError as exc:
                # Promotion changes two metadata files before the atomic move.
                # Restore both so a transient filesystem failure can be retried.
                try:
                    testcase_path.write_text(original_testcase, encoding="utf-8")
                    task_toml.write_text(original_toml, encoding="utf-8")
                except OSError:
                    pass
                raise CaptureError("captured task could not be promoted", 409) from exc
        return {
            "ok": True, "slug": slug, "promoted": True,
            "path": str(target),
        }

    def start(self, payload) -> dict:
        metadata = validate_start(payload)
        launch_id = metadata["launch_id"]
        with self._lock:
            if launch_id and launch_id in self._cancelled_launches:
                self._cancelled_launches.discard(launch_id)
                raise CaptureError("managed capture launch was cancelled", 409)
        self.output_root.mkdir(parents=True, exist_ok=True)
        target = self.output_root / metadata["slug"]
        if target.exists():
            raise CaptureError(f"a draft named {metadata['slug']!r} already exists", 409)
        session_id = secrets.token_hex(16)
        directory = Path(tempfile.mkdtemp(
            prefix=f".{metadata['slug']}-capture-", dir=self.output_root))
        metadata.update({"id": session_id, "started_at": _util.utc_now()})
        _util.write_json(directory / "session.json", metadata)
        managed = None
        workspace = None
        # Registered before managed Chrome starts, not after: start() opens the
        # authoring tab, whose page calls straight back in. Publishing the
        # session only once Chrome was up raced that first request and the
        # author saw "capture session was not found".
        session = {
            "directory": directory,
            "metadata": metadata,
            "next_chunk": 0,
            "bytes": 0,
            "managed": None,
            "workspace": None,
            "managed_mode": metadata["mode"] == "managed",
            "recording_started": metadata["mode"] != "managed",
            "seed": {},
            "seed_exported": False,
            "cancelled": False,
        }
        with self._lock:
            active_managed = [
                (active_id, item) for active_id, item in self._sessions.items()
                if item.get("managed_mode")
            ] if session["managed_mode"] else []
            if active_managed and not metadata["force"]:
                shutil.rmtree(directory, ignore_errors=True)
                active = active_managed[0][1].get("metadata", {})
                details = {
                    "code": "managed_capture_busy",
                    "held_by": f"capture-{active.get('slug', 'unknown')}",
                }
                try:
                    details["since"] = dt.datetime.fromisoformat(
                        active["started_at"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    pass
                raise CaptureError(
                    "another managed capture is already active", 409,
                    details=details)
            # A confirmed force retry replaces stale viewer state as well as
            # the fixture-host lease released later by AppSession.start().
            for active_id, _item in active_managed:
                self._discard_session(active_id, suppress_errors=True)
            self._sessions[session_id] = session

        if metadata["mode"] == "managed":
            try:
                from showAndTell.capture.runtime import ManagedCapture

                workspace = self.workspace_factory(metadata)
                with self._lock:
                    if session["cancelled"]:
                        raise CaptureError("managed capture was cancelled", 409)
                    session["workspace"] = workspace
                workspace.start()
                # Visible to the authoring page before the tab that reads it
                # can possibly exist.
                with self._lock:
                    if session["cancelled"]:
                        raise CaptureError("managed capture was cancelled", 409)
                    session["workspace"] = workspace
                _util.write_json(directory / "session.json", metadata)
                managed = ManagedCapture(
                    directory, metadata["surfaces"],
                    tools=self._seed_editor_tool(session_id, workspace))
                with self._lock:
                    if session["cancelled"]:
                        raise CaptureError("managed capture was cancelled", 409)
                    session["managed"] = managed
                managed.start()
                self.supervisor.register(session_id, cleanup=workspace.close)
            except Exception as exc:
                with self._lock:
                    self._sessions.pop(session_id, None)
                    self._cancelled_launches.discard(launch_id)
                if workspace is not None:
                    workspace.close()
                shutil.rmtree(directory, ignore_errors=True)
                if isinstance(exc, CaptureError):
                    raise
                raise CaptureError(
                    f"could not start managed Chrome capture: {exc}", 500) from exc

        with self._lock:
            self._cancelled_launches.discard(launch_id)

        return {
            "id": session_id,
            "slug": metadata["slug"],
            "managed": metadata["mode"] == "managed",
            "phase": "seed" if managed else "recording",
            "surfaces": metadata["surfaces"],
            "login_results": (getattr(managed, "login_results", [])
                              if managed is not None else []),
            "voice_recording_available": None if managed else True,
        }

    def cancel_pending(self, launch_id: str) -> dict:
        """Cancel a launch before its ordinary session response is available."""
        if not isinstance(launch_id, str) or not re.fullmatch(
                r"[0-9a-f]{32}", launch_id):
            raise CaptureError("pending capture launch was not found", 404)
        with self._lock:
            active_id = next((
                session_id for session_id, session in self._sessions.items()
                if session.get("metadata", {}).get("launch_id") == launch_id
            ), None)
            if active_id is not None:
                self._discard_session(active_id, suppress_errors=False)
            else:
                # The cancel may beat start() by a few milliseconds. Keep a
                # one-use tombstone so that request cannot register afterward.
                self._cancelled_launches.add(launch_id)
        return {"ok": True, "cancelled": True, "launch_id": launch_id}

    # -- seed editor -------------------------------------------------------
    def _seed_editor_tool(self, session_id: str, workspace) -> list[dict]:
        """The authoring tab, but only when some application needs one.

        A capture whose applications can all be set up in their own UI gets no
        extra tab, so the page never appears saying it has nothing to offer.
        """
        app_session = getattr(workspace, "session", None)
        if app_session is None or self.seed_editor_base is None:
            return []
        try:
            if not app_session.seed_forms():
                return []
        except Exception:  # noqa: BLE001 - never fail a capture over a convenience
            return []
        return [{"label": "Starting data",
                 "url": f"{self.seed_editor_base}/captures/{session_id}/seed"}]

    def seed_forms(self, session_id: str) -> dict:
        """What the authoring page renders. Thin: the session decides."""
        with self._lock:
            session = self._session(session_id)
        app_session = getattr(session.get("workspace"), "session", None)
        if app_session is None:
            return {"applications": []}
        remembered = session.setdefault("seed_rows", {})
        return {"applications": [
            {"name": name, "label": app_session.registry.manifest(name).label,
             "form": form, "rows": remembered.get(name, [])}
            for name, form in app_session.seed_forms().items()
        ]}

    def apply_seed(self, session_id: str, payload) -> dict:
        """Hand authored rows to the session and remember what stuck."""
        if not isinstance(payload, dict):
            raise CaptureError("request body must be an object")
        application = payload.get("application")
        rows = payload.get("rows")
        if not isinstance(application, str) or not isinstance(rows, list):
            raise CaptureError("application and rows are required")
        if len(rows) > 200:
            raise CaptureError("a seed form accepts at most 200 rows")

        with self._lock:
            session = self._session(session_id)
        app_session = getattr(session.get("workspace"), "session", None)
        if app_session is None:
            raise CaptureError("this capture has no managed applications", 409)
        try:
            applied = app_session.apply_form(application, rows)
        except KeyError as exc:
            raise CaptureError(str(exc).strip("'"), 404) from exc
        except Exception as exc:  # noqa: BLE001 - the author fixes this in the form
            raise CaptureError(str(exc)[:400], 400) from exc

        with self._lock:
            session.setdefault("seed_rows", {})[application] = list(rows)
        return {"ok": True, "application": application, "applied": applied}

    def begin_recording(self, session_id: str) -> dict:
        """Freeze setup state, then begin action/screen/microphone capture."""
        with self._lock:
            session = self._session(session_id)
            if session["managed"] is None:
                raise CaptureError("browser captures start recording immediately", 409)
            if session["recording_started"]:
                raise CaptureError("recording has already started", 409)
            try:
                demo = session["directory"] / "demo"
                demo.mkdir(parents=True, exist_ok=True)
                prepare_freeze = getattr(session["managed"], "prepare_freeze", None)
                live_surfaces: list[dict] = []
                if callable(prepare_freeze):
                    live_surfaces = prepare_freeze(timeout=30)
                validate_live = getattr(
                    session["workspace"], "validate_live_surfaces", None)
                if callable(validate_live):
                    validate_live(live_surfaces)
                # Applications hold unsaved work — an editor's buffer, a
                # mailbox, a database — so freeze it first. The export below
                # only describes state a seed planted, never state a person
                # built by hand, which is the whole reason capture_state runs.
                capture_state = getattr(
                    session["workspace"], "capture_state", None)
                captured: dict = {}
                if callable(capture_state):
                    captured = capture_state(demo)
                seed, exported = session["workspace"].export()
                for application, block in captured.items():
                    seed.setdefault(application, {}).update(block)
                # A snapshot is opaque binary state, not a seed the draft can
                # describe. Counting it as an export would make the draft claim
                # a declarative seed it does not have.
                exported = exported or any(
                    set(block) - {"snapshot"} for block in captured.values()
                    if isinstance(block, dict))
                # Applications that answered with a binary snapshot rather than
                # assets name the tar they wrote beside the seed.
                states = sorted(
                    block["snapshot"] for block in captured.values()
                    if isinstance(block, dict) and block.get("snapshot"))
                result = session["managed"].begin_recording(timeout=30)
            except Exception as exc:
                raise CaptureError(f"could not freeze seed and start recording: {exc}", 500) from exc
            session["seed"] = seed
            session["seed_exported"] = exported
            session["state_snapshots"] = states
            session["recording_started"] = True
            session["recording_started_at"] = _util.utc_now()
            return {
                "ok": True,
                "phase": "recording",
                "seed_exported": exported,
                "seed_keys": sorted(seed),
                "state_snapshots": states,
                "voice_recording_available": bool(result.get("voice_recorded")),
            }

    def _discard_session(self, session_id: str, *, suppress_errors: bool) -> None:
        """Release one unfinished session, guaranteeing its registry removal."""
        session = self._session(session_id)
        session["cancelled"] = True
        first_error = None
        if session.get("managed") is not None:
            try:
                session["managed"].stop(timeout=30)
            except Exception as exc:  # best-effort cleanup continues below
                first_error = exc
        elif session.get("workspace") is not None:
            try:
                session["workspace"].close()
            except Exception as exc:
                first_error = exc
        try:
            self.supervisor.complete(session_id)
        except Exception as exc:  # best-effort cleanup continues below
            first_error = first_error or exc
        shutil.rmtree(session["directory"], ignore_errors=True)
        self._sessions.pop(session_id, None)
        if first_error is not None and not suppress_errors:
            raise first_error

    def cancel(self, session_id: str) -> dict:
        """Abort an unfinished setup/capture and release apps plus Chrome."""
        with self._lock:
            self._discard_session(session_id, suppress_errors=False)
        return {"ok": True, "cancelled": True}

    def _session(self, session_id: str) -> dict:
        return _util.lookup_token(self._sessions, session_id, "capture",
                                  "capture session", CaptureError)

    def append_chunk(self, session_id: str, sequence: int, stream, length: int) -> dict:
        if length <= 0 or length > MAX_CHUNK_BYTES:
            raise CaptureError("recording chunk size is invalid", 413)
        with self._lock:
            session = self._session(session_id)
            if sequence != session["next_chunk"]:
                raise CaptureError(
                    f"expected recording chunk {session['next_chunk']}, got {sequence}", 409)
            if session["bytes"] + length > MAX_RECORDING_BYTES:
                raise CaptureError("recording exceeds the 2 GiB limit", 413)
            # Claim the slot now: ordering stays enforced while the
            # network-speed copy below runs without the store-wide lock, so
            # status polls and other sessions are not serialized behind it.
            session["next_chunk"] += 1
            target = session["directory"] / "recording.webm"
        try:
            remaining = length
            with target.open("ab") as handle:
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise CaptureError("recording chunk ended early")
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # Release the claim so the client's retry of this sequence is
            # accepted again.
            with self._lock:
                session["next_chunk"] -= 1
            raise
        with self._lock:
            session["bytes"] += length
            return {"ok": True, "next_chunk": session["next_chunk"]}

    def finish(self, session_id: str, payload) -> dict:
        finished = validate_finish(payload)
        with self._lock:
            session = self._session(session_id)
            if session.get("managed") is not None and not session["recording_started"]:
                raise CaptureError("start recording before finishing the capture", 409)
            directory = session["directory"]
            metadata = session["metadata"]
            managed_result = None
            if session.get("managed") is not None:
                try:
                    managed_result = session["managed"].stop(timeout=60)
                except Exception as exc:
                    raise CaptureError(f"managed Chrome capture could not finish: {exc}", 500) from exc
            recording = directory / "recording.webm"
            if managed_result and managed_result.get("recording"):
                recording = directory / managed_result["recording"]
            if (not managed_result
                    and (not recording.is_file() or recording.stat().st_size == 0)):
                raise CaptureError("no recording data was received", 409)
            target = self.output_root / metadata["slug"]
            if target.exists():
                raise CaptureError(f"a draft named {metadata['slug']!r} already exists", 409)

            completed_at = _util.utc_now()
            exported_applications = sorted(
                name for name, value in session["seed"].items()
                if CaptureWorkspace._has_content(value)
            )
            authored_events = []
            if managed_result is not None:
                from showAndTell.demonstration.events import is_action_event
                from showAndTell.capture.runtime import author_draft
                # Narration timestamps count from when /record returned; the
                # action clock counts from when the recorder began capturing,
                # several seconds earlier. Shift the spoken steps onto the
                # recording clock so voice keys attach to the right actions.
                lead = int(managed_result.get("narration_lead_ms") or 0)
                aligned_steps = ([
                    {**step, "at_ms": step["at_ms"] + lead}
                    for step in finished["steps"]
                ] if lead else finished["steps"])
                authored_events = author_draft(
                    directory, metadata, managed_result, aligned_steps,
                    seed=session["seed"],
                    # The structured export is the task seed artifact. Replaying
                    # setup gestures as a silent prelude also makes the draft
                    # immediately testable when an application's exporter is
                    # partial (common for third-party admin UIs).
                    replay_setup=True,
                )
                if recording.is_file():
                    demo_recording = directory / "demo" / f"recording{recording.suffix}"
                    os.replace(recording, demo_recording)
                    recording = demo_recording
            replay_action_count = sum(
                is_action_event(event)
                for event in authored_events
            ) if managed_result is not None else 0
            voice_recorded = (
                bool(managed_result.get("voice_recorded"))
                if managed_result is not None else finished["voice_recorded"]
            )
            testcase = {
                "schema_version": 1,
                "status": "captured-draft",
                "name": metadata["slug"],
                "title": metadata["title"],
                "summary": metadata["summary"],
                "applications": metadata["applications"],
                "primary_application": metadata["primary_application"],
                "surfaces": [
                    {**surface, "captured_url": surface["url"]}
                    for surface in metadata["surfaces"]
                ],
                "capture": {
                    "started_at": metadata["started_at"],
                    "completed_at": completed_at,
                    "duration_ms": finished["duration_ms"],
                    "recording": (str(recording.relative_to(directory))
                                  if recording.is_file() else None),
                    "voice_recorded": voice_recorded,
                    "transcript_supported": finished["transcript_supported"],
                    "mode": metadata["mode"],
                },
                "seed": {
                    "path": "demo/seed.json" if managed_result is not None else None,
                    # `exported` is a single flag across every application, so
                    # one application with real state hides another that
                    # returned an empty shell. Name the ones that contributed.
                    "exported_applications": exported_applications,
                    "source": ("state-snapshot" if session.get("state_snapshots")
                               else "fixture-export" if session["seed_exported"]
                               else "captured-setup-actions" if managed_result is not None
                               else "not-captured"),
                    "exported": session["seed_exported"],
                    # Exact application state for fixtures that cannot express
                    # operator-created data declaratively; restored before replay.
                    "state_snapshots": [
                        f"demo/{name}" for name in session.get("state_snapshots", [])
                    ],
                    "setup_action_count": len(
                        managed_result.get("setup_events", [])) if managed_result else 0,
                },
                "replay": {
                    "generated": managed_result is not None,
                    "demonstrate": "demonstrate.py" if managed_result is not None else None,
                    "events": "events.jsonl" if managed_result is not None else None,
                    "bundle": "capture_bundle" if managed_result is not None else None,
                    "action_count": replay_action_count,
                },
                "preconditions": [
                    "The selected applications are running and reachable at the captured URLs."
                ],
                "steps": finished["steps"],
                "expected_results": [],
                "authoring_todos": [
                    "Review the recording and correct the generated spoken steps.",
                    "Review the exported seed and replayable setup actions.",
                    "Confirm selectors remain stable after a clean reset.",
                    "Add expected outcomes and quiz questions.",
                    "Promote the reviewed draft into tasks/<name>/.",
                ],
            }
            _util.write_json(directory / "testcase.json", testcase)
            narration = "".join(json.dumps({
                "key": step["id"], "text": step["text"], "at_ms": step["at_ms"],
            }, ensure_ascii=False) + "\n" for step in finished["steps"])
            (directory / "narration_script.jsonl").write_text(narration, encoding="utf-8")
            recording_note = str(recording.relative_to(directory)) if recording.is_file() else "none"
            generated_notes = (
                "- `demonstrate.py` replays the captured actions with selector fallbacks.\n"
                "- `capture_bundle/` contains screenshots, accessibility trees, and traces.\n"
                "- `demo/narration_script.jsonl` is synchronized with replay steps.\n"
                if managed_result is not None else
                "- `narration_script.jsonl` contains the editable spoken steps.\n"
            )
            (directory / "README.md").write_text(
                f"# {metadata['title']}\n\n"
                "This is a captured testcase draft from the local Task Inspector.\n\n"
                f"- `{recording_note}` "
                "contains the screen and microphone evidence when available.\n"
                f"{generated_notes}"
                "- `demo/seed.json` contains fixture state exported immediately before recording.\n"
                "- `demo/seed_events.jsonl` preserves setup gestures as a replay fallback.\n"
                "- `testcase.json` contains selected applications and timestamped spoken steps.\n"
                "\n"
                "Review the recording and exported seed, add assertions, then promote the "
                "draft into `tasks/` using the repository's task-authoring workflow.\n",
                encoding="utf-8")
            # Read the recording before the move: `recording` points inside the
            # staging directory, which stops existing the moment it is renamed.
            recording_available = recording.is_file()
            recording_bytes = (recording.stat().st_size if recording_available
                               else session["bytes"])
            os.replace(directory, target)
            if session.get("workspace") is not None:
                # A completed capture owns nothing. Replays reacquire one
                # execution lease through the manager and restore this draft's
                # saved state; retaining the authoring workspace here left a
                # heartbeat alive forever after its browser tab disappeared.
                self.supervisor.complete(session_id)
            del self._sessions[session_id]
        return {
            "ok": True,
            "slug": metadata["slug"],
            "path": str(target),
            "steps": len(finished["steps"]),
            "actions": replay_action_count,
            "replay_generated": managed_result is not None,
            "voice_recorded": voice_recorded,
            "seed_exported": session["seed_exported"],
            "seed_keys": sorted(session["seed"]),
            "exported_applications": exported_applications,
            "state_snapshots": session.get("state_snapshots", []),
            "recording_available": recording_available,
            "bytes": recording_bytes,
        }

    def close(self) -> None:
        """Release active capture sessions on server shutdown."""
        with self._lock:
            for session_id in list(self._sessions):
                try:
                    self.cancel(session_id)
                except Exception:
                    pass
