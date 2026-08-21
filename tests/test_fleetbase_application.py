"""Fleetbase is a real app entry, independent of benchmark task content."""
from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from showAndTell.applications.fleetbase.bootstrap import bootstrap_fleetbase
from showAndTell.applications.fleetbase.driver import FleetbaseDriver
from showAndTell.applications.registry import default_registry
from showAndTell.applications.host.protocol import DriverError


ROOT = Path(__file__).parents[1]


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.stdin_seen: list[bytes | None] = []

    def run(
        self,
        argv,
        *,
        stdin_bytes=None,
        env=None,
        timeout=900.0,
        check=True,
    ):
        self.calls.append(list(argv))
        self.environments.append(dict(env or {}))
        self.stdin_seen.append(stdin_bytes)
        joined = " ".join(argv)
        stdout = b""
        if "mysqldump" in joined:
            stdout = b"-- fleetbase fixture database --"
        elif "--entrypoint tar" in joined:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                payload = b"fleetbase storage"
                info = tarfile.TarInfo("fixture.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            stdout = buffer.getvalue()
        return subprocess.CompletedProcess(
            argv, 0, stdout=stdout, stderr=b""
        )


def test_fleetbase_manifest_exposes_console_api_and_socket() -> None:
    manifest = default_registry().manifest("fleetbase")

    assert manifest.label == "Fleetbase + Fleet-Ops"
    assert manifest.compose_service == "console"
    assert manifest.surface_entry == "/auth"
    assert manifest.credentials == {
        "email": "operator@bluegrassfreight.com",
        "password": "ShowAndTell-Fleet1!K7",
    }
    assert manifest.snapshots is True
    assert {
        name: (port.service, port.container, port.host)
        for name, port in manifest.ports.items()
    } == {
        "app": ("console", 4200, 8045),
        "api": ("application", 8000, 8046),
        "socket": ("socket", 8000, 38045),
    }


def test_fleetbase_compose_is_release_pinned_and_bundles_fleetops() -> None:
    compose = (ROOT / "src/showAndTell/applications/fleetbase/compose.yaml").read_text()

    assert "fleetbase/fleetbase-api@sha256:" in compose
    assert "fleetbase/fleetbase-console@sha256:" in compose
    assert "valkey/valkey@sha256:" in compose
    assert "library/mysql@sha256:" in compose
    assert "socketcluster/socketcluster@sha256:" in compose
    assert ":latest" not in compose
    assert "build:" not in compose
    assert 'REGISTRY_PREINSTALLED_EXTENSIONS: "true"' in compose
    assert 'TELEMETRY_DISABLED: "true"' in compose
    assert "./console.conf.template:/etc/nginx/templates/showAndTell/" in compose
    assert "./database-init.sql:/docker-entrypoint-initdb.d/" in compose
    assert compose.count("disable: true") == 2
    assert "${SHOWANDTELL_FLEETBASE_BIND_HOST:-127.0.0.1}" in compose
    assert "${SHOWANDTELL_FLEETBASE_PORT:-8045}" in compose
    assert "${SHOWANDTELL_FLEETBASE_API_PORT:-8046}" in compose


def test_console_runtime_config_uses_browser_visible_origins() -> None:
    nginx = (
        ROOT / "src/showAndTell/applications/fleetbase/console.conf.template"
    ).read_text()
    assert 'API:{host:"${SHOWANDTELL_FLEETBASE_API_PUBLIC_URL}"' in nginx
    assert 'hostname:"${SHOWANDTELL_FLEETBASE_SOCKET_HOST}"' in nginx
    assert 'port:"${SHOWANDTELL_FLEETBASE_SOCKET_PORT}"' in nginx
    assert "location = /fleetbase.config.json" in nginx
    assert (
        "assets/@fleetbase/console-23a317771662ce487f76c8d7d9ff9eb1.js"
        "?showAndTell-config=v1"
    ) in nginx
    assert (
        "sub_filter ' integrity=\"sha256-NE40+PTHJ74u9mDrTgNFfDpGOTNWbYLCDHHRbr6j2t4="
        " sha512-GBY8brSKGa6R2b1aWHioESDwThaaSLBDgzG112pzijxQ7jGU8kKCesvp5M7fFuyxL+3QmeWSSDi9Fpa3XwnNWg==\"' '';"
    ) in nginx
    # The same image tag serves a different console bundle per architecture, so
    # an arm64 host is only unblocked when its bundle is exempted too.
    assert (
        "assets/@fleetbase/console-cf659853406bff7848f68d5b18dadbb7.js"
        "?showAndTell-config=v1"
    ) in nginx
    assert (
        "sub_filter ' integrity=\"sha256-gCuHgc3OCw03A5wNCWqtNfVdZRFhU9VRzTFbuloZKrA="
        " sha512-SlvSKIftcATXV4scvThYloezHDj4QNMhYQimLRYMEosQKfMBwt8h89UdHN+wfTG870igov4ASRpmPG7/RKiYDw==\"' '';"
    ) in nginx


def test_driver_derives_remote_api_and_socket_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = default_registry().manifest("fleetbase")
    monkeypatch.setenv("SHOWANDTELL_FLEETBASE_PUBLIC_URL", "https://fleet.example:9443")
    driver = FleetbaseDriver(
        manifest,
        root=tmp_path,
        runner=FakeRunner(),
        probe=lambda _url: True,
    )

    environment = driver._compose_env()

    assert environment["SHOWANDTELL_FLEETBASE_PUBLIC_URL"] == (
        "https://fleet.example:9443"
    )
    assert environment["SHOWANDTELL_FLEETBASE_API_PUBLIC_URL"] == (
        "https://fleet.example:8046"
    )
    assert environment["SHOWANDTELL_FLEETBASE_SOCKET_HOST"] == "fleet.example"
    assert environment["SHOWANDTELL_FLEETBASE_SOCKET_SECURE"] == "true"
    assert environment["SHOWANDTELL_FLEETBASE_SESSION_DOMAIN"] == "fleet.example"
    assert json.loads(environment["SHOWANDTELL_FLEETBASE_SOCKET_OPTIONS"]) == {
        "origins": "https://fleet.example:*,wss://fleet.example:*"
    }


def test_start_deploys_onboards_and_seeds_official_demo_data_in_order(
    tmp_path: Path,
) -> None:
    manifest = default_registry().manifest("fleetbase")
    runner = FakeRunner()
    bootstraps: list[tuple[str, dict[str, str]]] = []
    bootstrap_call_counts: list[int] = []

    def bootstrap(url, credentials):
        bootstrap_call_counts.append(len(runner.calls))
        bootstraps.append((url, dict(credentials)))
        return True

    driver = FleetbaseDriver(
        manifest,
        root=tmp_path,
        runner=runner,
        probe=lambda _url: True,
        bootstrap=bootstrap,
    )

    driver.start(wait=True)

    joined = [" ".join(call) for call in runner.calls]
    assert "up -d --quiet-pull" in joined[0]
    deploy_index = next(
        index
        for index, call in enumerate(joined)
        if "exec -T application ./deploy.sh" in call
    )
    assert deploy_index > 0
    purge_index = next(
        index
        for index, payload in enumerate(runner.stdin_seen)
        if payload and b"DELETE FROM orders" in payload
    )
    assert purge_index > deploy_index
    assert b"fleetops-testing" in runner.stdin_seen[purge_index]
    seed_indexes = [
        index
        for index, call in enumerate(joined)
        if "php artisan db:seed" in call
    ]
    assert len(seed_indexes) == 4
    assert seed_indexes[0] > purge_index
    assert bootstrap_call_counts == [purge_index]
    expected_seeders = [
        "FleetSeeder",
        "OrdersSeeder",
        "ConnectivitySeeder",
        "MaintenanceSeeder",
    ]
    for index, seeder in zip(seed_indexes, expected_seeders, strict=True):
        assert (
            "--class=Fleetbase\\FleetOps\\Seeders\\Testing\\" + seeder
            in joined[index]
        )
        assert "--force" in joined[index]
    enterprise_index = next(
        index
        for index, payload in enumerate(runner.stdin_seen)
        if payload and b"enterprise-operations:BGF-4821" in payload
    )
    assert enterprise_index > seed_indexes[-1]
    assert b"enterprise-operations:BGF-4821" in runner.stdin_seen[enterprise_index]
    assert bootstraps == [
        ("http://127.0.0.1:8046", manifest.credentials)
    ]


def test_snapshot_round_trip_preserves_database_and_storage(
    tmp_path: Path,
) -> None:
    manifest = default_registry().manifest("fleetbase")
    runner = FakeRunner()
    driver = FleetbaseDriver(
        manifest,
        root=tmp_path,
        runner=runner,
        probe=lambda _url: True,
    )

    driver.snapshot("draft-fleetbase")
    source = (
        tmp_path / "snapshots" / "fleetbase" / "draft-fleetbase"
    )
    assert gzip.decompress((source / "database.sql.gz").read_bytes()) == (
        b"-- fleetbase fixture database --"
    )
    with tarfile.open(source / "storage.tar.gz", "r:gz") as archive:
        assert archive.extractfile("fixture.txt").read() == (
            b"fleetbase storage"
        )
    snapshot_calls = [" ".join(call) for call in runner.calls]
    dump_command = next(
        call for call in snapshot_calls if "mysqldump" in call
    )
    assert (
        "--databases fleetbase fleetbase_sandbox fleetbase_storefront"
        in dump_command
    )

    portable = driver.fetch_snapshot("draft-fleetbase")
    driver.load_snapshot("replayed-fleetbase", portable)
    runner.calls.clear()
    runner.stdin_seen = []
    driver.restore("replayed-fleetbase")

    joined = [" ".join(call) for call in runner.calls]
    stop_index = next(
        index for index, call in enumerate(joined)
        if "stop application queue scheduler" in call
    )
    drop_index = next(
        index for index, call in enumerate(joined)
        if "DROP DATABASE IF EXISTS" in call
    )
    drop_command = joined[drop_index]
    assert "SET FOREIGN_KEY_CHECKS=0" in drop_command
    assert drop_command.index("fleetbase_storefront") < drop_command.index(
        "`fleetbase`"
    )
    assert "CREATE DATABASE `fleetbase`" in drop_command
    assert "CREATE DATABASE `fleetbase_sandbox`" in drop_command
    assert "CREATE DATABASE `fleetbase_storefront`" in drop_command
    storage_index = next(
        index for index, call in enumerate(joined)
        if "--entrypoint sh" in call
    )
    flush_index = next(
        index for index, call in enumerate(joined)
        if "valkey-cli FLUSHALL" in call
    )
    start_index = next(
        index for index, call in enumerate(joined)
        if "start application queue scheduler" in call
    )
    assert stop_index < drop_index < storage_index < flush_index < start_index
    credential_index = next(
        index for index, call in enumerate(joined)
        if "php artisan tinker --execute=" in call
    )
    assert start_index < credential_index
    credential_call = joined[credential_index]
    assert "LOWER(email) = ?" in credential_call
    assert "Hash::make($password)" in credential_call
    assert (
        "SHOWANDTELL_FLEETBASE_LOGIN_EMAIL="
        "operator@bluegrassfreight.com"
    ) in credential_call
    assert (
        "SHOWANDTELL_FLEETBASE_LOGIN_PASSWORD="
        "ShowAndTell-Fleet1!K7"
    ) in credential_call
    database_restore_index = next(
        index
        for index, call in enumerate(joined)
        if "mysql -uroot fleetbase" in call
    )
    assert runner.stdin_seen[database_restore_index] == (
        b"-- fleetbase fixture database --"
    )
    assert any(
        stdin and stdin.startswith(b"\x1f\x8b")
        for stdin in runner.stdin_seen
    )


def test_restore_rejects_a_corrupt_snapshot_before_stopping_services(
    tmp_path: Path,
) -> None:
    manifest = default_registry().manifest("fleetbase")
    runner = FakeRunner()
    driver = FleetbaseDriver(
        manifest,
        root=tmp_path,
        runner=runner,
        probe=lambda _url: True,
    )
    source = tmp_path / "snapshots" / "fleetbase" / "broken"
    source.mkdir(parents=True)
    (source / "database.sql.gz").write_bytes(b"not gzip")
    (source / "storage.tar.gz").write_bytes(b"not tar")

    with pytest.raises(DriverError, match="corrupt or incomplete"):
        driver.restore("broken")

    assert not runner.calls


def test_bootstrap_creates_only_the_first_supported_account() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("should-onboard"):
            return httpx.Response(200, json={"should_onboard": True})
        payload = json.loads(request.content)
        assert payload["email"] == "operator@bluegrassfreight.com"
        assert payload["password"] == payload["password_confirmation"]
        assert payload["organization_name"] == "Bluegrass Freight"
        return httpx.Response(200, json={"status": "success"})

    created = bootstrap_fleetbase(
        "http://127.0.0.1:8046",
        {
            "email": "operator@bluegrassfreight.com",
            "password": "ShowAndTell-Fleet1!K7",
        },
        transport=httpx.MockTransport(handler),
    )

    assert created is True
    assert [request.method for request in requests] == ["GET", "POST"]


def test_bootstrap_is_a_noop_after_an_organization_exists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"should_onboard": False})

    created = bootstrap_fleetbase(
        "http://127.0.0.1:8046",
        {"email": "operator@example.org", "password": "Secret1!"},
        transport=httpx.MockTransport(handler),
    )

    assert created is False


def test_bootstrap_rejects_a_malformed_status_response() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"should_onboard": "yes"})
    )

    with pytest.raises(RuntimeError, match="malformed onboarding status"):
        bootstrap_fleetbase(
            "http://127.0.0.1:8046",
            {"email": "operator@example.org", "password": "Secret1!"},
            transport=transport,
        )
