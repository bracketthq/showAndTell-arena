"""ONLYOFFICE's data plane: the workbooks behind the editor.

The connector holds the authoritative documents; Document Server holds only
cached conversions.  A workbook the operator built in the browser lives inside
an editing session until it is told to save, so ``capture`` forces the save and
copies the exact bytes out -- re-describing the sheet as rows would drop the
styles, merges and number formats that make it the sheet that was demonstrated.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from showAndTell.applications.onlyoffice.connector import ConnectorClient

ASSET_DIR = "onlyoffice"

# A Docs surface with no document is a file manager, not an editor. This blank
# workbook is what the operator actually starts typing into.
BLANK_WORKBOOK = {
    "schema_version": 1,
    "workbooks": [{
        "document_id": "task-setup",
        "filename": "Task Setup.xlsx",
        "format": "xlsx",
        "source": {"tab": "Sheet1", "columns": [], "rows": []},
    }],
}


class State:
    application = "onlyoffice"

    def __init__(self, manifest, *, client_factory=None) -> None:
        self.manifest = manifest
        self._client_factory = client_factory
        self.document_ids: tuple[str, ...] = ()

    def _client(self, ctx) -> ConnectorClient:
        if self._client_factory is not None:
            return self._client_factory(ctx)
        return ConnectorClient(self.connector_url(ctx),
                               str(ctx.secrets.get("connector_token", "")))

    def connector_url(self, ctx) -> str:
        """Where the editor lives — the connector, not Document Server."""
        declared = ctx.secrets.get("connector_url")
        if declared:
            return str(declared).rstrip("/")
        # The driver's reported port wins over the manifest default: only the
        # driver knows whether an override moved it.
        port = ctx.secrets.get("connector_port") or \
            self.manifest.ports["connector"].host
        return f"http://{ctx.host}:{port}"

    def editor_url(self, ctx, document_id: str | None = None) -> str:
        document_id = document_id or (self.document_ids[0] if self.document_ids else "")
        if not document_id:
            raise RuntimeError("no ONLYOFFICE workbook has been registered")
        return f"{self.connector_url(ctx)}/onlyoffice/editor/{document_id}"

    def browser_metadata(self, ctx) -> dict[str, str]:
        if not self.document_ids:
            return {}
        return {
            "browser_url": self.editor_url(ctx),
            "document_id": self.document_ids[0],
        }

    # -- contract ----------------------------------------------------------
    def prepare(self, ctx) -> None:
        self.document_ids = tuple(self._client(ctx).register(BLANK_WORKBOOK))

    def reset(self, ctx) -> None:
        self._client(ctx).clear()
        self.document_ids = ()

    @staticmethod
    def _resolve(ctx, workbook: Mapping[str, Any]) -> dict[str, Any]:
        """Give one workbook its bytes, from a task asset or from memory.

        A declarative workbook (``source``) and an in-session one (``content``)
        are already complete; only an ``asset`` needs the digest-checked read.
        """
        if (workbook.get("content") is not None
                or workbook.get("source") is not None
                or workbook.get("asset") is None):
            return dict(workbook)
        return {
            "document_id": workbook["document_id"],
            "filename": workbook.get("title") or f"{workbook['document_id']}.xlsx",
            "format": workbook.get("format", "xlsx"),
            "content": ctx.asset(workbook["asset"], workbook.get("sha256", "")),
        }

    def seed(self, ctx, block: Mapping[str, Any]) -> None:
        if not isinstance(block, Mapping):
            raise ValueError("onlyoffice seed block must be an object")
        workbooks = [self._resolve(ctx, row) for row in block.get("workbooks", [])]
        if not workbooks:
            return
        self.document_ids = tuple(self._client(ctx).register({
            "schema_version": block.get("schema_version", 1),
            "workbooks": workbooks,
        }))

    def export(self, ctx) -> dict[str, Any]:
        return {"documents": self._client(ctx).documents()}

    def capture(self, ctx, directory: Path | None) -> dict[str, Any]:
        """Flush live editors so the sheet the operator built joins the draft."""
        if directory is None:
            return {}
        rows = self._client(ctx).capture_documents(Path(directory) / ASSET_DIR)
        if not rows:
            return {}
        return {"workbooks": [{**row, "asset": f"{ASSET_DIR}/{row['asset']}"}
                              for row in rows]}
