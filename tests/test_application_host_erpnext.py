"""ErpnextDriver must preserve remote/erpnext.sh's operational discipline."""
import gzip
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from showAndTell.applications.host.protocol import DriverError
from showAndTell.applications.erpnext.driver import ErpnextDriver

from tests._app_fixtures import app_manifest


class FakeRunner:
    """Returns canned stdout keyed on argv substrings; records every call."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.stdin_seen: list[bytes | None] = []

    def run(self, argv, *, stdin_bytes=None, env=None, timeout=900.0, check=True):
        argv = list(argv)
        self.calls.append(argv)
        self.stdin_seen.append(stdin_bytes)
        joined = " ".join(argv)
        stdout = b""
        if "site_config.json" in joined:
            stdout = json.dumps({
                "db_name": "_abc123", "db_user": "_abc123",
                "db_password": "s3cret",
            }).encode()
        elif "command -v" in joined:
            stdout = b"/usr/bin/mariadb-dump\n" if "dump" in joined else b"/usr/bin/mariadb\n"
        elif "mariadb-dump" in joined:
            stdout = b"-- fake sql dump --"
        elif "tar czf" in joined:
            stdout = gzip.compress(b"fake-site-files")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")


@pytest.fixture()
def driver(tmp_path):
    runner = FakeRunner()
    d = ErpnextDriver(
        app_manifest("erpnext"), root=tmp_path, runner=runner,
        project="showAndTell-erpnext", probe=lambda url: True,
    )
    return d, runner


def compose_prefix(call):
    return call[:2] == ["docker", "compose"]


def test_start_brings_stack_up(driver):
    d, runner = driver
    d.start(wait=True)
    build = next(call for call in runner.calls if "build" in call)
    up = next(call for call in runner.calls if "up" in call)
    assert build[-2:] == ["build", "backend"]
    assert "-d" in up
    assert all(compose_prefix(c) for c in runner.calls)


def test_snapshot_quiesces_workers_and_writes_archives(driver, tmp_path):
    d, runner = driver
    d.snapshot("golden")
    joined = [" ".join(call) for call in runner.calls]
    stop_index = next(i for i, c in enumerate(joined) if "stop queue-long" in c)
    dump_index = next(i for i, c in enumerate(joined) if "mariadb-dump" in c)
    start_index = next(i for i, c in enumerate(joined) if "start queue-long" in c)
    assert stop_index < dump_index < start_index  # dump only while quiesced
    target = tmp_path / "snapshots" / "erpnext" / "golden"
    assert gzip.decompress((target / "site.sql.gz").read_bytes()) == b"-- fake sql dump --"
    assert (target / "db_name").read_text().strip() == "_abc123"
    assert (target / "taken_at").read_text().strip()


def test_restore_refuses_missing_snapshot(driver):
    d, _ = driver
    with pytest.raises(DriverError, match="no snapshot named 'ghost'"):
        d.restore("ghost")


def test_restore_replays_db_files_and_flushes_caches(driver):
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()
    runner.stdin_seen.clear()
    d.restore("golden")
    joined = [" ".join(call) for call in runner.calls]
    assert any("DROP DATABASE IF EXISTS" in c for c in joined)
    assert any(b"-- fake sql dump --" in (s or b"") for s in runner.stdin_seen)
    assert any("rm -rf sites/frontend" in c for c in joined)
    assert any("redis-cli FLUSHALL" in c for c in joined)
    assert any("clear-cache" in c for c in joined)
    # The sql client name is resolved BEFORE the quiesce (as in the shell
    # script), so stop is not call zero — but it must precede the DROP,
    # and the worker restart must be the very last call.
    stop_index = next(i for i, c in enumerate(joined) if "stop queue-long" in c)
    drop_index = next(i for i, c in enumerate(joined) if "DROP DATABASE" in c)
    start_index = next(i for i, c in enumerate(joined) if "start queue-long" in c)
    assert stop_index < drop_index
    assert start_index == len(joined) - 1
    # Web processes must restart AFTER the swapped database is in place:
    # Frappe v16's per-process client_cache outlives a redis FLUSHALL, and a
    # gunicorn worker serving pre-restore docs (Installed Applications'
    # is_setup_complete) reload-loops the desk through the setup wizard.
    restart_index = next(
        i for i, c in enumerate(joined)
        if "restart backend websocket erpnext" in c)
    clear_index = next(i for i, c in enumerate(joined) if "clear-cache" in c)
    assert clear_index < restart_index < start_index


def test_restore_terminates_site_connections_before_drop(driver):
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()
    original_run = runner.run

    def run_with_connections(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if "SELECT ID FROM information_schema.PROCESSLIST" in " ".join(argv):
            return subprocess.CompletedProcess(
                argv, 0, stdout=b"378249\n378250\n", stderr=b"")
        return result

    runner.run = run_with_connections
    d.restore("golden")

    joined = [" ".join(call) for call in runner.calls]
    select_index = next(
        i for i, call in enumerate(joined)
        if "SELECT ID FROM information_schema.PROCESSLIST" in call)
    kill_indexes = [
        i for i, call in enumerate(joined)
        if "mariadb -uroot -e KILL " in call]
    drop_index = next(
        i for i, call in enumerate(joined) if "DROP DATABASE" in call)
    # Every reported connection is killed, in one exec — `docker compose exec`
    # costs about a second each, and a restore finds tens of idle sessions.
    assert len(kill_indexes) == 1
    assert joined[kill_indexes[0]].endswith("KILL 378249;KILL 378250")
    assert select_index < kill_indexes[0] < drop_index


def test_restore_grants_the_snapshot_site_user_on_this_host(driver):
    """A snapshot carries site_config.json but never MariaDB's grant table.

    Restoring a snapshot taken on another machine — a capture recorded on the
    fixture VM, replayed locally — otherwise loads a database whose own site
    cannot log in, and every bench command after it fails.
    """
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()
    d.restore("golden")
    joined = [" ".join(call) for call in runner.calls]
    grant = next(c for c in joined if "GRANT ALL PRIVILEGES" in c)
    assert "CREATE USER IF NOT EXISTS `_abc123`@`%` IDENTIFIED BY 's3cret'" in grant
    assert "ALTER USER `_abc123`@`%` IDENTIFIED BY 's3cret'" in grant
    assert "ON `_abc123`.* TO `_abc123`@`%`" in grant
    # The credentials are read out of the RESTORED site files, and the grant
    # has to exist before anything speaks to the site.
    files_index = next(i for i, c in enumerate(joined) if "tar xzf" in c)
    grant_index = joined.index(grant)
    clear_index = next(i for i, c in enumerate(joined) if "clear-cache" in c)
    assert files_index < grant_index < clear_index


def test_restore_reapplies_current_admin_password_before_web_restart(driver):
    """Snapshot credentials must not override the current fixture manifest."""
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()

    d.restore("golden")

    joined = [" ".join(call) for call in runner.calls]
    password_index = next(
        index for index, call in enumerate(joined)
        if "bench --site frontend set-admin-password showAndTell-admin" in call
    )
    setup_index = max(
        index for index, call in enumerate(joined)
        if "run_post_setup_complete" in call
    )
    restart_index = next(
        index for index, call in enumerate(joined)
        if "restart backend websocket erpnext" in call
    )
    assert setup_index < password_index < restart_index


def test_reset_restores_golden(driver):
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()
    d.reset()
    assert any("DROP DATABASE" in " ".join(call) for call in runner.calls)


def test_reset_upgrades_old_golden_to_hrms_and_repairs_setup(driver):
    d, runner = driver
    d.snapshot("golden")
    runner.calls.clear()

    d.reset()

    joined = [" ".join(call) for call in runner.calls]
    assert any("DROP DATABASE" in call for call in joined)
    assert any("list-apps | grep -Eq" in call for call in joined)
    assert any("migrate" in call for call in joined)
    assert any("install-app hrms" in call for call in joined)
    for app in ("frappe", "erpnext", "hrms"):
        assert any(
            "frappe.db.set_value" in call
            and f'{{\"app_name\":\"{app}\"}}' in call
            and 'is_setup_complete\",1' in call
            for call in joined
        )
    post_setup = next(
        index for index, call in enumerate(joined)
        if "run_post_setup_complete" in call
    )
    disable_onboarding = next(
        index for index, call in enumerate(joined)
        if "frappe.db.set_single_value" in call
        and '["System Settings","enable_onboarding",0]' in call
    )

    last_setup = max(
        index for index, call in enumerate(joined)
        if "frappe.db.set_value" in call and "is_setup_complete" in call
    )
    last_clear = max(
        index for index, call in enumerate(joined) if "clear-cache" in call
    )
    last_restart = max(
        index for index, call in enumerate(joined)
        if "restart backend websocket erpnext" in call
    )
    assert last_setup < post_setup < disable_onboarding < last_clear < last_restart


def test_fetch_and_load_round_trip(driver, tmp_path):
    d, _ = driver
    d.snapshot("draft-1")
    tar_path = d.fetch_snapshot("draft-1")
    with tarfile.open(tar_path) as tar:
        assert "site.sql.gz" in tar.getnames()
    d.load_snapshot("copy", tar_path)
    assert (tmp_path / "snapshots" / "erpnext" / "copy" / "site.sql.gz").is_file()


def test_load_rejects_tar_without_dump(driver, tmp_path):
    d, _ = driver
    junk = tmp_path / "junk.txt"
    junk.write_text("nope")
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tar:
        tar.add(junk, arcname="junk.txt")
    with pytest.raises(DriverError, match="missing site.sql.gz"):
        d.load_snapshot("bad", bad)


def test_baseline_runs_setup_sequence(driver):
    d, runner = driver
    d.baseline()
    joined = [" ".join(call) for call in runner.calls]
    assert any("stage_fixtures" in c for c in joined)
    assert any("migrate" in c for c in joined)
    assert any("install-app hrms" in c for c in joined)
    assert any("enable_discounts_and_margin" in c for c in joined)
    assert any("update_versions" in c for c in joined)
    assert any("run_post_setup_complete" in c for c in joined)
    assert any(
        "frappe.db.set_single_value" in c
        and '["System Settings","enable_onboarding",0]' in c
        for c in joined
    )
    assert any("clear-cache" in c for c in joined)


def test_secrets_and_status(driver):
    d, _ = driver
    assert d.secrets() == {"admin_password": "showAndTell-admin"}
    status = d.status()
    assert status["state"] == "healthy"
    assert status["ports"] == {"app": 8080}
    assert "golden" not in status["snapshots"]  # none taken yet in this test


def test_failed_resnapshot_leaves_previous_intact(driver, tmp_path):
    """Regression: a failed snapshot must not clobber the existing one."""
    d, runner = driver
    # Create initial snapshot
    d.snapshot("golden")
    original_golden = tmp_path / "snapshots" / "erpnext" / "golden" / "site.sql.gz"
    original_content = original_golden.read_bytes()

    # Make the files-tar exec fail
    class FailingRunner:
        def __init__(self, delegate):
            self.delegate = delegate
            self.calls = delegate.calls
            self.stdin_seen = delegate.stdin_seen

        def run(self, argv, *, stdin_bytes=None, env=None, timeout=900.0, check=True):
            joined = " ".join(argv)
            if "tar czf" in joined:
                raise subprocess.CalledProcessError(1, argv, stderr=b"tar failed")
            return self.delegate.run(argv, stdin_bytes=stdin_bytes, env=env,
                                   timeout=timeout, check=check)

    d.runner = FailingRunner(runner)

    # Try to re-snapshot—should fail
    with pytest.raises(Exception):
        d.snapshot("golden")

    # Verify the old snapshot is still there and unchanged
    assert original_golden.read_bytes() == original_content


def test_fetch_snapshot_rejects_dangling_new_files(driver, tmp_path):
    """A snapshot interrupted before the final os.replace() leaves only the
    staging `.new` files behind; fetch must not treat that directory as a
    real snapshot just because it exists.
    """
    d, _ = driver
    partial = tmp_path / "snapshots" / "erpnext" / "partial"
    partial.mkdir(parents=True)
    (partial / "site.sql.gz.new").write_bytes(b"incomplete")
    (partial / "site-files.tar.gz.new").write_bytes(b"incomplete")
    with pytest.raises(DriverError, match="no snapshot named 'partial'"):
        d.fetch_snapshot("partial")


def test_restore_validates_corrupt_tar_before_drop_database(driver, tmp_path):
    """Regression: restore must validate BOTH archives before any DROP DATABASE."""
    d, runner = driver
    # Create and then corrupt a snapshot
    d.snapshot("test")

    # Corrupt the site-files.tar.gz (make it invalid gzip)
    tar_path = tmp_path / "snapshots" / "erpnext" / "test" / "site-files.tar.gz"
    tar_path.write_bytes(b"\x1f\x8b\x08\x00invalid gzip data here")

    runner.calls.clear()

    # Try to restore—should fail before any DROP DATABASE
    with pytest.raises(DriverError, match="corrupt"):
        d.restore("test")

    # Verify DROP DATABASE was never called
    joined = [" ".join(call) for call in runner.calls]
    assert not any("DROP DATABASE" in c for c in joined)


def test_explicit_port_kwarg_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_ERPNEXT_PORT", "9999")
    driver = ErpnextDriver(app_manifest("erpnext"), root=tmp_path, port=8180)
    assert driver.port == 8180
    # Without the kwarg the env override still wins, as today.
    ambient = ErpnextDriver(app_manifest("erpnext"), root=tmp_path)
    assert ambient.port == 9999
