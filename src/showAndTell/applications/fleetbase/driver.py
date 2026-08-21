"""Lifecycle for the pinned Fleetbase Core + Fleet-Ops deployment."""
from __future__ import annotations

import gzip
import os
import re
import shutil
import tarfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from showAndTell.applications.fleetbase.bootstrap import bootstrap_fleetbase
from showAndTell.applications.fleetbase.demo_dataset import render_enterprise_seed_sql
from showAndTell.applications.lifecycle.compose import ComposeDriver
from showAndTell.applications.lifecycle.public_url import validate_public_url
from showAndTell.applications.host.protocol import DriverError


FLEETOPS_TESTING_SEEDERS = (
    "Fleetbase\\FleetOps\\Seeders\\Testing\\FleetSeeder",
    "Fleetbase\\FleetOps\\Seeders\\Testing\\OrdersSeeder",
    "Fleetbase\\FleetOps\\Seeders\\Testing\\ConnectivitySeeder",
    "Fleetbase\\FleetOps\\Seeders\\Testing\\MaintenanceSeeder",
)
PURGE_FLEETOPS_TESTING_ORDERS_SQL = b"""
DELETE FROM orders
WHERE JSON_UNQUOTE(JSON_EXTRACT(meta, '$.seed')) = 'fleetops-testing';
"""
SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
STORAGE_PATH = "/fleetbase/api/storage/app"


