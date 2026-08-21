"""Declarative seed forms, so an author never hand-writes a seed block.

Some starting state cannot be created in the application's own UI. A mail
client composes outgoing mail, not mail arriving from a supplier; a helpdesk
shows tickets other people filed. For those, an application declares a form
here and the viewer renders it — without knowing what a message or a ticket
is, exactly as it does not know what a port or a credential means.

An application opts in with two members on its ``State``::

    SEED_FORM = SeedForm(
        title="Inbox",
        row_noun="message",
        fields=(Field("sender", "From", placeholder="Ops <ops@acme.test>"),
                Field("subject", "Subject", required=True), ...),
    )

    def form_block(self, ctx, rows): ...   # rows -> this app's seed block

``form_block`` is the application's own knowledge and stays with it: only
Roundcube knows that a row becomes an RFC822 message addressed to the mailbox
in ``ctx.credentials``. The framework never interprets a row.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KINDS = ("text", "textarea", "checkbox", "select", "datetime")


@dataclass(frozen=True, slots=True)
class Field:
    """One input in a seed-form row."""

    name: str
    label: str
    kind: str = "text"
    placeholder: str = ""
    default: Any = None
    required: bool = False
    options: tuple[str, ...] = ()
    help: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.label:
            raise ValueError("a seed field needs a name and a label")
        if self.kind not in KINDS:
            raise ValueError(f"unknown field kind {self.kind!r}; expected {KINDS}")
        if self.kind == "select" and not self.options:
            raise ValueError(f"select field {self.name!r} needs options")

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "placeholder": self.placeholder, "default": self.default,
                "required": self.required, "options": list(self.options),
                "help": self.help}


@dataclass(frozen=True, slots=True)
class SeedForm:
    """A repeating row of fields — one row per record the author adds."""

    title: str
    row_noun: str
    fields: tuple[Field, ...] = field(default_factory=tuple)
    help: str = ""

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("a seed form needs at least one field")
        names = [item.name for item in self.fields]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate seed field names: {duplicates}")

    def blank_row(self) -> dict[str, Any]:
        return {item.name: (item.default if item.default is not None
                            else (False if item.kind == "checkbox" else ""))
                for item in self.fields}

    def validate(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Drop empty rows, reject incomplete ones, and normalise the rest.

        A row the author started and abandoned is not an error; a row missing
        a field the application needs is, and saying which row and which field
        is the difference between a fixable message and a stack trace.
        """
        known = {item.name: item for item in self.fields}
        cleaned: list[dict[str, Any]] = []
        for index, raw in enumerate(rows, 1):
            if not isinstance(raw, Mapping):
                raise ValueError(f"{self.row_noun} {index} must be an object")
            unknown = sorted(set(raw) - set(known))
            if unknown:
                raise ValueError(
                    f"{self.row_noun} {index} has unknown fields: {unknown}")
            row = {**self.blank_row(), **{k: raw[k] for k in raw}}
            if all(_is_blank(row[name]) for name, item in known.items()
                   if item.kind != "checkbox"):
                continue
            missing = [item.label for name, item in known.items()
                       if item.required and _is_blank(row[name])]
            if missing:
                raise ValueError(
                    f"{self.row_noun} {index} is missing {', '.join(missing)}")
            for name, item in known.items():
                if item.kind == "checkbox":
                    row[name] = bool(row[name])
                elif item.kind == "select" and row[name] not in item.options:
                    raise ValueError(
                        f"{self.row_noun} {index}: {item.label} must be one of "
                        f"{list(item.options)}")
                elif item.kind == "datetime":
                    row[name] = _utc_iso(row[name], f"{self.row_noun} {index}",
                                         item.label)
                else:
                    row[name] = str(row[name]).strip()
            cleaned.append(row)
        return cleaned

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, "row_noun": self.row_noun,
                "help": self.help,
                "fields": [item.to_json() for item in self.fields],
                "blank_row": self.blank_row()}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _utc_iso(value: Any, where: str, label: str) -> str:
    """Normalise a picker value to an explicit UTC instant.

    ``datetime-local`` submits a wall-clock string with no zone, so the same
    pick would mean different instants on two machines. Seed data has to order
    identically on every run, so an unzoned value is read as UTC rather than
    as the author's local time.
    """
    text = str(value).strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{where}: {label} must be a date and time, got {text!r}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def describe(state: Any) -> dict[str, Any] | None:
    """The form an application declares, or None when it declares none."""
    form = getattr(state, "SEED_FORM", None)
    if form is None:
        return None
    if not isinstance(form, SeedForm):
        raise TypeError(
            f"{type(state).__name__}.SEED_FORM must be a SeedForm, got "
            f"{type(form).__name__}")
    if not callable(getattr(state, "form_block", None)):
        raise TypeError(
            f"{type(state).__name__} declares SEED_FORM but no form_block(); "
            f"only the application can turn its own rows into a seed block")
    return form.to_json()
