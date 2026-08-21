"""Managed Chrome profile for product adapters (Claude-in-Chrome, etc.).

`showAndTell browser setup --clone` copies your daily profile into a managed
one — extensions and logins ride along, because Chrome's cookie-encryption
key lives in the macOS Keychain, not the profile folder, so a same-machine
copy stays signed in. `--fresh` creates an empty profile for a one-time
manual login instead. `showAndTell browser launch` starts Chrome on the managed
profile with a CDP port the ghost operator can attach to.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome_bin() -> str:
    """The Chrome executable for this platform. SHOWANDTELL_CHROME_BIN overrides
    (resolved at call time so runs and tests can retarget it)."""
    env = os.environ.get("SHOWANDTELL_CHROME_BIN")
    if env:
        return env
    if sys.platform == "darwin":
        return _MAC_CHROME
    return (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
            or shutil.which("chromium") or "google-chrome")


def system_chrome_root() -> Path:
    """The daily-driver Chrome user-data dir --clone copies from. Note the
    macOS docstring caveat above (Keychain-held cookie key) is darwin-only:
    on Linux the key sits in the OS keyring, so a clone may land logged out —
    prefer `--fresh` + one manual login on a VM."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


MANAGED_ROOT = Path(os.environ.get(
    "SHOWANDTELL_CHROME_PROFILE", Path.home() / ".showAndTell/chrome-profile"
)).expanduser()
DEFAULT_CDP_PORT = 9223
PROFILE_NAME = "showAndTell"
BRACKETT_EXTENSION_ID = "kfmoekefkbopjebgmgnlhachbiinhdmo"
BRACKETT_WEBSTORE_URL = (
    "https://chromewebstore.google.com/detail/brackett/"
    f"{BRACKETT_EXTENSION_ID}"
)

# Heavy, regenerable, or machine-bound dirs that must not be cloned.
CLONE_EXCLUDES = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker", "Crashpad",
    "component_crx_cache", "optimization_guide_model_store", "BrowserMetrics",
}


def available_cdp_port(preferred: int, *, unavailable=()) -> int:
    """Return ``preferred`` when free, otherwise another free loopback port.

    The socket is released before Chrome starts, so this is a best-effort
    collision check for the single-run local workflow, not a reservation.
    """
    if not 1 <= preferred <= 65535:
        raise ValueError("CDP port must be between 1 and 65535")
    excluded = set(unavailable)
    if preferred not in excluded:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
            except OSError:
                pass
            else:
                return preferred
    for _attempt in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            selected = probe.getsockname()[1]
        if selected not in excluded:
            return selected
    raise RuntimeError("could not find a free local CDP port")


def ensure_managed_profile(profile_root: Path = MANAGED_ROOT) -> bool:
    """Create a safe empty managed profile when none exists.

    Returns True when the profile was created. This never reads or copies the
    user's daily Chrome profile; product extensions and logins remain an
    explicit one-time browser setup step.
    """
    default = Path(profile_root) / "Default"
    existed = default.is_dir()
    default.mkdir(parents=True, exist_ok=True)
    _set_profile_name(profile_root)
    return not existed


def _set_profile_name(profile_root: Path) -> None:
    """Give the one managed Chrome profile its stable visible name."""
    root = Path(profile_root)
    preferences = root / "Default" / "Preferences"
    try:
        data = json.loads(preferences.read_text()) if preferences.exists() else {}
    except (OSError, json.JSONDecodeError):
        return
    profile = data.setdefault("profile", {})
    if profile.get("name") != PROFILE_NAME:
        profile["name"] = PROFILE_NAME
        preferences.write_text(json.dumps(data))

    # Chrome also caches the profile picker's display name in Local State.
    # Keep both stores aligned so a cloned profile cannot retain its old name.
    local_state = root / "Local State"
    try:
        state = json.loads(local_state.read_text()) if local_state.exists() else {}
    except (OSError, json.JSONDecodeError):
        return
    cached = state.setdefault("profile", {}).setdefault(
        "info_cache", {}).setdefault("Default", {})
    if cached.get("name") != PROFILE_NAME:
        cached["name"] = PROFILE_NAME
        local_state.write_text(json.dumps(state))


def brackett_extension_path(profile_root: Path = MANAGED_ROOT) -> Path | None:
    """Return the newest installed Chrome Web Store build of Brackett."""
    extension_root = (
        Path(profile_root) / "Default" / "Extensions" / BRACKETT_EXTENSION_ID
    )
    manifests = list(extension_root.glob("*/manifest.json"))
    if not manifests:
        return None
    return max(
        (manifest.parent for manifest in manifests),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )


