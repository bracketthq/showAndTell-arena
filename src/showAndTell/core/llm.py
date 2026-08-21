"""Auditable, provider-neutral LLM access for benchmark judges.

The canonical backend is the Anthropic Messages API with a user-supplied key.
OpenAI and Gemini APIs, plus authenticated Claude and Codex CLI sessions, are
available for explicitly unofficial local research. SHOWANDTELL_LLM=stub
short-circuits at the caller for deterministic tests.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import quote

import httpx


def reply_has_answer(body: str, prompt_tail: str,
                     final_marker: str = "A1:") -> bool:
    """Whether text after an echoed quiz prompt contains the final answer."""
    reply = body.split(prompt_tail, 1)[-1] if prompt_tail in body else ""
    lowered = reply.lower()
    return "a1:" in lowered and final_marker.lower() in lowered


def mode() -> str:
    return os.environ.get("SHOWANDTELL_LLM", "live")


@dataclass(frozen=True)
class Completion:
    text: str
    metadata: dict


class JudgeUnavailable(RuntimeError):
    """The configured grading infrastructure is missing or inaccessible."""


def _anthropic_complete(prompt: str, system: str | None, *, model: str,
                        temperature: float, max_tokens: int,
                        timeout_seconds: int,
                        json_output: bool = False) -> Completion:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise JudgeUnavailable(
            "ANTHROPIC_API_KEY is not set; export a key for the canonical "
            "judge or set SHOWANDTELL_JUDGE_BACKEND=claude-cli for unofficial "
            "local grading")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"Anthropic judge request failed: {exc}") from exc
    data = response.json()
    blocks = data.get("content") or []
    text = "".join(
        str(block.get("text", "")) for block in blocks
        if isinstance(block, dict) and block.get("type") == "text")
    if not text:
        raise RuntimeError("Anthropic judge returned no text content")
    return Completion(text=text, metadata={
        "backend": "anthropic-api",
        "requested_model": model,
        "resolved_model": data.get("model"),
        "request_id": data.get("id"),
        "stop_reason": data.get("stop_reason"),
        "usage": data.get("usage") or {},
    })


def _openai_complete(prompt: str, system: str | None, *, model: str,
                     temperature: float, max_tokens: int,
                     timeout_seconds: int,
                     json_output: bool = False) -> Completion:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise JudgeUnavailable(
            "OPENAI_API_KEY is not set; export a key for the openai-api "
            "judge backend")
    input_messages = []
    if system:
        input_messages.append({"role": "system", "content": system})
    input_messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "input": input_messages,
        "temperature": temperature,
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if json_output:
        payload["text"] = {"format": {"type": "json_object"}}
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"OpenAI judge request failed: {exc}") from exc
    data = response.json()
    output = data.get("output") or []
    text = "".join(
        str(part.get("text", ""))
        for item in output
        if isinstance(item, dict) and item.get("type") == "message"
        for part in (item.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    )
    if not text:
        refusal = next((
            str(part.get("refusal"))
            for item in output if isinstance(item, dict)
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("type") == "refusal"
        ), None)
        detail = f": {refusal}" if refusal else ""
        raise RuntimeError(f"OpenAI judge returned no text content{detail}")
    headers = getattr(response, "headers", {})
    return Completion(text=text, metadata={
        "backend": "openai-api",
        "requested_model": model,
        "resolved_model": data.get("model"),
        "request_id": headers.get("x-request-id"),
        "response_id": data.get("id"),
        "status": data.get("status"),
        "incomplete_details": data.get("incomplete_details"),
        "usage": data.get("usage") or {},
    })


def _gemini_complete(prompt: str, system: str | None, *, model: str,
                     temperature: float, max_tokens: int,
                     timeout_seconds: int,
                     json_output: bool = False) -> Completion:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise JudgeUnavailable(
            "GEMINI_API_KEY is not set; export a key for the gemini-api "
            "judge backend")
    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_output:
        generation_config["responseMimeType"] = "application/json"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent"
    )
    try:
        response = httpx.post(
            url,
            headers={
                "x-goog-api-key": api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"Gemini judge request failed: {exc}") from exc
    data = response.json()
    candidates = data.get("candidates") or []
    first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    parts = (first.get("content") or {}).get("parts") or []
    text = "".join(
        str(part.get("text", "")) for part in parts
        if isinstance(part, dict) and "text" in part)
    if not text:
        reason = first.get("finishReason") or (
            data.get("promptFeedback") or {}).get("blockReason")
        detail = f" (reason={reason})" if reason else ""
        raise RuntimeError(f"Gemini judge returned no text content{detail}")
    provider_usage = data.get("usageMetadata") or {}
    usage = {
        "input_tokens": int(provider_usage.get("promptTokenCount") or 0),
        "output_tokens": int(provider_usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(provider_usage.get("totalTokenCount") or 0),
    }
    headers = getattr(response, "headers", {})
    return Completion(text=text, metadata={
        "backend": "gemini-api",
        "requested_model": model,
        "resolved_model": data.get("modelVersion") or model,
        "request_id": headers.get("x-request-id"),
        "response_id": data.get("responseId"),
        "finish_reason": first.get("finishReason"),
        "usage": usage,
        "provider_usage": provider_usage,
    })


def _claude_cli_complete(prompt: str, system: str | None, *, model: str,
                         timeout_seconds: int,
                         json_output: bool = False) -> Completion:
    if not shutil.which("claude"):
        raise JudgeUnavailable(
            "claude CLI is not installed; install and authenticate it or use "
            "the canonical anthropic-api backend")
    cmd = [
        "claude", "-p", prompt, "--output-format", "json", "--model", model,
        "--tools", "", "--disable-slash-commands", "--no-session-persistence",
    ]
    if system:
        cmd += ["--system-prompt", system]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds,
            check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JudgeUnavailable(f"claude CLI judge failed: {exc}") from exc
    data = json.loads(proc.stdout)
    if "result" not in data:
        raise RuntimeError(
            f"claude CLI returned no result (subtype={data.get('subtype')!r})")
    model_usage = data.get("modelUsage") or {}
    resolved = next(iter(model_usage), None) if len(model_usage) == 1 else None
    return Completion(text=data["result"], metadata={
        "backend": "claude-cli",
        "requested_model": model,
        "resolved_model": resolved,
        "session_id": data.get("session_id"),
        "usage": data.get("usage") or {},
        "model_usage": model_usage,
        "total_cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms"),
    })


def _codex_cli_complete(prompt: str, system: str | None, *, model: str,
                        timeout_seconds: int,
                        json_output: bool = False) -> Completion:
    """Run an isolated, ephemeral Codex session for unofficial local judging.

    Codex exec has no system-prompt flag, so the grading contract and data are
    combined into one prompt. This backend must therefore remain non-canonical.
    """
    if not shutil.which("codex"):
        raise JudgeUnavailable(
            "codex CLI is not installed; install and authenticate it or use "
            "an API judge backend")
    combined = prompt
    if system:
        combined = (
            "SYSTEM INSTRUCTIONS (grading contract):\n"
            f"{system}\n\nUSER DATA REQUEST:\n{prompt}"
        )
    if json_output:
        combined += "\n\nReturn only one valid JSON object."
    try:
        with tempfile.TemporaryDirectory(prefix="showAndTell-codex-judge-") as tmp:
            output_path = os.path.join(tmp, "last-message.txt")
            cmd = [
                "codex", "exec", "-", "--model", model, "--ephemeral",
                "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--sandbox", "read-only",
                "--color", "never", "--cd", tmp,
                "--output-last-message", output_path,
            ]
            subprocess.run(
                cmd, input=combined, capture_output=True, text=True,
                timeout=timeout_seconds, check=True)
            try:
                text = Path(output_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    "codex CLI returned no final-message artifact") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise JudgeUnavailable(f"codex CLI judge failed: {exc}") from exc
    if not text.strip():
        raise RuntimeError("codex CLI judge returned an empty final message")
    return Completion(text=text, metadata={
        "backend": "codex-cli",
        "requested_model": model,
        "resolved_model": None,
        "session_ephemeral": True,
        "usage": {},
        "unsupported_settings": ["temperature", "max_tokens"],
    })


def complete_detailed(prompt: str, system: str | None = None, *,
                      backend: str = "claude-cli", model: str | None = None,
                      temperature: float = 0.0, max_tokens: int = 300,
                      timeout_seconds: int = 300,
                      json_output: bool = False) -> Completion:
    if mode() == "stub":
        raise RuntimeError("llm completion called in stub mode; caller must branch")
    selected_model = model or os.environ.get("SHOWANDTELL_JUDGE_MODEL")
    if not selected_model:
        raise JudgeUnavailable("no judge model configured")
    if backend == "anthropic-api":
        return _anthropic_complete(
            prompt, system, model=selected_model, temperature=temperature,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            json_output=json_output)
    if backend == "openai-api":
        return _openai_complete(
            prompt, system, model=selected_model, temperature=temperature,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            json_output=json_output)
    if backend == "gemini-api":
        return _gemini_complete(
            prompt, system, model=selected_model, temperature=temperature,
            max_tokens=max_tokens, timeout_seconds=timeout_seconds,
            json_output=json_output)
    if backend == "claude-cli":
        return _claude_cli_complete(
            prompt, system, model=selected_model,
            timeout_seconds=timeout_seconds, json_output=json_output)
    if backend == "codex-cli":
        return _codex_cli_complete(
            prompt, system, model=selected_model,
            timeout_seconds=timeout_seconds, json_output=json_output)
    raise JudgeUnavailable(f"unsupported judge backend: {backend}")


def complete(prompt: str, system: str | None = None) -> str:
    """Backward-compatible unofficial Claude CLI completion helper."""
    model = os.environ.get("SHOWANDTELL_JUDGE_MODEL") or "claude-sonnet-4-6"
    return complete_detailed(
        prompt, system, backend="claude-cli", model=model).text


def complete_json(prompt: str, system: str | None = None) -> dict:
    text = complete(prompt, system).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def complete_json_detailed(prompt: str, system: str | None = None, **kwargs) \
        -> tuple[dict, dict]:
    completion = complete_detailed(
        prompt, system, json_output=True, **kwargs)
    text = completion.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text), {
        **completion.metadata,
        "raw_judge_output": completion.text,
    }


def preflight(*, backend: str, model: str, live: bool = False,
              temperature: float = 0.0, max_tokens: int = 64,
              timeout_seconds: int = 30) -> dict:
    """Validate judge installation/auth and optionally make a model smoke call."""
    if mode() == "stub":
        return {"ready": True, "mode": "stub", "backend": "stub", "model": None}
    if backend == "anthropic-api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise JudgeUnavailable("ANTHROPIC_API_KEY is not set")
    elif backend == "openai-api":
        if not os.environ.get("OPENAI_API_KEY"):
            raise JudgeUnavailable("OPENAI_API_KEY is not set")
    elif backend == "gemini-api":
        if not os.environ.get("GEMINI_API_KEY"):
            raise JudgeUnavailable("GEMINI_API_KEY is not set")
    elif backend == "claude-cli":
        if not shutil.which("claude"):
            raise JudgeUnavailable("claude CLI is not installed")
        try:
            proc = subprocess.run(
                ["claude", "auth", "status", "--json"], capture_output=True,
                text=True, timeout=10, check=True)
            status = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            raise JudgeUnavailable(
                f"claude CLI authentication check failed: {exc}") from exc
        if not status.get("loggedIn"):
            raise JudgeUnavailable("claude CLI is not authenticated")
    elif backend == "codex-cli":
        if not shutil.which("codex"):
            raise JudgeUnavailable("codex CLI is not installed")
        try:
            subprocess.run(
                ["codex", "login", "status"], capture_output=True,
                text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise JudgeUnavailable(
                f"codex CLI authentication check failed: {exc}") from exc
    else:
        raise JudgeUnavailable(f"unsupported judge backend: {backend}")
    result = {"ready": True, "mode": "live", "backend": backend, "model": model}
    if live:
        completion = complete_detailed(
            'Return exactly {"ready":true}.',
            "Return only the requested JSON object.", backend=backend,
            model=model, temperature=temperature, max_tokens=max_tokens,
            timeout_seconds=timeout_seconds, json_output=True)
        parsed = json.loads(completion.text.strip())
        if parsed != {"ready": True}:
            raise JudgeUnavailable("judge smoke test returned an unexpected response")
        result["resolved_model"] = completion.metadata.get("resolved_model")
    return result
