"""Focused protocol tests for the real ONLYOFFICE connector."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx
import pytest

from showAndTell.applications.onlyoffice.connector import (
    BRIDGE_HOST_PAGE_PATH,
    OnlyOfficeConnector,
    decode_jwt,
    encode_jwt,
)
from showAndTell.applications.onlyoffice.xlsx import write_xlsx


SECRET = "test-onlyoffice-jwt-secret"
ROOT = Path(__file__).resolve().parents[1]
HOST_PAGE = (ROOT / "src" / "showAndTell" / "applications" / "onlyoffice"
             / "host-page" / "editor.html")


def _sheet(value: str) -> dict:
    return {
        "tab": "Plan",
        "columns": ["Item", "Decision"],
        "rows": [{"row": 2, "item": "SD-010", "decision": value}],
    }


def _workbook_bytes(tmp_path: Path, value: str) -> bytes:
    path = write_xlsx(_sheet(value), tmp_path / f"{value}.xlsx")
    return path.read_bytes()


def _connector(
    tmp_path: Path, saved: bytes | None = None, *, bridge: bool = False
) -> OnlyOfficeConnector:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BRIDGE_HOST_PAGE_PATH:
            if bridge:
                return httpx.Response(200, content=b"<!doctype html>bridge")
            return httpx.Response(404)
        assert request.url == "http://documentserver.local/cache/edited.xlsx"
        return httpx.Response(200, content=saved or b"")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://documentserver.local",
        jwt_secret=SECRET,
        http_client=http,
    )
    connector.create_workbook("demand-plan", _sheet("Review"), title="Demand Plan.xlsx")
    return connector


def _path_and_query(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path + "?" + parsed.query


def test_generated_workbook_is_served_only_through_signed_url(tmp_path):
    connector = _connector(tmp_path)
    client = TestClient(connector.app)

    config = client.get("/onlyoffice/config/demand-plan").json()
    download = _path_and_query(config["document"]["url"])
    response = client.get(download)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    workbook = tmp_path / "served.xlsx"
    workbook.write_bytes(response.content)
    with ZipFile(workbook) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "Review" in sheet
    assert client.get(download.replace("signature=", "signature=bad")).status_code == 403


def test_config_is_a_real_signed_onlyoffice_cell_editor_config(tmp_path):
    connector = _connector(tmp_path)
    client = TestClient(connector.app)
    config = client.get("/onlyoffice/config/demand-plan").json()

    token = config.pop("token")
    assert decode_jwt(token, SECRET) == config
    assert config["documentType"] == "cell"
    assert config["document"]["fileType"] == "xlsx"
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["customization"]["forcesave"] is True
    editor = client.get("/onlyoffice/editor/demand-plan")
    assert editor.status_code == 200
    assert "documentserver.local/web-apps/apps/api/documents/api.js" in editor.text
    assert "new DocsAPI.DocEditor" in editor.text


def test_editor_redirects_to_same_origin_bridge_page_when_mounted(tmp_path):
    # With the host page mounted into the Document Server, the editor route
    # redirects there so the frameEditor iframe is same-origin with the top
    # page, carrying the signed config in the URL fragment (never sent to any
    # server, stripped by the page before recording can observe it).
    connector = _connector(tmp_path, bridge=True)
    client = TestClient(connector.app)

    response = client.get("/onlyoffice/editor/demand-plan", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    # `v` is the served page's content hash: Document Server nginx marks the
    # page `Cache-Control: immutable`, so the URL must change with the content.
    version = hashlib.sha256(b"<!doctype html>bridge").hexdigest()[:12]
    assert location.startswith(
        "http://documentserver.local"
        f"{BRIDGE_HOST_PAGE_PATH}?doc=demand-plan&v={version}#cfg="
    )
    fragment = location.split("#cfg=", 1)[1]
    config = json.loads(base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4)))
    token = config.pop("token")
    assert decode_jwt(token, SECRET) == config
    assert config["documentType"] == "cell"
    assert config["document"]["url"].startswith(
        "http://connector.local:8090/onlyoffice/documents/demand-plan"
    )


def test_bridge_probe_asks_the_internal_address_not_the_browsers(tmp_path):
    """Two audiences, two addresses — and the probe belongs to this process.

    ``document_server_url`` is what the operator's browser loads, which in a
    Compose deployment is a published port on the host. Inside the connector
    container that same address is the container's own loopback, where nothing
    is listening: probing it fails, the bridge is silently never offered, and
    every extension recorder then captures nothing from the editor iframe
    because it stays cross-origin.
    """
    probed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        if request.url.host != "documentserver.internal":
            return httpx.Response(404)      # the browser's address, from here
        return httpx.Response(200, content=b"<!doctype html>bridge")

    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://127.0.0.1:8081",
        command_url="http://documentserver.internal",
        jwt_secret=SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connector.create_workbook("demand-plan", _sheet("Review"), title="Demand Plan.xlsx")

    response = TestClient(connector.app).get(
        "/onlyoffice/editor/demand-plan", follow_redirects=False)

    assert response.status_code == 302
    assert probed and all("documentserver.internal" in url for url in probed), probed
    # The browser is still sent to the address the BROWSER can reach.
    assert response.headers["location"].startswith(
        f"http://127.0.0.1:8081{BRIDGE_HOST_PAGE_PATH}?doc=demand-plan&v=")


def test_callback_download_uses_internal_document_server_address(tmp_path):
    """A browser-facing loopback save URL is unreachable from the connector."""
    edited = _workbook_bytes(tmp_path, "Order")
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=edited)

    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://127.0.0.1:8081",
        command_url="http://documentserver.internal",
        jwt_secret=SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connector.create_workbook(
        "demand-plan", _sheet("Review"), title="Demand Plan.xlsx")
    key = connector.editor_config("demand-plan")["document"]["key"]
    body = {
        "key": key,
        "status": 6,
        "url": "http://127.0.0.1:8081/cache/edited.xlsx?token=signed",
    }

    result = connector.handle_callback(
        "demand-plan", body,
        authorization=encode_jwt({"payload": body}, SECRET),
    )

    assert result == {"error": 0}
    assert fetched == [
        "http://documentserver.internal/cache/edited.xlsx?token=signed"]
    assert connector._document_path("demand-plan").read_bytes() == edited


def test_callback_download_keeps_command_url_path_prefix(tmp_path):
    """A sub-path command_url must prefix the fetch like every other call site."""
    edited = _workbook_bytes(tmp_path, "Order")
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=edited)

    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://127.0.0.1:8081",
        command_url="http://proxy.internal/documentserver",
        jwt_secret=SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connector.create_workbook(
        "demand-plan", _sheet("Review"), title="Demand Plan.xlsx")
    key = connector.editor_config("demand-plan")["document"]["key"]
    body = {
        "key": key,
        "status": 6,
        "url": "http://127.0.0.1:8081/cache/edited.xlsx?token=signed",
    }

    result = connector.handle_callback(
        "demand-plan", body,
        authorization=encode_jwt({"payload": body}, SECRET),
    )

    assert result == {"error": 0}
    assert fetched == [
        "http://proxy.internal/documentserver/cache/edited.xlsx?token=signed"]
    assert connector._document_path("demand-plan").read_bytes() == edited


def test_editor_falls_back_inline_and_reprobes_until_bridge_appears(tmp_path):
    # A Document Server that is still starting (or an unmanaged remote one)
    # does not serve the host page: the editor must fall back to the inline
    # page, and the negative probe must NOT be cached — once the page appears
    # the very next editor load redirects. A positive probe IS cached.
    probe = {"status": 404, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == BRIDGE_HOST_PAGE_PATH
        probe["calls"] += 1
        return httpx.Response(probe["status"], content=b"<!doctype html>bridge")

    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://documentserver.local",
        jwt_secret=SECRET,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connector.create_workbook("demand-plan", _sheet("Review"), title="Demand Plan.xlsx")
    client = TestClient(connector.app)

    first = client.get("/onlyoffice/editor/demand-plan", follow_redirects=False)
    assert first.status_code == 200
    assert "new DocsAPI.DocEditor" in first.text

    probe["status"] = 200
    second = client.get("/onlyoffice/editor/demand-plan", follow_redirects=False)
    assert second.status_code == 302
    third = client.get("/onlyoffice/editor/demand-plan", follow_redirects=False)
    assert third.status_code == 302
    assert probe["calls"] == 2  # 404, then 200 (cached afterwards)


def test_bridge_host_page_asset_matches_the_connector_contract():
    # The static page the redirect lands on must exist where compose mounts
    # it, decode the #cfg= fragment, boot the editor, and bridge frameEditor
    # events (with the active-cell ref from the sdkjs name box) to the top
    # document for browser-extension recorders.
    text = HOST_PAGE.read_text()
    assert "#cfg=" in text or "cfg=" in text
    assert "DocsAPI.DocEditor" in text
    assert "frameEditor" in text
    assert "ce-cell-name" in text
    assert "replaceState" in text  # strips the signed config from the URL

    compose = (HOST_PAGE.parents[1] / "compose.yaml").read_text()
    assert "./host-page:/var/www/onlyoffice/documentserver/web-apps/brackett-host:ro" in compose


def test_status_two_callback_persists_download_and_rotates_document_key(tmp_path):
    edited = _workbook_bytes(tmp_path, "Order")
    connector = _connector(tmp_path, edited)
    client = TestClient(connector.app)
    before = connector.editor_config("demand-plan")
    body = {
        "key": before["document"]["key"],
        "status": 2,
        "url": "http://documentserver.local/cache/edited.xlsx",
    }
    callback = _path_and_query(before["editorConfig"]["callbackUrl"])

    response = client.post(
        callback,
        json=body,
        headers={"Authorization": "Bearer " + encode_jwt(body, SECRET)},
    )

    assert response.json() == {"error": 0}
    assert connector._document_path("demand-plan").read_bytes() == edited
    after = connector.editor_config("demand-plan")
    assert after["document"]["key"] != before["document"]["key"]
    metadata = json.loads(connector._metadata_path("demand-plan").read_text())
    assert metadata["revision"] == 2


def test_re_registering_identical_workbook_gets_a_fresh_session_key(tmp_path):
    """A fixture reset must not reconnect to a prior co-authoring session."""
    connector = _connector(tmp_path)
    first = connector.editor_config("demand-plan")["document"]["key"]
    source = _sheet("Review")

    connector.clear_documents()
    connector.create_workbook("demand-plan", source, title="Demand Plan.xlsx")
    second = connector.editor_config("demand-plan")["document"]["key"]

    assert second != first
    assert len(second) <= 128


def test_force_save_persists_file_but_keeps_current_session_key(tmp_path):
    edited = _workbook_bytes(tmp_path, "Order")
    connector = _connector(tmp_path, edited)
    before = connector.editor_config("demand-plan")
    body = {
        "key": before["document"]["key"],
        "status": 6,
        "url": "http://documentserver.local/cache/edited.xlsx",
    }

    result = connector.handle_callback(
        "demand-plan", body, authorization=encode_jwt({"payload": body}, SECRET)
    )

    assert result == {"error": 0}
    assert connector._document_path("demand-plan").read_bytes() == edited
    assert connector.editor_config("demand-plan")["document"]["key"] == before["document"]["key"]


def _force_save_connector(
    tmp_path: Path, edited: bytes, *, command_error: int = 0, admin_token: str | None = None
) -> tuple[OnlyOfficeConnector, list[dict]]:
    """A connector whose Document Server answers the force-save command.

    A real Document Server replies to the command and then posts the status-6
    callback that actually delivers the file.  Driving the callback from the
    command handler keeps that ordering without a live server.
    """
    commands: list[dict] = []
    holder: dict[str, OnlyOfficeConnector] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/coauthoring/CommandService.ashx":
            body = json.loads(request.content)
            commands.append(body)
            if command_error:
                return httpx.Response(200, json={"error": command_error})
            connector = holder["connector"]
            payload = {
                "key": body["key"],
                "status": 6,
                "url": "http://documentserver.local/cache/edited.xlsx",
            }
            connector.handle_callback(
                "demand-plan", payload,
                authorization=encode_jwt({"payload": payload}, SECRET),
            )
            return httpx.Response(200, json={"error": 0, "key": body["key"]})
        assert request.url == "http://documentserver.local/cache/edited.xlsx"
        return httpx.Response(200, content=edited)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local:8090",
        document_server_url="http://documentserver.local",
        jwt_secret=SECRET,
        http_client=http,
        admin_token=admin_token,
    )
    connector.create_workbook("demand-plan", _sheet("Review"), title="Demand Plan.xlsx")
    holder["connector"] = connector
    return connector, commands


def test_force_save_flushes_the_live_editing_session_into_storage(tmp_path):
    edited = _workbook_bytes(tmp_path, "Order")
    connector, commands = _force_save_connector(tmp_path, edited)
    key = connector.editor_config("demand-plan")["document"]["key"]

    saved = connector.force_save("demand-plan")

    assert saved is True
    assert connector._document_path("demand-plan").read_bytes() == edited
    assert [command["c"] for command in commands] == ["forcesave"]
    assert commands[0]["key"] == key
    assert decode_jwt(commands[0]["token"], SECRET)["key"] == key


def test_force_save_reports_no_save_when_the_document_has_no_pending_edits(tmp_path):
    edited = _workbook_bytes(tmp_path, "Order")
    connector, _ = _force_save_connector(tmp_path, edited, command_error=4)
    before = connector._document_path("demand-plan").read_bytes()

    saved = connector.force_save("demand-plan", timeout=0.01)

    assert saved is False
    assert connector._document_path("demand-plan").read_bytes() == before


def test_force_save_waits_for_an_inflight_autosave_callback(tmp_path):
    edited = _workbook_bytes(tmp_path, "Order")
    connector, _ = _force_save_connector(tmp_path, edited, command_error=4)
    metadata = connector._read_metadata("demand-plan")

    def finish_autosave():
        time.sleep(0.05)
        connector._document_path("demand-plan").write_bytes(edited)
        connector._write_metadata("demand-plan", {
            **metadata,
            "sha256": connector._digest(connector._document_path("demand-plan")),
        })

    worker = threading.Thread(target=finish_autosave)
    worker.start()
    try:
        saved = connector.force_save("demand-plan", timeout=1)
    finally:
        worker.join()

    assert saved is True
    assert connector.document_bytes("demand-plan") == edited


def test_force_save_raises_when_the_document_server_rejects_the_command(tmp_path):
    connector, _ = _force_save_connector(
        tmp_path, _workbook_bytes(tmp_path, "Order"), command_error=6)

    with pytest.raises(RuntimeError, match="force save"):
        connector.force_save("demand-plan")


def test_callback_rejects_unsigned_mismatched_and_untrusted_requests(tmp_path):
    connector = _connector(tmp_path, _workbook_bytes(tmp_path, "Order"))
    before = connector.editor_config("demand-plan")
    body = {
        "key": before["document"]["key"],
        "status": 2,
        "url": "http://documentserver.local/cache/edited.xlsx",
    }
    callback = _path_and_query(before["editorConfig"]["callbackUrl"])
    client = TestClient(connector.app)

    assert client.post(callback, json=body).status_code == 401
    wrong = {**body, "key": "another-key"}
    assert client.post(
        callback,
        json=wrong,
        headers={"Authorization": encode_jwt(wrong, SECRET)},
    ).status_code == 409
    untrusted = {**body, "url": "http://attacker.invalid/file.xlsx"}
    response = client.post(
        callback,
        json=untrusted,
        headers={"Authorization": encode_jwt(untrusted, SECRET)},
    )
    assert response.json()["error"] == 1
    assert "configured Document Server" in response.json()["message"]


def test_registration_rejects_path_traversal_and_non_workbooks(tmp_path):
    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local",
        document_server_url="http://documentserver.local",
        jwt_secret=SECRET,
    )
    plain = tmp_path / "not-a-workbook.xlsx"
    plain.write_text("not a zip")

    try:
        for unsafe in ("../escape", "nested/name", ""):
            try:
                connector.register_document(unsafe, plain)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe document id accepted: {unsafe!r}")
        try:
            connector.register_document("safe", plain)
        except ValueError as exc:
            assert "valid .xlsx" in str(exc)
        else:
            raise AssertionError("invalid workbook accepted")
    finally:
        connector.close()


def test_generated_workbook_title_cannot_escape_temporary_directory(tmp_path):
    connector = OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector.local",
        document_server_url="http://documentserver.local",
        jwt_secret=SECRET,
    )
    try:
        try:
            connector.create_workbook("safe", _sheet("Review"), title="../escape.xlsx")
        except ValueError as exc:
            assert "plain .xlsx filename" in str(exc)
        else:
            raise AssertionError("unsafe workbook title accepted")
        assert not (tmp_path / "escape.xlsx").exists()
    finally:
        connector.close()


# --- remote connector (applications on a separate fixture host) --------------


def _admin_connector(tmp_path: Path, **kwargs) -> OnlyOfficeConnector:
    return OnlyOfficeConnector(
        tmp_path / "storage",
        public_url="http://connector:8000",
        document_server_url="http://docserver.example:8081",
        jwt_secret=SECRET,
        **kwargs,
    )


def test_admin_routes_are_absent_until_a_token_is_configured(tmp_path):
    client = TestClient(_admin_connector(tmp_path).app)
    # An in-process connector must keep exactly its previous surface.
    assert client.get("/admin/workbooks").status_code == 404
    assert client.delete("/admin/workbooks").status_code == 404
    assert client.get("/health").json() == {"ok": True}


def test_admin_routes_reject_a_missing_or_wrong_token(tmp_path):
    client = TestClient(_admin_connector(tmp_path, admin_token="s3cret").app)
    assert client.get("/admin/workbooks").status_code == 401
    assert client.get(
        "/admin/workbooks",
        headers={"X-ShowAndTell-Connector-Token": "wrong"},
    ).status_code == 401
    assert client.get(
        "/admin/workbooks",
        headers={"X-ShowAndTell-Connector-Token": "s3cret"},
    ).json() == {"documents": []}


def test_admin_api_creates_lists_and_clears_workbooks(tmp_path):
    client = TestClient(_admin_connector(tmp_path, admin_token="s3cret").app)
    auth = {"X-ShowAndTell-Connector-Token": "s3cret"}

    created = client.post("/admin/workbooks", headers=auth, json={
        "document_id": "demand-plan",
        "title": "Demand Plan.xlsx",
        "source": _sheet("Approve"),
    })
    assert created.status_code == 200, created.text
    assert created.json() == {"document_id": "demand-plan"}

    documents = client.get("/admin/workbooks", headers=auth).json()["documents"]
    assert [row["document_id"] for row in documents] == ["demand-plan"]
    # The workbook is generated server-side, so only the seed crossed the wire.
    assert client.get(f"/onlyoffice/config/demand-plan").status_code == 200

    assert client.delete("/admin/workbooks", headers=auth).json() == {"ok": True}
    assert client.get("/admin/workbooks", headers=auth).json() == {"documents": []}


def test_admin_api_force_saves_and_serves_the_exact_stored_workbook(tmp_path):
    # The capture host drives the connector over this API, so the same flush
    # and byte-for-byte read must be reachable remotely.
    edited = _workbook_bytes(tmp_path, "Order")
    connector, commands = _force_save_connector(tmp_path, edited, admin_token="s3cret")
    client = TestClient(connector.app)
    auth = {"X-ShowAndTell-Connector-Token": "s3cret"}

    flushed = client.post("/admin/workbooks/demand-plan/forcesave", headers=auth)
    assert flushed.json() == {"saved": True}
    assert [command["c"] for command in commands] == ["forcesave"]

    content = client.get("/admin/workbooks/demand-plan/content", headers=auth)
    assert content.status_code == 200
    assert content.content == edited


def test_admin_content_and_forcesave_require_the_connector_token(tmp_path):
    connector, _ = _force_save_connector(
        tmp_path, _workbook_bytes(tmp_path, "Order"), admin_token="s3cret")
    client = TestClient(connector.app)

    assert client.get("/admin/workbooks/demand-plan/content").status_code == 401
    assert client.post("/admin/workbooks/demand-plan/forcesave").status_code == 401


def test_admin_create_rejects_a_malformed_descriptor(tmp_path):
    client = TestClient(_admin_connector(tmp_path, admin_token="s3cret").app)
    auth = {"X-ShowAndTell-Connector-Token": "s3cret"}
    assert client.post("/admin/workbooks", headers=auth,
                       json={"title": "x.xlsx", "source": {}}).status_code == 400
    assert client.post("/admin/workbooks", headers=auth, json={
        "document_id": "bad id", "title": "x.xlsx", "source": _sheet("a"),
    }).status_code == 400


def test_saves_are_accepted_from_any_configured_document_server_origin(tmp_path):
    # Document Server answers callbacks with whichever origin it was reached
    # on: the browser-facing URL or its in-network compose name.
    connector = _admin_connector(
        tmp_path, extra_document_server_urls=["http://onlyoffice"])
    assert connector.save_origins == {
        ("http", "docserver.example", 8081),
        ("http", "onlyoffice", 80),
    }
