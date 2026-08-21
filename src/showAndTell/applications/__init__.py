"""Application definitions and the framework that discovers and runs them."""
from __future__ import annotations

from pathlib import Path

APPLICATIONS_ROOT = Path(__file__).resolve().parent

__all__ = ["APPLICATIONS_ROOT"]
