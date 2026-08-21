"""Magento storefront lifecycle beside the WebArena container."""
from __future__ import annotations

from showAndTell.applications.lifecycle.docker_image import DockerImageDriver


class MagentoDriver(DockerImageDriver):
    image = "shopping_final_0712"
    local_only_image = True
    container_port = 80
    boot_timeout = 900.0

    @property
    def app_url(self) -> str:
        return self.public_url

    def _configure(self) -> bool:
        current = self._docker(
            "exec", self.container, "mysql", "-u", "magentouser",
            "-pMyPassword", "magentodb", "-N", "-B", "-e",
            'SELECT value FROM core_config_data WHERE path="web/unsecure/base_url";',
            check=False).stdout.decode().strip()
        want = f"{self.app_url}/"
        if current == want:
            return False
        self._docker(
            "exec", self.container, "mysql", "-u", "magentouser",
            "-pMyPassword", "magentodb", "-e",
            f'UPDATE core_config_data SET value="{want}" WHERE path IN '
            '("web/unsecure/base_url","web/secure/base_url");', check=False)
        self._docker(
            "exec", self.container, "/var/www/magento2/bin/magento",
            "setup:store-config:set", f"--base-url={self.app_url}", check=False)
        self._docker(
            "exec", self.container, "/var/www/magento2/bin/magento",
            "cache:flush", check=False)
        return True

    def secrets(self) -> dict:
        return {
            "admin_username": "admin",
            "admin_password": "admin1234",
        }


Driver = MagentoDriver
