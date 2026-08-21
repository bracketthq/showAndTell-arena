"""Create small, valid OOXML workbooks from the benchmark's sheet seeds.

ONLYOFFICE edits files; it is not itself a file store.  This writer produces a
real ``.xlsx`` artifact that a connector or bind-mounted document directory can
serve to ONLYOFFICE.  It uses only the standard library so fixture setup does
not acquire a hidden spreadsheet dependency.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile


_ALIASES = {
    "supplier_part_number": "supplier_part",
    "material_number": "material",
    "weight_lbs": "weight_lbs",
    "class": "freight_class",
    "invoiced_fsc": "invoiced_fsc",
    "contracted_fsc": "contracted_fsc_pct",
    "contracted_fsc_pct": "contracted_fsc_pct",
    "fuel_surcharge": "fuel_surcharge_pct",
    "variance_pct": "variance_pct",
}


def _slug(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return _ALIASES.get(key, key)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_name(value: object, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "-", str(value or "Sheet"))[:31] or "Sheet"
    name = base
    suffix = 2
    while name in used:
        tail = f"-{suffix}"
        name = base[:31 - len(tail)] + tail
        suffix += 1
    used.add(name)
    return name


def _column_keys(sheet: Mapping[str, Any]) -> list[str | None]:
    columns = list(sheet.get("columns") or [])
    fields = sheet.get("fields")
    if isinstance(fields, Mapping) and fields:
        keys = [str(value) for _, value in sorted(fields.items())]
        return keys + [None] * max(0, len(columns) - len(keys))
    keys: list[str | None] = []
    for label in columns:
        keys.append(_slug(str(label)) if str(label).strip() else None)
    return keys


def _tabs(source: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    raw_tabs = source.get("tabs")
    if isinstance(raw_tabs, Sequence) and not isinstance(raw_tabs, (str, bytes)):
        object_tabs = [tab for tab in raw_tabs if isinstance(tab, Mapping)]
        if object_tabs:
            return [(str(tab.get("name") or f"Sheet {i}"), tab)
                    for i, tab in enumerate(object_tabs, 1)]
        names = [str(tab) for tab in raw_tabs]
        if names:
            active = str(source.get("active_tab") or source.get("tab") or names[0])
            return [(name, source if name == active else {}) for name in names]
    return [(str(source.get("tab") or source.get("active_tab") or "Sheet1"), source)]


def _cell_xml(ref: str, value: Any) -> str:
    if value is None or value == "":
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = str(value)
    if text.startswith("="):
        return f'<c r="{ref}"><f>{escape(text[1:])}</f></c>'
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return (f'<c r="{ref}" t="inlineStr"><is><t{preserve}>'
            f'{escape(text)}</t></is></c>')


def _worksheet(sheet: Mapping[str, Any]) -> str:
    columns = list(sheet.get("columns") or [])
    keys = _column_keys(sheet)
    row_xml: list[str] = []
    if columns:
        cells = "".join(_cell_xml(f"{_column_name(i)}1", label)
                        for i, label in enumerate(columns, 1))
        row_xml.append(f'<row r="1">{cells}</row>')
    for ordinal, raw_row in enumerate(sheet.get("rows") or [], 2):
        if not isinstance(raw_row, Mapping):
            continue
        row_number = raw_row.get("row", ordinal)
        try:
            row_number = max(1, int(row_number))
        except (TypeError, ValueError):
            row_number = ordinal
        cells: list[str] = []
        for index, key in enumerate(keys, 1):
            if key is None:
                continue
            value = raw_row.get(key, "")
            cells.append(_cell_xml(f"{_column_name(index)}{row_number}", value))
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>')


def write_xlsx(source: Mapping[str, Any], destination: Path) -> Path:
    """Write ``source`` to ``destination`` and return the resolved path."""
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("ONLYOFFICE workbook destination must end in .xlsx")
    destination.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    sheets = [(_sheet_name(name, used), data) for name, data in _tabs(source)]
    if not sheets:
        raise ValueError("workbook must have at least one sheet")

    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{overrides}</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _) in enumerate(sheets, 1))
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{workbook_sheets}</sheets></workbook>')
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)) +
        '</Relationships>')

    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, (_, data) in enumerate(sheets, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet(data))
    return destination.resolve()
