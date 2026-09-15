"""Browser tests for Fleetbase's login surface (slow: chromium).

Fleetbase's Ember ``console`` route is mounted at ``/``, so an authenticated
session lives at the bare root or under ``/fleet-ops`` -- and the root is also
where an unauthenticated visitor is bounced. ``login`` therefore cannot decide
from the URL in either direction, and these pin what it decides instead. They
drive the real entry point against served markup, because the verdict is only
trustworthy if Playwright's own locator semantics are the thing producing it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

from showAndTell.applications.browser.runtime import origin
from showAndTell.applications.fleetbase import browser as fleet

pytestmark = pytest.mark.slow

APP = "http://fleet.test"
CREDENTIALS = {"email": "operator@example.test", "password": "secret"}

# The console banner as the capture bundle records it: an "Extension
# navigation" menubar holding one menuitem per installed extension. The
# menubar is the console's own chrome and the only on-screen proof of a live
# session.
DASHBOARD = """
<header>
  <a href="/">Fleetbase</a>
  <div role="menubar" aria-label="Extension navigation">
    <a role="menuitem" href="/fleet-ops">Fleet-Ops</a>
    <a role="menuitem" href="/storefront">Storefront</a>
  </div>
</header>
"""
BOOTING = "<div>console has not painted anything yet</div>"
# How an authenticated visitor leaves /auth: Fleetbase's Ember router bounces
# them from the client, not with an HTTP redirect.
BOUNCE_TO = '<meta http-equiv="refresh" content="0;url={destination}">'
SIGN_IN_FORM = """
<label for="email">Email address</label>
<input id="email" type="text">
<label for="password">Password</label>
<input id="password" type="password">
<button onclick="location.href='{destination}'">Sign in</button>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        chromium = pw.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    yield page
    page.close()


def serve(page, routes: dict[str, str]) -> None:
    """Answer every request from ``routes``, keyed by path within ``APP``.

    A key may also be an absolute URL, which is how a landing on a foreign
    origin gets served.
    """

    def handler(route):
        parts = urlsplit(route.request.url)
        request_origin = origin(parts)
        body = routes.get(f"{request_origin}{parts.path}")
        if body is None and request_origin == APP:
            body = routes.get(parts.path or "/")
        if body is None:
            route.fulfill(status=404, content_type="text/html", body="not here")
        else:
            route.fulfill(
                status=200, content_type="text/html",
                body=f"<html><body>{body}</body></html>")

    page.route("**/*", handler)


def test_login_accepts_an_authenticated_root_dashboard(page):
    """The session is already live; /auth bounces to / and there is no form."""
    serve(page, {"/auth": BOUNCE_TO.format(destination="/"), "/": DASHBOARD})

    fleet.login(page, APP, CREDENTIALS)

    assert urlsplit(page.url).path == "/"


def test_login_accepts_an_authenticated_extension_route(page):
    """The route the console actually spends its time on. Nothing about
    /fleet-ops reads as authenticated from the URL, and it does not have to."""
    serve(page, {
        "/auth": BOUNCE_TO.format(destination="/fleet-ops/manage/vehicles"),
        "/fleet-ops/manage/vehicles": DASHBOARD,
    })

    fleet.login(page, APP, CREDENTIALS)

    assert urlsplit(page.url).path == "/fleet-ops/manage/vehicles"


def test_login_rejects_a_root_without_the_console_shell(page, monkeypatch):
    """A bare root that never paints the console is not an authenticated
    surface, however much it looks like one from the URL."""
    monkeypatch.setattr(fleet, "READY_TIMEOUT_MS", 1_000)
    serve(page, {"/auth": BOUNCE_TO.format(destination="/"), "/": BOOTING})

    with pytest.raises(RuntimeError, match="login or authenticated surface"):
        fleet.login(page, APP, CREDENTIALS)


def test_login_signs_in_through_the_visible_form(page):
    """The path that already worked before any of this, kept working."""
    serve(page, {
        "/auth": SIGN_IN_FORM.format(destination="/console"),
        "/console": DASHBOARD,
    })

    fleet.login(page, APP, CREDENTIALS)

    assert urlsplit(page.url).path == "/console"


def test_login_rejects_a_signed_in_route_that_never_paints(page, monkeypatch):
    """Authenticated is not the same as usable. A route cannot buy a pass on
    its name: handing a replay a bundle that has painted nothing defers the
    failure to the first gesture, where the cause is out of sight."""
    monkeypatch.setattr(fleet, "READY_TIMEOUT_MS", 1_000)
    serve(page, {
        "/auth": SIGN_IN_FORM.format(destination="/console"),
        "/console": BOOTING,
    })

    with pytest.raises(RuntimeError, match="login did not expose"):
        fleet.login(page, APP, CREDENTIALS)


def test_login_rejects_a_console_on_a_foreign_origin(page):
    """A painted console is not this console. The fixture host dictates the
    origin, so leaving it means the session belongs to something else."""
    serve(page, {
        "/auth": SIGN_IN_FORM.format(destination="http://elsewhere.test/"),
        "http://elsewhere.test/": DASHBOARD,
    })

    with pytest.raises(RuntimeError, match="crossed to an unexpected origin"):
        fleet.login(page, APP, CREDENTIALS)


def test_login_rejects_a_form_that_never_reaches_a_session(page, monkeypatch):
    """Submitting and going nowhere has to fail loudly rather than hand a
    replay a login page it will try to drive gestures against."""
    monkeypatch.setattr(fleet, "READY_TIMEOUT_MS", 1_000)
    serve(page, {"/auth": SIGN_IN_FORM.format(destination="/auth")})

    with pytest.raises(RuntimeError, match="login did not expose"):
        fleet.login(page, APP, CREDENTIALS)
