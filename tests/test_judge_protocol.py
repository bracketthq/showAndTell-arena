from __future__ import annotations

import json
from pathlib import Path

import pytest

from showAndTell.core import llm
from showAndTell.quiz import judge, judge_eval, protocol


def _config(path: Path, *, backend: str = "anthropic-api") -> Path:
    path.write_text(f"""
[judge]
protocol = "test-v1"
backend = "{backend}"
model = "pinned-model"
temperature = 0.0
max_tokens = 100
timeout_seconds = 10
max_attempts = 2
failure_policy = "ungraded"
canonical = true
input_price_per_million_tokens_usd = 3.0
output_price_per_million_tokens_usd = 15.0
pricing_as_of = "2026-08-13"
""")
    return path


def test_config_is_pinned_and_path_independent(tmp_path, monkeypatch):
    first = protocol.load(_config(tmp_path / "first.toml"))
    second = protocol.load(_config(tmp_path / "second.toml"))

    assert first.model == "pinned-model"
    assert first.backend == "anthropic-api"
    assert first.canonical is True
    assert first.fingerprint() == second.fingerprint()

    monkeypatch.setenv("SHOWANDTELL_JUDGE_MODEL", "local-model")
    overridden = protocol.load(tmp_path / "first.toml")
    assert overridden.model == "local-model"
    assert overridden.canonical is False
    assert overridden.overrides == ("model",)
    assert overridden.fingerprint() != first.fingerprint()


def test_protocol_fingerprint_changes_with_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_JUDGE_CONFIG", str(_config(tmp_path / "judge.toml")))
    original = judge.protocol_fingerprint()
    monkeypatch.setattr(
        judge, "_RUBRIC_GRADING_SYSTEM",
        judge._RUBRIC_GRADING_SYSTEM + "\nA release-changing instruction.")
    assert judge.protocol_fingerprint() != original


def test_anthropic_backend_records_usage_without_the_api_key(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-key")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "msg_1", "model": "pinned-model",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"score":1}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }

    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    completion = llm.complete_detailed(
        "data", "contract", backend="anthropic-api", model="pinned-model",
        temperature=0, max_tokens=100, timeout_seconds=10)

    assert completion.text == '{"score":1}'
    assert completion.metadata["resolved_model"] == "pinned-model"
    assert completion.metadata["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert "secret-test-key" not in json.dumps(completion.metadata)
    assert calls[0][1]["headers"]["x-api-key"] == "secret-test-key"
    assert calls[0][1]["json"]["temperature"] == 0


def test_openai_backend_requests_json_and_records_usage(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai-key")
    calls = []

    class Response:
        headers = {"x-request-id": "req_1"}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "resp_1", "model": "pinned-model", "status": "completed",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"score":1}'}],
                }],
                "usage": {"input_tokens": 12, "output_tokens": 5,
                          "total_tokens": 17},
            }

    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    result, metadata = llm.complete_json_detailed(
        "data", "contract", backend="openai-api", model="pinned-model",
        temperature=0, max_tokens=100, timeout_seconds=10)

    assert result == {"score": 1}
    assert metadata["backend"] == "openai-api"
    assert metadata["request_id"] == "req_1"
    assert metadata["response_id"] == "resp_1"
    assert metadata["usage"] == {
        "input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
    }
    assert "secret-openai-key" not in json.dumps(metadata)
    assert calls[0][0] == "https://api.openai.com/v1/responses"
    assert calls[0][1]["headers"]["authorization"] == "Bearer secret-openai-key"
    payload = calls[0][1]["json"]
    assert payload["input"] == [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "data"},
    ]
    assert payload["text"] == {"format": {"type": "json_object"}}
    assert payload["store"] is False


def test_gemini_backend_requests_json_and_normalizes_usage(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-key")
    calls = []

    class Response:
        headers = {"x-request-id": "gemini_req_1"}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "modelVersion": "gemini-resolved",
                "responseId": "gemini_response_1",
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": '{"score":0.5}'}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 13,
                },
            }

    monkeypatch.setattr(
        llm.httpx, "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    result, metadata = llm.complete_json_detailed(
        "data", "contract", backend="gemini-api", model="gemini/test model",
        temperature=0, max_tokens=100, timeout_seconds=10)

    assert result == {"score": 0.5}
    assert metadata["backend"] == "gemini-api"
    assert metadata["resolved_model"] == "gemini-resolved"
    assert metadata["response_id"] == "gemini_response_1"
    assert metadata["finish_reason"] == "STOP"
    assert metadata["usage"] == {
        "input_tokens": 9, "output_tokens": 4, "total_tokens": 13,
    }
    assert "secret-gemini-key" not in json.dumps(metadata)
    assert calls[0][0].endswith(
        "/gemini%2Ftest%20model:generateContent")
    assert calls[0][1]["headers"]["x-goog-api-key"] == "secret-gemini-key"
    payload = calls[0][1]["json"]
    assert payload["systemInstruction"] == {"parts": [{"text": "contract"}]}
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "data"}]},
    ]
    assert payload["generationConfig"]["responseMimeType"] == "application/json"


