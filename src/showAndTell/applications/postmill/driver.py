"""Postmill lifecycle beside its WebArena container."""
from showAndTell.applications.lifecycle.docker_image import DockerImageDriver


class PostmillDriver(DockerImageDriver):
    image = "postmill-populated-exposed-withimg"
    local_only_image = True
    container_port = 80
    boot_timeout = 600.0


Driver = PostmillDriver
