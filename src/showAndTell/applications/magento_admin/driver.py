"""Magento Admin lifecycle beside the WebArena container."""
from __future__ import annotations

import platform

from showAndTell.applications.magento.driver import MagentoDriver


class MagentoAdminDriver(MagentoDriver):
    image = "shopping_admin_final_0719"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._apple_silicon = platform.machine().lower() in {"arm64", "aarch64"}
        if self._apple_silicon:
            # This archived amd64 image's OPcache extension crashes PHP-FPM
            # under Docker Desktop's Apple Silicon emulation. Magento does not
            # require OPcache for correctness, so disable only that extension
            # and keep every other archived service/configuration intact.
            self.run_command = (
                "-lc",
                'rm -f /usr/local/etc/php/conf.d/docker-php-ext-opcache.ini; '
                'exec /docker-entrypoint.sh "$@"',
                "showAndTell-magento-admin",
                "supervisord", "-n", "-j", "/supervisord.pid",
            )

    def _run_flags(self) -> tuple[str, ...]:
        if not self._apple_silicon:
            return super()._run_flags()
        return (*super()._run_flags(), "--platform", "linux/amd64",
                "--entrypoint", "/bin/sh")

    def _configure(self) -> bool:
        changed = super()._configure()
        if changed:
            self._docker(
                "exec", self.container, "php",
                "/var/www/magento2/bin/magento", "config:set",
                "admin/security/password_is_forced", "0", check=False)
            self._docker(
                "exec", self.container, "php",
                "/var/www/magento2/bin/magento", "config:set",
                "admin/security/password_lifetime", "0", check=False)
        return changed


Driver = MagentoAdminDriver
