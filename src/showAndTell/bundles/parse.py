"""Parser for managed-Chrome Show and Tell capture bundles.

A bundle pairs a generated Playwright ``traces.js`` with per-action PNGs and
``metadata/lineN.md`` files.  The metadata JSON is authoritative for selectors,
acted-element identity, and the Chromium accessibility tree.  This module reads
the format emitted by :mod:`showAndTell.capture.runtime` and
:mod:`showAndTell.bundles.snapshot` without launching a browser.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# The logical line number lives in the screenshot comment, not the JS source
# line. Managed captures and replay snapshots both emit PNGs.
_SCREENSHOT = re.compile(r"screenshot_line(\d+)\.png")
_PAGE = r"page\d*"
_PAGE_CALL = re.compile(
    rf"await\s+({_PAGE})\.(goto|click|fill|selectOption|press)\((.*)\);$"
)
_TYPE_CALL = re.compile(rf"await\s+({_PAGE})\.keyboard\.type\((.*)\);$")
_TAB_CALL = re.compile(rf"await\s+({_PAGE})\.bringToFront\(\);$")
_DRAG_CALL = re.compile(
    rf"await\s+({_PAGE})\.locator\((.*?)\)\.dragTo\(\1\.locator\((.*?)\)\);$"
)
_NARRATION = re.compile(r'User:\s*"([^"]*)"')
_TREE_LINE = re.compile(r"\[(-?\d+)\]\s+(.*)$")     # ids may be negative
_JSON_FENCE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


@dataclass
class AXNode:
    id: str
    depth: int
    role: str
    name: str


@dataclass
class AXTree:
    frame_id: str
    node_count: int
    nodes: list[AXNode]


@dataclass
class Target:
    role: str
    name: str
    node_id: str | None


@dataclass
class Step:
    line: int
    verb: str | None = None
    page: str | None = None
    selector: str | None = None
    value: str | None = None
    screenshot: str | None = None
    narration: list[str] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)
    frame_url: str | None = None
    target: Target | None = None
    trees: list[AXTree] = field(default_factory=list)


@dataclass
class Bundle:
    path: Path
    title: str
    steps: list[Step]


def parse_bundle(path: Path) -> Bundle:
    path = Path(path)
    if not path.is_dir():
        # Individual bundle files may be absent (partial bundles are normal),
        # but a missing bundle DIRECTORY is a caller mistake — an empty Bundle
        # here silently poisons downstream diffs with zero-step alignments.
        raise FileNotFoundError(f"bundle directory not found: {path}")
    title = _parse_title(path / "observation.md")
    traces_path = path / "traces.js"
    # A partial bundle may omit traces.js; metadata still reconstructs its steps.
    traces_text = traces_path.read_text(encoding="utf-8") if traces_path.exists() else ""
    traces = _parse_traces(traces_text)
    meta = _parse_metadata_dir(path / "metadata")
    steps = []
    for n in sorted(set(traces) | set(meta)):
        t = traces.get(n, {})
        m = meta.get(n, {})
        steps.append(Step(
            line=n,
            verb=t.get("verb"),
            page=t.get("page"),
            selector=t.get("selector"),
            value=t.get("value"),
            screenshot=t.get("screenshot") or m.get("screenshot"),
            narration=t.get("narration", []),
            selectors=m.get("selectors", []),
            frame_url=m.get("frame_url"),
            target=m.get("target"),
            trees=m.get("trees", []),
        ))
    return Bundle(path=path, title=title, steps=steps)


def _parse_title(observation: Path) -> str:
    if not observation.exists():
        return ""
    for line in observation.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _parse_traces(text: str) -> dict[int, dict]:
    """Lines whose comment references a screenshot become steps; setup lines
    (require/launch/newContext/newPage, empty comment) carry no line number.
    Empty ``text`` (including a missing traces.js) yields ``{}``."""
    out: dict[int, dict] = {}
    for line in text.splitlines():
        mo = _SCREENSHOT.search(line)
        if not mo:
            continue
        n = int(mo.group(1))
        step: dict = {"screenshot": f"screenshot_line{n}.png",
                      "narration": _NARRATION.findall(line)}
        code = line.split("  //", 1)[0]
        _apply_call(step, code.strip())
        out[n] = step
    return out


def _args(raw: str) -> list:
    """Decode arguments emitted by ``runtime._js`` (JSON-compatible JS)."""
    try:
        value = json.loads(f"[{raw}]")
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _apply_call(step: dict, code: str) -> None:
    """Fold one current managed-capture trace call into a step dictionary."""
    cm = _PAGE_CALL.fullmatch(code)
    if cm:
        page, method, rawargs = cm.groups()
        verb = "select" if method == "selectOption" else method
        args = _args(rawargs)
    elif cm := _TYPE_CALL.fullmatch(code):
        page, rawargs = cm.groups()
        verb, args = "type", _args(rawargs)
    elif cm := _TAB_CALL.fullmatch(code):
        page = cm.group(1)
        verb, args = "tab_switch", []
    elif cm := _DRAG_CALL.fullmatch(code):
        page, source, destination = cm.groups()
        verb, args = "drag", [*_args(source), *_args(destination)]
    else:
        return

    step["page"] = page
    step["verb"] = verb
    if verb == "goto":
        step["value"] = args[0] if args else None
    elif verb in {"click", "press", "select", "drag"}:
        step["selector"] = args[0] if args else None
        step["value"] = args[1] if len(args) > 1 else None
    elif verb == "fill":
        step["selector"] = args[0] if args else None
        step["value"] = args[1] if len(args) > 1 else None
    elif verb == "type":
        step["value"] = args[0] if args else None


def _parse_metadata_dir(metadir: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    if not metadir.is_dir():
        return out
    for md in metadir.glob("line*.md"):
        mo = re.match(r"line(\d+)\.md$", md.name)
        if not mo:
            continue
        out[int(mo.group(1))] = parse_metadata(md.read_text(encoding="utf-8"))
    return out


def _sections(text: str) -> dict[str, str]:
    """Split a metadata file into its `## <name>` section bodies. Trivial files
    (just `# Line N`) yield an empty mapping."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(buf)
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def parse_metadata(text: str) -> dict:
    """Read one `metadata/lineN.md` body: the reader counterpart of
    `format_metadata`. Public so nothing else has to re-derive the fence
    extraction or the a11y-tree line grammar (role is everything up to the FIRST
    `: `, and names may themselves contain `: `) — the format has one home.

    Returns the ``Step`` field subset the file carries: ``screenshot``,
    ``selectors``, ``frame_url``, ``target``, and parsed ``trees``."""
    sec = _sections(text)
    result: dict = {}

    if "Screenshot" in sec:
        mo = _SCREENSHOT.search(sec["Screenshot"])
        if mo:
            result["screenshot"] = f"screenshot_line{mo.group(1)}.png"

    if "Accessibility Tree" in sec:
        mo = _JSON_FENCE.search(sec["Accessibility Tree"])
        if mo:
            data = json.loads(mo.group(1))
            result["frame_url"] = data.get("frameUrl")
            result["selectors"] = [
                selector for selector in (data.get("selectors") or [])
                if isinstance(selector, str)
            ]
            te = data.get("targetElement")
            if te is not None:
                result["target"] = Target(role=te.get("role", ""),
                                          name=te.get("name", ""),
                                          node_id=te.get("nodeId"))
            result["trees"] = [
                _axtree(tr) for tr in (data.get("trees") or [])
                if isinstance(tr, dict)
            ]
    return result


