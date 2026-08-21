"""The always-on viewer: serve.py's live page behind HTTP Basic Auth.

serve.py stays a localhost-only dev tool; this wrapper is what the webarena
VM's `showAndTell-viewer` systemd service runs (see scripts/webarena-host).
Binds all interfaces, so credentials are mandatory — it refuses to start
unless SHOWANDTELL_VIEWER_AUTH="user:password" is set (the service reads it from
root-owned /etc/showAndTell-viewer.env). Stdlib only, like serve.py.

    SHOWANDTELL_VIEWER_AUTH=results:hunter2 python -m showAndTell.viewer.serve_public 8090
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import sys
from http.server import ThreadingHTTPServer

from . import serve


def _authorized(header: str | None, expected: str) -> bool:
    """True when an Authorization header carries exactly the expected
    "user:password" as Basic credentials. Constant-time compare."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        presented = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def make_server(port: int, expected: str, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class PublicViewerHandler(serve.ViewerHandler):
        def do_GET(self):
            if not _authorized(self.headers.get("Authorization"), expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="showAndTell-viewer"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_GET()

    return ThreadingHTTPServer((host, port), PublicViewerHandler)


def main() -> None:
    expected = os.environ.get("SHOWANDTELL_VIEWER_AUTH", "")
    if ":" not in expected:
        raise SystemExit("SHOWANDTELL_VIEWER_AUTH must be set to \"user:password\" — "
                         "this server binds all interfaces and never runs open.")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    server = make_server(port, expected)
    print(f"serving the viewer on 0.0.0.0:{server.server_address[1]} "
          "(Basic Auth required; rebuilds on every refresh)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
