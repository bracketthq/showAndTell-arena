"""showAndTell.viewer.serve_public — Basic-Auth wrapper for the public viewer."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

def load_serve_public():
    from showAndTell.viewer import serve_public

    return serve_public


sp = load_serve_public()


def _basic(user_pass: str) -> str:
    return "Basic " + base64.b64encode(user_pass.encode()).decode()


def test_authorized_accepts_exact_credentials():
    assert sp._authorized(_basic("results:s3cret"), "results:s3cret")


def test_authorized_rejects_wrong_missing_and_garbage():
    assert not sp._authorized(_basic("results:nope"), "results:s3cret")
    assert not sp._authorized(None, "results:s3cret")
    assert not sp._authorized("Basic not!!base64", "results:s3cret")
    assert not sp._authorized("Bearer abc", "results:s3cret")
    assert not sp._authorized("", "results:s3cret")


def test_server_gates_on_auth(monkeypatch):
    # Stub the expensive page build; the test is about the auth gate.
    monkeypatch.setattr(sp.serve.generate, "build", lambda: ({}, []))
    monkeypatch.setattr(sp.serve.generate, "assemble", lambda data: "<h1>ok</h1>")
    server = sp.make_server(0, "results:s3cret", host="127.0.0.1")
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        assert e.value.code == 401
        assert e.value.headers.get("WWW-Authenticate", "").startswith("Basic")

        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     headers={"Authorization": _basic("results:s3cret")})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert b"ok" in resp.read()

        # The public server deliberately has no edit token. Basic Auth grants
        # read access only; question writes remain local serve.py functionality.
        edit = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/questions", method="POST",
            data=json.dumps({}).encode(), headers={
                "Authorization": _basic("results:s3cret"),
                "Content-Type": "application/json",
            })
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(edit, timeout=5)
        assert e.value.code == 403
    finally:
        server.shutdown()


def test_main_refuses_to_start_without_credentials(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_VIEWER_AUTH", raising=False)
    with pytest.raises(SystemExit):
        sp.main()
