"""Fetching published task bundles from the Hugging Face dataset repo."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from showAndTell.bundles import hub


def test_resolve_task_dir_prefers_existing_local_path(tmp_path):
    task = tmp_path / "tasks" / "demo-task"
    task.mkdir(parents=True)

    assert hub.resolve_task_dir(task) == task.resolve()


def test_resolve_task_dir_fetches_bare_task_name(monkeypatch, tmp_path):
    fetched = {}

    def fake_fetch(name, **kwargs):
        fetched["name"] = name
        return tmp_path / "snapshot" / "tasks" / name

    monkeypatch.setattr(hub, "fetch_task", fake_fetch)

    out = hub.resolve_task_dir("rfq-quote-award")

    assert fetched["name"] == "rfq-quote-award"
    assert out.name == "rfq-quote-award"


def test_resolve_task_dir_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        hub.resolve_task_dir(tmp_path / "nope" / "missing")


def test_fetch_task_downloads_bundle_snapshot(monkeypatch, tmp_path):
    calls = {}

    def fake_snapshot_download(repo_id, *, repo_type, revision, allow_patterns):
        calls.update(repo_id=repo_id, repo_type=repo_type, revision=revision,
                     allow_patterns=allow_patterns)
        bundle = tmp_path / "snap" / "tasks" / "demo-task"
        bundle.mkdir(parents=True)
        return str(tmp_path / "snap")

    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        snapshot_download=fake_snapshot_download))
    monkeypatch.setenv("SHOWANDTELL_DATASET_REPO", "acme/showAndTell-bench")
    monkeypatch.setenv("SHOWANDTELL_DATASET_REVISION", "v1.0")

    bundle = hub.fetch_task("demo-task")

    assert bundle == tmp_path / "snap" / "tasks" / "demo-task"
    assert calls["repo_id"] == "acme/showAndTell-bench"
    assert calls["repo_type"] == "dataset"
    assert calls["revision"] == "v1.0"
    assert "tasks/demo-task/**" in calls["allow_patterns"]


def test_fetch_task_rejects_unknown_bundle(monkeypatch, tmp_path):
    (tmp_path / "snap").mkdir()
    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        snapshot_download=lambda *a, **k: str(tmp_path / "snap")))

    with pytest.raises(FileNotFoundError):
        hub.fetch_task("no-such-task")


def test_load_index_parses_rows(monkeypatch, tmp_path):
    index = tmp_path / "tasks.jsonl"
    index.write_text(json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n")
    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        hf_hub_download=lambda *a, **k: str(index)))

    rows = hub.load_index()

    assert [r["id"] for r in rows] == ["a", "b"]


def test_substitute_host_uses_env_override(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_FIXTURE_HOST", "10.0.0.5")

    templated = f"http://{hub.HOST_TOKEN}:8080/login"

    assert hub.substitute_host(templated) == "http://10.0.0.5:8080/login"


def test_cli_task_dir_fetches_bare_names_through_hub(monkeypatch, tmp_path):
    from showAndTell import cli

    bundle = tmp_path / "snapshot" / "tasks" / "demo-task"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(hub, "fetch_task", lambda name, **kwargs: bundle)

    assert cli._task_dir("demo-task") == bundle


def test_origin_replacements_maps_placeholder_origins(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_FIXTURE_HOST", "10.0.0.5")
    source = (f"goto('http://{hub.HOST_TOKEN}:8080/desk')\n"
              f"goto('http://{hub.HOST_TOKEN}:8086/onlyoffice/editor')\n")

    assert hub.origin_replacements(source) == {
        f"http://{hub.HOST_TOKEN}:8080": "http://10.0.0.5:8080",
        f"http://{hub.HOST_TOKEN}:8086": "http://10.0.0.5:8086",
    }


def test_fetch_dataset_downloads_every_bundle(monkeypatch, tmp_path):
    calls = {}

    def fake_snapshot_download(repo_id, *, repo_type, revision, allow_patterns):
        calls.update(repo_id=repo_id, repo_type=repo_type,
                     allow_patterns=allow_patterns)
        (tmp_path / "snap" / "tasks").mkdir(parents=True)
        return str(tmp_path / "snap")

    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        snapshot_download=fake_snapshot_download))

    root = hub.fetch_dataset()

    assert root == tmp_path / "snap" / "tasks"
    assert calls["repo_type"] == "dataset"
    assert "tasks/**" in calls["allow_patterns"]


def test_cached_dataset_root_is_none_before_first_pull(
        monkeypatch, real_cached_dataset_root):
    def offline(*args, **kwargs):
        raise FileNotFoundError("nothing cached")

    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        snapshot_download=offline))

    assert hub.cached_dataset_root() is None


def test_cached_dataset_info_reads_the_cached_snapshot(
        monkeypatch, real_cached_dataset_root, tmp_path):
    snap = tmp_path / "abcdef1234567890"
    (snap / "tasks" / "beta-two").mkdir(parents=True)
    (snap / "tasks" / "alpha-one").mkdir()

    def cached_only(repo_id, *, repo_type, revision, local_files_only,
                    allow_patterns):
        assert local_files_only
        assert "tasks/**" in allow_patterns
        return str(snap)

    monkeypatch.setattr(hub, "_hf", lambda: types.SimpleNamespace(
        snapshot_download=cached_only))

    info = hub.cached_dataset_info()

    assert info["repo"] == hub.dataset_repo()
    assert info["revision"] == "abcdef123456"
    assert [d.name for d in info["task_dirs"]] == ["alpha-one", "beta-two"]
