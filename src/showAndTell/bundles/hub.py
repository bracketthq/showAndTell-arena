"""Fetch published task bundles from the Hugging Face dataset repo.

The harness code is public; the task dataset lives on the Hub. A consumer
passes either a local task directory (authoring checkout) or a bare task
name, and :func:`resolve_task_dir` returns a directory that exists locally,
downloading the bundle into the huggingface_hub cache when needed.

Published files carry ``__SHOWANDTELL_HOST__`` in place of the fixture host so
the dataset never references live infrastructure. :func:`origin_replacements`
turns those placeholders into the ``url_replacements`` mapping that
``showAndTell.tasks.load_demonstrate_module`` already applies to driver source.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

HOST_TOKEN = "__SHOWANDTELL_HOST__"
DEFAULT_REPO = "brackettai/showtellarena"
INDEX_FILE = "tasks.jsonl"

_TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PLACEHOLDER_ORIGIN = re.compile(
    rf"https?://{re.escape(HOST_TOKEN)}(?::\d+)?")


def _hf():
    try:
        import huggingface_hub
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "fetching task bundles from the Hugging Face Hub requires the "
            "huggingface_hub package; install it (pip install "
            "huggingface_hub) and run `hf auth login` if the dataset is "
            "gated") from exc
    return huggingface_hub


def dataset_repo() -> str:
    return os.environ.get("SHOWANDTELL_DATASET_REPO", DEFAULT_REPO)


def dataset_revision() -> str | None:
    return os.environ.get("SHOWANDTELL_DATASET_REVISION") or None


def fixture_host() -> str:
    return os.environ.get("SHOWANDTELL_FIXTURE_HOST", "localhost")


def load_index(*, repo: str | None = None,
               revision: str | None = None) -> list[dict]:
    """Return the published tasks.jsonl index as a list of row dicts."""

    path = _hf().hf_hub_download(
        repo or dataset_repo(), INDEX_FILE, repo_type="dataset",
        revision=revision or dataset_revision())
    return [json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def fetch_task(name: str, *, repo: str | None = None,
               revision: str | None = None) -> Path:
    """Download one task bundle (plus the index) and return its directory."""

    root = _hf().snapshot_download(
        repo or dataset_repo(), repo_type="dataset",
        revision=revision or dataset_revision(),
        allow_patterns=[INDEX_FILE, f"tasks/{name}/**"])
    bundle = Path(root) / "tasks" / name
    if not bundle.is_dir():
        raise FileNotFoundError(
            f"task {name!r} is not in {repo or dataset_repo()}")
    return bundle


def fetch_dataset(*, repo: str | None = None,
                  revision: str | None = None) -> Path:
    """Download (or update) every published bundle; returns the tasks root.

    The hub cache keeps one snapshot folder per revision with
    content-addressed blobs shared between them, so re-pulling a new
    release only transfers the files that changed.
    """

    root = _hf().snapshot_download(
        repo or dataset_repo(), repo_type="dataset",
        revision=revision or dataset_revision(),
        allow_patterns=[INDEX_FILE, "tasks/**"])
    return Path(root) / "tasks"


def cached_dataset_root(*, repo: str | None = None,
                        revision: str | None = None) -> Path | None:
    """The already-downloaded dataset tasks root; never touches the network.

    None when the dataset has not been pulled yet (or huggingface_hub is
    unavailable) — callers degrade to local-only behavior.
    """

    try:
        # the same allow_patterns fetch_dataset pulls with, so the cache
        # completeness check does not demand files we never download
        root = _hf().snapshot_download(
            repo or dataset_repo(), repo_type="dataset",
            revision=revision or dataset_revision(), local_files_only=True,
            allow_patterns=[INDEX_FILE, "tasks/**"])
    except Exception:
        return None
    tasks = Path(root) / "tasks"
    return tasks if tasks.is_dir() else None


def cached_dataset_info() -> dict | None:
    """Repo, revision, and task dirs of the cached dataset, or None.

    The revision is the snapshot folder's name — the commit the cached
    copy was resolved to (the cache keeps one folder per revision).
    """

    root = cached_dataset_root()
    if root is None:
        return None
    return {
        "repo": dataset_repo(),
        "revision": root.parent.name[:12],
        "task_dirs": sorted(p for p in root.iterdir() if p.is_dir()),
    }


def resolve_task_dir(spec: str | Path) -> Path:
    """Resolve a CLI task argument to a local directory.

    An existing path wins (the authoring checkout keeps working unchanged);
    a bare task name is fetched from the dataset repo; anything else is a
    path the caller got wrong, reported as such rather than sent to the Hub.
    """

    path = Path(spec)
    if path.exists():
        return path.resolve()
    if _TASK_NAME.fullmatch(str(spec)):
        return fetch_task(str(spec))
    raise FileNotFoundError(
        f"{spec}: no such task directory (a bare task name would be "
        f"fetched from {dataset_repo()})")


def substitute_host(text: str, host: str | None = None) -> str:
    """Replace the published host placeholder with the deployment's host."""

    return text.replace(HOST_TOKEN, host or fixture_host())


def origin_replacements(source: str, host: str | None = None) -> dict[str, str]:
    """Map placeholder origins in published text onto live origins.

    The result feeds ``load_demonstrate_module(url_replacements=...)``, which
    replaces complete origins only.
    """

    live = host or fixture_host()
    return {origin: origin.replace(HOST_TOKEN, live)
            for origin in set(_PLACEHOLDER_ORIGIN.findall(source))}