class FleetbaseDriver(ComposeDriver):
    boot_timeout = 1200.0
    deploy_timeout = 1200.0

    def __init__(
        self,
        *args,
        bootstrap: Callable[..., bool] = bootstrap_fleetbase,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.api_port = int(
            os.environ.get(
                f"SHOWANDTELL_{self.name.upper()}_API_PORT",
                self.manifest.ports["api"].host,
            )
        )
        self.socket_port = int(
            os.environ.get(
                f"SHOWANDTELL_{self.name.upper()}_SOCKET_PORT",
                self.manifest.ports["socket"].host,
            )
        )
        self._bootstrap = bootstrap
        self.db_root_password = os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_DB_ROOT_PASSWORD",
            "showAndTell-fleetbase-root-local-only",
        )
        self.snapshots = self.root / "snapshots" / self.name

    def _browser_origin(self) -> tuple[str, str]:
        parsed = urlparse(self.public_url)
        if not parsed.hostname:  # validate_public_url already enforces this.
            raise ValueError("Fleetbase public URL has no hostname")
        return parsed.scheme, parsed.hostname

    @property
    def api_public_url(self) -> str:
        configured = os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_API_PUBLIC_URL"
        )
        if configured:
            return validate_public_url(configured)
        scheme, hostname = self._browser_origin()
        return f"{scheme}://{hostname}:{self.api_port}"

    def _compose_env(self) -> dict[str, str]:
        scheme, hostname = self._browser_origin()
        socket_secure = "true" if scheme == "https" else "false"
        socket_scheme = "wss" if socket_secure == "true" else "ws"
        return {
            **super()._compose_env(),
            f"SHOWANDTELL_{self.name.upper()}_API_PORT": str(self.api_port),
            f"SHOWANDTELL_{self.name.upper()}_API_PUBLIC_URL": self.api_public_url,
            f"SHOWANDTELL_{self.name.upper()}_SOCKET_PORT": str(self.socket_port),
            f"SHOWANDTELL_{self.name.upper()}_SOCKET_HOST": hostname,
            f"SHOWANDTELL_{self.name.upper()}_SOCKET_SECURE": socket_secure,
            f"SHOWANDTELL_{self.name.upper()}_SOCKET_OPTIONS": (
                '{"origins":"'
                f"{scheme}://{hostname}:*,{socket_scheme}://{hostname}:*"
                '"}'
            ),
            f"SHOWANDTELL_{self.name.upper()}_SESSION_DOMAIN": hostname,
        }

    def _api_health_url(self) -> str:
        # As with the console, an assumed-running API answers where it was
        # published rather than on the machine driving the replay.
        base = (self.api_public_url.rstrip("/") if self._assume_running()
                else f"http://127.0.0.1:{self.api_port}")
        return f"{base}/int/v1/onboard/should-onboard"

    def _wait_api(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        while not self._probe(self._api_health_url()):
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"{self.name} API did not become healthy at "
                    f"{self._api_health_url()}"
                )
            self._sleep(self.poll_interval)

    def _seed_demo_data(self) -> None:
        # Fleet-Ops ships this dataset with the pinned release. Its seeders
        # purge only records carrying their own `fleetops-testing` marker,
        # making the operation repeatable without touching user-owned data.
        # Upstream FleetSeeder deletes vehicles before OrdersSeeder deletes
        # the orders that reference them, so a second run otherwise violates
        # orders_vehicle_assigned_uuid_foreign. Remove only the marked orders
        # first; FleetSeeder and OrdersSeeder can then rebuild their fixtures.
        self._exec(
            "database",
            "mysql",
            "-uroot",
            "fleetbase",
            stdin_bytes=PURGE_FLEETOPS_TESTING_ORDERS_SQL,
            env={"MYSQL_PWD": self.db_root_password},
            timeout=self.deploy_timeout,
        )
        # Run the useful fixtures separately: the upstream NetworkSeeder has
        # a Polygon serialization bug in v0.7.52, while the fleet, orders,
        # connectivity, and maintenance datasets are independent and valid.
        for seeder in FLEETOPS_TESTING_SEEDERS:
            self._dc(
                "exec",
                "-T",
                "application",
                "php",
                "artisan",
                "db:seed",
                f"--class={seeder}",
                "--force",
                timeout=self.deploy_timeout,
            )
        self._exec(
            "database",
            "mysql",
            "-uroot",
            "fleetbase",
            stdin_bytes=render_enterprise_seed_sql().encode(),
            env={"MYSQL_PWD": self.db_root_password},
            timeout=self.deploy_timeout,
        )

    def _exec(
        self,
        service: str,
        *args: str,
        stdin_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 900.0,
    ):
        extra: list[str] = []
        for key, value in (env or {}).items():
            extra.extend(("-e", f"{key}={value}"))
        return self._dc(
            "exec",
            "-T",
            *extra,
            service,
            *args,
            stdin_bytes=stdin_bytes,
            timeout=timeout,
        )

    def _snapshot_dir(self, name: str) -> Path:
        if not SNAPSHOT_NAME.fullmatch(name):
            raise DriverError(f"invalid snapshot name: {name}")
        return self.snapshots / name

    @staticmethod
    def _validate_snapshot(source: Path, name: str) -> tuple[bytes, bytes]:
        try:
            sql = gzip.decompress((source / "database.sql.gz").read_bytes())
            storage = (source / "storage.tar.gz").read_bytes()
            with tarfile.open(source / "storage.tar.gz", "r:gz") as archive:
                archive.getmembers()
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise DriverError(
                f"snapshot '{name}' is corrupt or incomplete"
            ) from exc
        return sql, storage

    def _storage_archive(self) -> bytes:
        result = self._dc(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "tar",
            "application",
            "czf",
            "-",
            "-C",
            STORAGE_PATH,
            ".",
            timeout=self.deploy_timeout,
        )
        return result.stdout

    def _restore_storage(self, archive: bytes) -> None:
        command = (
            f"find {STORAGE_PATH} -mindepth 1 -maxdepth 1 "
            f"-exec rm -rf -- {{}} + && tar xzf - -C {STORAGE_PATH}"
        )
        self._dc(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "sh",
            "application",
            "-c",
            command,
            stdin_bytes=archive,
            timeout=self.deploy_timeout,
        )

    def _restore_login_credential(self) -> None:
        """Reapply the manifest login after importing a portable snapshot."""
        script = (
            '$email = strtolower(trim((string) getenv('
            '"SHOWANDTELL_FLEETBASE_LOGIN_EMAIL"))); '
            '$password = (string) getenv('
            '"SHOWANDTELL_FLEETBASE_LOGIN_PASSWORD"); '
            '$updated = DB::table("users")'
            '->whereRaw("LOWER(email) = ?", [$email])'
            '->update(["password" => Hash::make($password), '
            '"remember_token" => null]); '
            'if ($updated !== 1) { throw new RuntimeException('
            '"Fleetbase fixture administrator was not found"); }'
        )
        self._exec(
            "application",
            "php",
            "artisan",
            "tinker",
            f"--execute={script}",
            env={
                "SHOWANDTELL_FLEETBASE_LOGIN_EMAIL": (
                    self.manifest.credentials["email"]
                ),
                "SHOWANDTELL_FLEETBASE_LOGIN_PASSWORD": (
                    self.manifest.credentials["password"]
                ),
            },
        )

    def start(self, *, wait: bool = True) -> None:
        if self._assume_running():
            # Deployment, organization bootstrap and demo seeding all ran when
            # the shared instance was built, and none of them is safe to repeat
            # against it: the seeders abort on data they already inserted,
            # which is what makes this application unstartable a second time.
            if wait:
                self._wait_api()
                self._wait_healthy()
            return
        self._dc("up", "-d", "--quiet-pull", timeout=1800.0)
        if not wait:
            return

        # This is Fleetbase's own release deployment script. It creates the
        # database, applies Core and Fleet-Ops migrations, seeds roles and
        # permissions, and initializes the bundled extension registry.
        self._dc(
            "exec",
            "-T",
            "application",
            "./deploy.sh",
            timeout=self.deploy_timeout,
        )
        self._wait_api()
        self._bootstrap(
            f"http://127.0.0.1:{self.api_port}",
            self.manifest.credentials,
        )
        self._seed_demo_data()
        self._wait_healthy()

    def reset(self) -> None:
        # Restarting is not this run's to do when the instance is shared.
        if self._assume_running():
            return
        # No task state belongs to this application yet. Preserve Fleetbase's
        # one-time organization bootstrap and restart only its processes.
        self._dc("restart", timeout=600.0)
        self._wait_api()
        self._wait_healthy()

    def snapshot(self, name: str) -> None:
        target = self._snapshot_dir(name)
        staging = target.with_name(f".{target.name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        self._dc(
            "stop", "application", "queue", "scheduler", timeout=300.0
        )
        try:
            dump = self._exec(
                "database",
                "mysqldump",
                "-uroot",
                "--single-transaction",
                "--routines",
                "--triggers",
                "--events",
                "--set-gtid-purged=OFF",
                "--databases",
                "fleetbase",
                "fleetbase_sandbox",
                "fleetbase_storefront",
                env={"MYSQL_PWD": self.db_root_password},
                timeout=self.deploy_timeout,
            )
            (staging / "database.sql.gz").write_bytes(
                gzip.compress(dump.stdout)
            )
            (staging / "storage.tar.gz").write_bytes(
                self._storage_archive()
            )
            (staging / "taken_at").write_text(
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                + "\n"
            )
            self._validate_snapshot(staging, name)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            self._dc(
                "start", "application", "queue", "scheduler", timeout=300.0
            )
            self._wait_api()

        previous = target.with_name(f".{target.name}.previous")
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            os.replace(target, previous)
        try:
            os.replace(staging, target)
        except BaseException:
            if previous.exists():
                os.replace(previous, target)
            raise
        shutil.rmtree(previous, ignore_errors=True)

    def fetch_snapshot(self, name: str) -> Path:
        source = self._snapshot_dir(name)
        if not (source / "database.sql.gz").is_file():
            raise DriverError(f"no snapshot named '{name}'")
        self._validate_snapshot(source, name)
        output = self.root / "outgoing" / f"{self.name}-{name}.tar"
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w") as archive:
            for item in sorted(source.iterdir()):
                archive.add(item, arcname=item.name)
        return output

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        target = self._snapshot_dir(name)
        staging = target.with_name(f".{target.name}.upload")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(tar_path) as archive:
                archive.extractall(staging, filter="data")
            self._validate_snapshot(staging, name)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)

    def restore(self, name: str) -> None:
        source = self._snapshot_dir(name)
        if not source.is_dir():
            raise DriverError(f"no snapshot named '{name}'")
        sql, storage = self._validate_snapshot(source, name)

        self._dc(
            "stop", "application", "queue", "scheduler", timeout=300.0
        )
        try:
            self._exec(
                "database",
                "mysql",
                "-uroot",
                "-e",
                "SET FOREIGN_KEY_CHECKS=0; "
                "DROP DATABASE IF EXISTS `fleetbase_storefront`; "
                "DROP DATABASE IF EXISTS `fleetbase_sandbox`; "
                "DROP DATABASE IF EXISTS `fleetbase`; "
                "SET FOREIGN_KEY_CHECKS=1; "
                "CREATE DATABASE `fleetbase` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                "CREATE DATABASE `fleetbase_sandbox` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                "CREATE DATABASE `fleetbase_storefront` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
                env={"MYSQL_PWD": self.db_root_password},
            )
            self._exec(
                "database",
                "mysql",
                "-uroot",
                # Select the core schema explicitly for legacy snapshots made
                # before --databases was enabled. Current dumps contain their
                # own USE statements and switch schemas as they are imported.
                "fleetbase",
                stdin_bytes=sql,
                env={"MYSQL_PWD": self.db_root_password},
                timeout=self.deploy_timeout,
            )
            self._restore_storage(storage)
            self._exec("cache", "valkey-cli", "FLUSHALL")
        finally:
            self._dc(
                "start", "application", "queue", "scheduler", timeout=300.0
            )
        # Database snapshots carry the password hash from capture time. Keep
        # portable task state compatible with the current fixture manifest.
        self._restore_login_credential()
        self._wait_api()
        self._wait_healthy()

    def status(self) -> dict:
        healthy = self._probe(self._health_url()) and self._probe(
            self._api_health_url()
        )
        return {
            "state": "healthy" if healthy else "unreachable",
            "ports": {
                "app": self.port,
                "api": self.api_port,
                "socket": self.socket_port,
            },
        }


Driver = FleetbaseDriver