def _axtree(tr: dict) -> AXTree:
    """Build an ``AXTree`` from the recorder's indented tree string."""
    nodes = _parse_tree(tr.get("tree") or "")
    node_count = tr.get("nodeCount")
    if not isinstance(node_count, int):
        node_count = len(nodes)
    return AXTree(frame_id=tr.get("frameId") or "", node_count=node_count, nodes=nodes)


def _parse_tree(tree: str) -> list[AXNode]:
    """Two spaces of indent per depth level. Role is everything up to the FIRST
    `: `; names may themselves contain `: `."""
    nodes = []
    for raw in tree.split("\n"):
        if not raw.strip():
            continue
        depth = (len(raw) - len(raw.lstrip(" "))) // 2
        mo = _TREE_LINE.match(raw.strip())
        if not mo:
            continue
        nid, rest = mo.groups()
        role, sep, name = rest.partition(": ")
        nodes.append(AXNode(id=nid, depth=depth, role=role, name=name if sep else ""))
    return nodes


# --- writer counterparts -----------------------------------------------------
# Kept beside the parser so the bundle format has one home and writer/reader
# cannot drift (bundle_snapshot emits through these; parse_bundle reads back).

def serialize_tree(entries: Iterable[tuple[str, int, str, str]]) -> str:
    """Inverse of `_parse_tree`: (id, depth, role, name) tuples -> the bundle's
    indented tree string (`[id] role: name`, the `: name` omitted when empty)."""
    lines = []
    for nid, depth, role, name in entries:
        head = f"{'  ' * depth}[{nid}] {role}"
        lines.append(f"{head}: {name}" if name else head)
    return "\n".join(lines)


def format_metadata(n: int, ax: dict, *, screenshot: bool = True) -> str:
    """Render a `metadata/lineN.md` body in the exact shape `parse_bundle` reads."""
    parts = [f"# Line {n}", ""]
    if screenshot:
        parts += ["## Screenshot", f"![screenshot](screenshot_line{n}.png)", ""]
    parts += ["## Accessibility Tree", "```json", json.dumps(ax), "```"]
    return "\n".join(parts) + "\n"
