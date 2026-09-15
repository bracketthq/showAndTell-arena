"""Local, layered configuration for the serve-mode viewer.

Repository configuration remains authoritative for canonical scoring. This
store adds repository-local ``.env`` preferences beneath explicit environment
variables. Secret values are persisted only in that gitignored, mode-0600 file
and are never returned through the viewer API.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from showAndTell.applications.host import client as host_client
from showAndTell.quiz import protocol

from . import _util


DEFAULT_PATH = Path(__file__).resolve().parents[3] / ".env"
HOST_ENV = (host_client.URL_ENV, host_client.TOKEN_ENV)
JUDGE_ENV = ("SHOWANDTELL_JUDGE_BACKEND", "SHOWANDTELL_JUDGE_MODEL")
API_KEYS = {
    "automatic": "ANTHROPIC_API_KEY",
    "anthropic-api": "ANTHROPIC_API_KEY",
    "openai-api": "OPENAI_API_KEY",
    "gemini-api": "GEMINI_API_KEY",
}
RELEVANT_ENV = (*HOST_ENV, *JUDGE_ENV, *API_KEYS.values())
_ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$")


class SettingsError(_util.ViewerError):
    pass


def _table(payload: object, name: str) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SettingsError(f"{name} settings must be a table")
    return payload


class SettingsStore:
    """Resolve environment > local preferences > repository defaults."""

    def __init__(self, path: Path | str | None = None, *,
                 environment: Mapping[str, str] | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH).expanduser()
        source = dict(os.environ if environment is None else environment)
        self._original_runtime = {
            key: os.environ.get(key) for key in RELEVANT_ENV
        }
        self._external = {
            key: value for key, value in source.items()
            if key in RELEVANT_ENV and value
        }
        self._secrets: dict[str, str] = {}
        self._lock = threading.RLock()
        self.warning: str | None = None
        self._local = self._load()
        self._base_judge = protocol.load(apply_environment=False)
        self._apply()

    def close(self) -> None:
        """Restore the process environment owned before this store started."""
        with self._lock:
            for key, value in self._original_runtime.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _load(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self.warning = f"Could not read {self.path}: {exc}"
            return {}
        values: dict[str, str] = {}
        for number, line in enumerate(text.splitlines(), start=1):
            match = _ENV_ASSIGNMENT.match(line)
            if not match or match.group("name") not in RELEVANT_ENV:
                continue
            try:
                value = self._decode_env_value(line.split("=", 1)[1])
            except ValueError as exc:
                self.warning = f"Could not read {self.path}:{number}: {exc}"
                continue
            if value:
                values[match.group("name")] = value
        for name in (host_client.TOKEN_ENV, *API_KEYS.values()):
            if values.get(name):
                self._secrets[name] = values[name]
        fixture: dict[str, str] = {"mode": "local"}
        if values.get(host_client.URL_ENV):
            fixture = {"mode": "remote", "url": values[host_client.URL_ENV]}
        judge = {}
        if values.get("SHOWANDTELL_JUDGE_BACKEND"):
            judge["backend"] = values["SHOWANDTELL_JUDGE_BACKEND"]
        if values.get("SHOWANDTELL_JUDGE_MODEL"):
            judge["model"] = values["SHOWANDTELL_JUDGE_MODEL"]
        return {"fixture_host": fixture, "judge": judge}

    @staticmethod
    def _decode_env_value(raw: str) -> str:
        value = raw.strip()
        if not value:
            return ""
        if value.startswith('"'):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid quoted .env value") from exc
            if not isinstance(decoded, str):
                raise ValueError(".env value must be a string")
            return decoded
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ValueError("invalid quoted .env value")
            return value[1:-1]
        return value.split(" #", 1)[0].strip()

    def _host_local(self) -> tuple[str, str]:
        table = self._local.get("fixture_host", {})
        if not isinstance(table, dict):
            return "local", ""
        mode = table.get("mode")
        url = table.get("url")
        return (
            mode if mode in {"local", "remote"} else "local",
            url.strip() if isinstance(url, str) else "",
        )

    def _judge_local(self) -> tuple[str | None, str | None]:
        table = self._local.get("judge", {})
        if not isinstance(table, dict):
            return None, None
        backend = table.get("backend")
        model = table.get("model")
        return (
            backend.strip() if isinstance(backend, str) and backend.strip() else None,
            model.strip() if isinstance(model, str) and model.strip() else None,
        )

    def _set_runtime(self, name: str, value: str | None) -> None:
        if name in self._external:
            os.environ[name] = self._external[name]
        elif value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    def _apply(self) -> None:
        mode, url = self._host_local()
        self._set_runtime(host_client.URL_ENV, url if mode == "remote" else None)
        self._set_runtime(
            host_client.TOKEN_ENV,
            self._secrets.get(host_client.TOKEN_ENV) if mode == "remote" else None,
        )
        backend, model = self._judge_local()
        self._set_runtime("SHOWANDTELL_JUDGE_BACKEND", backend)
        self._set_runtime("SHOWANDTELL_JUDGE_MODEL", model)
        effective_backend = (
            self._external.get("SHOWANDTELL_JUDGE_BACKEND")
            or backend or self._base_judge.backend)
        active_key = API_KEYS.get(effective_backend)
        for key_name in API_KEYS.values():
            self._set_runtime(
                key_name,
                self._secrets.get(key_name) if key_name == active_key else None)

    @staticmethod
    def _source(*, external: bool, local: bool, fallback: str) -> str:
        return "environment" if external else "dotenv" if local else fallback

    def public(self) -> dict:
        with self._lock:
            local_mode, local_url = self._host_local()
            host_locked = host_client.URL_ENV in self._external
            host_url = self._external.get(host_client.URL_ENV) or (
                local_url if local_mode == "remote" else "")
            host_mode = "remote" if host_url else "local"
            token_external = host_client.TOKEN_ENV in self._external
            token_saved = host_client.TOKEN_ENV in self._secrets

            local_backend, local_model = self._judge_local()
            backend = (
                self._external.get("SHOWANDTELL_JUDGE_BACKEND")
                or local_backend or self._base_judge.backend
            )
            model = (
                self._external.get("SHOWANDTELL_JUDGE_MODEL")
                or local_model or self._base_judge.model
            )
            key_name = API_KEYS.get(backend)
            key_external = bool(key_name and key_name in self._external)
            key_saved = bool(key_name and key_name in self._secrets)
            overridden = bool(
                backend != self._base_judge.backend
                or model != self._base_judge.model
                or "SHOWANDTELL_JUDGE_BACKEND" in self._external
                or "SHOWANDTELL_JUDGE_MODEL" in self._external
                or local_backend or local_model
            )
            return {
                "fixture_host": {
                    "mode": host_mode,
                    "mode_source": self._source(
                        external=host_locked,
                        local=local_mode == "remote" or "fixture_host" in self._local,
                        fallback="default",
                    ),
                    "url": host_url,
                    "url_source": self._source(
                        external=host_locked, local=bool(local_url), fallback="default"),
                    "locked": host_locked,
                    "token": {
                        "configured": token_external or token_saved,
                        "source": (
                            "environment" if token_external else
                            "dotenv" if token_saved else "missing"),
                        "locked": token_external,
                    },
                },
                "judge": {
                    "backend": backend,
                    "backend_source": self._source(
                        external="SHOWANDTELL_JUDGE_BACKEND" in self._external,
                        local=bool(local_backend), fallback="repository"),
                    "backend_locked": "SHOWANDTELL_JUDGE_BACKEND" in self._external,
                    "model": model,
                    "model_source": self._source(
                        external="SHOWANDTELL_JUDGE_MODEL" in self._external,
                        local=bool(local_model), fallback="repository"),
                    "model_locked": "SHOWANDTELL_JUDGE_MODEL" in self._external,
                    "canonical": bool(self._base_judge.canonical and not overridden),
                    "backends": sorted(protocol.SUPPORTED_BACKENDS),
                    "credential": {
                        "required": key_name is not None,
                        "variable": key_name,
                        "configured": key_external or key_saved,
                        "source": (
                            "environment" if key_external else
                            "dotenv" if key_saved else
                            "not required" if key_name is None else "missing"),
                        "locked": key_external,
                    },
                },
                "settings_path": str(self.path),
                "warning": self.warning,
            }

    @staticmethod
    def _validate_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SettingsError("remote fixture host URL is required")
        url = value.strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SettingsError("fixture host URL must be an http:// or https:// URL")
        if parsed.username or parsed.password:
            raise SettingsError("fixture host credentials must not be included in the URL")
        return url

    def update(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise SettingsError("settings request must be an object")
        with self._lock:
            session_secrets = dict(self._secrets)
            local = {
                "fixture_host": dict(_table(
                    self._local.get("fixture_host"), "fixture_host")),
                "judge": dict(_table(self._local.get("judge"), "judge")),
            }
            fixture = payload.get("fixture_host")
            if fixture is not None:
                fixture = _table(fixture, "fixture_host")
                if host_client.URL_ENV in self._external and any(
                        key in fixture for key in ("mode", "url")):
                    raise SettingsError(
                        "fixture host is controlled by SHOWANDTELL_FIXTURE_HOST_URL", 409)
                mode = fixture.get("mode", local["fixture_host"].get("mode", "local"))
                if mode not in {"local", "remote"}:
                    raise SettingsError("fixture host mode must be local or remote")
                local["fixture_host"]["mode"] = mode
                if mode == "remote":
                    local["fixture_host"]["url"] = self._validate_url(
                        fixture.get("url", local["fixture_host"].get("url")))
                else:
                    local["fixture_host"].pop("url", None)
                    if host_client.TOKEN_ENV not in self._external:
                        session_secrets.pop(host_client.TOKEN_ENV, None)
                token = fixture.get("token")
                if token is not None:
                    if host_client.TOKEN_ENV in self._external:
                        raise SettingsError(
                            "fixture host token is controlled by the environment", 409)
                    if not isinstance(token, str):
                        raise SettingsError("fixture host token must be a string")
                    if token:
                        session_secrets[host_client.TOKEN_ENV] = token
                if fixture.get("clear_token") is True:
                    session_secrets.pop(host_client.TOKEN_ENV, None)

            judge = payload.get("judge")
            if judge is not None:
                judge = _table(judge, "judge")
                if judge.get("canonical") is True:
                    for name in JUDGE_ENV:
                        if name not in self._external:
                            local["judge"].pop(
                                "backend" if name.endswith("BACKEND") else "model", None)
                else:
                    backend = judge.get("backend", local["judge"].get(
                        "backend", self._base_judge.backend))
                    model = judge.get("model", local["judge"].get(
                        "model", self._base_judge.model))
                    if "SHOWANDTELL_JUDGE_BACKEND" not in self._external:
                        if backend not in protocol.SUPPORTED_BACKENDS:
                            raise SettingsError(f"unsupported judge backend {backend!r}")
                        local["judge"]["backend"] = backend
                    if "SHOWANDTELL_JUDGE_MODEL" not in self._external:
                        if not isinstance(model, str) or not model.strip():
                            raise SettingsError("judge model must be a non-empty string")
                        local["judge"]["model"] = model.strip()
                api_key = judge.get("api_key")
                if api_key is not None:
                    effective_backend = (
                        self._external.get("SHOWANDTELL_JUDGE_BACKEND")
                        or local["judge"].get("backend") or self._base_judge.backend)
                    key_name = API_KEYS.get(effective_backend)
                    if key_name is None:
                        raise SettingsError(
                            f"{effective_backend} does not use an API key")
                    if key_name in self._external:
                        raise SettingsError(
                            f"{key_name} is controlled by the environment", 409)
                    if not isinstance(api_key, str):
                        raise SettingsError("judge API key must be a string")
                    if api_key:
                        session_secrets[key_name] = api_key
                if judge.get("clear_api_key") is True:
                    effective_backend = (
                        self._external.get("SHOWANDTELL_JUDGE_BACKEND")
                        or local["judge"].get("backend") or self._base_judge.backend)
                    key_name = API_KEYS.get(effective_backend)
                    if key_name and key_name not in self._external:
                        session_secrets.pop(key_name, None)

            self._validate_local(local)
            self._write(local, session_secrets)
            self._local = local
            self._secrets = session_secrets
            self.warning = None
            self._apply()
            return self.public()

    def would_change_host(self, payload: object) -> bool:
        """Whether a fixture-host payload changes effective connection state."""
        if not isinstance(payload, dict):
            return False
        fixture = payload.get("fixture_host")
        if not isinstance(fixture, dict):
            return False
        if "token" in fixture or fixture.get("clear_token") is True:
            return True
        current = self.public()["fixture_host"]
        mode = fixture.get("mode", current["mode"])
        url = fixture.get("url", current["url"])
        if isinstance(url, str):
            url = url.strip().rstrip("/")
        return mode != current["mode"] or (mode == "remote" and url != current["url"])

    def _validate_local(self, local: dict) -> None:
        mode = local["fixture_host"].get("mode", "local")
        if mode == "remote":
            self._validate_url(local["fixture_host"].get("url"))
        backend = local["judge"].get("backend")
        if backend is not None and backend not in protocol.SUPPORTED_BACKENDS:
            raise SettingsError(f"unsupported judge backend {backend!r}")
        model = local["judge"].get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise SettingsError("judge model must be a non-empty string")

    def _write(self, local: dict, session_secrets: dict[str, str]) -> None:
        fixture = local["fixture_host"]
        judge = local["judge"]
        wanted = {
            host_client.URL_ENV: (
                fixture.get("url") if fixture.get("mode") == "remote" else None),
            host_client.TOKEN_ENV: (
                session_secrets.get(host_client.TOKEN_ENV)
                if fixture.get("mode") == "remote" else None),
            "SHOWANDTELL_JUDGE_BACKEND": judge.get("backend"),
            "SHOWANDTELL_JUDGE_MODEL": judge.get("model"),
            **{name: session_secrets.get(name) for name in API_KEYS.values()},
        }
        try:
            existing = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            existing = []
        except OSError as exc:
            raise SettingsError(f"could not read settings from {self.path}: {exc}", 500) from exc
        lines = []
        written: set[str] = set()
        for line in existing:
            match = _ENV_ASSIGNMENT.match(line)
            name = match.group("name") if match else None
            if name not in wanted:
                lines.append(line)
                continue
            value = wanted[name]
            if value and name not in written:
                lines.append(f"{name}={json.dumps(value)}")
            written.add(name)
        additions = [
            f"{name}={json.dumps(value)}"
            for name, value in wanted.items()
            if value and name not in written
        ]
        if additions:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("# ShowAndTell viewer settings")
            lines.extend(additions)
        encoded = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, raw = tempfile.mkstemp(
                prefix="settings-", suffix=".env", dir=self.path.parent)
            temp = Path(raw)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
        except OSError as exc:
            raise SettingsError(f"could not save settings to {self.path}: {exc}", 500) from exc

    def test_host(self) -> dict:
        try:
            configured = host_client.configured_client()
            if configured is None:
                configured = host_client.local_agent_client()
            health = configured.health()
            apps = configured.apps()
        except Exception as exc:
            raise SettingsError(f"fixture host connection failed: {exc}", 502) from exc
        return {
            "ok": True,
            "mode": self.public()["fixture_host"]["mode"],
            "health": health,
            "applications": [
                item.get("name") for item in apps
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ],
        }

    def test_judge(self) -> dict:
        try:
            from showAndTell.quiz import judge

            return {"ok": True, "preflight": judge.require_ready(live=True)}
        except Exception as exc:
            raise SettingsError(f"judge test failed: {exc}", 502) from exc
