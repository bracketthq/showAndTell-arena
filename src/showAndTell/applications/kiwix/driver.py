"""Kiwix lifecycle for the pinned offline Wikipedia archive."""
from __future__ import annotations

import os
from pathlib import Path

from showAndTell.applications.host.protocol import DriverError
from showAndTell.applications.lifecycle.docker_image import DockerImageDriver


class KiwixDriver(DockerImageDriver):
    image = "ghcr.io/kiwix/kiwix-serve:3.3.0"
    container_port = 80
    boot_timeout = 300.0
    recreate_on_reset = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_command = (
            os.environ.get("SHOWANDTELL_WIKIPEDIA_ZIM")
            or os.environ.get("SHOWANDTELL_KIWIX_ZIM")
            or "wikipedia_en_all_maxi_2022-05.zim",
        )

    def _archive_dir(self) -> Path:
        return Path(os.environ.get(
            "WEBARENA_ZIM_DIR", str(Path.home() / ".cache" / "webarena-images")))

    def start(self, *, wait: bool = True) -> None:
        archive = self._archive_dir() / self.run_command[0]
        if not self._assume_running() and not archive.is_file():
            raise DriverError(
                f"kiwix requires the offline Wikipedia archive {archive}, "
                "but it is not installed. Place the WebArena ZIM there or set "
                "WEBARENA_ZIM_DIR and SHOWANDTELL_WIKIPEDIA_ZIM, then retry.")
        super().start(wait=wait)

    def _run_flags(self) -> tuple[str, ...]:
        return ("--volume", f"{self._archive_dir()}:/data:ro")


Driver = KiwixDriver
