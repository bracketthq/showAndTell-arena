import stat
import subprocess

import pytest

from showAndTell.applications.host.protocol import DriverError, Unsupported
from showAndTell.applications.onlyoffice.driver import OnlyOfficeDriver

from tests._app_fixtures import app_manifest


class FakeRunner:
    def __init__(self, ps_output: bytes = b""):
        self.calls: list[list[str]] = []
        self.ps_output = ps_output

    def run(self, argv, *, stdin_bytes=None, env=None, timeout=900.0, check=True):
        self.calls.append(list(argv))
        self.env_seen = dict(env or {})
        # Return canned output for "ps" command, empty for others
        if "ps" in argv:
            return subprocess.CompletedProcess(list(argv), 0, stdout=self.ps_output, stderr=b"")
        return subprocess.CompletedProcess(list(argv), 0, stdout=b"", stderr=b"")


@pytest.fixture()
def driver(tmp_path):
    runner = FakeRunner()
    calls = []

    def admin_call(method, url, token):
        calls.append((method, url, token))

    d = OnlyOfficeDriver(
        app_manifest("onlyoffice"), root=tmp_path, runner=runner,
        project="showAndTell-onlyoffice",
        probe=lambda url: True, admin_call=admin_call, sleep=lambda s: None,
    )
    return d, runner, calls


def test_secrets_creates_stable_0600_token(driver, tmp_path):
    d, _, _ = driver
    token = d.secrets()["connector_token"]
    assert len(token) == 64
    token_file = tmp_path / "connector.token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert d.secrets()["connector_token"] == token


def test_start_skips_compose_when_both_services_are_healthy(driver):
    d, runner, _ = driver
    d.start(wait=True)
    assert runner.calls == []


def test_cold_start_uses_connector_profile_and_exports_token(tmp_path):
    runner = FakeRunner()
    d = OnlyOfficeDriver(
        app_manifest("onlyoffice"), root=tmp_path, runner=runner,
        project="showAndTell-onlyoffice", probe=lambda url: False,
    )
    d.start(wait=False)
    up = next(call for call in runner.calls if "up" in call)
    assert "--profile" in up and "connector" in up
    assert runner.env_seen["SHOWANDTELL_CONNECTOR_ADMIN_TOKEN"] == \
        d.secrets()["connector_token"]


def test_reset_clears_workbooks_then_caches(driver):
    d, runner, admin_calls = driver
    d.reset()
    assert admin_calls == [
        ("DELETE", "http://127.0.0.1:8086/admin/workbooks",
         d.secrets()["connector_token"]),
    ]
    joined = [" ".join(call) for call in runner.calls]
    assert any("cache/files" in c for c in joined)
    assert any("docbuilder" in c for c in joined)


def test_snapshot_family_is_unsupported(driver):
    d, _, _ = driver
    with pytest.raises(Unsupported):
        d.snapshot("x")
    with pytest.raises(Unsupported):
        d.restore("x")
    with pytest.raises(Unsupported):
        d.baseline()


def test_status_reports_both_ports(driver):
    d, _, _ = driver
    status = d.status()
    assert status["ports"] == {"app": 8081, "connector": 8086}
    assert status["state"] == "healthy"


def test_document_server_url_default_in_env(driver):
    d, _, _ = driver
    assert d._compose_env()["SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL"] == \
        "http://127.0.0.1:8081"


def test_document_server_url_env_override(tmp_path, monkeypatch):
    runner = FakeRunner()
    monkeypatch.setenv("SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL",
                      "http://remote-docserver:8080")
    d = OnlyOfficeDriver(
        app_manifest("onlyoffice"), root=tmp_path, runner=runner,
        project="showAndTell-onlyoffice", probe=lambda url: True,
    )
    assert d._compose_env()["SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL"] == \
        "http://remote-docserver:8080"


def test_wait_healthy_timeout_includes_diagnostics(tmp_path):
    ps_output = b"CONTAINER   IMAGE   STATUS\nonlyoffice  image1  Up 2s\nconnector   image2  Exited"
    runner = FakeRunner(ps_output=ps_output)

    d = OnlyOfficeDriver(
        app_manifest("onlyoffice"), root=tmp_path, runner=runner,
        project="showAndTell-onlyoffice",
        probe=lambda url: False,  # Always report unhealthy
        sleep=lambda s: None,  # Don't actually sleep
    )

    # Should timeout quickly with small budget and always-unhealthy probe
    with pytest.raises(DriverError) as exc_info:
        d._wait_healthy(budget_s=0.1)

    error_msg = str(exc_info.value)
    # Verify diagnostics are included and contain the ps output
    assert "Docker Compose State:" in error_msg
    assert ps_output.decode("utf-8") in error_msg


def test_explicit_ports_beat_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_ONLYOFFICE_PORT", "9999")
    monkeypatch.setenv("SHOWANDTELL_CONNECTOR_PORT", "9998")
    driver = OnlyOfficeDriver(app_manifest("onlyoffice"), root=tmp_path,
                              port=8181, connector_port=8186)
    assert driver.port == 8181
    assert driver.connector_port == 8186
