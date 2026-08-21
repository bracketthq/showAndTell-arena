"""Lifecycle boundary for an externally managed OpenStreetMap stack."""
from showAndTell.applications.host.protocol import DriverError
from showAndTell.applications.lifecycle.docker_image import DockerImageDriver


class OpenStreetMapDriver(DockerImageDriver):
    recreate_on_reset = False

    def start(self, *, wait: bool = True) -> None:
        if wait and not self._probe(self._health_url()):
            raise DriverError(
                "openstreetmap is externally managed and is not reachable at "
                f"{self._health_url()}. Start the WebArena OpenStreetMap stack "
                "or set SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL, then retry.")

    def stop(self) -> None:
        return None

    def reset(self) -> None:
        self.start(wait=True)


Driver = OpenStreetMapDriver