def brackett_extension_installed(profile_root: Path = MANAGED_ROOT) -> bool:
    """Whether the Chrome Web Store build of Brackett is in this profile."""
    return brackett_extension_path(profile_root) is not None


def clone_profile(src_root: Path, dst_root: Path, profile: str = "Default") -> Path:
    """Copy one profile out of a Chrome user-data dir into a managed user-data
    dir, always named Default there so launch flags stay uniform."""
    src_profile = src_root / profile
    if not src_profile.is_dir():
        raise FileNotFoundError(f"no Chrome profile at {src_profile}")
    shutil.rmtree(dst_root, ignore_errors=True)
    dst_root.mkdir(parents=True)

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        return CLONE_EXCLUDES & set(names)

    shutil.copytree(src_profile, dst_root / "Default", ignore=_ignore)
    local_state = src_root / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, dst_root / "Local State")
    _set_profile_name(dst_root)
    return dst_root


def wait_for_cdp(port: int, timeout_s: float = 20, poll_s: float = 0.5) -> bool:
    """Poll a CDP endpoint until it answers; False on timeout."""
    deadline = time.time() + timeout_s
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            return True
        except Exception:
            if time.time() >= deadline:
                return False
            time.sleep(poll_s)


def _silence_password_popups(user_data_dir: Path) -> None:
    """Turn off the profile prefs behind Chrome's focus-stealing password
    dialogs. The harness logs the fixtures in with throwaway creds (e.g.
    WebArena's byteblaze/hello1234), which trip Chrome's data-breach check and
    raise a 'compromised password' dialog right after sign-in; the 'save
    password?' bubble is the same problem. Both render as overlays that steal
    focus, so the demo's OS-level clicks (and even Playwright's actionability
    checks) land on the dialog instead of the page — the cause of the
    'OS action did not take effect' / selector-timeout demo failures. Written
    before each launch, while Chrome is down. No-op until the profile exists."""
    prefs_path = user_data_dir / "Default" / "Preferences"
    if not prefs_path.exists():
        return
    try:
        data = json.loads(prefs_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    data["credentials_enable_service"] = False              # no 'save password?' bubble
    data.setdefault("profile", {})["password_manager_leak_detection"] = False  # no breach dialog
    prefs_path.write_text(json.dumps(data))


def launch(user_data_dir: Path = MANAGED_ROOT, port: int = DEFAULT_CDP_PORT,
           url: str | None = None, extra_args: list[str] | None = None) -> subprocess.Popen:
    ensure_managed_profile(user_data_dir)
    _silence_password_popups(user_data_dir)
    cmd = [chrome_bin(), f"--user-data-dir={user_data_dir}",
           "--profile-directory=Default", "--no-first-run",
           # Belt-and-suspenders with the profile prefs above: kill the leak
           # check at the feature level too, so no 'compromised password'
           # overlay can steal focus mid-demo.
           "--disable-features=PasswordLeakDetection",
           f"--remote-debugging-port={port}", *(extra_args or [])]
    if url:
        cmd.append(url)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


WINDOW_BOUNDS = {"left": 60, "top": 60, "width": 1500, "height": 940}


def window_bounds() -> dict:
    """CDP bounds for the teach window; maximize unless explicitly fixed.

    CDP rejects ``windowState`` combined with geometry, so the two modes
    return disjoint dictionaries. ``SHOWANDTELL_WINDOW_STATE=fixed`` keeps the
    old deterministic 1500x940 frame for debugging and constrained displays.
    """
    if os.environ.get("SHOWANDTELL_WINDOW_STATE") == "fixed":
        return WINDOW_BOUNDS
    return {"windowState": "maximized"}


def kill_managed_chrome(profile_root: Path = MANAGED_ROOT) -> None:
    subprocess.run(["pkill", "-f", f"[u]ser-data-dir={profile_root}"], check=False)
    time.sleep(1.5)


def kill_all_managed_chrome(profile_root: Path = MANAGED_ROOT) -> None:
    """Close the ShowAndTell-managed Chrome window without touching daily Chrome."""
    root = Path(profile_root)
    output = subprocess.run(
        ["ps", "-Axo", "pid=,command="], capture_output=True, text=True,
    ).stdout
    pids = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid_text, command = fields
        if "Google Chrome.app/Contents/MacOS/Google Chrome" not in command:
            continue
        if "--type=" in command:
            continue
        if f"--user-data-dir={root}" in command:
            pids.append(int(pid_text))
    if not pids:
        return
    subprocess.run(["kill", "-TERM", *map(str, pids)], check=False)
    deadline = time.time() + 2
    remaining = list(pids)
    while remaining and time.time() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if Path(f"/proc/{pid}").exists()] \
            if sys.platform != "darwin" else [
                pid for pid in remaining
                if subprocess.run(["kill", "-0", str(pid)],
                                  capture_output=True).returncode == 0
            ]
    if remaining:
        subprocess.run(["kill", "-KILL", *map(str, remaining)], check=False)


