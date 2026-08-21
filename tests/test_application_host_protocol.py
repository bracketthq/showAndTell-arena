import subprocess

import pytest

from showAndTell.applications.host.protocol import CommandRunner, DriverError


def test_run_captures_stdout():
    result = CommandRunner().run(["printf", "hello"])
    assert result.stdout == b"hello"


def test_run_raises_driver_error_with_stderr_detail():
    with pytest.raises(DriverError) as excinfo:
        CommandRunner().run(["sh", "-c", "echo broken >&2; exit 3"])
    assert "broken" in str(excinfo.value)


def test_run_check_false_returns_failure():
    result = CommandRunner().run(["sh", "-c", "exit 3"], check=False)
    assert result.returncode == 3


def test_run_passes_stdin_and_env():
    result = CommandRunner().run(
        ["sh", "-c", "cat; printf %s \"$MARKER\""],
        stdin_bytes=b"in:", env={"MARKER": "extra"},
    )
    assert result.stdout == b"in:extra"


def test_run_closes_stdin_when_none():
    """stdin-reading commands complete immediately with empty stdout when stdin is None."""
    result = CommandRunner().run(["sh", "-c", "cat"])
    assert result.stdout == b""
    assert result.returncode == 0


def test_run_raises_driver_error_on_timeout():
    """A hung command (e.g. a wedged docker exec) must fail as DriverError,
    not let subprocess.TimeoutExpired escape into the caller unhandled.
    """
    with pytest.raises(DriverError) as excinfo:
        CommandRunner().run(["sleep", "5"], timeout=0.05)
    message = str(excinfo.value)
    assert "sleep" in message
    assert "timed out" in message
    assert "0.05" in message


def test_run_explains_when_docker_is_not_installed(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr("showAndTell.applications.host.protocol.subprocess.run", missing)
    with pytest.raises(DriverError) as excinfo:
        CommandRunner().run(["docker", "compose", "up"])
    message = str(excinfo.value)
    assert "Docker is required" in message
    assert "Install and start Docker Desktop" in message


def test_run_explains_when_docker_daemon_is_stopped(monkeypatch):
    stopped = subprocess.CompletedProcess(
        ["docker", "info"], 1, b"",
        b"Cannot connect to the Docker daemon. Is the docker daemon running?",
    )
    monkeypatch.setattr(
        "showAndTell.applications.host.protocol.subprocess.run",
        lambda *args, **kwargs: stopped,
    )
    with pytest.raises(DriverError) as excinfo:
        CommandRunner().run(["docker", "info"])
    message = str(excinfo.value)
    assert "installed but its daemon is not running" in message
    assert "Start Docker Desktop" in message
