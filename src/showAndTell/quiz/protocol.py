"""Versioned configuration and provenance for the public grading protocol."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tomllib


_REPOSITORY_CONFIG_PATH = Path(__file__).resolve().parents[3] / "judge.toml"
_PACKAGED_CONFIG_PATH = Path(__file__).resolve().parents[1] / "judge.toml"
DEFAULT_CONFIG_PATH = (
    _REPOSITORY_CONFIG_PATH
    if _REPOSITORY_CONFIG_PATH.exists()
    else _PACKAGED_CONFIG_PATH
)
SUPPORTED_BACKENDS = {
    "automatic",
    "anthropic-api", "openai-api", "gemini-api", "claude-cli", "codex-cli",
}

# "automatic" resolution order: explicit API keys are deliberate user
# intent and outrank ambient CLI installs; claude-cli outranks the
# experimental codex-cli. None = keep the configured model.
AUTOMATIC_CANDIDATES = (
    ("anthropic-api", None, lambda env, which: bool(env.get("ANTHROPIC_API_KEY"))),
    ("openai-api", "gpt-5.6-terra", lambda env, which: bool(env.get("OPENAI_API_KEY"))),
    ("gemini-api", "gemini-2.5-pro", lambda env, which: bool(env.get("GEMINI_API_KEY"))),
    ("claude-cli", None, lambda env, which: bool(which("claude"))),
    ("codex-cli", "gpt-5.6-terra", lambda env, which: bool(which("codex"))),
)


def _resolve_automatic(config: "JudgeConfig") -> "JudgeConfig":
    """Resolve backend "automatic" to the first available grader.

    Resolution to the release pairing (anthropic-api with the configured
    model) yields a config identical to one that named it outright — same
    canonical flag, same fingerprint, so caches carry over. Any other
    resolution is an override and therefore unofficial. With nothing
    available the config stays "automatic": preflight and grading then
    fail with a clear message and the run is saved ungraded.
    """
    import shutil

    for backend, model, available in AUTOMATIC_CANDIDATES:
        if not available(os.environ, shutil.which):
            continue
        resolved = replace(config, backend=backend,
                           model=model or config.model)
        if backend == "anthropic-api" and resolved.model == config.model:
            return resolved
        extra = ("backend",) + (("model",) if model else ())
        return replace(resolved, canonical=False,
                       overrides=tuple(dict.fromkeys(config.overrides + extra)))
    return replace(config, canonical=False,
                   overrides=tuple(dict.fromkeys(config.overrides + ("backend",))))
SUPPORTED_FAILURE_POLICIES = {"ungraded"}


class JudgeConfigError(RuntimeError):
    """The release judge configuration is missing or invalid."""


@dataclass(frozen=True)
class JudgeConfig:
    protocol: str
    backend: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: int
    max_attempts: int
    failure_policy: str
    canonical: bool
    input_price_per_million_tokens_usd: float
    output_price_per_million_tokens_usd: float
    pricing_as_of: str
    source: str
    overrides: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        data = asdict(self)
        data["overrides"] = list(self.overrides)
        return data

    def fingerprint(self) -> str:
        data = self.public_dict()
        data.pop("source", None)
        canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _validate(config: JudgeConfig) -> JudgeConfig:
    if not config.protocol.strip():
        raise ValueError("judge protocol must not be empty")
    if config.backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"unsupported judge backend {config.backend!r}; expected one of "
            f"{sorted(SUPPORTED_BACKENDS)}")
    if not config.model.strip():
        raise ValueError("judge model must not be empty")
    if not 0 <= config.temperature <= 1:
        raise ValueError("judge temperature must be between 0 and 1")
    if config.max_tokens < 32:
        raise ValueError("judge max_tokens must be at least 32")
    if config.timeout_seconds < 1:
        raise ValueError("judge timeout_seconds must be positive")
    if config.max_attempts < 1:
        raise ValueError("judge max_attempts must be positive")
    if config.failure_policy not in SUPPORTED_FAILURE_POLICIES:
        raise ValueError(
            f"unsupported judge failure_policy {config.failure_policy!r}")
    if (config.input_price_per_million_tokens_usd < 0
            or config.output_price_per_million_tokens_usd < 0):
        raise ValueError("judge token prices must not be negative")
    if not config.pricing_as_of.strip():
        raise ValueError("judge pricing_as_of must not be empty")
    return config


def load(path: Path | str | None = None, *,
         apply_environment: bool = True) -> JudgeConfig:
    """Load the release config and apply documented environment overrides.

    Overrides are deliberately reflected in provenance and make a result
    unofficial, so local experimentation cannot masquerade as release scoring.
    """
    selected = Path(
        path or os.environ.get("SHOWANDTELL_JUDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        payload = tomllib.loads(selected.read_text(encoding="utf-8"))["judge"]
    except FileNotFoundError as exc:
        raise JudgeConfigError(
            f"judge config not found: {selected}; set SHOWANDTELL_JUDGE_CONFIG") from exc
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise JudgeConfigError(f"invalid judge config {selected}: {exc}") from exc

    try:
        config = JudgeConfig(
            protocol=str(payload["protocol"]),
            backend=str(payload["backend"]),
            model=str(payload["model"]),
            temperature=float(payload["temperature"]),
            max_tokens=int(payload["max_tokens"]),
            timeout_seconds=int(payload["timeout_seconds"]),
            max_attempts=int(payload["max_attempts"]),
            failure_policy=str(payload["failure_policy"]),
            canonical=bool(payload["canonical"]),
            input_price_per_million_tokens_usd=float(
                payload["input_price_per_million_tokens_usd"]),
            output_price_per_million_tokens_usd=float(
                payload["output_price_per_million_tokens_usd"]),
            pricing_as_of=str(payload["pricing_as_of"]),
            source=selected.name,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise JudgeConfigError(f"invalid judge config {selected}: {exc}") from exc

    overrides = []
    backend = os.environ.get("SHOWANDTELL_JUDGE_BACKEND") if apply_environment else None
    model = os.environ.get("SHOWANDTELL_JUDGE_MODEL") if apply_environment else None
    if backend:
        config = replace(config, backend=backend)
        overrides.append("backend")
    if model:
        config = replace(config, model=model)
        overrides.append("model")
    if overrides:
        config = replace(config, canonical=False, overrides=tuple(overrides))
    if config.backend == "automatic":
        config = _resolve_automatic(config)
    try:
        return _validate(config)
    except ValueError as exc:
        raise JudgeConfigError(f"invalid judge config {selected}: {exc}") from exc


def manifest(config: JudgeConfig | None = None) -> dict:
    config = config or load()
    return {
        **config.public_dict(),
        "config_fingerprint": config.fingerprint(),
    }
