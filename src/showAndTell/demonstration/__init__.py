"""Portable demonstration artifacts shared by capture, replay, and teaching.

The package defines what a demonstrated workflow is and the pure or
dependency-light transformations that normalize, compile, and narrate it. It
does not record browsers, execute gestures, control student products, or grade
results.
"""

from .model import Demonstration, DemonstrationEvent, NarrationBeat, Surface

__all__ = ["Demonstration", "DemonstrationEvent", "NarrationBeat", "Surface"]
