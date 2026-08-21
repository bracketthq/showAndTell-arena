import subprocess

import pytest

from showAndTell.applications.host.protocol import DriverError, Unsupported
from showAndTell.applications.roundcube.driver import RoundcubeDriver

from tests._app_fixtures import app_manifest


class FakeRunner:
    def __init__(self, ps_output: bytes = b""):
        self.calls: list[list[str]] = []
        self.env_seen: dict[str, str] = {}
        self.ps_output = ps_output

    def run(self, argv, *, stdin_bytes=None, env=None, timeout=900.0, check=True):
        self.calls.append(list(argv))
        self.env_seen = dict(env or {})
        if "ps" in argv:
            return subprocess.CompletedProcess(list(argv), 0, stdout=self.ps_output, stderr=b"")
        return subprocess.CompletedProcess(list(argv), 0, stdout=b"", stderr=b"")


@pytest.fixture()
def driver(tmp_path):
    runner = FakeRunner()
    d = RoundcubeDriver(
        app_manifest("roundcube"), root=tmp_path, runner=runner,
        project="showAndTell-roundcube",
        probe=lambda url: True, imap_probe=lambda host, port: True,
        sleep=lambda s: None,
    )
    return d, runner


def test_status_reports_app_and_imap_ports(driver):
    d, _ = driver
    status = d.status()
    assert status["ports"] == {"app": 8082, "imap": 1143}
    assert status["state"] == "healthy"


def test_status_is_unreachable_when_imap_is_down(tmp_path):
    d = RoundcubeDriver(
        app_manifest("roundcube"), root=tmp_path, runner=FakeRunner(),
        probe=lambda url: True, imap_probe=lambda host, port: False,
        sleep=lambda s: None,
    )
    assert d.status()["state"] == "unreachable"


def test_start_publishes_bind_host_and_catalog_password(driver):
    d, runner = driver
    d.start(wait=True)
    assert any("up" in call for call in runner.calls)
    # The catalog authenticates the capture surface as showAndTell-mail; Dovecot
    # must be started with that password or managed-Chrome login fails.
    assert runner.env_seen["SHOWANDTELL_MAIL_PASSWORD"] == "showAndTell-mail"
    assert runner.env_seen["SHOWANDTELL_ROUNDCUBE_BIND_HOST"] == "127.0.0.1"
    assert runner.env_seen["SHOWANDTELL_ROUNDCUBE_PORT"] == "8082"
    assert runner.env_seen["SHOWANDTELL_IMAP_PORT"] == "1143"


def test_reset_expunges_the_mailbox_through_doveadm(driver):
    """Verified against dovecot:2.4.3: the image ships no rm, ls or find.

    A filesystem wipe of /srv/vmail therefore fails, `|| true` swallows it, and
    every capture silently inherits the previous one's inbox.
    """
    d, runner = driver
    d.reset()
    joined = [" ".join(call) for call in runner.calls]
    assert any("doveadm expunge" in c for c in joined), joined
    # -u, not -A: the testing image's passdb accepts any account and can list
    # none of them, so -A fails with "userdb list: User listing returned failure".
    assert any("-u agent@showAndTell.test" in c for c in joined), joined
    # A wildcard so a message filed into another folder during setup also goes.
    assert any("mailbox * all" in c for c in joined), joined


def test_reset_never_shells_out_to_rm_inside_dovecot(driver):
    d, runner = driver
    dovecot = [" ".join(c) for c in runner.calls if "dovecot" in c]
    d.reset()
    dovecot = [" ".join(c) for c in runner.calls if "dovecot" in c]
    assert not any("rm " in c for c in dovecot), dovecot


def test_reset_clears_the_roundcube_message_cache(driver):
    """Roundcube's sqlite db lists mail that no longer exists after an expunge."""
    d, runner = driver
    d.reset()
    joined = [" ".join(call) for call in runner.calls]
    assert any("/var/roundcube/db" in c for c in joined), joined


def test_reset_restarts_services_after_wiping_state(driver):
    d, runner = driver
    d.reset()
    joined = [" ".join(call) for call in runner.calls]
    wipe = max(i for i, c in enumerate(joined) if "doveadm expunge" in c)
    restart = max(i for i, c in enumerate(joined) if "restart" in c)
    # Dovecot caches mailbox state; a wipe that is not followed by a restart
    # leaves the previous capture's mail visible over IMAP.
    assert restart > wipe, joined
    assert any("restart dovecot roundcube mailpit" in c for c in joined), joined


def test_secrets_publish_imap_wiring(driver):
    d, _ = driver
    secrets = d.secrets()
    assert secrets["mail_password"] == "showAndTell-mail"
    assert secrets["imap_port"] == 1143
    # Agent-backed fixtures are assume_running, which today derives plaintext.
    # The driver owns the container, so the driver states the answer.
    assert secrets["imap_starttls"] is True


def test_snapshot_family_is_unsupported(driver):
    d, _ = driver
    for call in (lambda: d.snapshot("x"), lambda: d.restore("x"),
                 lambda: d.baseline(), lambda: d.golden(),
                 lambda: d.fetch_snapshot("x")):
        with pytest.raises(Unsupported):
            call()


def test_compose_file_consumes_every_variable_the_driver_publishes(tmp_path):
    """A remote host publishes off loopback only if compose reads the override."""
    manifest = app_manifest("roundcube")
    compose = manifest.compose_file.read_text()
    d = RoundcubeDriver(manifest, root=tmp_path, runner=FakeRunner())
    for name in d._compose_env():
        assert f"${{{name}" in compose, f"{name} is published but never read"


def test_roundcube_send_uses_an_internal_smtp_sink_without_imap_credentials():
    """Dovecot submission is a proxy, not an SMTP delivery backend.

    Pointing Roundcube at it without a relay accepts authentication and then
    rejects every message with 451. The fixture's SMTP sink must be internal
    and Roundcube must not forward its IMAP password to that unauthenticated
    test service.
    """
    manifest = app_manifest("roundcube")
    compose = manifest.compose_file.read_text()
    config = (manifest.compose_file.parent / "config" /
              "showAndTell-tls.inc.php").read_text()

    assert "axllent/mailpit:v1.30.0" in compose
    assert "ROUNDCUBEMAIL_SMTP_SERVER: mailpit" in compose
    assert 'ROUNDCUBEMAIL_SMTP_PORT: "1025"' in compose
    assert "$config['smtp_user'] = '';" in config
    assert "$config['smtp_pass'] = '';" in config


def test_wait_healthy_timeout_includes_diagnostics(tmp_path):
    ps_output = b"CONTAINER   IMAGE   STATUS\ndovecot  img1  Up 2s\nroundcube  img2  Exited"
    d = RoundcubeDriver(
        app_manifest("roundcube"), root=tmp_path,
        runner=FakeRunner(ps_output=ps_output),
        probe=lambda url: False, imap_probe=lambda host, port: False,
        sleep=lambda s: None,
    )
    with pytest.raises(DriverError) as excinfo:
        d._wait_healthy(budget_s=0.1)
    assert "Docker Compose State:" in str(excinfo.value)
    assert ps_output.decode() in str(excinfo.value)


def test_explicit_ports_beat_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_ROUNDCUBE_PORT", "9999")
    monkeypatch.setenv("SHOWANDTELL_IMAP_PORT", "9998")
    driver = RoundcubeDriver(app_manifest("roundcube"), root=tmp_path,
                             port=8182, imap_port=1243)
    assert driver.port == 8182
    assert driver.imap_port == 1243
