"""Roundcube driver: webmail over a Dovecot store and local SMTP sink.

Three services, two of which hold transient state. Dovecot's ``/srv/vmail``
is the mailbox; Roundcube's sqlite database is prefs and a message cache;
Mailpit receives outbound SMTP. Reset clears all three, because a reused
fixture must never expose a previous capture's mail. There is no expensive
install to freeze and the seed carries mail declaratively, so the snapshot
family is Unsupported.
"""
from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from showAndTell.applications.host.protocol import (
    CommandRunner, DriverError, Unsupported, bind_host)

ROUNDCUBE_DB = "/var/roundcube/db"


def _default_imap_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5.0) as sock:
            return sock.recv(4).startswith(b"* OK")
    except OSError:
        return False


class RoundcubeDriver:
    SERVICES = ("dovecot", "roundcube", "mailpit")

    def __init__(self, manifest=None, *, root: Path | None = None,
                 runner: CommandRunner | None = None,
                 compose_file: Path | None = None,
                 project: str | None = None,
                 port: int | None = None,
                 imap_port: int | None = None,
                 probe: Callable[[str], bool] | None = None,
                 imap_probe: Callable[[str, int], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.manifest = manifest
        self.name = manifest.name if manifest is not None else "roundcube"
        self.root = Path(root or Path.home() / ".showAndTell" / "fixture-host")
        self.runner = runner or CommandRunner()
        self.compose_file = compose_file or (
            manifest.compose_file if manifest is not None
            else Path(__file__).with_name("compose.yaml")
        )
        self.project = project or os.environ.get(
            "SHOWANDTELL_ROUNDCUBE_PROJECT", "showandtell-roundcube")
        # Explicit kwargs > environment > manifest (see ErpnextDriver).
        self.port = int(port if port is not None else os.environ.get(
            "SHOWANDTELL_ROUNDCUBE_PORT",
            manifest.ports["app"].host if manifest is not None else 8082))
        self.imap_port = int(imap_port if imap_port is not None
                             else os.environ.get(
                                 "SHOWANDTELL_IMAP_PORT",
                                 manifest.ports["imap"].host
                                 if manifest is not None else 1143))
        self.bind_host = bind_host(manifest.name if manifest is not None else "roundcube")
        # The manifest is the single source of truth for the credential the
        # capture surface types in; Dovecot must accept exactly that one.
        self.mail_password = os.environ.get(
            "SHOWANDTELL_MAIL_PASSWORD",
            manifest.credentials["password"] if manifest is not None else "showAndTell-mail")
        # doveadm addresses a mailbox by account, and this is the account the
        # capture surface signs in as.
        self.mail_user = (
            manifest.credentials["email"] if manifest is not None
            else "agent@showAndTell.test"
        )
        self._probe = probe or self._http_probe
        self._imap_probe = imap_probe or _default_imap_probe
        self._sleep = sleep

    def _compose_env(self) -> dict[str, str]:
        return {
            "SHOWANDTELL_ROUNDCUBE_BIND_HOST": self.bind_host,
            "SHOWANDTELL_ROUNDCUBE_PORT": str(self.port),
            "SHOWANDTELL_MAIL_BIND_HOST": self.bind_host,
            "SHOWANDTELL_IMAP_PORT": str(self.imap_port),
            "SHOWANDTELL_MAIL_PASSWORD": self.mail_password,
        }

    def _dc(self, *args: str, timeout: float = 900.0, check: bool = True):
        argv = ["docker", "compose", "-p", self.project,
                "-f", str(self.compose_file), *args]
        return self.runner.run(argv, env=self._compose_env(), timeout=timeout,
                               check=check)

    def _http_probe(self, url: str) -> bool:
        try:
            return httpx.get(url, timeout=5.0).status_code < 400
        except httpx.HTTPError:
            return False

    def _healthy(self) -> bool:
        return (self._probe(f"http://127.0.0.1:{self.port}/")
                and self._imap_probe("127.0.0.1", self.imap_port))

    def start(self, *, wait: bool = True) -> None:
        self._dc("up", "-d", "--quiet-pull", timeout=1800.0)
        if wait:
            self._wait_healthy()

    def _wait_healthy(self, budget_s: float = 300.0) -> None:
        deadline = time.monotonic() + budget_s
        while not self._healthy():
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"Roundcube did not become healthy\n{self._get_diagnostics()}")
            self._sleep(3.0)

    def _get_diagnostics(self) -> str:
        try:
            result = self._dc("ps", timeout=60, check=False)
            output = result.stdout.decode("utf-8", errors="replace").strip()
            if not output:
                output = result.stderr.decode("utf-8", errors="replace").strip()
            if len(output) > 500:
                output = output[:500] + "..."
            return f"Docker Compose State:\n{output}"
        except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the timeout
            return f"Failed to get diagnostics: {exc}"

    def stop(self) -> None:
        self._dc("stop", timeout=300.0)

    def status(self) -> dict:
        return {"state": "healthy" if self._healthy() else "unreachable",
                "ports": {"app": self.port, "imap": self.imap_port}}

    def reset(self) -> None:
        """Empty the mailbox and Roundcube's cache of it.

        The mail is expunged through ``doveadm`` rather than by deleting
        ``/srv/vmail``: the pinned Dovecot image ships no shell utilities at
        all — no ``rm``, no ``ls`` — so a filesystem wipe silently succeeds
        while doing nothing, and every capture inherits the previous one's
        inbox.  ``-u`` rather than ``-A`` because the testing image's passdb
        accepts any account and can list none of them.

        Roundcube's sqlite database holds a message list for mail that no
        longer exists, so it is cleared too. Restarting Mailpit clears its
        in-memory SMTP capture alongside the two mailbox services.
        """
        self._dc("exec", "-T", "dovecot", "doveadm", "expunge",
                 "-u", self.mail_user, "mailbox", "*", "all", check=False)
        self._dc("exec", "-T", "roundcube", "sh", "-c",
                 f"rm -rf {ROUNDCUBE_DB}/* 2>/dev/null || true")
        self._dc("restart", *self.SERVICES, timeout=300.0)
        self._wait_healthy()

    # Mail is carried declaratively by the task seed, so there is no state
    # worth freezing here and nothing an expensive install would justify.
    def baseline(self) -> None:
        raise Unsupported("roundcube has no baseline stage")

    def golden(self) -> None:
        raise Unsupported("roundcube has no golden state")

    def snapshot(self, name: str) -> None:
        raise Unsupported("roundcube has no task state to snapshot")

    def fetch_snapshot(self, name: str) -> Path:
        raise Unsupported("roundcube has no snapshots")

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        raise Unsupported("roundcube has no snapshots")

    def restore(self, name: str) -> None:
        raise Unsupported("roundcube has no snapshots")

    def secrets(self) -> dict:
        # imap_starttls is stated rather than derived: an agent-backed fixture
        # is assume_running, which the family hooks read as "external, use
        # plaintext" -- wrong for a container this driver started itself.
        return {"mail_password": self.mail_password,
                "imap_port": self.imap_port,
                "imap_starttls": True}


Driver = RoundcubeDriver