def launch_managed_chrome(port: int, extra_args: list[str] | None = None,
                          profile_root: Path = MANAGED_ROOT) -> int:
    if not (profile_root / "Default").exists():
        # First run on this machine: start a fresh profile instead of dying.
        # (The old SystemExit escaped the capture thread's `except Exception`
        # and surfaced only as a 45s 'did not become ready' timeout.) Flows
        # that need a signed-in profile still raise their own specific errors;
        # `showAndTell browser setup --clone` carries logins/extensions over.
        print(f"no managed Chrome profile at {profile_root} — creating a fresh one")
        (profile_root / "Default").mkdir(parents=True)
    # Auto-accept the mic permission so a recording product's voice capture
    # starts without a dialog; the real default mic then hears the narration
    # played aloud.
    proc = launch(user_data_dir=profile_root, port=port,
                  extra_args=["--use-fake-ui-for-media-stream", *(extra_args or [])])
    if not wait_for_cdp(port):
        raise RuntimeError("managed Chrome did not come up")
    return proc.pid


def await_target(cdp_port: int, match, timeout_s: float = 10.0) -> str | None:
    """webSocketDebuggerUrl of the first CDP target matching `match`, polling
    /json/list until timeout; None if it never appears."""
    deadline = time.time() + timeout_s
    while True:
        try:
            targets = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/list"))
        except Exception:
            targets = []
        for t in targets:
            if match(t):
                return t["webSocketDebuggerUrl"]
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def cdp_rpc(ws_url: str, method: str, params: dict, timeout_s: float = 30) -> dict:
    """One CDP command over a raw websocket to any target; returns its result.

    Runs in a dedicated thread with the timeout INSIDE the coroutine: callers
    may sit inside Playwright's sync API, whose thread already owns an event
    loop asyncio.run would trip on, and a future-side timeout would unwind
    into the executor's blocking shutdown and hang on a dead target. The
    imports are local so the viewer server (which loads this module at
    startup) doesn't pay for asyncio + websockets on every boot.
    """
    import asyncio
    import concurrent.futures

    import websockets

    async def go():
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run,
                           asyncio.wait_for(go(), timeout_s)).result(timeout=timeout_s + 10)


def cdp_target_rpc(browser_ws_url: str, target_id: str, method: str,
                   params: dict, timeout_s: float = 30) -> dict:
    """Send one command to a target that has no standalone debugger URL.

    Out-of-process iframes are discoverable through ``Target.getTargets`` but
    are not reliably listed by Chrome's ``/json/list`` endpoint. Attach from
    the browser target and issue the command over that flattened session in a
    single websocket lifetime; closing the socket then cleans up the session.
    """
    import asyncio
    import concurrent.futures

    import websockets

    async def go():
        async with websockets.connect(browser_ws_url, max_size=50_000_000) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            }))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 1:
                    if message.get("error"):
                        raise RuntimeError(str(message["error"])[:300])
                    session_id = message.get("result", {}).get("sessionId")
                    if not session_id:
                        raise RuntimeError("CDP target attach returned no session")
                    break
            await ws.send(json.dumps({
                "id": 2, "sessionId": session_id,
                "method": method, "params": params,
            }))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 2 and message.get("sessionId") == session_id:
                    if message.get("error"):
                        raise RuntimeError(str(message["error"])[:300])
                    return message.get("result", {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run,
                           asyncio.wait_for(go(), timeout_s)).result(timeout=timeout_s + 10)


def cdp_eval(ws_url: str, expr: str, timeout_s: float = 30):
    """Runtime.evaluate in any CDP target (extension service worker or page),
    always with userGesture set."""
    result = cdp_rpc(ws_url, "Runtime.evaluate",
                     {"expression": expr, "returnByValue": True,
                      "awaitPromise": True, "userGesture": True}, timeout_s)
    if result.get("exceptionDetails"):
        raise RuntimeError(str(result["exceptionDetails"])[:300])
    return result.get("result", {}).get("value")