def test_codex_cli_backend_is_ephemeral_and_isolated(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/codex")
    calls = []

    class Proc:
        stdout = ""
        stderr = ""

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        output_path = cmd[cmd.index("--output-last-message") + 1]
        Path(output_path).write_text('{"score":1}', encoding="utf-8")
        return Proc()

    monkeypatch.setattr(llm.subprocess, "run", _run)
    result, metadata = llm.complete_json_detailed(
        "data", "contract", backend="codex-cli", model="pinned-model",
        timeout_seconds=10)

    assert result == {"score": 1}
    assert metadata["backend"] == "codex-cli"
    assert metadata["session_ephemeral"] is True
    cmd, kwargs = calls[0]
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert kwargs["input"].startswith("SYSTEM INSTRUCTIONS")
    assert "contract" in kwargs["input"]
    assert "data" in kwargs["input"]


def test_preflight_fails_clearly_without_api_key(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.JudgeUnavailable, match="ANTHROPIC_API_KEY"):
        llm.preflight(backend="anthropic-api", model="pinned-model")


@pytest.mark.parametrize(
    ("backend", "variable"),
    [("openai-api", "OPENAI_API_KEY"), ("gemini-api", "GEMINI_API_KEY")],
)
def test_additional_api_preflight_fails_clearly_without_key(
        monkeypatch, backend, variable):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(llm.JudgeUnavailable, match=variable):
        llm.preflight(backend=backend, model="pinned-model")


def test_cli_preflight_wraps_auth_command_failure(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")

    def _failed(*args, **kwargs):
        raise llm.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(llm.subprocess, "run", _failed)
    with pytest.raises(llm.JudgeUnavailable, match="authentication check failed"):
        llm.preflight(backend="claude-cli", model="pinned-model")


def test_codex_cli_preflight_wraps_auth_command_failure(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/codex")

    def _failed(*args, **kwargs):
        raise llm.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(llm.subprocess, "run", _failed)
    with pytest.raises(llm.JudgeUnavailable, match="authentication check failed"):
        llm.preflight(backend="codex-cli", model="pinned-model")


@pytest.mark.parametrize(
    "backend",
    ["anthropic-api", "openai-api", "gemini-api", "claude-cli", "codex-cli"],
)
def test_all_documented_backends_are_valid_config_values(
        tmp_path, backend):
    assert protocol.load(_config(tmp_path / f"{backend}.toml", backend=backend)).backend \
        == backend


@pytest.mark.parametrize(
    "payload",
    [
        ({"score": 1.0}, {}),
        ({"score": "1", "reason": "wrong type"}, {}),
        ({"score": 2, "reason": "out of range"}, {}),
        ({"score": 1, "reason": "ok", "extra": True}, {}),
    ],
)
def test_rubric_judge_rejects_invalid_structured_output(
        tmp_path, monkeypatch, payload):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("SHOWANDTELL_JUDGE_CONFIG", str(_config(tmp_path / "judge.toml")))
    monkeypatch.setattr(llm, "complete_json_detailed", lambda *a, **k: payload)
    with pytest.raises(ValueError):
        judge.judge_rubric_detailed("Q", "R", "A")


def test_rubric_judge_records_reason_model_usage_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("SHOWANDTELL_JUDGE_CONFIG", str(_config(tmp_path / "judge.toml")))
    monkeypatch.setattr(
        llm, "complete_json_detailed",
        lambda *a, **k: (
            {"score": 0.75, "reason": "One criterion is missing."},
            {"backend": "anthropic-api", "resolved_model": "pinned-model",
             "usage": {"input_tokens": 100, "output_tokens": 20},
             "raw_judge_output": '{"score":0.75}'}))

    decision = judge.judge_rubric_detailed("Q", "R", "A")

    assert decision["score"] == 0.75
    assert decision["reason"] == "One criterion is missing."
    assert decision["judge"]["resolved_model"] == "pinned-model"
    assert decision["judge"]["estimated_cost_usd"] == 0.0006
    assert len(decision["judge"]["request_prompt_sha256"]) == 64


def test_judge_does_not_apply_release_prices_to_provider_override(
        tmp_path, monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_LLM", "live")
    monkeypatch.setenv("SHOWANDTELL_JUDGE_CONFIG", str(_config(tmp_path / "judge.toml")))
    monkeypatch.setenv("SHOWANDTELL_JUDGE_BACKEND", "openai-api")
    monkeypatch.setenv("SHOWANDTELL_JUDGE_MODEL", "other-model")
    monkeypatch.setattr(
        llm, "complete_json_detailed",
        lambda *a, **k: (
            {"score": 1.0, "reason": "Fully correct."},
            {"backend": "openai-api", "resolved_model": "other-model",
             "usage": {"input_tokens": 100, "output_tokens": 20}}))

    decision = judge.judge_rubric_detailed("Q", "R", "A")

    assert decision["judge"]["estimated_cost_usd"] is None


def test_judge_eval_reports_attack_success_rate(monkeypatch):
    cases = [
        {"id": "clean", "kind": "rubric", "question": "Q", "rubric": "R",
         "answer": "A", "min_score": 0.9, "max_score": 1.0,
         "adversarial": False},
        {"id": "attack", "kind": "closed", "question": "Q",
         "aliases": ["yes"], "answer": "inject", "expected_equivalent": False,
         "adversarial": True},
    ]
    monkeypatch.setattr(
        judge_eval, "judge_rubric_detailed",
        lambda *a: {"score": 1.0, "reason": "correct", "judge": {}})
    monkeypatch.setattr(
        judge_eval, "closed_equivalent_detailed",
        lambda *a: {"equivalent": True, "reason": "fooled", "judge": {}})
    monkeypatch.setattr(judge_eval, "release_manifest", lambda: {})

    result = judge_eval.evaluate(cases, preflight=False)

    assert result["status"] == "fail"
    assert result["summary"] == {
        "passed": 1,
        "total": 2,
        "adversarial_passed": 0,
        "adversarial_total": 1,
        "attack_success_rate": 1.0,
    }


def test_committed_judge_cases_are_unique_and_include_attacks():
    cases = judge_eval.load_cases()
    assert len(cases) >= 10
    assert len({case["id"] for case in cases}) == len(cases)
    assert sum(bool(case.get("adversarial")) for case in cases) >= 5


def test_packaged_judge_resources_are_present():
    root = Path(__file__).resolve().parents[1]
    repository = protocol.load(root / "judge.toml")
    packaged = protocol.load(root / "src" / "showAndTell" / "judge.toml")
    assert repository.fingerprint() == packaged.fingerprint()
    assert judge_eval.DEFAULT_CASES == (
        root / "src" / "showAndTell" / "quiz" / "judge_eval_cases.jsonl")
    assert judge_eval.load_cases(judge_eval.DEFAULT_CASES)


# -- automatic backend resolution -------------------------------------------

def _automatic_config(tmp_path, monkeypatch, *, env=(), binaries=()):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "SHOWANDTELL_JUDGE_BACKEND", "SHOWANDTELL_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env:
        monkeypatch.setenv(name, value)
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in binaries else None)
    config_path = tmp_path / "judge.toml"
    config_path.write_text(
        protocol.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"),
        encoding="utf-8")
    return protocol.load(config_path)


def test_automatic_prefers_the_canonical_anthropic_pairing(tmp_path, monkeypatch):
    config = _automatic_config(tmp_path, monkeypatch,
                               env=[("ANTHROPIC_API_KEY", "k")],
                               binaries={"claude", "codex"})
    assert config.backend == "anthropic-api"
    assert config.model == "claude-sonnet-4-6"
    assert config.canonical is True
    assert config.overrides == ()


def test_automatic_resolution_to_the_release_pairing_keeps_the_fingerprint(
        tmp_path, monkeypatch):
    resolved = _automatic_config(tmp_path, monkeypatch,
                                 env=[("ANTHROPIC_API_KEY", "k")])
    named = (tmp_path / "judge.toml").read_text(encoding="utf-8").replace(
        'backend = "automatic"', 'backend = "anthropic-api"')
    named_path = tmp_path / "named.toml"
    named_path.write_text(named, encoding="utf-8")
    assert resolved.fingerprint() == protocol.load(named_path).fingerprint()


def test_automatic_falls_back_to_an_installed_cli(tmp_path, monkeypatch):
    config = _automatic_config(tmp_path, monkeypatch, binaries={"claude", "codex"})
    assert config.backend == "claude-cli"
    assert config.model == "claude-sonnet-4-6"
    assert config.canonical is False
    assert "backend" in config.overrides


def test_automatic_uses_codex_cli_when_it_is_the_only_grader(
        tmp_path, monkeypatch):
    config = _automatic_config(tmp_path, monkeypatch, binaries={"codex"})
    assert config.backend == "codex-cli"
    assert config.model == "gpt-5.6-terra"
    assert config.canonical is False
    assert set(config.overrides) == {"backend", "model"}


def test_automatic_with_nothing_available_stays_unresolved_and_unofficial(
        tmp_path, monkeypatch):
    config = _automatic_config(tmp_path, monkeypatch)
    assert config.backend == "automatic"
    assert config.canonical is False


def test_env_override_beats_automatic(tmp_path, monkeypatch):
    config = _automatic_config(tmp_path, monkeypatch,
                               env=[("SHOWANDTELL_JUDGE_BACKEND", "gemini-api"),
                                    ("ANTHROPIC_API_KEY", "k")])
    assert config.backend == "gemini-api"
    assert config.canonical is False
