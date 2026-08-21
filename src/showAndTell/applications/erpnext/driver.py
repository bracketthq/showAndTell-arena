"""Unified ERPNext + HRMS driver.

The expensive part of an ERPNext fixture is never the container: it is
`bench new-site` / `bench reinstall`.  This driver does that work once,
freezes the result as a named snapshot, and makes restore a database
load. HRMS is installed on the same Frappe site and repaired after restores,
including restores of older ERP-only snapshots. Containers and images are
never rebuilt here.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tarfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from showAndTell.applications.host.protocol import CommandRunner, DriverError, bind_host

SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


class ErpnextDriver:
    name = "erpnext"
    WORKERS = ("queue-long", "queue-short", "scheduler")
    # Long-lived web processes ("erpnext" is the nginx frontend service).
    # Restarted after a restore so no gunicorn worker keeps serving its
    # pre-restore in-process cache.
    WEB_SERVICES = ("backend", "websocket", "erpnext")

    def __init__(self, manifest=None, *, root: Path | None = None,
                 runner: CommandRunner | None = None,
                 compose_file: Path | None = None,
                 project: str | None = None,
                 port: int | None = None,
                 probe: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.manifest = manifest
        self.name = manifest.name if manifest is not None else "erpnext"
        self.root = Path(root or Path.home() / ".showAndTell" / "fixture-host")
        self.runner = runner or CommandRunner()
        self.compose_file = compose_file or manifest.compose_file
        self.project = project or os.environ.get(
            "SHOWANDTELL_ERPNEXT_PROJECT", "showandtell-erpnext")
        self.site = os.environ.get("SHOWANDTELL_ERPNEXT_SITE", "frontend")
        # Explicit kwarg > environment > manifest: the agent addresses each
        # replica by kwarg, so a host-level env override moves only the base
        # instance and can never fold every replica onto one port.
        self.port = int(port if port is not None else os.environ.get(
            "SHOWANDTELL_ERPNEXT_PORT",
            manifest.ports["app"].host if manifest is not None else 8080))
        self.bind_host = bind_host(manifest.name)
        self.db_password = os.environ.get(
            "SHOWANDTELL_ERPNEXT_DB_ROOT_PASSWORD", "showAndTell-db-local-only")
        self.admin_password = os.environ.get(
            "SHOWANDTELL_ERPNEXT_ADMIN_PASSWORD",
            manifest.credentials["password"] if manifest is not None else "showAndTell-admin")
        self.snapshots = self.root / "snapshots" / self.name
        self._probe = probe or self._http_probe
        self._sleep = sleep

    # -- plumbing -----------------------------------------------------------
    def _compose_env(self) -> dict[str, str]:
        return {
            "SHOWANDTELL_ERPNEXT_BIND_HOST": self.bind_host,
            "SHOWANDTELL_ERPNEXT_PORT": str(self.port),
            "SHOWANDTELL_ERPNEXT_DB_ROOT_PASSWORD": self.db_password,
            "SHOWANDTELL_ERPNEXT_ADMIN_PASSWORD": self.admin_password,
        }

    def _dc(self, *args: str, stdin_bytes: bytes | None = None,
            timeout: float = 900.0):
        argv = ["docker", "compose", "-p", self.project,
                "-f", str(self.compose_file), *args]
        return self.runner.run(argv, stdin_bytes=stdin_bytes,
                               env=self._compose_env(), timeout=timeout)

    def _exec(self, service: str, *args: str, stdin_bytes: bytes | None = None,
              env: dict[str, str] | None = None, timeout: float = 900.0):
        extra = []
        for key, value in (env or {}).items():
            extra += ["-e", f"{key}={value}"]
        return self._dc("exec", "-T", *extra, service, *args,
                        stdin_bytes=stdin_bytes, timeout=timeout)

    def _bench(self, *args: str):
        return self._exec("backend", "bench", "--site", self.site, *args)

    def _http_probe(self, url: str) -> bool:
        try:
            return httpx.get(url, timeout=5.0).status_code < 500
        except httpx.HTTPError:
            return False

    def _db_name(self) -> str:
        result = self._exec(
            "backend", "sh", "-c", f"cat sites/{self.site}/site_config.json")
        try:
            return json.loads(result.stdout.decode())["db_name"]
        except (ValueError, KeyError) as exc:
            raise DriverError("could not resolve the site database name") from exc

    def _tool(self, candidates: str) -> str:
        result = self._exec("db", "sh", "-c", f"command -v {candidates}")
        found = result.stdout.decode().strip().splitlines()
        if not found:
            raise DriverError(f"none of `{candidates}` exist in the db container")
        return Path(found[0]).name

    def _snapshot_dir(self, name: str) -> Path:
        if not SNAPSHOT_NAME.fullmatch(name):
            raise DriverError(f"invalid snapshot name: {name}")
        return self.snapshots / name

    # -- lifecycle ------------------------------------------------------------
    def start(self, *, wait: bool = True) -> None:
        # The shared ShowAndTell image is built by the backend service.  Without
        # building it first, a fully pruned Docker store can make Compose try
        # to resolve that local tag from a registry for the sibling services
        # and wait indefinitely instead of using the checked-in Dockerfile.
        # A separate build also avoids Compose's parallel up/build planner
        # racing those sibling pulls ahead of the backend image export.
        self._dc("build", "backend", timeout=1800.0)
        self._dc("up", "-d", "--quiet-pull", timeout=1800.0)
        if wait:
            self._wait_healthy()

    def _wait_healthy(self, budget_s: float = 1200.0) -> None:
        deadline = time.monotonic() + budget_s
        while not self._probe(f"http://127.0.0.1:{self.port}/login"):
            if time.monotonic() >= deadline:
                raise DriverError(f"erpnext did not become healthy on :{self.port}")
            self._sleep(3.0)

    def stop(self) -> None:
        self._dc("stop", timeout=300.0)

    def status(self) -> dict:
        healthy = self._probe(f"http://127.0.0.1:{self.port}/login")
        names = sorted(p.name for p in self.snapshots.glob("*")
                       if p.is_dir()) if self.snapshots.is_dir() else []
        return {"state": "healthy" if healthy else "unreachable",
                "ports": {"app": self.port}, "snapshots": names}

    # -- golden state -----------------------------------------------------------
    def baseline(self) -> None:
        self._ensure_hrms_installed()
        self._bench("execute",
                    "erpnext.setup.setup_wizard.setup_wizard.stage_fixtures",
                    "--kwargs", '{"args":{"country":"United States"}}')
        self._bench("execute", "frappe.db.set_single_value", "--args",
                    '["Accounts Settings","enable_discounts_and_margin",1]')
        self._bench("execute",
                    "erpnext.accounts.doctype.accounts_settings.accounts_settings."
                    "toggle_sales_discount_section", "--args", "[false]")
        self._complete_setup()

    def _ensure_hrms_installed(self) -> None:
        """Install HRMS when an older ERP-only site or snapshot is encountered."""
        self._exec(
            "backend", "bash", "-c",
            'bench --site "$1" list-apps | grep -Eq \'^hrms([[:space:]]|$)\' '
            '|| { bench --site "$1" migrate && '
            'bench --site "$1" install-app hrms; }',
            "showAndTell-install-hrms", self.site,
        )

    def _complete_setup(self) -> None:
        """Make all applications agree that the shared site is configured."""
        # Frappe v16 derives setup completion from one child row per installed
        # app; without these rows Desk redirects into the setup wizard.
        self._bench("execute",
                    'frappe.get_single("Installed Applications").update_versions')
        for app in ("frappe", "erpnext", "hrms"):
            self._bench("execute", "frappe.db.set_value", "--args",
                        f'["Installed Application",{{"app_name":"{app}"}},'
                        f'"is_setup_complete",1]')
        self._bench(
            "execute",
            "frappe.desk.page.setup_wizard.setup_wizard.run_post_setup_complete",
            "--kwargs", '{"args":{}}')
        # Frappe's post-setup hook unconditionally enables onboarding.  That
        # is correct for an interactive first login, but this fixture restores
        # an already-prepared Desk and the resulting Getting Started panel can
        # cover replay targets.  Reassert the fixture invariant after the hook
        # so both baseline creation and every snapshot restore finish clean.
        self._bench(
            "execute", "frappe.db.set_single_value", "--args",
            '["System Settings","enable_onboarding",0]')
        self._bench("clear-cache")

    def golden(self) -> None:
        self.snapshot("golden")

    def reset(self) -> None:
        self.restore("golden")

    # -- snapshots ------------------------------------------------------------
    def snapshot(self, name: str) -> None:
        target = self._snapshot_dir(name)
        db = self._db_name()
        target.mkdir(parents=True, exist_ok=True)
        # Quiesce workers so nothing writes between the dump and the file tar.
        self._dc("stop", *self.WORKERS, timeout=300.0)
        try:
            # Stage to temp files: a failed dump must not overwrite a known-good snapshot.
            dump = self._exec(
                "db", self._tool("mariadb-dump mysqldump"), "-uroot",
                "--single-transaction", "--routines", "--triggers", db,
                env={"MYSQL_PWD": self.db_password})
            sql_path_new = target / "site.sql.gz.new"
            sql_path_new.write_bytes(gzip.compress(dump.stdout))
            files = self._exec("backend", "tar", "czf", "-", "-C", "sites", self.site)
            files_path_new = target / "site-files.tar.gz.new"
            files_path_new.write_bytes(files.stdout)
            # Verify both archives before committing them into place.
            try:
                with gzip.open(sql_path_new, 'rb') as gz:
                    gz.read(8192)  # Read a chunk to validate the gzip stream
                with gzip.open(files_path_new, 'rb') as gz:
                    gz.read(8192)  # Read a chunk to validate the gzip stream
            except Exception as exc:
                sql_path_new.unlink(missing_ok=True)
                files_path_new.unlink(missing_ok=True)
                raise DriverError("snapshot produced corrupt archive(s)") from exc
            # Move into final place on success.
            os.replace(sql_path_new, target / "site.sql.gz")
            os.replace(files_path_new, target / "site-files.tar.gz")
            (target / "db_name").write_text(db + "\n")
            (target / "taken_at").write_text(
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
        finally:
            self._dc("start", *self.WORKERS, timeout=300.0)

    def _grant_site_user(self, sql_tool: str, db: str) -> None:
        """Give the restored site's database user a grant on THIS host.

        A snapshot carries the site's ``site_config.json``, whose ``db_user``
        and ``db_password`` are what MariaDB was told when the site was
        created — on whatever machine created it. The grant itself lives in
        ``mysql.user``, which no snapshot contains, so a snapshot taken on the
        fixture VM and restored on a laptop loads a database its own site
        cannot log into, and every bench command after it fails. Recreating
        the grant here makes restore host-independent; on the host that took
        the snapshot it re-asserts what is already true.
        """
        result = self._exec(
            "backend", "sh", "-c", f"cat sites/{self.site}/site_config.json")
        try:
            config = json.loads(result.stdout.decode())
        except ValueError as exc:
            raise DriverError(
                "restored site_config.json is unreadable") from exc
        user = str(config.get("db_user") or db)
        password = config.get("db_password")
        if not password:
            raise DriverError("restored site_config.json carries no db_password")
        quoted = str(password).replace("\\", "\\\\").replace("'", "\\'")
        identifier = user.replace("`", "``")
        self._exec("db", sql_tool, "-uroot", "-e", ";".join((
            f"CREATE USER IF NOT EXISTS `{identifier}`@`%` IDENTIFIED BY '{quoted}'",
            f"ALTER USER `{identifier}`@`%` IDENTIFIED BY '{quoted}'",
            f"GRANT ALL PRIVILEGES ON `{db.replace('`', '``')}`.* "
            f"TO `{identifier}`@`%`",
            "FLUSH PRIVILEGES",
        )), env={"MYSQL_PWD": self.db_password})

    def _terminate_site_connections(self, sql_tool: str, db: str) -> None:
        """Release metadata locks before replacing the snapshot database.

        Long-lived gunicorn workers keep idle MariaDB sessions open.  MariaDB
        then waits indefinitely for their metadata locks when reset issues
        ``DROP DATABASE``.  Kill only sessions currently attached to the site
        database; the web processes are restarted later in the restore.
        """
        quoted = db.replace("\\", "\\\\").replace("'", "\\'")
        result = self._exec(
            "db", sql_tool, "-uroot", "-N", "-B", "-e",
            "SELECT ID FROM information_schema.PROCESSLIST "
            f"WHERE DB = '{quoted}' AND ID <> CONNECTION_ID()",
            env={"MYSQL_PWD": self.db_password},
        )
        connection_ids = []
        for raw_id in result.stdout.decode().splitlines():
            connection_id = raw_id.strip()
            if not connection_id:
                continue
            if not connection_id.isdigit():
                raise DriverError(
                    f"MariaDB returned an invalid connection id: {connection_id!r}")
            connection_ids.append(connection_id)
        if not connection_ids:
            return
        # One exec for the whole set: `docker compose exec` costs about a second
        # of CLI + daemon overhead, and a restore routinely finds tens of idle
        # gunicorn sessions. Every id is digits-only, checked above.
        self._exec(
            "db", sql_tool, "-uroot", "-e",
            ";".join(f"KILL {cid}" for cid in connection_ids),
            env={"MYSQL_PWD": self.db_password},
        )

    def restore(self, name: str) -> None:
        source = self._snapshot_dir(name)
        if not (source / "site.sql.gz").is_file():
            raise DriverError(f"no snapshot named '{name}' — snapshot it first")
        # Verify both archives before touching anything: restore destroys the
        # current site, so a truncated copy would leave nothing to fall back to.
        try:
            sql = gzip.decompress((source / "site.sql.gz").read_bytes())
            # Validate site-files.tar.gz is readable gzip data by reading through it.
            files_gzipped = (source / "site-files.tar.gz").read_bytes()
            with gzip.open(source / "site-files.tar.gz", 'rb') as gz:
                # Read a chunk to validate the gzip stream is readable
                gz.read(8192)
            files = files_gzipped  # Use the compressed bytes for streaming restore
        except Exception as exc:
            raise DriverError(f"snapshot '{name}' is corrupt — refusing") from exc
        db = (source / "db_name").read_text().strip()
        sql_tool = self._tool("mariadb mysql")
        self._dc("stop", *self.WORKERS, timeout=300.0)
        try:
            self._terminate_site_connections(sql_tool, db)
            self._exec("db", sql_tool, "-uroot", "-e",
                       f"DROP DATABASE IF EXISTS `{db}`; CREATE DATABASE `{db}`;",
                       env={"MYSQL_PWD": self.db_password})
            self._exec("db", sql_tool, "-uroot", db, stdin_bytes=sql,
                       env={"MYSQL_PWD": self.db_password})
            self._exec("backend", "sh", "-c", f"rm -rf sites/{self.site}")
            self._exec("backend", "tar", "xzf", "-", "-C", "sites",
                       stdin_bytes=files)
            # The site files are in place, so the snapshot's own credentials
            # are readable — and must exist in this MariaDB before any bench
            # command tries to use them.
            self._grant_site_user(sql_tool, db)
            # Frappe caches doctype metadata and the boot payload in Redis; a
            # restored database behind a stale cache serves the old schema.
            self._exec("redis-cache", "redis-cli", "FLUSHALL")
            self._exec("redis-queue", "redis-cli", "FLUSHALL")
            self._bench("clear-cache")
            # Portable snapshots may have been captured before the fixture was
            # unified. Upgrade those ERP-only databases in place, then repair
            # setup flags before any browser can receive a cached boot payload.
            self._ensure_hrms_installed()
            self._complete_setup()
            # Portable task snapshots can predate a fixture credential change.
            # The SQL restore also restores the old Administrator password, so
            # reapply the current manifest credential at the restore boundary.
            self._bench("set-admin-password", self.admin_password)
            # The database was swapped under long-running gunicorn workers,
            # and Frappe v16's per-process client_cache outlives a redis
            # FLUSHALL (the invalidation counters vanish rather than bump).
            # A stale worker then serves pre-restore docs — Installed
            # Applications' is_setup_complete among them — which makes the
            # desk reload-loop through the setup wizard. Restart the web
            # processes so every worker rebuilds from the restored site.
            self._dc("restart", *self.WEB_SERVICES, timeout=300.0)
        finally:
            self._dc("start", *self.WORKERS, timeout=300.0)
        self._wait_healthy()

    def fetch_snapshot(self, name: str) -> Path:
        source = self._snapshot_dir(name)
        if not (source / "site.sql.gz").is_file():
            raise DriverError(f"no snapshot named '{name}'")
        # Instance-qualified so two replicas fetching the same snapshot name
        # never write over each other (the base keeps "erpnext-<name>.tar").
        out = self.root / "outgoing" / f"{self.name}-{name}.tar"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w") as tar:
            for item in sorted(source.iterdir()):
                tar.add(item, arcname=item.name)
        return out

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        target = self._snapshot_dir(name)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        with tarfile.open(tar_path) as tar:
            tar.extractall(target, filter="data")
        if not (target / "site.sql.gz").is_file():
            shutil.rmtree(target)
            raise DriverError("uploaded snapshot is missing site.sql.gz")

    def secrets(self) -> dict:
        return {"admin_password": self.admin_password}


Driver = ErpnextDriver
