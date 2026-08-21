import json
import socket
from pathlib import Path
from types import SimpleNamespace

from showAndTell.students import brackett as brackett_chrome
from showAndTell.core import chrome
from showAndTell import cli


def _fake_chrome_root(root: Path) -> Path:
    default = root / "Default"
    (default / "Extensions" / "abc123" / "1.0").mkdir(parents=True)
    (default / "Extensions" / "abc123" / "1.0" / "manifest.json").write_text("{}")
    (default / "Service Worker" / "CacheStorage").mkdir(parents=True)
    (default / "Service Worker" / "CacheStorage" / "big.bin").write_text("x" * 1000)
    (default / "Cache").mkdir()
    (default / "Cache" / "junk").write_text("junk")
    (default / "Cookies").write_text("cookie-db")
    (root / "Local State").write_text(json.dumps({"profile": {}}))
    return root


def test_wait_for_cdp_polls_until_endpoint_answers():
    import http.server
    import threading

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    assert not chrome.wait_for_cdp(1, timeout_s=0.3)  # port 1: nothing listening
    srv = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert chrome.wait_for_cdp(srv.server_port, timeout_s=5)
    finally:
        srv.shutdown()


def test_clone_profile_copies_essentials_and_skips_caches(tmp_path):
    src = _fake_chrome_root(tmp_path / "src")
    dst = tmp_path / "dst"
    chrome.clone_profile(src, dst, profile="Default")
    assert (dst / "Default" / "Cookies").read_text() == "cookie-db"
    assert (dst / "Default" / "Extensions" / "abc123" / "1.0" / "manifest.json").exists()
    assert (dst / "Local State").exists()
    assert not (dst / "Default" / "Service Worker").exists()
    assert not (dst / "Default" / "Cache").exists()


def test_clone_profile_renames_source_profile_to_default(tmp_path):
    src = tmp_path / "src"
    (src / "Profile 3").mkdir(parents=True)
    (src / "Profile 3" / "Cookies").write_text("p3")
    (src / "Local State").write_text("{}")
    chrome.clone_profile(src, tmp_path / "dst", profile="Profile 3")
    assert (tmp_path / "dst" / "Default" / "Cookies").read_text() == "p3"


def test_clone_profile_wipes_previous_clone(tmp_path):
    src = _fake_chrome_root(tmp_path / "src")
    dst = tmp_path / "dst"
    stale = dst / "Default" / "stale-file"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    chrome.clone_profile(src, dst)
    assert not stale.exists()


def test_ensure_managed_profile_creates_named_showAndTell_profile(tmp_path):
    root = tmp_path / "managed"

    assert chrome.ensure_managed_profile(root)
    assert (root / "Default").is_dir()
    preferences = json.loads((root / "Default" / "Preferences").read_text())
    assert preferences["profile"]["name"] == "showAndTell"
    local_state = json.loads((root / "Local State").read_text())
    assert local_state["profile"]["info_cache"]["Default"]["name"] == "showAndTell"
    assert not chrome.ensure_managed_profile(root)


def test_available_cdp_port_falls_back_when_preferred_is_busy():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        busy = occupied.getsockname()[1]

        selected = chrome.available_cdp_port(busy)

    assert selected != busy
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", selected))


def test_available_cdp_port_honors_exclusions():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as preferred:
        preferred.bind(("127.0.0.1", 0))
        port = preferred.getsockname()[1]
    assert chrome.available_cdp_port(port, unavailable={port}) != port


def test_clone_profile_renames_the_managed_profile_showAndTell(tmp_path):
    src = _fake_chrome_root(tmp_path / "src")

    target = chrome.clone_profile(src, tmp_path / "dst")

    preferences = json.loads((target / "Default" / "Preferences").read_text())
    assert preferences["profile"]["name"] == "showAndTell"
    local_state = json.loads((target / "Local State").read_text())
    assert local_state["profile"]["info_cache"]["Default"]["name"] == "showAndTell"


def test_brackett_extension_installed_uses_web_store_id(tmp_path):
    root = tmp_path / "managed"
    assert not chrome.brackett_extension_installed(root)

    manifest = (root / "Default" / "Extensions" /
                chrome.BRACKETT_EXTENSION_ID / "1.2.3_0" / "manifest.json")
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    assert chrome.brackett_extension_installed(root)
    assert chrome.brackett_extension_path(root) == manifest.parent


def test_browser_launch_opens_brackett_store_when_missing(monkeypatch, capsys):
    launched = {}
    monkeypatch.setattr(chrome, "brackett_extension_installed", lambda: False)
    monkeypatch.setattr(chrome, "available_cdp_port", lambda port, **_kwargs: port)
    monkeypatch.setattr(
        chrome, "launch",
        lambda **kwargs: launched.update(kwargs),
    )

    cli._cmd_browser_launch(SimpleNamespace(port=9223, url=None))

    assert launched["url"] == chrome.BRACKETT_WEBSTORE_URL
    assert "Add to Chrome" in capsys.readouterr().out


