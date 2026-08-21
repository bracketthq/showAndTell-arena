"""Roundcube's data plane: the mail behind the webmail.

A mailbox an operator fills during a capture's setup phase exists only inside
Dovecot.  A declarative export can only describe mail the task itself planted,
so it reports nothing for mail nobody declared -- which is how setup work gets
silently dropped.  ``capture`` asks the mailbox what is actually in it and
writes every message into the draft verbatim.
"""
from __future__ import annotations

import hashlib
import ssl
from datetime import datetime
from email.utils import format_datetime
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from showAndTell.applications.roundcube.imap import IMAPSeeder
from showAndTell.applications.authoring.seedform import Field, SeedForm

ASSET_DIR = "roundcube"


def _permissive_tls_context() -> ssl.SSLContext:
    """Trust the pinned Dovecot container's ephemeral local certificate only."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class State:
    application = "roundcube"

    # A mail client composes outgoing mail. Nothing in Roundcube can fabricate
    # a message arriving from a supplier, which is what a task is usually
    # about, so that is the one thing this form exists to add.
    SEED_FORM = SeedForm(
        title="Inbox",
        row_noun="message",
        help=("Each message is delivered straight into the mailbox, so it "
              "appears in Roundcube immediately and is captured with the rest "
              "of the setup."),
        fields=(
            Field("sender", "From", placeholder="Ops <ops@acme.test>",
                  required=True,
                  help="A display name and address, as it should read in the list."),
            Field("subject", "Subject", required=True),
            Field("body", "Message", kind="textarea"),
            Field("sent_at", "Received", kind="datetime",
                  help="Fixed, so the inbox orders the same way on every run. "
                       "Read as UTC."),
            Field("unread", "Leave unread", kind="checkbox", default=True),
        ),
    )

    def __init__(self, manifest, *,
                 seeder_factory: Callable[[Any], IMAPSeeder] | None = None) -> None:
        self.manifest = manifest
        self._seeder_factory = seeder_factory
        self._last_block: dict[str, Any] | None = None

    # -- plumbing ----------------------------------------------------------
    def _seeder(self, ctx) -> IMAPSeeder:
        """Connect to Dovecot using what the driver reported about it.

        STARTTLS is stated by the driver rather than inferred: this plane runs
        beside the harness and cannot see whether the container it is talking
        to is a managed one or somebody's external mail server.
        """
        if self._seeder_factory is not None:
            return self._seeder_factory(ctx)
        host = ctx.secrets.get("imap_host") or ctx.host
        port = int(ctx.secrets.get("imap_port")
                   or self.manifest.ports["imap"].host)
        starttls = bool(ctx.secrets.get("imap_starttls", True))
        return IMAPSeeder(
            host, port, ssl=False, starttls=starttls,
            tls_context=_permissive_tls_context() if starttls else None)

    @staticmethod
    def _mailbox(ctx) -> dict[str, str]:
        return {"email": ctx.credentials["email"],
                "password": ctx.credentials["password"],
                "folder": "INBOX"}

    @staticmethod
    def _resolve(ctx, message: Mapping[str, Any]) -> dict[str, Any]:
        """Give one message its bytes, from a task asset or from memory.

        A promoted task carries digest-checked files under its demo root; a
        viewer trial restores in-session, before a draft has a root at all.
        """
        if message.get("content") is not None or message.get("asset") is None:
            return dict(message)
        return {**message,
                "content": ctx.asset(message["asset"], message.get("sha256", ""))}

    # -- contract ----------------------------------------------------------
    def prepare(self, ctx) -> None:
        """Provision the capture mailbox so the surface opens on a real inbox.

        Dovecot's testing configuration creates a mailbox on first successful
        login, so connecting as the manifest's account is what brings it into
        existence before managed Chrome tries to sign in.
        """
        self._seeder(ctx).dump(self._mailbox(ctx))
        self._last_block = None

    def reset(self, ctx) -> None:
        if self._last_block is not None:
            self._seeder(ctx).reset(self._last_block)
        self._last_block = None

    def seed(self, ctx, block: Mapping[str, Any]) -> None:
        if not isinstance(block, Mapping):
            raise ValueError("roundcube seed block must be an object")
        resolved = {
            "mailboxes": list(block.get("mailboxes") or [self._mailbox(ctx)]),
            "messages": [self._resolve(ctx, row)
                         for row in block.get("messages", [])],
        }
        self._seeder(ctx).seed(resolved)
        self._last_block = resolved

    def form_block(self, ctx, rows) -> dict[str, Any]:
        """Turn seed-form rows into this application's own seed block.

        Only Roundcube knows a row is a message, addressed to the mailbox the
        manifest names, keyed by an id derived from its position so re-applying
        the same form replaces rather than duplicates.
        """
        mailbox = self._mailbox(ctx)
        messages = []
        for index, row in enumerate(rows, 1):
            external_id = f"authored-{index:04d}"
            headers = {"From": row["sender"], "To": mailbox["email"],
                       "Subject": row["subject"]}
            if row.get("sent_at"):
                # A mail Date is RFC 2822, which is this application's business
                # rather than the form's; the form hands over an ISO instant.
                headers["Date"] = format_datetime(
                    datetime.fromisoformat(row["sent_at"]))
            messages.append({
                "external_id": external_id,
                "mailbox": mailbox["email"],
                "folder": mailbox["folder"],
                "headers": headers,
                "body_text": row.get("body", ""),
                "seen": not row.get("unread", True),
            })
        return {"mailboxes": [mailbox], "messages": messages}

    def export(self, ctx) -> dict[str, Any]:
        if self._last_block is None:
            return {"messages": []}
        return {"messages": self._seeder(ctx).export(self._last_block)}

    def capture(self, ctx, directory: Path | None) -> dict[str, Any]:
        """Copy the operator's mailbox into the draft as verbatim .eml assets."""
        mailbox = self._mailbox(ctx)
        dumped = self._seeder(ctx).dump(mailbox)
        target = Path(directory) / ASSET_DIR if directory is not None else None
        if target is not None and dumped:
            target.mkdir(parents=True, exist_ok=True)
        messages: list[dict[str, Any]] = []
        for index, row in enumerate(dumped, 1):
            raw = row["raw"]
            if target is not None:
                (target / f"{index:04d}.eml").write_bytes(raw)
            messages.append({
                # external_id labels the asset; message_id is the real key the
                # mailbox is searched by, so nothing here is invented.
                "external_id": f"captured-{index:04d}",
                "message_id": row["message_id"],
                "mailbox": mailbox["email"],
                "folder": mailbox["folder"],
                "asset": f"{ASSET_DIR}/{index:04d}.eml",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "seen": bool(row["seen"]),
            })
        if not messages:
            return {}
        return {"mailboxes": [mailbox], "messages": messages}
