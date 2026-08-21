"""Public Playwright error taxonomy shared by ShowAndTell layers."""
from __future__ import annotations

try:  # keep importable without Playwright installed
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PWTimeout
except Exception:  # pragma: no cover
    class PlaywrightError(Exception):
        pass

    class PWTimeout(PlaywrightError):
        pass


STALE_READ_SUBSTRINGS = (
    "Execution context was destroyed",
    "Element is not attached to the DOM",
    "Node is detached from document",
)


def is_stale_read(exc) -> bool:
    """Return whether a Playwright read raced navigation or a DOM replacement."""
    return isinstance(exc, PlaywrightError) and any(
        substring in str(exc) for substring in STALE_READ_SUBSTRINGS)
