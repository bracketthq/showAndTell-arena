"""GitLab CE lifecycle beside its WebArena container."""
from __future__ import annotations

from showAndTell.applications.lifecycle.docker_image import DockerImageDriver


class GitLabDriver(DockerImageDriver):
    image = "gitlab-populated-final-port8023"
    local_only_image = True
    container_port = 8023
    health_path = "/explore"
    run_command = ("/opt/gitlab/embedded/bin/runsvdir-start",)
    boot_timeout = 2400.0

    @property
    def app_url(self) -> str:
        return self.public_url

    def _configure(self) -> bool:
        current = self._docker(
            "exec", self.container, "grep", "^external_url",
            "/etc/gitlab/gitlab.rb", check=False).stdout.decode().strip()
        want = f"external_url '{self.app_url}'"
        if current == want:
            return False
        self._docker("exec", self.container, "update-permissions", check=False)
        self._docker(
            "exec", self.container, "sed", "-i",
            f"s|^external_url.*|{want}|", "/etc/gitlab/gitlab.rb", check=False)
        self._docker(
            "exec", self.container, "gitlab-ctl", "reconfigure", check=False,
            timeout=1800.0)
        return True


Driver = GitLabDriver
