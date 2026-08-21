"""HTTP control plane for container-backed fixture apps.

Typed resources only — the agent owns the Docker socket, so this surface
must never accept a command to run.  Mutations require a lease; reads do
not.  A failed reset/restore marks the app degraded until a later attempt
succeeds, so nothing pretends a half-restored app is usable.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from .protocol import AppDriver, DriverError, Unsupported
from .leases import LeaseConflict, LeaseStore, UnknownLease

AGENT_VERSION = "1"
SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
TOKEN_HINT = "the token file lives at ~/.showAndTell/fixture-host-token on the agent host"


def _error(status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"code": code, **extra}, status_code=status)


class _AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy_with: str | None = None
        self.busy_since: float | None = None
        self.degraded = False


def build_agent_api(
    drivers: Mapping[str, AppDriver], token: str,
    leases: LeaseStore | None = None,
    families: Mapping[str, tuple[str, ...]] | None = None,
) -> FastAPI:
    api = FastAPI()
    # Identity families keep single-instance agents byte-identical to today.
    pools = ({name: tuple(members) for name, members in families.items()}
             if families else {name: (name,) for name in drivers})
    store = leases if leases is not None else LeaseStore(families=pools)
    states = {name: _AppState() for name in drivers}

    @api.middleware("http")
    async def check_token(request: Request, call_next):
        provided = request.headers.get("authorization", "")
        # secrets.compare_digest rejects non-ASCII str arguments outright
        # (TypeError, surfaced as a 500); comparing bytes accepts any header.
        provided_bytes = provided.encode("utf-8", errors="replace")
        expected_bytes = f"Bearer {token}".encode("utf-8")
        if not secrets.compare_digest(provided_bytes, expected_bytes):
            return _error(401, "bad_token", message=TOKEN_HINT)
        return await call_next(request)

    def _lease_gate(request: Request, app: str) -> JSONResponse | None:
        lease_id = request.headers.get("x-showAndTell-lease")
        if store.covers(lease_id, app):
            return None
        holder = store.holder_of(app)
        if holder is not None:
            return _error(409, "lease_held", held_by=holder.holder,
                          since=store.held_since(holder.lease_id))
        return _error(409, "lease_required",
                      message="acquire a lease via POST /v1/leases first")

    def _operate(request: Request | None, app: str, operation: str, action):
        """Run one driver operation with busy/degraded bookkeeping.

        request=None skips the lease gate (start/stop are how a host is
        brought up before anyone can hold a lease).
        """
        driver = drivers.get(app)
        if driver is None:
            return _error(404, "unknown_app")
        if request is not None:
            gate = _lease_gate(request, app)
            if gate is not None:
                return gate
        state = states[app]
        if not state.lock.acquire(blocking=False):
            return _error(423, "app_busy", operation=state.busy_with)
        state.busy_with, state.busy_since = operation, time.time()
        try:
            action(driver)
            if operation in ("start", "reset", "restore"):
                state.degraded = False
            return {"ok": True}
        except Unsupported:
            return _error(501, "unsupported", operation=operation)
        except DriverError as exc:
            if operation in ("reset", "restore"):
                state.degraded = True
            return _error(500, "operation_failed", operation=operation,
                          message=str(exc)[:800])
        except Exception as exc:
            # An unexpected exception must not bypass the degraded invariant
            # or the JSON error contract the way an unhandled 500 would.
            if operation in ("reset", "restore"):
                state.degraded = True
            return _error(500, "operation_failed", operation=operation,
                          message=str(exc)[:800])
        finally:
            state.busy_with = state.busy_since = None
            state.lock.release()

    def _safe_status(app: str, driver: AppDriver) -> dict:
        state = states[app]
        meta = {"busy_with": state.busy_with, "since": state.busy_since,
                "degraded": state.degraded}
        try:
            return {**driver.status(), **meta}
        except DriverError as exc:
            return {**meta, "state": "error", "message": str(exc)[:200]}

    @api.get("/v1/health")
    def health():
        return {"version": AGENT_VERSION, "apps": sorted(pools),
                "pools": {name: len(members)
                          for name, members in pools.items()}}

    @api.get("/v1/apps")
    def apps():
        return [{"name": name, **_safe_status(name, driver)}
                for name, driver in sorted(drivers.items())]

    @api.get("/v1/apps/{app}/status")
    def status(app: str):
        driver = drivers.get(app)
        if driver is None:
            return _error(404, "unknown_app")
        return _safe_status(app, driver)

    @api.post("/v1/apps/{app}/start")
    def start(app: str, body: dict | None = None):
        values = body or {}
        wait = bool(values.get("wait", True))
        public_url = values.get("public_url")

        def start_driver(driver) -> None:
            configure = getattr(driver, "set_public_url", None)
            if public_url is not None and callable(configure):
                configure(str(public_url))
            driver.start(wait=wait)

        return _operate(None, app, "start", start_driver)

    @api.post("/v1/apps/{app}/stop")
    def stop(app: str):
        return _operate(None, app, "stop", lambda d: d.stop())

    @api.post("/v1/apps/{app}/reset")
    def reset(app: str, request: Request):
        return _operate(request, app, "reset", lambda d: d.reset())

    @api.post("/v1/apps/{app}/baseline")
    def baseline(app: str, request: Request):
        return _operate(request, app, "baseline", lambda d: d.baseline())

    @api.post("/v1/apps/{app}/golden")
    def golden(app: str, request: Request):
        return _operate(request, app, "golden", lambda d: d.golden())

    @api.post("/v1/apps/{app}/snapshots/{name:path}/restore")
    def restore_snapshot(app: str, name: str, request: Request):
        if not SNAPSHOT_NAME.fullmatch(name):
            return _error(400, "bad_snapshot_name")
        return _operate(request, app, "restore", lambda d: d.restore(name))

    @api.post("/v1/apps/{app}/snapshots/{name:path}")
    def take_snapshot(app: str, name: str, request: Request):
        if not SNAPSHOT_NAME.fullmatch(name):
            return _error(400, "bad_snapshot_name")
        return _operate(request, app, "snapshot", lambda d: d.snapshot(name))

    @api.get("/v1/apps/{app}/snapshots/{name:path}")
    def pull_snapshot(app: str, name: str):
        driver = drivers.get(app)
        if driver is None:
            return _error(404, "unknown_app")
        if not SNAPSHOT_NAME.fullmatch(name):
            return _error(400, "bad_snapshot_name")
        try:
            tar_path = driver.fetch_snapshot(name)
        except Unsupported:
            return _error(501, "unsupported", operation="fetch_snapshot")
        except DriverError as exc:
            return _error(404, "unknown_snapshot", message=str(exc)[:200])
        digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        return FileResponse(tar_path, media_type="application/x-tar",
                            headers={"x-showAndTell-sha256": digest})

    @api.put("/v1/apps/{app}/snapshots/{name:path}")
    async def push_snapshot(app: str, name: str, request: Request):
        if not SNAPSHOT_NAME.fullmatch(name):
            return _error(400, "bad_snapshot_name")
        body = await request.body()
        want_sha = request.headers.get("x-showAndTell-sha256")
        if want_sha and want_sha != hashlib.sha256(body).hexdigest():
            return _error(400, "snapshot_corrupt")
        with tempfile.NamedTemporaryFile(suffix=".tar") as upload:
            Path(upload.name).write_bytes(body)
            return await run_in_threadpool(
                _operate, request, app, "load_snapshot",
                lambda d: d.load_snapshot(name, Path(upload.name)),
            )

    @api.get("/v1/apps/{app}/secrets")
    def app_secrets(app: str):
        driver = drivers.get(app)
        if driver is None:
            return _error(404, "unknown_app")
        try:
            return driver.secrets()
        except Unsupported:
            return _error(501, "unsupported", operation="secrets")

    @api.post("/v1/leases")
    def acquire_lease(body: dict):
        requested = list(body.get("apps") or [])
        unknown = [app for app in requested if app not in drivers]
        if unknown:
            return _error(404, "unknown_app", apps=unknown)
        try:
            lease = store.acquire(requested, str(body.get("holder") or "unknown"),
                                  float(body.get("ttl_s") or 900.0))
        except ValueError:
            return _error(400, "bad_request", message="apps must be non-empty")
        except LeaseConflict as exc:
            return _error(409, "lease_held", held_by=exc.held_by, since=exc.since)
        return {"lease_id": lease.lease_id, "expires_at": lease.expires_at,
                "apps": lease.assignment}

    @api.get("/v1/leases")
    def list_leases():
        return {"leases": [{
            "lease_id": lease.lease_id,
            "holder": lease.holder,
            "apps": lease.assignment,
            "execution": lease.execution,
            "granted_at": granted_at,
        } for lease, granted_at in store.active()]}

    @api.put("/v1/leases/{lease_id}/execution")
    def annotate_lease(lease_id: str, body: dict):
        try:
            lease = store.annotate(lease_id, body)
        except UnknownLease:
            return _error(404, "unknown_lease")
        return {"lease_id": lease.lease_id, "execution": lease.execution}

    @api.post("/v1/leases/force-release")
    def force_release(body: dict):
        """Operator escape hatch: break stale leases that block these apps."""
        requested = list(body.get("apps") or [])
        if not requested:
            return _error(400, "bad_request", message="apps must be non-empty")
        unknown = [app for app in requested if app not in drivers]
        if unknown:
            return _error(404, "unknown_app", apps=unknown)
        removed = store.force_release(requested)
        return {"released": [{"holder": lease.holder, "apps": sorted(lease.apps)}
                             for lease in removed]}

    @api.post("/v1/leases/{lease_id}/heartbeat")
    def heartbeat(lease_id: str):
        try:
            lease = store.heartbeat(lease_id)
        except UnknownLease:
            return _error(404, "unknown_lease")
        return {"lease_id": lease.lease_id, "expires_at": lease.expires_at}

    @api.delete("/v1/leases/{lease_id}")
    def release(lease_id: str):
        store.release(lease_id)
        return {"ok": True}

    return api
