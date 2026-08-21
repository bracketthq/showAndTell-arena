"""Capture a Show-and-Tell-shaped "pseudo-bundle" from a task's demonstration.

`showAndTell bundle-snapshot --task tasks/pcn-triage --out <dir>` boots the task's
fixture, seeds it, and replays the task's `demonstrate(...)` driver headlessly.
At each narration beat (`on_step`) it snapshots the page: a full-page screenshot
plus the Chromium accessibility tree, serialized into the SAME metadata shape a
real Brackett Show-and-Tell bundle carries. The result — `metadata/lineN.md`,
`screenshot_lineN.png`, `observation.md`, `traces.js` — lets a fixture's
semantic surface be diffed against the original human recording by one diff tool
that reads both.

This is a STATE capture, not a replay recording: each beat records what the page
looks like at that moment. Unlike a real recording it does not know which element
was acted on, so `targetElement` is always null and `traces.js` carries only
per-beat marker comments rather than executable Playwright calls.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from showAndTell.core.cache import TASK_ARTIFACT_FILES
from showAndTell.task_runtime import booted_task
from showAndTell.tasks import load_demonstrate, load_task

from .parse import format_metadata, serialize_tree


def _serialize_axtree(nodes: list[dict]) -> tuple[str, int]:
    """Flatten `Accessibility.getFullAXTree`'s node list into the bundle's tree
    string (via `snt_bundle.serialize_tree`, the format's one home). Ignored
    nodes are dropped and their children spliced up to keep the tree connected.
    Returns (tree, nodeCount) where nodeCount is the number of emitted nodes."""
    nodes = [n for n in nodes if "nodeId" in n]   # defensive at the CDP boundary
    by_id = {n["nodeId"]: n for n in nodes}
    child_ids: set[str] = set()
    for n in nodes:
        child_ids.update(n.get("childIds") or [])
    roots = [n["nodeId"] for n in nodes if n["nodeId"] not in child_ids]

    entries: list[tuple[str, int, str, str]] = []

    def walk(node_id: str, depth: int) -> None:
        node = by_id.get(node_id)
        if node is None:
            return
        if node.get("ignored"):
            # Splice children up: keep the tree connected, drop the ignored node.
            for child in node.get("childIds") or []:
                walk(child, depth)
            return
        role = (node.get("role") or {}).get("value", "") or ""
        name = (node.get("name") or {}).get("value", "") or ""
        entries.append((node_id, depth, role, name))
        for child in node.get("childIds") or []:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return serialize_tree(entries), len(entries)


def _write_observation(out_dir: Path, name: str, beats: list[tuple[str, str]]) -> None:
    lines = [f"# {name}", "", f"**Actions:** {len(beats)} events", "", "## Steps", ""]
    for i, (key, desc) in enumerate(beats, 1):
        lines.append(f"{i}. {key} — {desc}")
    (out_dir / "observation.md").write_text("\n".join(lines) + "\n")


def _write_traces(out_dir: Path, beats: list[tuple[str, str]]) -> None:
    lines = [
        "// Fixture pseudo-bundle captured by `showAndTell bundle-snapshot`.",
        "// State capture of the fixture at each narration beat — NOT a replay script.",
        "",
    ]
    for i, (key, _desc) in enumerate(beats, 1):
        lines.append(f"// beat: {key} | Screenshot: screenshot_line{i}.png")
    (out_dir / "traces.js").write_text("\n".join(lines) + "\n")


# task_sources keys keep the pre-consolidation manifest order so an unchanged
# task re-snapshots to byte-identical manifest bytes (external stores compare
# bundles by content hash). Membership stays owned by TASK_ARTIFACT_FILES: an
# artifact file this tuple does not know yet serializes after the pinned ones.
_PROVENANCE_KEY_ORDER = (
    "task.toml", "task_logic.py", "demonstrate.py", "demo/seed.json",
    "demo/narration_script.jsonl", "quiz/questions.json",
)


def _task_provenance(task_dir: Path, cfg: Any) -> dict[str, Any]:
    hashes = {
        relative: hashlib.sha256((task_dir / relative).read_bytes()).hexdigest()
        for relative in TASK_ARTIFACT_FILES
        if (task_dir / relative).is_file()
    }
    sources = {key: hashes.pop(key) for key in _PROVENANCE_KEY_ORDER if key in hashes}
    sources.update(hashes)
    recordings = sorted((task_dir / "demo").glob("recording.*"))
    recording = None
    if len(recordings) == 1 and recordings[0].is_file():
        recording = {
            "path": recordings[0].relative_to(task_dir).as_posix(),
            "sha256": hashlib.sha256(recordings[0].read_bytes()).hexdigest(),
        }
    return {
        "applications": list(getattr(cfg, "applications", ())),
        "primary_application": getattr(cfg, "primary_application", None),
        "task_sources": sources,
        "recording": recording,
    }


def _write_manifest(
    out_dir: Path,
    task_name: str,
    beats: list[tuple[str, str]],
    *,
    provenance: dict[str, Any] | None = None,
) -> Path:
    """Bind each replay beat to the screenshot bytes actually captured.

    The manifest deliberately contains no credentials, fixture tokens, or
    inferred page content.  Descriptions come from the demonstration callback,
    and hashes come from the PNG files Playwright wrote.
    """
    steps = []
    for line, (key, description) in enumerate(beats, 1):
        screenshot = out_dir / f"screenshot_line{line}.png"
        metadata = out_dir / "metadata" / f"line{line}.md"
        if not screenshot.is_file() or not metadata.is_file():
            raise RuntimeError(
                f"capture beat {key!r} is incomplete: expected {screenshot.name} "
                f"and metadata/{metadata.name}"
            )
        payload = screenshot.read_bytes()
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(
                f"capture beat {key!r} did not produce a PNG screenshot"
            )
        steps.append({
            "line": line,
            "step": key,
            "description": description,
            "screenshot": screenshot.name,
            "metadata": f"metadata/{metadata.name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            # The frame is pixels; the metadata beside it is the accessibility
            # tree and rendered text a graded agent actually reads. Hashing both
            # is what makes a hand-edited tree as visible as a hand-edited image.
            "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        })

    manifest = {
        "version": 1,
        "task": task_name,
        "capture_method": (
            "Playwright replay of demonstrate.py; "
            "one full-page screenshot at every on_step beat."
        ),
        "provenance": provenance,
        "steps": steps,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def capture_task_replay(task_dir: Path, out_dir: Path, page: Any,
                        app_url: str, creds: dict) -> int:
    """Replay one seeded task into an existing browser page.

    This is the reusable capture boundary for managed runtimes such as the
    EnterpriseArena integration lane: the caller owns application and browser
    lifecycle, while this function records only the real pages supplied to
    literal ``on_step`` calls.  It returns the number of captured beats.
    """
    task_dir = Path(task_dir).resolve()
    requested_out = Path(out_dir)
    out_dir = requested_out.parent.resolve() / requested_out.name
    if out_dir.is_symlink():
        raise ValueError(f"capture output must not be a symbolic link: {out_dir}")
    if out_dir.exists() and not out_dir.is_dir():
        raise NotADirectoryError(f"capture output is not a directory: {out_dir}")
    cfg = load_task(task_dir)
    demo = load_demonstrate(task_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(
        prefix=f".{out_dir.name}.staging-",
        dir=out_dir.parent,
    ))
    (staging_dir / "metadata").mkdir()
    beats: list[tuple[str, str]] = []
    sessions: dict[object, object] = {}

    def on_step(key: str, pg: Any, description: str) -> None:
        if any(previous == key for previous, _description in beats):
            raise RuntimeError(f"demonstration emitted duplicate on_step key {key!r}")
        n = len(beats) + 1
        pg.screenshot(
            path=str(staging_dir / f"screenshot_line{n}.png"),
            full_page=True,
        )
        if pg not in sessions:
            sessions[pg] = pg.context.new_cdp_session(pg)
        ax = sessions[pg].send("Accessibility.getFullAXTree")
        tree, node_count = _serialize_axtree(ax.get("nodes", []))
        meta = {
            "frameUrl": pg.url,
            "targetElement": None,
            "trees": [{
                "frameId": "main",
                "nodeCount": node_count,
                "timestamp": int(time.time() * 1000),
                "tree": tree,
            }],
        }
        (staging_dir / "metadata" / f"line{n}.md").write_text(
            format_metadata(n, meta)
        )
        beats.append((key, description))

    backup_dir: Path | None = None
    try:
        demo(page, app_url, creds, on_step)
        _write_observation(staging_dir, cfg.name, beats)
        _write_traces(staging_dir, beats)
        _write_manifest(
            staging_dir,
            cfg.name,
            beats,
            provenance=_task_provenance(task_dir, cfg),
        )

        if out_dir.exists():
            backup_dir = Path(tempfile.mkdtemp(
                prefix=f".{out_dir.name}.backup-",
                dir=out_dir.parent,
            ))
            out_dir.replace(backup_dir)
        try:
            staging_dir.replace(out_dir)
        except BaseException as publish_error:
            if backup_dir is not None and backup_dir.exists():
                try:
                    backup_dir.replace(out_dir)
                except BaseException as rollback_error:
                    publish_error.add_note(
                        "capture rollback failed; prior output remains at "
                        f"{backup_dir}: {rollback_error!r}"
                    )
            raise
        if backup_dir is not None:
            shutil.rmtree(backup_dir)
            backup_dir = None
        return len(beats)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        # A failed move of the prior output leaves its unique backup intact so
        # recovery remains possible. Empty reservations are always disposable.
        if backup_dir is not None and backup_dir.exists() and not any(
            backup_dir.iterdir()
        ):
            backup_dir.rmdir()


def snapshot_task(task_dir: Path, out_dir: Path) -> int:
    """Boot the task's fixture, replay its demonstration headlessly, and write a
    pseudo-bundle into `out_dir`. Returns the number of captured beats."""
    from playwright.sync_api import sync_playwright

    task_dir = Path(task_dir).resolve()
    out_dir = Path(out_dir).resolve()
    with booted_task(task_dir) as (app, creds):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            try:
                return capture_task_replay(
                    task_dir, out_dir, page, app, creds
                )
            finally:
                browser.close()
