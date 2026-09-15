"""Typed, lossless values that make up a portable demonstration."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping


def _frozen_copy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True, slots=True)
class Surface:
    """One application surface visible during a demonstration.

    The original mapping is retained losslessly because generated-driver source
    is byte-reviewed and may contain application-specific extension fields.
    Typed properties expose the stable cross-application contract.
    """

    data: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Surface":
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError("demonstration surface requires a non-empty id")
        if not isinstance(value.get("url"), str) or not value["url"]:
            raise ValueError(
                f"demonstration surface {value['id']!r} requires a non-empty url"
            )
        return cls(_frozen_copy(value))

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def application(self) -> str:
        return str(self.data.get("application") or self.id)

    @property
    def url(self) -> str:
        return str(self.data["url"])

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self.data))


@dataclass(frozen=True, slots=True)
class DemonstrationEvent:
    """One normalized setup, navigation, or authored-action event."""

    data: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DemonstrationEvent":
        if not isinstance(value.get("type"), str) or not value["type"]:
            raise ValueError("demonstration event requires a non-empty type")
        return cls(_frozen_copy(value))

    @property
    def type(self) -> str:
        return str(self.data["type"])

    @property
    def page(self) -> str:
        return str(self.data.get("page") or "page")

    @property
    def at_ms(self) -> int:
        return int(self.data.get("at_ms") or 0)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self.data))


@dataclass(frozen=True, slots=True)
class NarrationBeat:
    """Narration text associated with a stable demonstration action key."""

    key: str
    text: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NarrationBeat":
        key = value.get("key")
        text = value.get("text")
        if not isinstance(key, str) or not key:
            raise ValueError("narration beat requires a non-empty key")
        if not isinstance(text, str):
            raise ValueError(f"narration beat {key!r} requires text")
        return cls(key=key, text=text)


@dataclass(frozen=True, slots=True)
class Demonstration:
    """A complete, portable demonstrated workflow."""

    surfaces: tuple[Surface, ...]
    events: tuple[DemonstrationEvent, ...]
    setup_events: tuple[DemonstrationEvent, ...] = ()
    narration: tuple[NarrationBeat, ...] = ()

    def __post_init__(self) -> None:
        if not self.surfaces:
            raise ValueError("a demonstration requires at least one surface")
        ids = [surface.id for surface in self.surfaces]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate demonstration surface ids: {ids}")

    @classmethod
    def from_values(
        cls,
        *,
        surfaces: Iterable[Surface | Mapping[str, Any]],
        events: Iterable[DemonstrationEvent | Mapping[str, Any]],
        setup_events: Iterable[DemonstrationEvent | Mapping[str, Any]] = (),
        narration: Iterable[NarrationBeat | Mapping[str, Any]] = (),
    ) -> "Demonstration":
        return cls(
            surfaces=tuple(
                value if isinstance(value, Surface) else Surface.from_mapping(value)
                for value in surfaces
            ),
            events=tuple(
                value
                if isinstance(value, DemonstrationEvent)
                else DemonstrationEvent.from_mapping(value)
                for value in events
            ),
            setup_events=tuple(
                value
                if isinstance(value, DemonstrationEvent)
                else DemonstrationEvent.from_mapping(value)
                for value in setup_events
            ),
            narration=tuple(
                value
                if isinstance(value, NarrationBeat)
                else NarrationBeat.from_mapping(value)
                for value in narration
            ),
        )

    def surface_dicts(self) -> list[dict[str, Any]]:
        return [surface.to_dict() for surface in self.surfaces]

    def event_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]

    def setup_event_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.setup_events]
