"""HTTP coverage for question-evidence screenshots served by the viewer."""
from __future__ import annotations

import threading
from urllib.error import HTTPError
from urllib.request import urlopen

from tests._viewer_fixture import load_serve, mini_repo


def test_viewer_serves_evidence_png_and_rejects_other_task_files(tmp_path, monkeypatch):
    mini_repo(tmp_path)
    serve = load_serve()
    monkeypatch.setattr(serve.generate, "TASKS_DIR", tmp_path / "tasks")
    server = serve.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/tasks/alpha-one/evidence/close.png") as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            assert response.read().startswith(b"\x89PNG")
        try:
            urlopen(base + "/tasks/alpha-one/task.toml")
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("non-PNG task files must not be served")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
