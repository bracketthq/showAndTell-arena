"""The seed editor over real HTTP: the page, its token, and applying rows.

Reported from the browser as "invalid edit token" — the page is served by the
viewer but was not given the write token the viewer's own page carries, so
every apply was refused. These cover the whole round trip rather than the
handler in isolation, because that gap was exactly between the two.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from tests._viewer_fixture import load_serve, write_task

SESSION = "a" * 32


class _Session:
    """The slice of an AppSession the seed routes touch."""

    def __init__(self):
        self.applied = []

    applications = ("roundcube",)

    def seed_forms(self):
        from showAndTell.applications.authoring.seedform import Field, SeedForm

        return {"roundcube": SeedForm(
            title="Inbox", row_noun="message",
            fields=(Field("sender", "From", required=True),
                    Field("subject", "Subject", required=True))).to_json()}

    def apply_form(self, application, rows):
        self.applied.append((application, list(rows)))
        return len(rows)

    class registry:
        @staticmethod
        def manifest(name):
            from tests._app_fixtures import app_manifest

            return app_manifest(name)


@pytest.fixture()
def viewer(tmp_path, monkeypatch):
    write_task(tmp_path, "demo-task")
    serve = load_serve()
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    server = serve.make_server(0)
    store = server.capture_store
    workspace = type("W", (), {"session": _Session()})()
    store._sessions[SESSION] = {
        "directory": tmp_path, "metadata": {}, "next_chunk": 0, "bytes": 0,
        "managed": None, "workspace": workspace, "managed_mode": True,
        "recording_started": False, "seed": {}, "seed_exported": False,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", server, workspace.session
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _post(url, payload, *, token=None, origin=None):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    if token is not None:
        request.add_header("X-ShowAndTell-Edit-Token", token)
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_page_is_served_with_the_session_and_the_write_token(viewer):
    base, server, _ = viewer
    status, body = _get(f"{base}/captures/{SESSION}/seed")
    assert status == 200
    assert SESSION in body
    # The bug: the page carried no token, so every apply came back 403.
    assert server.question_edit_token in body
    assert "__EDIT_TOKEN__" not in body
    assert "__SESSION_ID__" not in body


def test_the_forms_endpoint_describes_what_to_render(viewer):
    base, _, _ = viewer
    status, body = _get(f"{base}/api/captures/{SESSION}/seed")
    assert status == 200
    payload = json.loads(body)
    assert [app["name"] for app in payload["applications"]] == ["roundcube"]
    assert payload["applications"][0]["form"]["row_noun"] == "message"


def test_applying_rows_with_the_page_token_reaches_the_session(viewer):
    base, server, session = viewer
    rows = [{"sender": "Ops <ops@x.test>", "subject": "PO 4471"}]
    status, body = _post(f"{base}/api/captures/{SESSION}/seed",
                         {"application": "roundcube", "rows": rows},
                         token=server.question_edit_token, origin=base)
    assert status == 200, body
    assert body == {"ok": True, "application": "roundcube", "applied": 1}
    assert session.applied == [("roundcube", rows)]


def test_applying_without_the_token_is_refused(viewer):
    base, _, session = viewer
    status, body = _post(f"{base}/api/captures/{SESSION}/seed",
                         {"application": "roundcube", "rows": []})
    assert status == 403
    assert body["error"] == "invalid edit token"
    assert session.applied == []


def test_an_unknown_session_says_so_rather_than_serving_an_empty_form(viewer):
    base, _, _ = viewer
    status, body = _get(f"{base}/api/captures/{'b' * 32}/seed")
    assert status == 404
    assert "not found" in json.loads(body)["error"]


def test_rows_the_application_refuses_come_back_as_a_fixable_message(viewer):
    base, server, session = viewer

    def explode(application, rows):
        raise ValueError("message 1 is missing Subject")

    session.apply_form = explode
    status, body = _post(f"{base}/api/captures/{SESSION}/seed",
                         {"application": "roundcube",
                          "rows": [{"sender": "a@x", "subject": ""}]},
                         token=server.question_edit_token, origin=base)
    assert status == 400
    assert body["error"] == "message 1 is missing Subject"
