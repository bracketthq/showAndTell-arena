"""Deterministically seed the IMAP mailboxes displayed by Roundcube."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from email.message import EmailMessage
from email.policy import SMTP
import ssl as ssl_module
from typing import Any, Protocol
import imaplib


class IMAPConnection(Protocol):
    def starttls(self, ssl_context=None): ...
    def login(self, user: str, password: str): ...
    def select(self, mailbox: str): ...
    def search(self, charset, *criteria: str): ...
    def fetch(self, message_set: bytes, message_parts: str): ...
    def store(self, message_set: bytes, command: str, flags: str): ...
    def expunge(self): ...
    def append(self, mailbox: str, flags: str | None, date_time, message: bytes): ...
    def logout(self): ...


class IMAPSeeder:
    """One connection per mailbox; accounts are provisioned by the mail stack."""

    def __init__(
        self,
        host: str,
        port: int = 993,
        *,
        ssl: bool = True,
        starttls: bool = False,
        tls_context: ssl_module.SSLContext | None = None,
        factory: Callable[[str, int], IMAPConnection] | None = None,
    ) -> None:
        if not host:
            raise ValueError("IMAP host is required")
        if ssl and starttls:
            raise ValueError("IMAP implicit TLS and STARTTLS are mutually exclusive")
        self.host, self.port = host, port
        self.starttls = starttls
        self.tls_context = tls_context
        self.factory = factory or (imaplib.IMAP4_SSL if ssl else imaplib.IMAP4)

    @classmethod
    def _message(cls, spec: Mapping[str, Any]) -> bytes:
        # Captured mail is carried verbatim: it was composed in a real client
        # and re-deriving it from headers would lose everything the operator
        # actually built.  Authored seeds still declare headers plus a body.
        content = spec.get("content")
        if content is not None:
            if not isinstance(content, (bytes, bytearray)):
                raise ValueError("roundcube message content must be bytes")
            return bytes(content)
        headers = spec.get("headers")
        if not isinstance(headers, Mapping):
            raise ValueError("roundcube message headers must be an object")
        message = EmailMessage(policy=SMTP)
        for name, value in headers.items():
            message[str(name)] = str(value)
        if "Message-ID" not in message:
            # The id this seeder searches, deletes and exports by has to be the
            # id actually written. Leaving it to the author means an omission
            # delivers mail that looks right in the mailbox but that reset can
            # never find and export always reports missing.
            message["Message-ID"] = cls.message_id(spec)
        message.set_content(str(spec.get("body_text", "")))
        return message.as_bytes()

    @staticmethod
    def message_id(spec: Mapping[str, Any]) -> str:
        """The IMAP key for one message.

        Authored seeds mint a stable id from ``external_id``.  Captured mail
        already carries the Message-ID its client wrote, and that real value
        is what identifies it in the mailbox.
        """
        declared = spec.get("message_id")
        if declared:
            return str(declared)
        return f"<{spec['external_id']}@showAndTell.seed>"

    @staticmethod
    def _delete_message_id(conn: IMAPConnection, message_id: str) -> None:
        status, data = conn.search(None, "HEADER", "Message-ID", f'"{message_id}"')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed for {message_id}")
        for message_set in data or []:
            if message_set:
                conn.store(message_set, "+FLAGS", r"(\Deleted)")
        conn.expunge()

    def _connect(self, mailbox: Mapping[str, Any]) -> IMAPConnection:
        conn = self.factory(self.host, self.port)
        if self.starttls:
            status, _ = conn.starttls(ssl_context=self.tls_context)
            if status != "OK":
                raise RuntimeError(f"IMAP STARTTLS failed for {mailbox['email']}")
        status, _ = conn.login(str(mailbox["email"]), str(mailbox["password"]))
        if status != "OK":
            raise RuntimeError(f"IMAP login failed for {mailbox['email']}")
        status, _ = conn.select(str(mailbox.get("folder", "INBOX")))
        if status != "OK":
            raise RuntimeError(f"IMAP select failed for {mailbox['email']}")
        return conn

    def seed(self, block: Mapping[str, Any]) -> None:
        mailboxes = {str(row["email"]): row for row in block.get("mailboxes", [])}
        grouped: dict[str, list[Mapping[str, Any]]] = {email: [] for email in mailboxes}
        for message in block.get("messages", []):
            grouped.setdefault(str(message["mailbox"]), []).append(message)
        unknown = set(grouped) - set(mailboxes)
        if unknown:
            raise ValueError(f"messages target undeclared mailboxes: {sorted(unknown)}")
        for email, mailbox in mailboxes.items():
            conn = self._connect(mailbox)
            try:
                folder = str(mailbox.get("folder", "INBOX"))
                for message in grouped[email]:
                    external_id = str(message["external_id"])
                    self._delete_message_id(conn, self.message_id(message))
                    flags = r"(\Seen)" if message.get("seen") else None
                    status, _ = conn.append(folder, flags, None, self._message(message))
                    if status != "OK":
                        raise RuntimeError(f"IMAP append failed for {external_id}")
            finally:
                conn.logout()

    def reset(self, block: Mapping[str, Any]) -> None:
        mailboxes = {str(row["email"]): row for row in block.get("mailboxes", [])}
        grouped: dict[str, list[str]] = {email: [] for email in mailboxes}
        for message in block.get("messages", []):
            grouped.setdefault(str(message["mailbox"]), []).append(
                self.message_id(message))
        for email, mailbox in mailboxes.items():
            conn = self._connect(mailbox)
            try:
                for message_id in grouped[email]:
                    self._delete_message_id(conn, message_id)
            finally:
                conn.logout()

    def dump(self, mailbox: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return every message in one mailbox, verbatim.

        ``export`` answers "is the mail this task declared still there?".  This
        answers "what is in here?", which is the question a capture asks about
        a mailbox nobody declared in advance.
        """
        conn = self._connect(mailbox)
        try:
            status, data = conn.search(None, "ALL")
            if status != "OK":
                raise RuntimeError(f"IMAP search failed for {mailbox['email']}")
            rows: list[dict[str, Any]] = []
            for message_set in b" ".join(data or []).split():
                # BODY.PEEK[] rather than RFC822: a plain RFC822 fetch sets
                # \Seen, so reading a mailbox would rewrite the very read
                # state the capture is trying to record.
                status, fetched = conn.fetch(message_set, "(FLAGS BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError(
                        f"IMAP fetch failed for {message_set!r} in {mailbox['email']}")
                meta, raw = self._fetched(fetched)
                if raw is None:
                    continue
                rows.append({
                    "message_id": self._header(raw, "Message-ID"),
                    "raw": raw,
                    "seen": "\\Seen" in meta,
                })
            return rows
        finally:
            conn.logout()

    @staticmethod
    def _fetched(fetched) -> tuple[str, bytes | None]:
        """Split imaplib's mixed literal response into metadata and payload."""
        meta: list[bytes] = []
        payload: bytes | None = None
        for item in fetched or []:
            if isinstance(item, tuple):
                for index, part in enumerate(item):
                    if not isinstance(part, bytes):
                        continue
                    if index == 0:
                        meta.append(part)
                    elif payload is None:
                        payload = part
            elif isinstance(item, bytes):
                meta.append(item)
        return b" ".join(meta).decode("utf-8", "replace"), payload

    @staticmethod
    def _header(raw: bytes, name: str) -> str:
        prefix = f"{name.lower()}:"
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                break
            if line.lower().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    def export(self, block: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Report presence and flags for each task-owned stable Message-ID."""
        mailboxes = {str(row["email"]): row for row in block.get("mailboxes", [])}
        grouped: dict[str, list[Mapping[str, Any]]] = {
            email: [] for email in mailboxes}
        for message in block.get("messages", []):
            grouped.setdefault(str(message["mailbox"]), []).append(message)
        rows: list[dict[str, Any]] = []
        for email, mailbox in mailboxes.items():
            conn = self._connect(mailbox)
            try:
                for message in grouped[email]:
                    external_id = str(message["external_id"])
                    message_id = self.message_id(message)
                    status, data = conn.search(
                        None, "HEADER", "Message-ID", f'"{message_id}"')
                    if status != "OK":
                        raise RuntimeError(f"IMAP search failed for {message_id}")
                    matches = b" ".join(data or []).split()
                    flags: list[str] = []
                    if matches:
                        status, fetched = conn.fetch(matches[-1], "(FLAGS)")
                        if status != "OK":
                            raise RuntimeError(f"IMAP flags fetch failed for {message_id}")
                        chunks: list[bytes] = []
                        for item in fetched or []:
                            if isinstance(item, bytes):
                                chunks.append(item)
                            elif isinstance(item, tuple):
                                chunks.extend(part for part in item
                                              if isinstance(part, bytes))
                        raw = b" ".join(chunks).decode("utf-8", "replace")
                        flags = [token for token in raw.replace("(", " ").replace(
                            ")", " ").split() if token.startswith("\\")]
                    rows.append({"external_id": external_id, "mailbox": email,
                                 "present": bool(matches), "flags": sorted(set(flags))})
            finally:
                conn.logout()
        return rows
