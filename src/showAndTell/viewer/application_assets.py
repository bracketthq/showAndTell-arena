"""On-demand installation for archived WebArena capture applications.

The current benchmark fixtures build from this repository.  A few adapters
retained for archived WebArena tasks instead depend on upstream image archives
or a ZIM file.  This module makes those prerequisites explicit and installable
from the local viewer without making the ordinary setup download hundreds of
gigabytes.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urljoin

import httpx

from showAndTell.applications.host.protocol import CommandRunner
from showAndTell.applications.registry import default_registry

from . import _util


class AssetError(_util.ViewerError):
    pass


@dataclass(frozen=True, slots=True)
class AssetFile:
    filename: str
    size: int
    url: str


@dataclass(frozen=True, slots=True)
class AssetSpec:
    application: str
    label: str
    kind: str
    filename: str = ""
    size: int = 0
    url: str = ""
    image: str = ""
    sha256: str = ""
    google_drive_id: str = ""
    files: tuple[AssetFile, ...] = ()
    catalog_url: str = ""
    catalog_pattern: str = ""


_MAP_IMAGE = (
    "am1n3e/webarena-verified-map@"
    "sha256:c769aa312c979d68e696dace7049d58eeeb016ce608f0f8779a181bf3714206f"
)
# Sum of the pinned manifest's compressed layer and config sizes. Keeping this
# exact makes the confirmation total include Docker's download as well as data.
_MAP_IMAGE_DOWNLOAD_BYTES = 1_187_513_735
_MAP_FILES = (
    AssetFile(
        "osm_tile_server.tar", 41_280_327_680,
        "https://webarena-map-server-data.s3.amazonaws.com/osm_tile_server.tar"),
    AssetFile(
        "nominatim_volumes.tar", 124_774_901_760,
        "https://webarena-map-server-data.s3.amazonaws.com/nominatim_volumes.tar"),
    AssetFile(
        "osrm_routing.tar", 21_278_935_040,
        "https://webarena-map-server-data.s3.amazonaws.com/osrm_routing.tar"),
)
_MAP_VOLUMES = (
    ("webarena_verified_map_tile_db", "osm_tile_server.tar",
     "projects/ogma3/docker/volumes/osm-data/_data", 6),
    ("webarena_verified_map_routing_car", "osrm_routing.tar", "car", 1),
    ("webarena_verified_map_routing_bike", "osrm_routing.tar", "bike", 1),
    ("webarena_verified_map_routing_foot", "osrm_routing.tar", "foot", 1),
    ("webarena_verified_map_nominatim_db", "nominatim_volumes.tar",
     "projects/metis2/docker/docker/volumes/nominatim-data/_data", 7),
    ("webarena_verified_map_nominatim_flatnode", "nominatim_volumes.tar",
     "projects/metis2/docker/docker/volumes/nominatim-flatnode/_data", 7),
)
_MAP_EMPTY_VOLUMES = (
    "webarena_verified_map_website_db",
    "webarena_verified_map_tiles",
    "webarena_verified_map_style",
)
_MAP_MOUNTS = {
    "webarena_verified_map_tile_db": "/data/database",
    "webarena_verified_map_routing_car": "/data/routing/car",
    "webarena_verified_map_routing_bike": "/data/routing/bike",
    "webarena_verified_map_routing_foot": "/data/routing/foot",
    "webarena_verified_map_nominatim_db": "/data/nominatim/postgres",
    "webarena_verified_map_nominatim_flatnode": "/data/nominatim/flatnode",
    "webarena_verified_map_website_db": "/var/lib/postgresql/14/main",
    "webarena_verified_map_tiles": "/data/tiles",
    "webarena_verified_map_style": "/data/style",
}


_CMU = "http://metis.lti.cs.cmu.edu/webarena-images"
_SPECS = {
    "gitlab": AssetSpec(
        "gitlab", "GitLab WebArena image", "docker",
        "gitlab-populated-final-port8023.tar", 77_755_595_776,
        url=f"{_CMU}/gitlab-populated-final-port8023.tar",
        image="gitlab-populated-final-port8023",
        google_drive_id="19W8qM0DPyRvWCLyQe0qtnCWAHGruolMR"),
    "magento": AssetSpec(
        "magento", "Magento storefront WebArena image", "docker",
        "shopping_final_0712.tar", 67_575_898_112,
        url=f"{_CMU}/shopping_final_0712.tar",
        image="shopping_final_0712",
        google_drive_id="1gxXalk9O0p9eu1YkIJcmZta1nvvyAJpA"),
    "magento_admin": AssetSpec(
        "magento_admin", "Magento Admin WebArena image", "docker",
        "shopping_admin_final_0719.tar", 9_640_032_256,
        url=f"{_CMU}/shopping_admin_final_0719.tar",
        image="shopping_admin_final_0719",
        google_drive_id="1See0ZhJRw0WTTL9y8hFlgaduwPZ_nGfd"),
    "postmill": AssetSpec(
        "postmill", "Postmill WebArena image", "docker",
        "postmill-populated-exposed-withimg.tar", 53_435_097_088,
        url=f"{_CMU}/postmill-populated-exposed-withimg.tar",
        image="postmill-populated-exposed-withimg",
        google_drive_id="17Qpp1iu_mPqzgO_73Z9BnFjHrzmX9DGf"),
    "kiwix": AssetSpec(
        "kiwix", "Current offline English Wikipedia", "file",
        size=130 * 1024**3,
        catalog_url="https://download.kiwix.org/zim/wikipedia/",
        catalog_pattern=r"wikipedia_en_all_maxi_(\d{4}-\d{2})\.zim"),
    "openstreetmap": AssetSpec(
        "openstreetmap", "OpenStreetMap WebArena map stack", "map",
        size=sum(item.size for item in _MAP_FILES), image=_MAP_IMAGE,
        files=_MAP_FILES),
}


def _install_required_bytes(spec: AssetSpec, download_bytes: int) -> int:
    required = download_bytes
    if spec.kind == "docker":
        # Docker import temporarily keeps both the archive and unpacked image.
        required += spec.size
    elif spec.kind == "map":
        # Map archives are retained and copied into volumes; reserve working
        # room for the extracted image and metadata as well.
        required += spec.size + 8 * 1024**3
    return required


def fresh_install_required_bytes(application: str) -> int:
    """Peak free space needed to install one app's optional archived assets.

    This is deliberately cache-independent: the application picker should be
    stable and comparable before a preflight checks this particular machine.
    The later preflight still reports the smaller, machine-specific remainder.
    """
    spec = _SPECS.get(application)
    if spec is None or spec.kind == "external":
        return default_registry().manifest(application).fresh_install_bytes
    download_bytes = spec.size + (
        _MAP_IMAGE_DOWNLOAD_BYTES if spec.kind == "map" else 0)
    return _install_required_bytes(spec, download_bytes)


def fresh_install_space_is_estimate(application: str) -> bool:
    """Bundled Compose apps vary slightly by platform and Docker cache."""
    spec = _SPECS.get(application)
    return spec is None or bool(spec.catalog_url)


class _Cancelled(Exception):
    pass


class ApplicationAssetStore:
    """Thread-safe download/import jobs polled by the New Task dialog."""

    def __init__(self, cache_root: Path | str | None = None, *,
                 runner: CommandRunner | None = None,
                 http_factory=httpx.Client, probe=None) -> None:
        self.cache_root = Path(
            cache_root or Path.home() / ".cache" / "showAndTell" / "webarena-assets")
        self.runner = runner or CommandRunner()
        self.http_factory = http_factory
        self.probe = probe or self._probe_url
        self._jobs: dict[str, dict] = {}
        self._resolved_specs: dict[str, AssetSpec] = {}
        self._lock = threading.RLock()

    def _spec(self, application: str) -> AssetSpec | None:
        spec = _SPECS.get(application)
        if spec is None or not spec.catalog_url:
            return spec
        with self._lock:
            cached = self._resolved_specs.get(application)
        if cached is not None:
            return cached
        resolved = self._resolve_catalog_spec(spec)
        with self._lock:
            return self._resolved_specs.setdefault(application, resolved)

    def _resolve_catalog_spec(self, spec: AssetSpec) -> AssetSpec:
        """Resolve the newest dated Kiwix archive from its official catalog."""
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
        try:
            with self.http_factory(follow_redirects=True, timeout=timeout) as client:
                catalog = client.get(spec.catalog_url)
                catalog.raise_for_status()
                matches = sorted(set(re.findall(spec.catalog_pattern, catalog.text)))
                if not matches:
                    raise AssetError(
                        f"{spec.label} was not listed in the official Kiwix catalog")
                version = matches[-1]
                filename = f"wikipedia_en_all_maxi_{version}.zim"
                url = urljoin(spec.catalog_url, filename)
                with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as head:
                    head.raise_for_status()
                    content_range = head.headers.get("content-range", "")
                    match = re.search(r"/(\d+)$", content_range)
                    if match:
                        size = int(match.group(1))
                    elif head.status_code == 200:
                        size = int(head.headers.get("content-length", "0"))
                    else:
                        size = 0
                checksum = client.get(f"{url}.sha256")
                checksum.raise_for_status()
                digest = re.search(r"\b([0-9a-fA-F]{64})\b", checksum.text)
                if size <= 0 or digest is None:
                    raise AssetError(
                        f"{filename} metadata is incomplete in the official Kiwix catalog")
        except AssetError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise AssetError(
                f"could not resolve {spec.label} from the official Kiwix catalog: {exc}") from exc
        return replace(
            spec, label=f"Offline English Wikipedia ({version})",
            filename=filename, size=size, url=url,
            sha256=digest.group(1).lower(), catalog_url="", catalog_pattern="")

    def _zim_target(self, spec: AssetSpec) -> Path:
        directory = Path(os.environ.get(
            "WEBARENA_ZIM_DIR", str(Path.home() / ".cache" / "webarena-images")))
        filename = (os.environ.get("SHOWANDTELL_WIKIPEDIA_ZIM")
                    or os.environ.get("SHOWANDTELL_KIWIX_ZIM") or spec.filename)
        return directory / filename

    def _archive(self, spec: AssetSpec) -> Path:
        return self.cache_root / spec.filename

    def _map_archive(self, item: AssetFile) -> Path:
        return self.cache_root / "map" / item.filename

    def _partial(self, spec: AssetSpec) -> Path:
        target = self._zim_target(spec) if spec.kind == "file" else self._archive(spec)
        return target.with_name(f"{target.name}.part")

    def _docker_image_available(self, image: str) -> bool:
        result = self.runner.run(
            ["docker", "image", "inspect", image], check=False, timeout=30.0)
        return result.returncode == 0

    def _external_available(self, application: str) -> bool:
        try:
            return default_registry().driver(application).status()["state"] == "healthy"
        except Exception:  # noqa: BLE001 - preflight reports unavailable, not a traceback
            return False

    @staticmethod
    def _probe_url(url: str) -> bool:
        try:
            response = httpx.get(url, follow_redirects=True, timeout=10.0)
            if response.status_code != 200:
                return False
            if "/tile/" in url:
                return ("image" in response.headers.get("content-type", "")
                        or response.content.startswith(b"\x89PNG"))
            if "/nominatim/" in url:
                payload = response.json()
                return isinstance(payload, list) and bool(payload)
            if "/osrm/" in url:
                return response.json().get("code") == "Ok"
            return True
        except (httpx.HTTPError, ValueError):
            return False

    def _map_ready(self) -> bool:
        base = os.environ.get(
            "SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL", "http://127.0.0.1:3000"
        ).rstrip("/")
        checks = (
            f"{base}/",
            f"{base}/tile/0/0/0.png",
            f"{base}/nominatim/search?q=Pittsburgh&format=json&limit=1",
            f"{base}/osrm/routed-car/route/v1/driving/"
            "-80.000,40.440;-79.990,40.440?overview=false",
        )
        return all(self.probe(url) for url in checks)

    def _installed(self, spec: AssetSpec) -> bool:
        if spec.kind == "docker":
            return self._docker_image_available(spec.image)
        if spec.kind == "file":
            target = self._zim_target(spec)
            return target.is_file() and target.stat().st_size == spec.size
        if spec.kind == "map":
            return self._map_ready()
        return self._external_available(spec.application)

    @staticmethod
    def _application_names(applications) -> list[str]:
        registry = default_registry()
        if (not isinstance(applications, list) or not applications
                or any(not isinstance(item, str) for item in applications)):
            raise AssetError("applications must be a non-empty array of names")
        unknown = sorted(set(applications) - set(registry.names()))
        if unknown:
            raise AssetError(f"unknown applications: {unknown}")
        return list(dict.fromkeys(applications))

    def _asset_row(self, spec: AssetSpec) -> dict:
        installed = self._installed(spec)
        if spec.kind == "map":
            cached = 0
            for item in spec.files:
                archive = self._map_archive(item)
                partial = archive.with_name(f"{archive.name}.part")
                if archive.is_file() and archive.stat().st_size == item.size:
                    cached += item.size
                elif partial.is_file():
                    cached += min(partial.stat().st_size, item.size)
        else:
            partial = self._partial(spec)
            cached = min(partial.stat().st_size, spec.size) if partial.is_file() else 0
        if spec.kind == "docker":
            archive = self._archive(spec)
            if archive.is_file() and archive.stat().st_size == spec.size:
                cached = spec.size
        elif spec.kind == "file" and installed:
            cached = spec.size
        row = {
            "id": spec.application,
            "application": spec.application,
            "label": spec.label,
            "kind": spec.kind,
            "installed": installed,
            "size_bytes": spec.size,
            "cached_bytes": cached,
        }
        if (spec.kind == "map" and not installed
                and os.environ.get("SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL")):
            row["kind"] = "external"
            row["message"] = (
                "The configured SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL is not ready. "
                "Start that deployment or unset the variable to install a local "
                "WebArena-Verified map stack.")
        elif spec.kind == "external" and not installed:
            row["message"] = (
                "Start the WebArena OpenStreetMap stack, or configure "
                "SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL.")
        elif spec.kind == "docker":
            row["verification"] = "exact archive size and imported Docker image name"
            row["source"] = (
                "WebArena Google Drive (HTTPS), with its official CMU HTTP mirror "
                "when Google applies a download quota")
        elif spec.kind == "file":
            row["verification"] = "SHA-256"
        elif spec.kind == "map":
            image_installed = installed or self._docker_image_available(spec.image)
            row["verification"] = (
                "exact S3 archive sizes, successful volume extraction, and "
                "live website, tile, geocoding, and routing probes")
            row["source"] = (
                "ServiceNow WebArena-Verified image and official WebArena S3 data")
            row["platform"] = "linux/amd64 (Docker emulation on Apple Silicon)"
            row["runtime_image_bytes"] = _MAP_IMAGE_DOWNLOAD_BYTES
            row["runtime_image_installed"] = image_installed
            row["download_bytes"] = max(0, spec.size - cached) + (
                0 if image_installed else _MAP_IMAGE_DOWNLOAD_BYTES)
        return row

    def preflight(self, applications) -> dict:
        names = self._application_names(applications)
        specs = {name: self._spec(name) for name in names}
        rows = [self._asset_row(spec) for spec in specs.values() if spec is not None]
        downloads = [row for row in rows
                     if not row["installed"] and row["kind"] != "external"]
        blockers = [row for row in rows
                    if not row["installed"] and row["kind"] == "external"]
        download_bytes = sum(
            row.get("download_bytes",
                    max(0, row["size_bytes"] - row["cached_bytes"]))
            for row in downloads)
        required = sum(
            _install_required_bytes(
                specs[row["application"]],
                row.get("download_bytes",
                        max(0, row["size_bytes"] - row["cached_bytes"])),
            )
            for row in downloads
        )
        probe = self.cache_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        return {
            "ok": not downloads and not blockers,
            "applications": names,
            "assets": rows,
            "downloads": downloads,
            "blockers": blockers,
            "download_bytes": download_bytes,
            "required_bytes": required,
            "free_bytes": free,
            "enough_space": free >= required,
        }

    def start(self, applications) -> dict:
        preflight = self.preflight(applications)
        if preflight["blockers"]:
            raise AssetError(
                preflight["blockers"][0]["message"], 409,
                details={"code": "external_asset_required",
                         "preflight": preflight})
        if not preflight["downloads"]:
            return {"id": None, "status": "completed", "progress": 1.0,
                    "preflight": preflight}
        if not preflight["enough_space"]:
            raise AssetError(
                "not enough free disk space for the selected application assets",
                409, details={"code": "insufficient_disk",
                              "preflight": preflight})
        with self._lock:
            active = next((job for job in self._jobs.values()
                           if job["status"] in {"queued", "running", "canceling"}), None)
            if active is not None:
                if set(active["applications"]) == set(preflight["applications"]):
                    return self._public(active)
                raise AssetError(
                    "another application asset installation is already running", 409,
                    details={"code": "asset_install_busy", "job": self._public(active)})
            job_id = secrets.token_hex(16)
            job = {
                "id": job_id,
                "applications": preflight["applications"],
                "status": "queued",
                "stage": "queued",
                "current": None,
                "downloaded_bytes": 0,
                "total_bytes": preflight["download_bytes"],
                "message": "Preparing downloads…",
                "error": None,
                "assets": [dict(row, status="waiting", progress=0.0)
                           for row in preflight["downloads"]],
                "cancel": threading.Event(),
                "updated_at": time.time(),
            }
            self._jobs[job_id] = job
            thread = threading.Thread(
                target=self._run, args=(job_id,), daemon=True,
                name=f"showAndTell-assets-{job_id[:8]}")
            job["thread"] = thread
            thread.start()
            return self._public(job)

    def _public(self, job: dict) -> dict:
        total = job["total_bytes"]
        progress = (job["downloaded_bytes"] / total if total else
                    1.0 if job["status"] == "completed" else 0.0)
        return {
            key: value for key, value in job.items()
            if key not in {"cancel", "thread"}
        } | {"progress": min(1.0, progress)}

    def status(self, job_id: str) -> dict:
        with self._lock:
            try:
                return self._public(self._jobs[job_id])
            except KeyError:
                raise AssetError("application asset job was not found", 404) from None

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                raise AssetError("application asset job was not found", 404) from None
            if job["status"] in {"queued", "running"}:
                job["status"] = "canceling"
                job["message"] = "Canceling; the partial download will be retained…"
                job["cancel"].set()
            return self._public(job)

    def _update(self, job_id: str, **values) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(values, updated_at=time.time())

    def _asset_update(self, job_id: str, application: str, **values) -> None:
        with self._lock:
            job = self._jobs[job_id]
            row = next(item for item in job["assets"] if item["id"] == application)
            row.update(values)
            job["updated_at"] = time.time()

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            applications = list(job["applications"])
            cancel = job["cancel"]
        self._update(job_id, status="running")
        try:
            for application in applications:
                spec = self._spec(application)
                if spec is None or self._installed(spec):
                    continue
                if cancel.is_set():
                    raise _Cancelled
                self._install(job_id, spec, cancel)
                if cancel.is_set():
                    raise _Cancelled
            self._update(
                job_id, status="completed", stage="completed", current=None,
                message="Assets installed. Starting clean applications…")
        except _Cancelled:
            self._update(
                job_id, status="canceled", stage="canceled", current=None,
                message="Download canceled. Partial files were retained for resume.")
        except Exception as exc:  # noqa: BLE001 - worker errors are surfaced to the UI
            self._update(
                job_id, status="failed", stage="failed", current=None,
                message="Asset installation failed.", error=str(exc)[:2000])

    def _install(self, job_id: str, spec: AssetSpec,
                 cancel: threading.Event) -> None:
        if spec.kind == "map":
            self._install_map(job_id, spec, cancel)
            return
        destination = self._zim_target(spec) if spec.kind == "file" else self._archive(spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.stat().st_size != spec.size:
            if spec.google_drive_id:
                self._download_google_drive(job_id, spec, destination, cancel)
            else:
                self._download(job_id, spec, destination, cancel)
        if cancel.is_set():
            raise _Cancelled
        if spec.kind == "file":
            self._asset_update(job_id, spec.application, status="verifying", progress=1.0)
            self._update(job_id, stage="verifying", current=spec.application,
                         message=f"Verifying {spec.label}…")
            digest = hashlib.sha256()
            with destination.open("rb") as handle:
                while chunk := handle.read(8 * 1024 * 1024):
                    if cancel.is_set():
                        raise _Cancelled
                    digest.update(chunk)
            if digest.hexdigest() != spec.sha256:
                destination.unlink(missing_ok=True)
                raise AssetError(f"{spec.filename} failed SHA-256 verification")
        else:
            self._asset_update(job_id, spec.application, status="importing", progress=1.0)
            self._update(job_id, stage="importing", current=spec.application,
                         message=f"Importing {spec.label} into Docker…")
            result = subprocess.run(
                ["docker", "load", "--input", str(destination)],
                capture_output=True, timeout=3600.0)
            if result.returncode:
                detail = (result.stderr or result.stdout or b"no output")[:1500]
                raise AssetError(
                    f"docker load failed for {spec.filename}: "
                    f"{detail.decode(errors='replace')}")
            if not self._docker_image_available(spec.image):
                raise AssetError(
                    f"{spec.filename} loaded but did not contain {spec.image!r}")
        self._asset_update(job_id, spec.application, status="completed", progress=1.0)

    def _run_cancelable(self, job_id: str, argv: list[str], *,
                        cancel: threading.Event, timeout: float,
                        message: str, container: str | None = None) -> None:
        """Run a long Docker operation without making Cancel a dead control."""
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except FileNotFoundError as exc:
            raise AssetError(
                "Docker is required to install the OpenStreetMap environment. "
                "Install and start Docker Desktop, then retry.") from exc
        recent: deque[str] = deque(maxlen=12)

        def read_output() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                cleaned = line.strip()
                if cleaned:
                    recent.append(cleaned)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        last_detail = ""
        while process.poll() is None:
            if cancel.wait(0.25):
                if container:
                    self.runner.run(
                        ["docker", "rm", "-f", container], check=False,
                        timeout=60.0)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise _Cancelled
            if time.monotonic() >= deadline:
                if container:
                    self.runner.run(
                        ["docker", "rm", "-f", container], check=False,
                        timeout=60.0)
                process.kill()
                process.wait(timeout=5)
                raise AssetError(f"{message} timed out after {int(timeout)} seconds")
            detail = recent[-1][-300:] if recent else ""
            if detail and detail != last_detail:
                self._update(job_id, message=f"{message} {detail}")
                last_detail = detail
        reader.join(timeout=1)
        if process.returncode:
            detail = "\n".join(recent)[-1500:] or "no command output"
            raise AssetError(f"{message} failed: {detail}")

    def _map_volume_ready(self, spec: AssetSpec, volume: str) -> bool:
        result = self.runner.run([
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "--entrypoint", "/usr/bin/test", "--volume", f"{volume}:/vol",
            spec.image, "-f", "/vol/.showAndTell-ready",
        ], check=False, timeout=60.0)
        return result.returncode == 0

    def _extract_map_volume(self, job_id: str, spec: AssetSpec, *,
                            volume: str, archive: Path, source: str,
                            strip_components: int,
                            cancel: threading.Event) -> None:
        suffix = volume.removeprefix("webarena_verified_map_").replace("_", "-")
        container = f"showAndTell-map-extract-{suffix}"
        self.runner.run(
            ["docker", "rm", "-f", container], check=False, timeout=60.0)
        self._run_cancelable(job_id, [
            "docker", "run", "--rm", "--name", container,
            "--platform", "linux/amd64", "--entrypoint", "/usr/bin/tar",
            "--volume", f"{archive.parent}:/tar:ro",
            "--volume", f"{volume}:/vol", spec.image,
            "-xf", f"/tar/{archive.name}", "-C", "/vol",
            f"--strip-components={strip_components}", source,
        ], cancel=cancel, timeout=4 * 3600.0,
            message=f"Extracting {archive.name} into {suffix}…",
            container=container)
        self.runner.run([
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "--entrypoint", "/usr/bin/touch", "--volume", f"{volume}:/vol",
            spec.image, "/vol/.showAndTell-ready",
        ], timeout=60.0)

    def _install_map(self, job_id: str, spec: AssetSpec,
                     cancel: threading.Event) -> None:
        if not self._docker_image_available(spec.image):
            self._asset_update(
                job_id, spec.application, status="pulling", progress=0.0)
            self._update(
                job_id, stage="pulling", current=spec.application,
                message="Pulling the pinned WebArena-Verified map image…")
            self._run_cancelable(
                job_id, ["docker", "pull", "--platform", "linux/amd64", spec.image],
                cancel=cancel,
                timeout=3600.0, message="Pulling the map runtime image…")
            with self._lock:
                job = self._jobs[job_id]
                job["downloaded_bytes"] += _MAP_IMAGE_DOWNLOAD_BYTES
                job["updated_at"] = time.time()

        completed = 0
        for item in spec.files:
            destination = self._map_archive(item)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and destination.stat().st_size == item.size:
                completed += item.size
                continue
            file_spec = AssetSpec(
                spec.application, f"{spec.label}: {item.filename}", "map-file",
                item.filename, item.size, item.url)
            self._download(
                job_id, file_spec, destination, cancel,
                partial=destination.with_name(f"{destination.name}.part"),
                asset_offset=completed, asset_total=spec.size)
            completed += item.size

        self._asset_update(
            job_id, spec.application, status="extracting",
            downloaded_bytes=spec.size, progress=1.0)
        all_volumes = [item[0] for item in _MAP_VOLUMES] + list(_MAP_EMPTY_VOLUMES)
        for volume in all_volumes:
            if cancel.is_set():
                raise _Cancelled
            self.runner.run(
                ["docker", "volume", "create", volume], timeout=60.0)

        for volume, filename, source, strip_components in _MAP_VOLUMES:
            if cancel.is_set():
                raise _Cancelled
            if self._map_volume_ready(spec, volume):
                continue
            archive = next(
                self._map_archive(item) for item in spec.files
                if item.filename == filename)
            self._update(
                job_id, stage="extracting", current=spec.application,
                message=f"Extracting {filename} into Docker volumes…")
            self._extract_map_volume(
                job_id, spec, volume=volume, archive=archive, source=source,
                strip_components=strip_components, cancel=cancel)

        if cancel.is_set():
            raise _Cancelled
        container = "showAndTell-openstreetmap"
        self.runner.run(
            ["docker", "rm", "-f", container], check=False, timeout=60.0)
        command = [
            "docker", "run", "--name", container, "--detach",
            "--platform", "linux/amd64",
            "--env", "WA_ENV_CTRL_EXTERNAL_SITE_URL=http://127.0.0.1:3000",
            "--publish", "127.0.0.1:3000:8080",
            "--publish", "127.0.0.1:3001:8877",
        ]
        for volume, mount in _MAP_MOUNTS.items():
            command.extend(("--volume", f"{volume}:{mount}"))
        command.append(spec.image)
        self._update(
            job_id, stage="starting", current=spec.application,
            message="Starting OpenStreetMap, tiles, geocoding, and routing…")
        self.runner.run(command, timeout=300.0)

        deadline = time.monotonic() + 20 * 60.0
        while not self._map_ready():
            if cancel.wait(2.0):
                self.runner.run(
                    ["docker", "rm", "-f", container], check=False,
                    timeout=60.0)
                raise _Cancelled
            if time.monotonic() >= deadline:
                logs = self.runner.run(
                    ["docker", "logs", "--tail", "40", container],
                    check=False, timeout=30.0)
                detail = (logs.stderr or logs.stdout or b"no container logs")[-1500:]
                raise AssetError(
                    "OpenStreetMap did not become ready within 20 minutes: "
                    f"{detail.decode(errors='replace')}")
        self._asset_update(
            job_id, spec.application, status="completed", progress=1.0)

    def _download_google_drive(self, job_id: str, spec: AssetSpec,
                               destination: Path,
                               cancel: threading.Event) -> None:
        """Run gdown out-of-process so cancellation remains immediate.

        WebArena's Google Drive links are HTTPS and are the upstream project's
        preferred mirrors.  Watching the destination file gives the viewer
        byte progress while gdown handles Drive confirmation and range resume.
        """
        partial = self._partial(spec)
        partial.parent.mkdir(parents=True, exist_ok=True)
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > spec.size:
            partial.unlink()
            offset = 0
        self._asset_update(
            job_id, spec.application, status="downloading",
            downloaded_bytes=offset, progress=offset / spec.size)
        self._update(job_id, stage="downloading", current=spec.application,
                     message=f"Downloading {spec.label}…")
        process = subprocess.Popen(
            [sys.executable, "-m", "gdown", "--continue", "--quiet",
             "--output", str(partial), spec.google_drive_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        previous = offset
        while process.poll() is None:
            if cancel.wait(0.25):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                self._adopt_gdown_partial(partial)
                raise _Cancelled
            current = self._gdown_partial_size(partial, previous)
            if current < previous:
                # gdown restarted instead of resuming; reflect the extra bytes
                # it must transfer rather than allowing progress above 100%.
                with self._lock:
                    self._jobs[job_id]["total_bytes"] += previous
                previous = 0
            if current != previous:
                with self._lock:
                    job = self._jobs[job_id]
                    job["downloaded_bytes"] += current - previous
                    job["updated_at"] = time.time()
                previous = current
                self._asset_update(
                    job_id, spec.application, downloaded_bytes=current,
                    progress=min(1.0, current / spec.size))
        self._adopt_gdown_partial(partial)
        current = partial.stat().st_size if partial.is_file() else previous
        if current != previous:
            with self._lock:
                self._jobs[job_id]["downloaded_bytes"] += current - previous
        if process.returncode:
            detail = (process.stderr.read() if process.stderr else b"no output")[:1500]
            if spec.url:
                self._update(
                    job_id, message=(
                        f"Google Drive is unavailable; resuming {spec.label} "
                        "from WebArena's CMU mirror…"))
                self._download(job_id, spec, destination, cancel)
                return
            raise AssetError(f"download failed for {spec.filename}: "
                             f"{detail.decode(errors='replace')}")
        if current != spec.size:
            raise AssetError(
                f"{spec.filename} is incomplete: received {current} of {spec.size} bytes")
        os.replace(partial, destination)

    @staticmethod
    def _gdown_partials(partial: Path) -> list[Path]:
        """Files gdown may use while its confirmed download is in flight."""
        candidates = [partial]
        candidates.extend(partial.parent.glob(f"{partial.name}*.part"))
        return [path for path in candidates if path.is_file()]

    def _gdown_partial_size(self, partial: Path, fallback: int = 0) -> int:
        sizes = [path.stat().st_size for path in self._gdown_partials(partial)]
        return max(sizes, default=fallback)

    def _adopt_gdown_partial(self, partial: Path) -> None:
        """Keep gdown's randomized temporary file resumable by our fallback."""
        candidates = self._gdown_partials(partial)
        if not candidates:
            return
        largest = max(candidates, key=lambda path: path.stat().st_size)
        if largest != partial:
            os.replace(largest, partial)

    def _download(self, job_id: str, spec: AssetSpec, destination: Path,
                  cancel: threading.Event, *, partial: Path | None = None,
                  asset_offset: int = 0,
                  asset_total: int | None = None) -> None:
        partial = partial or self._partial(spec)
        asset_total = asset_total or spec.size
        partial.parent.mkdir(parents=True, exist_ok=True)
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > spec.size:
            partial.unlink()
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        self._asset_update(
            job_id, spec.application, status="downloading",
            downloaded_bytes=asset_offset + offset,
            progress=(asset_offset + offset) / asset_total)
        self._update(job_id, stage="downloading", current=spec.application,
                     message=f"Downloading {spec.label}…")
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
        with self.http_factory(follow_redirects=True, timeout=timeout) as client:
            with client.stream("GET", spec.url, headers=headers) as response:
                response.raise_for_status()
                append = bool(offset and response.status_code == 206)
                if offset and not append:
                    with self._lock:
                        # This mirror ignored Range, so the job now has to
                        # transfer the cached prefix again.
                        self._jobs[job_id]["total_bytes"] += offset
                    offset = 0
                mode = "ab" if append else "wb"
                written = offset
                with partial.open(mode) as handle:
                    # Small chunks keep progress and cancellation responsive
                    # even when an archival mirror serves data slowly.
                    for chunk in response.iter_bytes(64 * 1024):
                        if cancel.is_set():
                            raise _Cancelled
                        handle.write(chunk)
                        written += len(chunk)
                        if written > spec.size:
                            raise AssetError(
                                f"{spec.filename} exceeded its expected size")
                        self._asset_update(
                            job_id, spec.application,
                            downloaded_bytes=asset_offset + written,
                            progress=(asset_offset + written) / asset_total)
                        with self._lock:
                            job = self._jobs[job_id]
                            job["downloaded_bytes"] += len(chunk)
                            job["updated_at"] = time.time()
                    handle.flush()
                    os.fsync(handle.fileno())
        if written != spec.size:
            raise AssetError(
                f"{spec.filename} is incomplete: received {written} of {spec.size} bytes")
        os.replace(partial, destination)

    def close(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                if job["status"] in {"queued", "running", "canceling"}:
                    job["cancel"].set()
