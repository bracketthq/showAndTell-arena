"""OpenStreetMap browser operations for teach demonstrations.

Selectors target the stock openstreetmap-website UI (the Rails app WebArena's
`map` fixture serves on :3000): the geocoder sidebar renders one
`.search_results_entry` row per hit whose `a.set_position` link carries
`data-lat`/`data-lon` and whose leading text is Nominatim's kind prefix
("City", "Town", "County", ...), and the directions pane summarises a route in
`#routing_summary` ("Distance: 6.4km. Time: 0:08."). Unlike gitlab_ui, these
selectors are NOT yet verified against a live WebArena map instance — they are
written from the openstreetmap-website source and should be smoke-tested once
the fixture is up.

The site is browsed anonymously (there is no account and creds are empty), so
login() is a deliberate no-op kept only so every fixture's demonstrate.py can
call the same adapter surface. Sidebar content loads asynchronously after the
page, so search and directions poll like gitlab_ui.open_issue_iids does.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

ENGINE_CAR = "fossgis_osrm_car"
ENGINE_BIKE = "fossgis_osrm_bike"
ENGINE_FOOT = "fossgis_osrm_foot"
ENGINES = {"car": ENGINE_CAR, "bike": ENGINE_BIKE, "foot": ENGINE_FOOT}

_POLL_TRIES = 15
_POLL_MS = 1000

_DISTANCE_RE = re.compile(r"Distance:\s*([\d.,]+)\s*(km|m)", re.I)
_TIME_RE = re.compile(r"Time:\s*(\d+):(\d+)", re.I)


def login(page, app_url: str, creds: dict) -> None:
    """No-op: the map site has no accounts and is browsed anonymously, but
    every task's demonstrate() calls ops.login() first so the adapters can
    treat all fixtures uniformly (signature parity with gitlab_ui.login)."""


def goto_home(page, app_url: str) -> None:
    page.goto(app_url, wait_until="networkidle")


def search(page, app_url: str, query: str) -> list[dict]:
    """Sidebar geocoder results as [{"name", "kind", "lat", "lon"}].

    Each geocoder source is an AJAX call that lands after page load, so poll
    for `.search_results_entry` rows rather than trusting the first render.
    `name` is the result link's text, `kind` the Nominatim type prefix rendered
    before the link ("City", "Town", "River", ...), lat/lon come from the
    link's data attributes. Returns [] only when the poll window elapsed with
    no results (no match, or the geocoder errored)."""
    page.goto(f"{app_url}/search?query={quote_plus(query)}", wait_until="networkidle")
    for _ in range(_POLL_TRIES):
        results: list[dict] = []
        for row in page.query_selector_all(".search_results_entry"):
            link = row.query_selector("a.set_position[data-lat][data-lon]")
            if link is None:
                continue
            name = (link.inner_text() or "").strip()
            if not name:
                continue
            kind = (row.inner_text() or "").replace(name, "", 1).strip(" ,\t\n")
            results.append({"name": name, "kind": kind,
                            "lat": float(link.get_attribute("data-lat")),
                            "lon": float(link.get_attribute("data-lon"))})
        if results:
            return results
        page.wait_for_timeout(_POLL_MS)
    return []


def directions(page, app_url: str, engine: str, frm: tuple, to: tuple) -> dict | None:
    """Route between two (lat, lon) points with the given OSRM engine
    (ENGINE_CAR / ENGINE_BIKE / ENGINE_FOOT), as
    {"distance_km": float, "time_min": int}, or None when the router reports
    no route.

    The route is computed client-side after load, so poll for
    `#routing_summary` and parse its text: distances read "6.4km" (sub-km
    routes read "800m"), times are h:mm totals ("0:08" = 8 min)."""
    route = f"{frm[0]},{frm[1]};{to[0]},{to[1]}"
    page.goto(f"{app_url}/directions?engine={engine}&route={route}",
              wait_until="networkidle")
    for _ in range(_POLL_TRIES):
        el = page.query_selector("#routing_summary")
        text = (el.inner_text() or "") if el else ""
        dist, time = _DISTANCE_RE.search(text), _TIME_RE.search(text)
        if dist and time:
            value = float(dist.group(1).replace(",", ""))
            km = value / 1000.0 if dist.group(2).lower() == "m" else value
            return {"distance_km": km,
                    "time_min": int(time.group(1)) * 60 + int(time.group(2))}
        sidebar = page.query_selector("#sidebar_content")
        body = ((sidebar.inner_text() or "") if sidebar else "").lower()
        if "no route" in body or "couldn't find a route" in body:
            return None
        page.wait_for_timeout(_POLL_MS)
    return None
