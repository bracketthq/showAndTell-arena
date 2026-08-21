"""Repository-wide pytest helpers."""
from __future__ import annotations

import pytest

import httpx
from fastapi.testclient import TestClient


def asgi_sync_transport(app) -> httpx.BaseTransport:
    """Create a sync httpx transport for testing ASGI apps.

    httpx.ASGITransport is async-only; TestClient provides a sync bridge
    using anyio's blocking portal. This helper wraps TestClient's transport
    to ensure response streams are converted to sync for use with httpx.Client.
    """
    import asyncio

    base_transport = TestClient(app)._transport

    class _StreamConverterTransport(httpx.BaseTransport):
        """Wraps TestClient transport to ensure sync response streams."""

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            response = base_transport.handle_request(request)
            # Convert async response to sync by reading all content upfront.
            # TestClient's transport returns responses with async streams.
            loop = asyncio.new_event_loop()
            try:
                content = loop.run_until_complete(response.aread())
            finally:
                loop.close()
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=content,
                request=request,
            )

    return _StreamConverterTransport()


from showAndTell.bundles import hub as _hub

_REAL_CACHED_DATASET_ROOT = _hub.cached_dataset_root


@pytest.fixture(scope="session", autouse=True)
def _no_cached_dataset():
    """Keep tests hermetic: never see a dataset the dev machine has cached.

    Session-scoped so module-scoped fixtures (set up before function-scoped
    ones) are covered too. Tests of the dataset view monkeypatch
    hub.cached_dataset_info (or use real_cached_dataset_root) instead.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    mp.setattr(_hub, "cached_dataset_root", lambda **kw: None)
    yield
    mp.undo()


@pytest.fixture
def real_cached_dataset_root(monkeypatch):
    """Restore the real cache lookup for tests that exercise it."""
    monkeypatch.setattr(_hub, "cached_dataset_root", _REAL_CACHED_DATASET_ROOT)
