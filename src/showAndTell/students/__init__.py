"""Public extension surface for systems that learn from demonstrations."""

from .base import ProductAdapter, Session
from .registry import get_student, list_students

__all__ = ["ProductAdapter", "Session", "get_student", "list_students"]
