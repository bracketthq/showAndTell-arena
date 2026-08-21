"""origin(): the one place a scheme://netloc is derived from an address."""
from __future__ import annotations

from urllib.parse import urlsplit

from showAndTell.applications.browser.runtime import origin


def test_origin_accepts_a_url_or_its_presplit_parts():
    url = "https://app.test:8443/path?q=1#f"
    assert origin(url) == "https://app.test:8443"
    assert origin(urlsplit(url)) == "https://app.test:8443"
