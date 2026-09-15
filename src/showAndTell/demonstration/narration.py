"""Load and deliver narration that belongs to a demonstration artifact."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

from .model import NarrationBeat


def narration_path(root: Path | str) -> Path:
    """Return the canonical narration script beneath a task or draft root."""
    return Path(root) / "demo" / "narration_script.jsonl"


def load_narration(root: Path | str) -> tuple[NarrationBeat, ...]:
    """Load and validate a demonstration's ordered narration beats."""
    path = narration_path(root)
    beats: list[NarrationBeat] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: narration beat must be an object")
        beat = NarrationBeat.from_mapping(value)
        if beat.key in seen:
            raise ValueError(f"{path}:{line_number}: duplicate narration key {beat.key!r}")
        seen.add(beat.key)
        beats.append(beat)
    return tuple(beats)


def _speak(text: str, voice_args: list[str] | None) -> None:
    if voice_args is None:
        return
    from showAndTell.core import tts

    tts.speak(text, voice_args)


def make_narrator(
    root: Path | str,
    voice: list[str] | None,
    out: Callable[[str], None],
    *,
    beats: Iterable[NarrationBeat] | None = None,
    speaker: Callable[[str, list[str] | None], None] = _speak,
):
    """Build an ``on_step`` callback that speaks each beat at most once."""
    script = {beat.key: beat.text for beat in (beats or load_narration(root))}
    spoken: set[str] = set()

    def on_step(key: str, page, description: str) -> None:
        del page
        out(f"  {description}")
        if key in script and key not in spoken:
            spoken.add(key)
            speaker(script[key], voice)

    return on_step


def recorded_narration_path(root: Path | str) -> Path | None:
    """Return captured human narration when the manifest proves it is audio."""
    root = Path(root)
    try:
        testcase = json.loads((root / "testcase.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    capture = testcase.get("capture") or {}
    relative = capture.get("recording")
    if not capture.get("voice_recorded") or not isinstance(relative, str):
        return None
    recording = root / relative
    try:
        # A Git LFS pointer has the correct name but is not playable media.
        if not recording.is_file() or recording.stat().st_size < 1024:
            return None
    except OSError:
        return None
    return recording
