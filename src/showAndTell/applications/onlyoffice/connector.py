"""Minimal storage connector for a real ONLYOFFICE Document Server.

ONLYOFFICE Docs is an editor, not a file store.  This module supplies the
small integration surface it requires: stable document URLs, signed editor
configuration, and a callback that atomically stores the edited workbook.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Sequence
from typing import Any, Mapping
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile, is_zipfile

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import httpx

from showAndTell.applications.onlyoffice.xlsx import write_xlsx


_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SAVE_STATUSES = {2, 6}
# Document Server's answers that mean "there is nothing to flush", rather than
# a failure: 4 is an open session with no unsaved changes, 1 is no session at
# all. A capture hits 1 for every workbook the operator never opened, and the
# stored bytes are already current for those.
_NOTHING_TO_SAVE = frozenset({1, 4})

# Path (on the Document Server origin) of the same-origin editor host page that
# applications/onlyoffice/compose.yaml mounts into the container. Serving
# the host page from the Document Server itself makes the frameEditor iframe
# same-origin with the top document, which lets the page bridge in-frame events
# to browser-extension recorders that only listen on the top frame (see
# applications/onlyoffice/host-page/editor.html). A remote or unmanaged
# Document Server does not carry the mount; the editor route probes for the
# page and falls back to the classic inline host page.
BRIDGE_HOST_PAGE_PATH = "/web-apps/brackett-host/editor.html"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encode_jwt(payload: Mapping[str, Any], secret: str) -> str:
    """Return an HS256 JWT without acquiring a separate JWT dependency."""

    if not secret:
        raise ValueError("ONLYOFFICE JWT secret must not be empty")
    header = _b64url(_json_bytes({"alg": "HS256", "typ": "JWT"}))
    body = _b64url(_json_bytes(payload))
    signing_input = f"{header}.{body}".encode("ascii")
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(signature)}"


def decode_jwt(token: str, secret: str) -> dict[str, Any]:
    """Verify an HS256 JWT and return its object payload."""

    try:
        raw_header, raw_payload, raw_signature = token.split(".")
        header = json.loads(_unb64url(raw_header))
        payload = json.loads(_unb64url(raw_payload))
        signature = _unb64url(raw_signature)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JWT") from exc
    if (
        not isinstance(header, dict)
        or header.get("alg") != "HS256"
        or header.get("typ") not in {None, "JWT"}
        or not isinstance(payload, dict)
    ):
        raise ValueError("invalid JWT")
    expected = hmac.new(
        secret.encode(), f"{raw_header}.{raw_payload}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid JWT signature")
    now = int(time.time())
    if "exp" in payload and int(payload["exp"]) < now:
        raise ValueError("expired JWT")
    if "nbf" in payload and int(payload["nbf"]) > now:
        raise ValueError("JWT is not active")
    return payload


def _absolute_http_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an absolute http(s) URL without credentials")
    return value.rstrip("/")


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlparse(value)
    default_port = 80 if parsed.scheme == "http" else 443 if parsed.scheme == "https" else None
    return parsed.scheme, parsed.hostname or "", parsed.port or default_port


class OnlyOfficeConnector:
    """File-backed ONLYOFFICE connector with a FastAPI application."""

    def __init__(
        self,
        storage_dir: Path | str,
        *,
        public_url: str,
        document_server_url: str,
        jwt_secret: str,
        link_ttl: int = 3600,
        max_save_bytes: int = 64 * 1024 * 1024,
        http_client: httpx.Client | None = None,
        admin_token: str | None = None,
        extra_document_server_urls: Sequence[str] = (),
        command_url: str | None = None,
    ) -> None:
        if link_ttl <= 0:
            raise ValueError("link_ttl must be positive")
        if max_save_bytes <= 0:
            raise ValueError("max_save_bytes must be positive")
        if not jwt_secret:
            raise ValueError("ONLYOFFICE JWT secret must not be empty")
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.public_url = _absolute_http_url(public_url, "public_url")
        # Two audiences, two addresses. ``document_server_url`` is what the
        # operator's browser loads api.js from; ``command_url`` is how this
        # process reaches Document Server for commands, probes, and callback
        # downloads. When the connector runs as a container beside Document
        # Server those differ, and using the browser's address here reaches
        # the connector container's own loopback.
        self._command_url_override = command_url
        self.document_server_url = _absolute_http_url(
            document_server_url, "document_server_url"
        )
        # A Document Server reached over a container network answers callbacks
        # with its internal origin while the browser loaded it over an external
        # one.  Both must be accepted or every save is rejected as foreign.
        self.save_origins = {_origin(self.document_server_url)} | {
            _origin(_absolute_http_url(value, "document server url"))
            for value in extra_document_server_urls if value
        }
        self.command_url = _absolute_http_url(
            self._command_url_override or self.document_server_url,
            "command_url")
        self.admin_token = admin_token or None
        self.jwt_secret = jwt_secret
        self.link_ttl = link_ttl
        self.max_save_bytes = max_save_bytes
        self._http = http_client or httpx.Client(timeout=60.0, follow_redirects=False)
        self._owns_http = http_client is None
        # Content hash of the served bridge host page, set by a successful
        # probe. Doubles as the availability flag and the cache-busting `v`
        # query value: Document Server nginx serves the page with
        # `Cache-Control: immutable, max-age=1y`, so the URL must change
        # whenever the page content does.
        self._bridge_version: str | None = None
        self.app = self._build_app()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    @staticmethod
    def _validate_document_id(document_id: str) -> str:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError(
                "document_id must contain only letters, numbers, '.', '_' or '-'"
            )
        return document_id

    @staticmethod
    def _validate_title(title: str) -> str:
        if (
            not title
            or len(title) > 255
            or not title.lower().endswith(".xlsx")
            or "/" in title
            or "\\" in title
        ):
            raise ValueError("title must be a plain .xlsx filename")
        return title

    def _directory(self, document_id: str) -> Path:
        return self.storage_dir / self._validate_document_id(document_id)

    def _document_path(self, document_id: str) -> Path:
        return self._directory(document_id) / "document.xlsx"

    def _metadata_path(self, document_id: str) -> Path:
        return self._directory(document_id) / "metadata.json"

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _document_key(document_id: str, revision: int, digest: str) -> str:
        """Return a key for one isolated Document Server editing version.

        ONLYOFFICE uses the key as the identity of a co-authoring session, not
        merely as a content checksum.  A benchmark reset often restores the
        exact same source bytes.  A deterministic key would reconnect that new
        run to the prior session still held in Document Server/Redis, allowing
        stale edits and locks to leak across otherwise clean fixture resets.

        The nonce keeps each registration/version distinct while the document
        id, revision and digest prefix retain useful diagnostics.  At the
        maximum supported 80-character document id the key remains below
        ONLYOFFICE's 128-character limit.
        """
        return (
            f"{document_id}-{revision}-{digest[:20]}-"
            f"{secrets.token_hex(8)}"
        )

    def _read_metadata(self, document_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._metadata_path(document_id).read_text())
        except FileNotFoundError as exc:
            raise KeyError(document_id) from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"invalid metadata for {document_id!r}")
        return value

    def _write_metadata(self, document_id: str, value: Mapping[str, Any]) -> None:
        directory = self._directory(document_id)
        directory.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(prefix="metadata-", suffix=".json", dir=directory)
        temp = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(value, target, sort_keys=True, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, self._metadata_path(document_id))
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _assert_xlsx(path: Path) -> None:
        if not is_zipfile(path):
            raise ValueError("saved file is not a valid .xlsx archive")
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise ValueError("saved file is not an OOXML workbook")
        except BadZipFile as exc:
            raise ValueError("saved file is not a valid .xlsx archive") from exc

    def register_document(
        self, document_id: str, source: Path | str, *, title: str | None = None
    ) -> Path:
        """Copy an existing workbook into connector-managed storage."""

        self._validate_document_id(document_id)
        source_path = Path(source).resolve()
        if source_path.suffix.lower() != ".xlsx" or not source_path.is_file():
            raise ValueError("source must be an existing .xlsx file")
        self._assert_xlsx(source_path)
        display_title = title or source_path.name
        self._validate_title(display_title)
        directory = self._directory(document_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = self._document_path(document_id)
        shutil.copyfile(source_path, destination)
        digest = self._digest(destination)
        self._write_metadata(document_id, {
            "document_id": document_id,
            "title": display_title,
            "revision": 1,
            "key": self._document_key(document_id, 1, digest),
            "sha256": digest,
        })
        return destination

    def create_workbook(
        self, document_id: str, source: Mapping[str, Any], *, title: str
    ) -> Path:
        """Generate a real workbook from a benchmark sheet seed and register it."""

        self._validate_title(title)
        with tempfile.TemporaryDirectory(prefix="showAndTell-xlsx-") as temporary:
            generated = write_xlsx(source, Path(temporary) / title)
            return self.register_document(document_id, generated, title=title)

    def store_workbook(self, document_id: str, payload: bytes, *, title: str) -> Path:
        """Register a workbook that already exists as bytes.

        A captured sheet is restored from the file the operator saved, so its
        styles, merges and number formats survive the round trip that
        regenerating it from seed rows would flatten.
        """

        self._validate_title(title)
        with tempfile.TemporaryDirectory(prefix="showAndTell-xlsx-") as temporary:
            staged = Path(temporary) / title
            staged.write_bytes(payload)
            return self.register_document(document_id, staged, title=title)

    def clear_documents(self) -> None:
        """Remove connector-owned documents while keeping the storage root."""
        if self.storage_dir.exists():
            for child in self.storage_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

    def documents(self) -> list[dict[str, Any]]:
        """Return deterministic metadata for all registered workbooks."""
        rows = []
        if not self.storage_dir.exists():
            return rows
        for child in sorted(self.storage_dir.iterdir(), key=lambda path: path.name):
            if not child.is_dir():
                continue
            try:
                rows.append(self._read_metadata(child.name))
            except KeyError:
                continue
        return rows

    def _link_signature(self, purpose: str, document_id: str, expires: int) -> str:
        message = f"{purpose}\n{document_id}\n{expires}".encode()
        return _b64url(hmac.new(self.jwt_secret.encode(), message, hashlib.sha256).digest())

    def _signed_link(self, purpose: str, document_id: str, path: str) -> str:
        expires = int(time.time()) + self.link_ttl
        signature = self._link_signature(purpose, document_id, expires)
        return f"{self.public_url}{path}?expires={expires}&signature={signature}"

    def _verify_link(
        self, purpose: str, document_id: str, expires: int, signature: str
    ) -> None:
        if expires < int(time.time()):
            raise HTTPException(status_code=403, detail="expired connector link")
        expected = self._link_signature(purpose, document_id, expires)
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="invalid connector signature")

    def _bridge_host_page_version(self) -> str | None:
        """Probe the Document Server for the bridge host page.

        Over ``command_url``, because this request comes from this process and
        not from the browser: ``document_server_url`` is a published host port,
        which inside a container is that container's own loopback with nothing
        listening.  Probing the browser's address there fails, the bridge is
        never offered, and the editor iframe silently stays cross-origin — where
        an extension recorder sees none of the operator's spreadsheet work.

        Returns the served page's content hash (the redirect's cache-busting
        ``v`` value) or ``None`` when the page is absent. Only a positive
        result is cached: a Document Server that is still starting must not
        permanently disable the bridge for this connector, so a failed or
        negative probe is retried on the next editor request.
        """
        if self._bridge_version is not None:
            return self._bridge_version
        try:
            response = self._http.get(
                f"{self.command_url}{BRIDGE_HOST_PAGE_PATH}", timeout=2.0
            )
        except Exception:  # noqa: BLE001 - any probe failure means "no bridge yet"
            return None
        if response.status_code != 200:
            return None
        self._bridge_version = hashlib.sha256(response.content).hexdigest()[:12]
        return self._bridge_version

    def editor_config(
        self,
        document_id: str,
        *,
        user_id: str = "showAndTell-agent",
        user_name: str = "ShowAndTell Agent",
    ) -> dict[str, Any]:
        """Build the signed configuration consumed by ``DocsAPI.DocEditor``."""

        metadata = self._read_metadata(document_id)
        config: dict[str, Any] = {
            "document": {
                "fileType": "xlsx",
                "key": metadata["key"],
                "title": metadata["title"],
                "url": self._signed_link(
                    "download", document_id, f"/onlyoffice/documents/{document_id}"
                ),
                "permissions": {"download": True, "edit": True, "print": True},
            },
            "documentType": "cell",
            "editorConfig": {
                "callbackUrl": self._signed_link(
                    "callback", document_id, f"/onlyoffice/callback/{document_id}"
                ),
                "mode": "edit",
                "user": {"id": user_id, "name": user_name},
                "customization": {"forcesave": True},
            },
            "type": "desktop",
        }
        config["token"] = encode_jwt(config, self.jwt_secret)
        return config

    def _verify_callback_jwt(
        self, authorization: str | None, body: Mapping[str, Any]
    ) -> None:
        if not authorization:
            raise HTTPException(status_code=401, detail="missing callback JWT")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            payload = decode_jwt(token, self.jwt_secret)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        signed = payload.get("payload", payload)
        if not isinstance(signed, Mapping):
            raise HTTPException(status_code=401, detail="invalid callback JWT payload")
        for field in ("key", "status", "url"):
            if field in signed and signed[field] != body.get(field):
                raise HTTPException(status_code=401, detail="callback JWT payload mismatch")

    def _save_callback_file(self, document_id: str, body: Mapping[str, Any]) -> None:
        raw_url = body.get("url")
        if not isinstance(raw_url, str) or _origin(raw_url) not in self.save_origins:
            raise ValueError("callback save URL is not from the configured Document Server")
        # Document Server reports the origin it was configured with. In a
        # Compose deployment that is the browser-facing published address
        # (often 127.0.0.1), which is not the same network location from this
        # connector container. The origin was authenticated above; preserve
        # its exact path/query and route the fetch through the connector's
        # trusted internal Document Server address, keeping any sub-path that
        # address carries just like the other ``command_url`` call sites do.
        # Plain concatenation is safe: ``_absolute_http_url`` strips trailing
        # slashes, and the reported path always starts with one.
        reported = urlparse(raw_url)
        internal = urlparse(self.command_url)
        fetch_url = internal._replace(
            path=internal.path + reported.path,
            params=reported.params,
            query=reported.query,
            fragment="",
        ).geturl()
        directory = self._directory(document_id)
        fd, raw_temp = tempfile.mkstemp(prefix="save-", suffix=".xlsx", dir=directory)
        temp = Path(raw_temp)
        total = 0
        try:
            with os.fdopen(fd, "wb") as target, self._http.stream("GET", fetch_url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_save_bytes:
                        raise ValueError("saved workbook exceeds maximum size")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            self._assert_xlsx(temp)
            os.replace(temp, self._document_path(document_id))
        finally:
            temp.unlink(missing_ok=True)

    def handle_callback(
        self,
        document_id: str,
        body: Mapping[str, Any],
        *,
        authorization: str | None,
    ) -> dict[str, int]:
        """Validate and apply an ONLYOFFICE callback payload."""

        metadata = self._read_metadata(document_id)
        self._verify_callback_jwt(authorization, body)
        if body.get("key") != metadata["key"]:
            raise HTTPException(status_code=409, detail="stale document key")
        status = body.get("status")
        if not isinstance(status, int):
            raise HTTPException(status_code=400, detail="callback status must be an integer")
        if status not in _SAVE_STATUSES:
            return {"error": 0}
        self._save_callback_file(document_id, body)
        digest = self._digest(self._document_path(document_id))
        metadata["sha256"] = digest
        # A normal close/save starts a new editing session.  A force save must
        # keep the key so current co-editors remain attached to their session.
        if status == 2:
            revision = int(metadata["revision"]) + 1
            metadata["revision"] = revision
            metadata["key"] = self._document_key(document_id, revision, digest)
        self._write_metadata(document_id, metadata)
        return {"error": 0}

    def force_save(self, document_id: str, *, timeout: float = 30.0) -> bool:
        """Flush a live editing session into storage; False when nothing was pending.

        Edits an operator makes in the browser live in the Document Server, not
        here — this connector only learns of them through a save callback.  A
        capture that exports without asking for this freezes the workbook as it
        was before the operator touched it.
        """
        metadata = self._read_metadata(document_id)
        before = metadata.get("sha256")
        command = {"c": "forcesave", "key": metadata["key"]}
        response = self._http.post(
            f"{self.command_url}/coauthoring/CommandService.ashx",
            json={**command, "token": encode_jwt(command, self.jwt_secret)},
            headers={
                "Authorization":
                    "Bearer " + encode_jwt({"payload": command}, self.jwt_secret),
            },
        )
        response.raise_for_status()
        error = response.json().get("error", 0)
        if error in _NOTHING_TO_SAVE:
            # An autosave callback can already be in flight when Document
            # Server answers "nothing to save".  Give that callback a short
            # grace period before the capture reads connector storage; without
            # it the command response can win the race and export the previous
            # workbook bytes.
            deadline = time.monotonic() + min(timeout, 1.0)
            while time.monotonic() < deadline:
                if self._read_metadata(document_id).get("sha256") != before:
                    return True
                time.sleep(0.05)
            return False
        if error:
            raise RuntimeError(
                f"ONLYOFFICE force save was refused with error {error}")
        # The command returns as soon as it is accepted; the file itself only
        # arrives with the callback it triggers.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._read_metadata(document_id).get("sha256") != before:
                return True
            time.sleep(0.05)
        raise RuntimeError(
            f"ONLYOFFICE force save of {document_id!r} produced no callback "
            f"within {timeout:g}s")

    def document_bytes(self, document_id: str) -> bytes:
        """Return the stored workbook exactly as the Document Server saved it."""

        self._read_metadata(document_id)  # unknown id ⇒ KeyError, not a stray read
        return self._document_path(document_id).read_bytes()

    def _verify_admin(self, token: str | None) -> None:
        """Guard the control surface used when the connector runs off-machine.

        Admin routes exist only when a token is configured, so an in-process
        connector keeps exactly the endpoint surface it had before.
        """
        if not self.admin_token:
            raise HTTPException(status_code=404, detail="not found")
        if not token or not hmac.compare_digest(token, self.admin_token):
            raise HTTPException(status_code=401, detail="invalid connector token")

    def _build_app(self) -> FastAPI:
        api = FastAPI(title="ShowAndTell ONLYOFFICE connector")

        @api.get("/health")
        def health():
            return {"ok": True}

        @api.get("/admin/workbooks")
        def admin_list(x_showAndTell_connector_token: str | None = Header(default=None)):
            self._verify_admin(x_showAndTell_connector_token)
            return {"documents": self.documents()}

        @api.post("/admin/workbooks")
        def admin_create(
            body: dict[str, Any],
            x_showAndTell_connector_token: str | None = Header(default=None),
        ):
            self._verify_admin(x_showAndTell_connector_token)
            try:
                document_id = str(body["document_id"])
                title = str(body["title"])
                if "content" in body:
                    payload = base64.b64decode(str(body["content"]), validate=True)
                    if len(payload) > self.max_save_bytes:
                        raise ValueError("workbook exceeds maximum size")
                    self.store_workbook(document_id, payload, title=title)
                else:
                    source = body["source"]
                    if not isinstance(source, Mapping):
                        raise ValueError("source must be an object")
                    self.create_workbook(document_id, source, title=title)
            except KeyError as exc:
                raise HTTPException(
                    status_code=400, detail=f"missing field {exc.args[0]!r}") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return {"document_id": document_id}

        @api.delete("/admin/workbooks")
        def admin_clear(x_showAndTell_connector_token: str | None = Header(default=None)):
            self._verify_admin(x_showAndTell_connector_token)
            self.clear_documents()
            return {"ok": True}

        @api.post("/admin/workbooks/{document_id}/forcesave")
        def admin_force_save(
            document_id: str,
            x_showAndTell_connector_token: str | None = Header(default=None),
        ):
            self._verify_admin(x_showAndTell_connector_token)
            try:
                return {"saved": self.force_save(document_id)}
            except KeyError:
                raise HTTPException(status_code=404, detail="unknown document") from None
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None

        @api.get("/admin/workbooks/{document_id}/content")
        def admin_content(
            document_id: str,
            x_showAndTell_connector_token: str | None = Header(default=None),
        ):
            self._verify_admin(x_showAndTell_connector_token)
            try:
                metadata = self._read_metadata(document_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="unknown document") from None
            return FileResponse(
                self._document_path(document_id),
                filename=metadata["title"],
                media_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            )

        @api.get("/onlyoffice/config/{document_id}")
        def config(document_id: str):
            try:
                return self.editor_config(document_id)
            except (KeyError, ValueError):
                raise HTTPException(status_code=404, detail="unknown document") from None

        @api.get("/onlyoffice/editor/{document_id}", response_class=HTMLResponse)
        def editor(document_id: str):
            try:
                config_value = self.editor_config(document_id)
            except (KeyError, ValueError):
                raise HTTPException(status_code=404, detail="unknown document") from None
            bridge_version = self._bridge_host_page_version()
            if bridge_version is not None:
                # The signed config travels in the fragment: it never reaches
                # the Document Server, and the host page strips it from the
                # URL before any recorder can observe it. document_id is
                # validated to URL-safe characters, so it is safe in a query.
                fragment = _b64url(
                    json.dumps(config_value, separators=(",", ":")).encode("utf-8")
                )
                return RedirectResponse(
                    f"{self.document_server_url}{BRIDGE_HOST_PAGE_PATH}"
                    f"?doc={document_id}&v={bridge_version}#cfg={fragment}",
                    status_code=302,
                )
            # Escaping '<' prevents a document title from closing the script.
            serialized = json.dumps(config_value).replace("<", "\\u003c")
            script = json.dumps(f"{self.document_server_url}/web-apps/apps/api/documents/api.js")
            return HTMLResponse(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>ONLYOFFICE</title><style>html,body,#editor{height:100%;margin:0}</style>"
                f"<script src={script}></script></head><body><div id='editor'></div>"
                f"<script>new DocsAPI.DocEditor('editor',{serialized});</script></body></html>"
            )

        @api.get("/onlyoffice/documents/{document_id}")
        def document(
            document_id: str,
            expires: int = Query(...),
            signature: str = Query(...),
        ):
            self._verify_link("download", document_id, expires, signature)
            try:
                metadata = self._read_metadata(document_id)
            except (KeyError, ValueError):
                raise HTTPException(status_code=404, detail="unknown document") from None
            return FileResponse(
                self._document_path(document_id),
                filename=metadata["title"],
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        @api.post("/onlyoffice/callback/{document_id}")
        def callback(
            document_id: str,
            body: dict[str, Any],
            expires: int = Query(...),
            signature: str = Query(...),
            authorization: str | None = Header(default=None),
        ):
            self._verify_link("callback", document_id, expires, signature)
            try:
                return self.handle_callback(
                    document_id, body, authorization=authorization
                )
            except KeyError:
                raise HTTPException(status_code=404, detail="unknown document") from None
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - protocol returns save errors as JSON
                return JSONResponse({"error": 1, "message": str(exc)[:400]})

        return api


class RemoteOnlyOfficeConnector:
    """Drive a connector running elsewhere through its admin API.

    Exposes the application data plane's required operations over the
    token-guarded admin API. Generation stays server-side — a seed sends
    rows, not a file — but a captured workbook is the operator's own bytes, so
    those do cross the wire in both directions.
    """

    def __init__(self, base_url: str, admin_token: str,
                 http_client: httpx.Client | None = None) -> None:
        if not admin_token:
            raise ValueError("remote ONLYOFFICE connector requires an admin token")
        self.base_url = _absolute_http_url(base_url, "connector base_url")
        self.admin_token = admin_token
        self._http = http_client or httpx.Client(timeout=60.0, follow_redirects=False)
        self._owns_http = http_client is None

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self._http.request(
            method, f"{self.base_url}{path}",
            headers={"X-ShowAndTell-Connector-Token": self.admin_token}, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(
                f"connector {method} {path} failed "
                f"({response.status_code}): {response.text[:300]}")
        return response.json()

    def health(self) -> None:
        response = self._http.get(f"{self.base_url}/health")
        response.raise_for_status()

    def create_workbook(self, document_id: str, source: Mapping[str, Any], *,
                        title: str) -> None:
        self._request("POST", "/admin/workbooks", json={
            "document_id": document_id, "title": title, "source": dict(source),
        })

    def store_workbook(self, document_id: str, payload: bytes, *,
                       title: str) -> None:
        self._request("POST", "/admin/workbooks", json={
            "document_id": document_id, "title": title,
            "content": base64.b64encode(payload).decode("ascii"),
        })

    def clear_documents(self) -> None:
        self._request("DELETE", "/admin/workbooks")

    def documents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/admin/workbooks")["documents"]

    def force_save(self, document_id: str, *, timeout: float = 30.0) -> bool:
        return bool(self._request(
            "POST", f"/admin/workbooks/{document_id}/forcesave")["saved"])

    def document_bytes(self, document_id: str) -> bytes:
        path = f"/admin/workbooks/{document_id}/content"
        response = self._http.get(
            f"{self.base_url}{path}",
            headers={"X-ShowAndTell-Connector-Token": self.admin_token})
        if response.status_code >= 400:
            raise RuntimeError(
                f"connector GET {path} failed "
                f"({response.status_code}): {response.text[:300]}")
        return response.content

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


def _document_id_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    # Split CamelCase before normalizing punctuation so DemandPlan becomes the
    # stable id demand-plan expected by existing task adapters.
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", stem)
    value = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    if not value:
        raise ValueError(f"cannot derive document id from filename {filename!r}")
    return OnlyOfficeConnector._validate_document_id(value[:80])


__all__ = [
    "ConnectorClient", "OnlyOfficeConnector", "RemoteOnlyOfficeConnector",
    "decode_jwt", "encode_jwt",
]


class ConnectorClient:
    """Talk to a connector the driver already started.

    The application driver owns the connector lifecycle, so the data plane
    needs only these operations against the address and token it reports.
    """

    def __init__(self, base_url: str, admin_token: str) -> None:
        self.connector = RemoteOnlyOfficeConnector(base_url, admin_token)

    def register(self, block: Mapping[str, Any]) -> tuple[str, ...]:
        """Materialize a seed block's workbooks, generated or restored."""
        if block.get("schema_version") != 1 or not isinstance(
                block.get("workbooks"), list):
            raise ValueError("unsupported ONLYOFFICE seed schema")
        prepared: list[tuple[str, str, Any, Any]] = []
        for descriptor in block["workbooks"]:
            if not isinstance(descriptor, Mapping):
                raise ValueError("invalid ONLYOFFICE workbook descriptor")
            filename = descriptor.get("filename")
            source = descriptor.get("source")
            # A workbook is either generated from seed rows or restored from
            # the bytes a capture saved; anything else cannot be materialized.
            content = descriptor.get("content")
            if (not isinstance(filename, str)
                    or descriptor.get("format") != "xlsx"
                    or not (isinstance(source, Mapping)
                            or isinstance(content, (bytes, bytearray)))):
                raise ValueError("invalid ONLYOFFICE workbook descriptor")
            raw_id = descriptor.get("document_id")
            document_id = (
                OnlyOfficeConnector._validate_document_id(str(raw_id))
                if raw_id is not None else _document_id_from_filename(filename)
            )
            if any(document_id == row[0] for row in prepared):
                raise ValueError(f"duplicate ONLYOFFICE document id {document_id!r}")
            prepared.append((document_id, filename, source, content))
        try:
            for document_id, filename, source, content in prepared:
                if content is not None:
                    self.connector.store_workbook(
                        document_id, bytes(content), title=filename)
                else:
                    self.connector.create_workbook(document_id, source, title=filename)
        except Exception:
            self.connector.clear_documents()
            raise
        return tuple(row[0] for row in prepared)

    def documents(self) -> list[dict[str, Any]]:
        return self.connector.documents()

    def clear(self) -> None:
        self.connector.clear_documents()

    def capture_documents(self, directory: Path | str) -> list[dict[str, Any]]:
        """Flush every live editor and copy its workbook into ``directory``.

        The workbook is kept as the exact bytes the Document Server saved.
        Re-describing it as seed rows would drop everything the writer cannot
        express — styles, merges, number formats — so a captured sheet would no
        longer look like the one that was demonstrated.
        """
        directory = Path(directory)
        captured: list[dict[str, Any]] = []
        rows = self.connector.documents()
        if rows:
            directory.mkdir(parents=True, exist_ok=True)
        for row in rows:
            document_id = str(row["document_id"])
            self.connector.force_save(document_id)
            payload = self.connector.document_bytes(document_id)
            asset = f"{document_id}.xlsx"
            (directory / asset).write_bytes(payload)
            captured.append({
                "document_id": document_id,
                "title": row.get("title"),
                "format": "xlsx",
                "asset": asset,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        return captured
