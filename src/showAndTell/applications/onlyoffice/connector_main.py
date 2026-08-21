"""Run the ONLYOFFICE storage connector as a standalone service.

Needed when the applications live on a remote fixture host. Document Server
cannot reach a connector running inside the operator's laptop process, and the
operator's browser cannot reach one bound to the fixture host's loopback, so
the connector is deployed beside Document Server and driven over the admin API
in ``onlyoffice.py``.

It runs as a flat module inside the image rather than as part of the showAndTell
package: importing the benchmark runtime would drag unrelated orchestration
code into a container that needs none of it.

Configuration is entirely environment-driven because the container is started
by Compose, not by the fixture:

  SHOWANDTELL_CONNECTOR_PUBLIC_URL          what Document Server fetches from and
                                         posts callbacks to — normally the
                                         connector's compose service URL
  SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL browser-reachable Document Server, used
                                         for the editor's api.js and to
                                         validate callback save URLs
  SHOWANDTELL_CONNECTOR_COMMAND_URL         Document Server as THIS process reaches
                                         it, for CommandService. Defaults to the
                                         browser URL, which is wrong whenever
                                         the two are on different networks
  SHOWANDTELL_CONNECTOR_EXTRA_DOCUMENT_SERVER_URLS
                                         comma-separated additional origins to
                                         accept saves from (the container
                                         network name, typically)
  SHOWANDTELL_ONLYOFFICE_JWT_SECRET         shared with Document Server
  SHOWANDTELL_CONNECTOR_ADMIN_TOKEN         guards the admin API
  SHOWANDTELL_CONNECTOR_STORAGE_DIR         workbook storage root (default /data)
  SHOWANDTELL_CONNECTOR_BIND_HOST/PORT      listen address (default 0.0.0.0:8000)
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from showAndTell.applications.onlyoffice.connector import OnlyOfficeConnector


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _extra_document_server_urls() -> list[str]:
    raw = os.environ.get("SHOWANDTELL_CONNECTOR_EXTRA_DOCUMENT_SERVER_URLS", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def build_connector() -> OnlyOfficeConnector:
    return OnlyOfficeConnector(
        Path(os.environ.get("SHOWANDTELL_CONNECTOR_STORAGE_DIR", "/data")),
        public_url=_require("SHOWANDTELL_CONNECTOR_PUBLIC_URL"),
        document_server_url=_require("SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL"),
        jwt_secret=_require("SHOWANDTELL_ONLYOFFICE_JWT_SECRET"),
        admin_token=_require("SHOWANDTELL_CONNECTOR_ADMIN_TOKEN"),
        extra_document_server_urls=_extra_document_server_urls(),
        command_url=os.environ.get("SHOWANDTELL_CONNECTOR_COMMAND_URL") or None,
    )


def main() -> None:
    connector = build_connector()
    uvicorn.run(
        connector.app,
        host=os.environ.get("SHOWANDTELL_CONNECTOR_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("SHOWANDTELL_CONNECTOR_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
