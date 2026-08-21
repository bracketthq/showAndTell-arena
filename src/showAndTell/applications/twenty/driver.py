"""Twenty lifecycle plus supported-API workspace bootstrap."""
from __future__ import annotations

from showAndTell.applications.twenty.bootstrap import bootstrap_twenty
from showAndTell.applications.lifecycle.compose import ComposeDriver


class TwentyDriver(ComposeDriver):
    boot_timeout = 1200.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._bootstrap = None

    def _ensure_bootstrap(self) -> None:
        credentials = self.manifest.credentials
        self._bootstrap = bootstrap_twenty(
            base_url=f"http://127.0.0.1:{self.port}",
            email=credentials["email"],
            password=credentials["password"],
        )

    def start(self, *, wait: bool = True) -> None:
        super().start(wait=wait)
        if wait:
            self._ensure_bootstrap()

    def secrets(self) -> dict:
        if self._bootstrap is None:
            self._ensure_bootstrap()
        return {
            "api_token": self._bootstrap.api_token,
            "workspace_id": self._bootstrap.workspace_id,
            "workspace_member_id": self._bootstrap.workspace_member_id,
        }


Driver = TwentyDriver
