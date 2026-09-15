"""A compose application can be pointed at an already-deployed instance.

Some compose applications cannot be brought up beside a replay: Fleetbase's
seeders abort against an existing database, so the local project never becomes
healthy and the task cannot be verified at all. Assuming the deployed instance
is running is what makes those tasks testable, and it must not manage — start,
stop or restart — something this run does not own.
"""
from __future__ import annotations

import pytest

from showAndTell.applications.lifecycle.compose import ComposeDriver


class FakeManifest:
    name = "fleetbase"
    env: dict[str, str] = {}
    compose_file = "/nowhere/compose.yaml"
    health_http = "/auth"
    external = False

    class app_port:  # noqa: N801 - mirrors the manifest's attribute shape
        host = 8045


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        raise AssertionError(f"compose must not be invoked: {argv}")


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_FLEETBASE_ASSUME_RUNNING", "1")
    monkeypatch.setenv(
        "SHOWANDTELL_FLEETBASE_PUBLIC_URL", "http://203.0.113.10:8045")
    return ComposeDriver(
        FakeManifest(), runner=RecordingRunner(),
        probe=lambda url: True, sleep=lambda _: None)


def test_health_is_asked_of_where_the_instance_was_published(driver):
    """A deployed instance is not on this machine, so 127.0.0.1 cannot answer."""
    assert driver._health_url() == "http://203.0.113.10:8045/auth"


def test_start_does_not_bring_up_an_instance_it_does_not_own(driver):
    driver.start(wait=True)  # RecordingRunner raises if compose is invoked

    assert driver.runner.calls == []


def test_stop_and_reset_leave_a_shared_instance_alone(driver):
    driver.stop()
    driver.reset()

    assert driver.runner.calls == []


def test_fleetbase_does_not_redeploy_or_reseed_a_shared_instance(monkeypatch):
    """Fleetbase overrides ``start``/``reset``, so it needs the guard itself.

    Its start runs deploy.sh, an organization bootstrap and the demo seeders.
    Repeating those against an instance that already has that data is what
    fails with an integrity-constraint violation, so an assumed-running
    Fleetbase must do none of them.
    """
    from showAndTell.applications.fleetbase.driver import FleetbaseDriver

    monkeypatch.setenv("SHOWANDTELL_FLEETBASE_ASSUME_RUNNING", "1")
    monkeypatch.setenv(
        "SHOWANDTELL_FLEETBASE_PUBLIC_URL", "http://203.0.113.10:8045")

    class Manifest(FakeManifest):
        ports = {"api": type("p", (), {"host": 8046}),
                 "socket": type("p", (), {"host": 38045})}
        credentials = {"username": "u", "password": "p"}

    bootstrapped: list[object] = []
    driver = FleetbaseDriver(
        Manifest(), runner=RecordingRunner(), probe=lambda url: True,
        sleep=lambda _: None,
        bootstrap=lambda *a, **k: bootstrapped.append(a) or True)

    driver.start(wait=True)
    driver.reset()

    assert driver.runner.calls == []       # no compose up, deploy.sh or restart
    assert bootstrapped == []              # no organization bootstrap
    assert driver._api_health_url().startswith("http://203.0.113.10:8046")


def test_a_local_instance_is_still_managed_and_probed_locally(monkeypatch):
    """Without the override nothing changes: compose runs, health is local."""
    monkeypatch.delenv("SHOWANDTELL_FLEETBASE_ASSUME_RUNNING", raising=False)
    monkeypatch.delenv("SHOWANDTELL_FLEETBASE_PUBLIC_URL", raising=False)
    calls: list[list[str]] = []

    class Runner:
        def run(self, argv, **kwargs):
            calls.append(list(argv))
            return None

    local = ComposeDriver(FakeManifest(), runner=Runner(),
                          probe=lambda url: True, sleep=lambda _: None)

    assert local._health_url() == "http://127.0.0.1:8045/auth"
    local.start(wait=True)
    assert any("up" in call for call in calls)
