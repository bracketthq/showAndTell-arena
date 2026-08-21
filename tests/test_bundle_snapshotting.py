"""Snapshot the pcn-triage demo into a pseudo-bundle and round-trip it through
the bundle parser — the writer/reader format contract is asserted end to end.

Slow: boots the pcnfix fixture and drives one headless chromium session.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from showAndTell.bundles.snapshot import capture_task_replay

TASK = Path(__file__).resolve().parents[1] / "tasks" / "pcn-triage"


# --- fast serialization tests (review-pinned; no browser needed) -------------

def test_serialize_axtree_empty_is_graceful():
    from showAndTell.bundles.snapshot import _serialize_axtree
    assert _serialize_axtree([]) == ("", 0)


def test_serialize_axtree_splices_ignored_chain_at_same_depth():
    """A chain of ignored containers must not deepen (or orphan) their children:
    children surface at the depth the ignored ancestor occupied."""
    from showAndTell.bundles.snapshot import _serialize_axtree
    nodes = [
        {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "T"},
         "childIds": ["2"]},
        {"nodeId": "2", "ignored": True, "childIds": ["3"]},
        {"nodeId": "3", "ignored": True, "childIds": ["4"]},
        {"nodeId": "4", "role": {"value": "button"}, "name": {"value": "Go"},
         "childIds": []},
    ]
    tree, count = _serialize_axtree(nodes)
    assert count == 2
    assert tree == "[1] RootWebArea: T\n  [4] button: Go"


def test_serialize_axtree_skips_nodes_without_ids():
    from showAndTell.bundles.snapshot import _serialize_axtree
    nodes = [
        {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "T"},
         "childIds": []},
        {"role": {"value": "generic"}},   # no nodeId: dropped, no KeyError
    ]
    tree, count = _serialize_axtree(nodes)
    assert count == 1 and tree == "[1] RootWebArea: T"


class _FakeCdpSession:
    def send(self, method: str) -> dict:
        assert method == "Accessibility.getFullAXTree"
        return {
            "nodes": [{
                "nodeId": "1",
                "role": {"value": "RootWebArea"},
                "name": {"value": "Paperless Documents"},
                "childIds": [],
            }]
        }


class _FakeContext:
    def __init__(self) -> None:
        self.sessions: list[_FakeCdpSession] = []

    def new_cdp_session(self, page) -> _FakeCdpSession:
        assert isinstance(page, _FakePage)
        session = _FakeCdpSession()
        self.sessions.append(session)
        return session


class _FakePage:
    def __init__(self) -> None:
        self.url = "http://paperless.test/documents"
        self.context = _FakeContext()
        self.screenshot_calls: list[dict] = []

    def screenshot(self, **kwargs) -> None:
        self.screenshot_calls.append(kwargs)
        # A PNG signature plus distinct captured bytes is sufficient here: the
        # production capture bytes are always written by Playwright itself.
        Path(kwargs["path"]).write_bytes(
            b"\x89PNG\r\n\x1a\n" + bytes([len(self.screenshot_calls)])
        )


def _capture_sidecars(parent: Path, output_name: str) -> list[Path]:
    return sorted(
        (*parent.glob(f".{output_name}.staging-*"),
         *parent.glob(f".{output_name}.backup-*")),
        key=lambda path: path.name,
    )


def _tree_bytes(directory: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_capture_task_replay_writes_full_page_hash_bound_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    import showAndTell.bundles.snapshot as capture

    credentials = {"email": "clerk@example.test", "password": "not-an-artifact"}

    def demonstrate(page, app_url, creds, on_step) -> None:
        assert app_url == "http://onlyoffice.test"
        assert creds is credentials
        on_step("open_inbox", page, "visible inbox")
        page.url = "http://onlyoffice.test/documents/7/details"
        on_step("save_actionable", page, "visible saved decision")

    monkeypatch.setattr(
        capture,
        "load_task",
        lambda _task_dir: SimpleNamespace(
            name="invoice-intake",
            applications=("onlyoffice",),
            primary_application="onlyoffice",
        ),
    )
    monkeypatch.setattr(capture, "load_demonstrate", lambda _task_dir: demonstrate)
    page = _FakePage()

    assert capture_task_replay(
        tmp_path / "task", tmp_path / "evidence", page,
        "http://onlyoffice.test", credentials,
    ) == 2

    captured_paths = [Path(call["path"]) for call in page.screenshot_calls]
    assert [path.name for path in captured_paths] == [
        "screenshot_line1.png", "screenshot_line2.png",
    ]
    assert {path.parent for path in captured_paths} == {captured_paths[0].parent}
    assert captured_paths[0].parent.parent == tmp_path
    assert captured_paths[0].parent.name.startswith(".evidence.staging-")
    assert all(call["full_page"] is True for call in page.screenshot_calls)
    assert not captured_paths[0].parent.exists()
    assert len(page.context.sessions) == 1
    assert _capture_sidecars(tmp_path, "evidence") == []

    manifest_path = tmp_path / "evidence/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["task"] == "invoice-intake"
    assert manifest["capture_method"] == (
        "Playwright replay of demonstrate.py; "
        "one full-page screenshot at every on_step beat."
    )
    assert manifest["provenance"] == {
        "applications": ["onlyoffice"],
        "primary_application": "onlyoffice",
        "task_sources": {},
        "recording": None,
    }
    assert [step["step"] for step in manifest["steps"]] == [
        "open_inbox", "save_actionable"
    ]
    for step in manifest["steps"]:
        payload = (manifest_path.parent / step["screenshot"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == step["sha256"]
        # The metadata carries the accessibility tree and rendered text a graded
        # agent reads, so the quiz-evidence standard holds it to its bytes the
        # same way it holds the frame. Emitting it here is what keeps that
        # standard satisfiable by a re-capture instead of by a hand-written hash.
        metadata = (manifest_path.parent / step["metadata"]).read_bytes()
        assert hashlib.sha256(metadata).hexdigest() == step["metadata_sha256"]
    serialized_output = b"\n".join(_tree_bytes(tmp_path / "evidence").values())
    assert credentials["email"].encode() not in serialized_output
    assert credentials["password"].encode() not in serialized_output


def test_capture_task_replay_preserves_previous_output_on_capture_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import showAndTell.bundles.snapshot as capture

    output = tmp_path / "evidence"
    (output / "metadata").mkdir(parents=True)
    (output / "manifest.json").write_text('{"complete": true}\n')
    (output / "metadata/line1.md").write_text("prior metadata\n")
    (output / "screenshot_line1.png").write_bytes(b"prior screenshot")
    previous = _tree_bytes(output)

    def demonstrate(page, _app_url, _creds, on_step) -> None:
        on_step("replacement", page, "new state")
        raise RuntimeError("capture interrupted")

    monkeypatch.setattr(
        capture, "load_task", lambda _task_dir: SimpleNamespace(name="replacement")
    )
    monkeypatch.setattr(capture, "load_demonstrate", lambda _task_dir: demonstrate)

    with pytest.raises(RuntimeError, match="capture interrupted"):
        capture_task_replay(
            tmp_path / "task", output, _FakePage(),
            "http://paperless.test", {},
        )

    assert _tree_bytes(output) == previous
    assert _capture_sidecars(tmp_path, "evidence") == []


def test_capture_task_replay_shorter_recapture_removes_stale_files(
    tmp_path: Path, monkeypatch
) -> None:
    import showAndTell.bundles.snapshot as capture

    output = tmp_path / "evidence"
    (output / "metadata").mkdir(parents=True)
    for line in range(1, 4):
        (output / f"screenshot_line{line}.png").write_bytes(b"stale")
        (output / f"metadata/line{line}.md").write_text("stale metadata\n")
    (output / "unrelated-stale-file.txt").write_text("remove me\n")

    def demonstrate(page, _app_url, _creds, on_step) -> None:
        on_step("only", page, "the only current beat")

    monkeypatch.setattr(
        capture, "load_task", lambda _task_dir: SimpleNamespace(name="shorter")
    )
    monkeypatch.setattr(capture, "load_demonstrate", lambda _task_dir: demonstrate)

    assert capture_task_replay(
        tmp_path / "task", output, _FakePage(),
        "http://paperless.test", {},
    ) == 1

    assert set(_tree_bytes(output)) == {
        Path("manifest.json"),
        Path("metadata/line1.md"),
        Path("observation.md"),
        Path("screenshot_line1.png"),
        Path("traces.js"),
    }
    manifest = json.loads((output / "manifest.json").read_text())
    assert [step["step"] for step in manifest["steps"]] == ["only"]
    assert _capture_sidecars(tmp_path, "evidence") == []


def test_capture_task_replay_rolls_back_if_publish_fails(
    tmp_path: Path, monkeypatch
) -> None:
    import showAndTell.bundles.snapshot as capture

    output = tmp_path / "evidence"
    output.mkdir()
    (output / "prior-complete.txt").write_text("keep this exact output\n")
    previous = _tree_bytes(output)

    def demonstrate(page, _app_url, _creds, on_step) -> None:
        on_step("replacement", page, "new complete state")

    monkeypatch.setattr(
        capture, "load_task", lambda _task_dir: SimpleNamespace(name="replacement")
    )
    monkeypatch.setattr(capture, "load_demonstrate", lambda _task_dir: demonstrate)
    real_replace = Path.replace

    def fail_staging_publish(path: Path, target: Path) -> Path:
        if path.name.startswith(".evidence.staging-") and Path(target) == output:
            raise OSError("simulated publish failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_staging_publish)

    with pytest.raises(OSError, match="simulated publish failure"):
        capture_task_replay(
            tmp_path / "task", output, _FakePage(),
            "http://paperless.test", {},
        )

    assert _tree_bytes(output) == previous
    assert _capture_sidecars(tmp_path, "evidence") == []


def test_capture_task_replay_rejects_duplicate_evidence_keys(
    tmp_path: Path, monkeypatch
) -> None:
    import showAndTell.bundles.snapshot as capture

    def demonstrate(page, _app_url, _creds, on_step) -> None:
        on_step("same", page, "first")
        on_step("same", page, "second")

    monkeypatch.setattr(
        capture, "load_task", lambda _task_dir: SimpleNamespace(name="duplicate")
    )
    monkeypatch.setattr(capture, "load_demonstrate", lambda _task_dir: demonstrate)

    with pytest.raises(RuntimeError, match="duplicate on_step key 'same'"):
        capture_task_replay(
            tmp_path / "task", tmp_path / "evidence", _FakePage(),
            "http://paperless.test", {},
        )
    assert not (tmp_path / "evidence").exists()
    assert _capture_sidecars(tmp_path, "evidence") == []


def test_manifest_refuses_non_png_capture_bytes(tmp_path: Path) -> None:
    from showAndTell.bundles.snapshot import _write_manifest

    (tmp_path / "metadata").mkdir()
    (tmp_path / "screenshot_line1.png").write_bytes(b"not a screenshot")
    (tmp_path / "metadata/line1.md").write_text("metadata")

    with pytest.raises(RuntimeError, match="did not produce a PNG"):
        _write_manifest(tmp_path, "invoice-intake", [("open", "visible")])


def test_task_provenance_binds_sources_and_original_recording(tmp_path: Path) -> None:
    from showAndTell.bundles.snapshot import _task_provenance

    task = tmp_path / "task"
    (task / "demo").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\nname='bound'\n")
    (task / "task_logic.py").write_text("RULE = 'ready'\n")
    (task / "demo/recording.webm").write_bytes(b"original recording")
    cfg = SimpleNamespace(
        applications=("onlyoffice",),
        primary_application="onlyoffice",
    )

    provenance = _task_provenance(task, cfg)

    assert provenance["task_sources"] == {
        "task.toml": hashlib.sha256((task / "task.toml").read_bytes()).hexdigest(),
        "task_logic.py": hashlib.sha256(
            (task / "task_logic.py").read_bytes()
        ).hexdigest(),
    }
    assert provenance["recording"] == {
        "path": "demo/recording.webm",
        "sha256": hashlib.sha256(b"original recording").hexdigest(),
    }


def test_task_provenance_serializes_sources_in_the_stored_bundle_order(
        tmp_path: Path) -> None:
    """Manifest bytes must stay reproducible against bundles snapshotted
    before the provenance list merged into TASK_ARTIFACT_FILES: same keys,
    same serialization order."""
    from showAndTell.bundles.snapshot import _task_provenance

    task = tmp_path / "task"
    for relative in ("task.toml", "task_logic.py", "demonstrate.py",
                     "quiz/questions.json",
                     "demo/narration_script.jsonl", "demo/seed.json"):
        path = task / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    cfg = SimpleNamespace(applications=(), primary_application=None)

    provenance = _task_provenance(task, cfg)

    assert list(provenance["task_sources"]) == [
        "task.toml", "task_logic.py", "demonstrate.py", "demo/seed.json",
        "demo/narration_script.jsonl", "quiz/questions.json",
    ]