def test_browser_launch_opens_brackett_sign_in_when_installed(monkeypatch, capsys):
    launched = {}
    monkeypatch.setattr(chrome, "brackett_extension_installed", lambda: True)
    monkeypatch.setattr(chrome, "available_cdp_port", lambda port, **_kwargs: port)
    monkeypatch.setattr(
        brackett_chrome, "brackett_url", lambda: "https://brackett.example/")
    monkeypatch.setattr(
        chrome, "launch",
        lambda **kwargs: launched.update(kwargs),
    )

    cli._cmd_browser_launch(SimpleNamespace(port=9223, url=None))

    assert launched["url"] == "https://brackett.example/"
    assert "Sign in" in capsys.readouterr().out


def test_browser_launch_reports_automatic_port_fallback(monkeypatch, capsys):
    launched = {}
    monkeypatch.setattr(chrome, "brackett_extension_installed", lambda: True)
    monkeypatch.setattr(chrome, "available_cdp_port", lambda *_args, **_kwargs: 19456)
    monkeypatch.setattr(
        brackett_chrome, "brackett_url", lambda: "https://brackett.example/")
    monkeypatch.setattr(chrome, "launch", lambda **kwargs: launched.update(kwargs))

    cli._cmd_browser_launch(SimpleNamespace(port=9223, url=None))

    assert launched["port"] == 19456
    assert "9223 is busy; using 19456" in capsys.readouterr().out


def test_chrome_bin_env_override_wins(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_CHROME_BIN", "/opt/weird/chrome")
    assert chrome.chrome_bin() == "/opt/weird/chrome"


def test_chrome_bin_darwin_is_the_app_bundle(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_CHROME_BIN", raising=False)
    monkeypatch.setattr(chrome.sys, "platform", "darwin")
    assert chrome.chrome_bin() == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_chrome_bin_linux_prefers_google_chrome_on_path(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_CHROME_BIN", raising=False)
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(chrome.shutil, "which",
                        lambda n: "/usr/bin/google-chrome" if n == "google-chrome" else None)
    assert chrome.chrome_bin() == "/usr/bin/google-chrome"


def test_system_chrome_root_per_platform(monkeypatch):
    monkeypatch.setattr(chrome.sys, "platform", "darwin")
    assert chrome.system_chrome_root() == Path.home() / "Library/Application Support/Google/Chrome"
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    assert chrome.system_chrome_root() == Path.home() / ".config/google-chrome"


def test_window_bounds_default_is_maximized(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_WINDOW_STATE", raising=False)
    assert chrome.window_bounds() == {"windowState": "maximized"}


def test_window_bounds_fixed_env(monkeypatch):
    # CDP rejects windowState combined with a geometry, so maximized mode
    # and fixed mode must return disjoint dictionaries.
    monkeypatch.setenv("SHOWANDTELL_WINDOW_STATE", "fixed")
    assert chrome.window_bounds() == chrome.WINDOW_BOUNDS


class _FakeChromeProc:
    pid = 4242


def _stub_chrome_launch(monkeypatch, launches: list):
    def fake_launch(user_data_dir, port, url=None, extra_args=None):
        launches.append(user_data_dir)
        return _FakeChromeProc()

    monkeypatch.setattr(chrome, "launch", fake_launch)
    monkeypatch.setattr(chrome, "wait_for_cdp", lambda port: True)


def test_launch_managed_chrome_creates_a_missing_profile(monkeypatch, tmp_path):
    """A brand-new machine has no managed profile yet. Launch must create a
    fresh one and proceed — the old SystemExit escaped the capture thread's
    `except Exception` (SystemExit is a BaseException), so the viewer hung
    45s and reported only 'managed Chrome capture did not become ready'."""
    launches: list = []
    _stub_chrome_launch(monkeypatch, launches)
    root = tmp_path / "chrome-profile"
    pid = chrome.launch_managed_chrome(9223, profile_root=root)
    assert pid == 4242
    assert (root / "Default").is_dir()
    assert launches == [root]


def test_launch_managed_chrome_keeps_an_existing_profile(monkeypatch, tmp_path):
    """Only a MISSING profile is created; an existing one is never reset."""
    launches: list = []
    _stub_chrome_launch(monkeypatch, launches)
    root = tmp_path / "chrome-profile"
    marker = root / "Default" / "Preferences"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    chrome.launch_managed_chrome(9223, profile_root=root)
    assert marker.read_text() == "{}"
    assert launches == [root]
