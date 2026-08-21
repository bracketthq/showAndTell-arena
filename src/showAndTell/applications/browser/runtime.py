"""Runtime routing shared by capture and replay browser surfaces.

Application browser planes own product-specific login and readiness behavior.
This module locates those planes and relocates captured URLs and credentials
onto the live application session. It deliberately knows nothing about how a
demonstration was captured, compiled, taught, or graded.
"""
from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from .context import CONTEXT_KEY


def origin(url: str | SplitResult) -> str:
    """The scheme://host:port an address belongs to, with nothing after it.

    Accepts already-split parts so a caller that has parsed the address for
    its own checks derives the origin without a second parse.
    """
    parsed = urlsplit(url) if isinstance(url, str) else url
    return f"{parsed.scheme}://{parsed.netloc}"


def browser_plane(application: str | None):
    """Return the browser plane shipped by an application, or ``None``."""
    from ..registry import UnknownApplication, default_registry

    if not application:
        return None
    registry = default_registry()
    if application in registry.names() and registry.has_browser(application):
        try:
            return registry.browser(application)
        except UnknownApplication:
            pass
    return None


def wait_ready(page, application: str) -> bool:
    """Block until an application's browser plane says it can be driven."""
    ready = getattr(browser_plane(application), "ready", None)
    if not callable(ready):
        return False
    ready(page)
    return True


def wait_after_login(page, application: str) -> None:
    """Wait for the authenticated landing page to become usable."""
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        # Polling applications may never become literally idle. Their browser
        # plane remains the authority on whether the surface is usable.
        pass
    wait_ready(page, application)


def _runtime_application(credentials, application: str | None) -> dict:
    """Return an application's live entry from task browser credentials."""
    if not isinstance(credentials, dict) or not application:
        return {}
    context = credentials.get(CONTEXT_KEY)
    entry = context.get(application) if isinstance(context, dict) else None
    return entry if isinstance(entry, dict) else {}


def runtime_application_url(
    credentials,
    application: str,
    captured_url: str,
    captured_surface_url: str,
) -> str:
    """Relocate a captured supporting-app URL onto the live task runtime."""
    entry = _runtime_application(credentials, application)
    browser_url = entry.get("browser_url") or entry.get("url")
    if not isinstance(browser_url, str) or not browser_url:
        return captured_url

    normalizer = getattr(browser_plane(application), "normalize_replay_url", None)

    def normalized(url: str) -> str:
        return normalizer(url) if callable(normalizer) else url

    surface_base = str(captured_surface_url).rstrip("/")
    if captured_url.rstrip("/") == surface_base:
        return normalized(browser_url)
    if surface_base and captured_url.startswith(surface_base):
        return normalized(
            browser_url.rstrip("/") + captured_url[len(surface_base):]
        )

    service_url = entry.get("url")
    captured_origin = origin(captured_url)
    if (
        isinstance(service_url, str)
        and service_url
        and captured_url.startswith(captured_origin)
    ):
        return normalized(
            service_url.rstrip("/") + captured_url[len(captured_origin):]
        )
    return captured_url


def auto_login(
    page,
    surface: dict,
    *,
    app_url: str | None = None,
    credentials: dict | None = None,
) -> bool:
    """Sign into a surface through its application-owned browser plane."""
    entry = _runtime_application(credentials, surface.get("application"))
    if entry.get("credentials"):
        creds = dict(entry["credentials"])
    elif credentials is not None:
        creds = {
            key: value for key, value in credentials.items() if key != CONTEXT_KEY
        }
    else:
        creds = dict(surface.get("credentials") or {})
    if not (creds.get("email") or creds.get("password")):
        return False
    adapter = browser_plane(surface.get("application") or surface.get("id"))
    if adapter is None:
        return False
    base = str(entry.get("url") or app_url or origin(surface["url"])).rstrip("/")
    adapter.login(page, base, creds)
    return True
