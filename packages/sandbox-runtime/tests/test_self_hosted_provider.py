import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sandbox_runtime.self_hosted_provider import (
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_OUTPUT_LIMIT,
    build_self_hosted_provider,
)
from tests.runtime_helpers import make_opencode_server

SELF_HOSTED_SESSION = {
    "SESSION_CONFIG": json.dumps({"provider": "self-hosted", "model": "default"}),
}
SPARK = {
    "SELF_HOSTED_BASE_URL": "http://10.0.0.50:8000/v1",
    "SELF_HOSTED_MODEL": "qwen3.6-35b-a3b",
}


class TestBuildSelfHostedProvider:
    def test_builds_openai_compatible_provider(self):
        result = build_self_hosted_provider(
            {**SPARK, "SELF_HOSTED_CONTEXT_LIMIT": "262144", "SELF_HOSTED_API_KEY": "secret"}
        )

        assert result.warnings == []
        assert result.model == "qwen3.6-35b-a3b"
        assert result.entry == {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Self-hosted",
            "options": {
                "baseURL": "http://10.0.0.50:8000/v1",
                "apiKey": "{env:SELF_HOSTED_API_KEY}",
            },
            "models": {
                "qwen3.6-35b-a3b": {
                    "name": "qwen3.6-35b-a3b",
                    "tool_call": True,
                    "limit": {"context": 262_144, "output": DEFAULT_OUTPUT_LIMIT},
                }
            },
        }

    def test_never_embeds_the_api_key_value(self):
        result = build_self_hosted_provider({**SPARK, "SELF_HOSTED_API_KEY": "sk-do-not-leak"})

        assert "sk-do-not-leak" not in json.dumps(result.entry)

    def test_omits_api_key_when_unset(self):
        result = build_self_hosted_provider(SPARK)

        assert result.entry is not None
        assert "apiKey" not in result.entry["options"]

    def test_trims_whitespace_and_trailing_slash(self):
        result = build_self_hosted_provider(
            {"SELF_HOSTED_BASE_URL": " http://host:8000/v1/ ", "SELF_HOSTED_MODEL": " m \n"}
        )

        assert result.model == "m"
        assert result.entry is not None
        assert result.entry["options"]["baseURL"] == "http://host:8000/v1"

    @pytest.mark.parametrize(
        ("env", "missing"),
        [
            ({}, "SELF_HOSTED_BASE_URL and SELF_HOSTED_MODEL are"),
            ({"SELF_HOSTED_MODEL": "m"}, "SELF_HOSTED_BASE_URL is"),
            (
                {"SELF_HOSTED_BASE_URL": "http://h/v1", "SELF_HOSTED_MODEL": "  "},
                "SELF_HOSTED_MODEL is",
            ),
        ],
    )
    def test_missing_required_variables(self, env, missing):
        result = build_self_hosted_provider(env)

        assert result.entry is None
        assert result.model is None
        assert len(result.warnings) == 1
        assert missing in result.warnings[0]

    @pytest.mark.parametrize("raw", ["0", "-5", "lots", "1.5"])
    def test_invalid_limits_fall_back_with_a_warning(self, raw):
        result = build_self_hosted_provider({**SPARK, "SELF_HOSTED_CONTEXT_LIMIT": raw})

        assert result.entry is not None
        limit = result.entry["models"]["qwen3.6-35b-a3b"]["limit"]
        assert limit == {"context": DEFAULT_CONTEXT_LIMIT, "output": DEFAULT_OUTPUT_LIMIT}
        assert any("SELF_HOSTED_CONTEXT_LIMIT" in warning for warning in result.warnings)

    def test_warns_on_url_without_scheme(self):
        result = build_self_hosted_provider({**SPARK, "SELF_HOSTED_BASE_URL": "10.0.0.50:8000/v1"})

        assert result.entry is not None
        assert any("http://" in warning for warning in result.warnings)


async def _start(tmp_path, monkeypatch, env, session=SELF_HOSTED_SESSION):
    for name in [*SPARK, "SELF_HOSTED_API_KEY", "SELF_HOSTED_CONTEXT_LIMIT"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    warnings = []
    server = make_opencode_server(session, workspace_path=tmp_path)
    server.record_boot_warning = lambda **kwargs: warnings.append(kwargs)
    with (
        patch.object(server, "_setup_managed_oauth"),
        patch.object(server, "_prepare_opencode_filesystem", return_value=set()),
        patch.object(server, "_wait_for_health", new_callable=AsyncMock),
        patch(
            "sandbox_runtime.opencode_server.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=MagicMock(stdout=None),
        ) as spawn,
        patch(
            "sandbox_runtime.opencode_server.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ),
    ):
        await server.start((), tmp_path)
    return json.loads(spawn.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"]), warnings


async def test_opencode_config_targets_the_served_model(tmp_path, monkeypatch):
    config, warnings = await _start(tmp_path, monkeypatch, SPARK)

    assert warnings == []
    assert config["model"] == "self-hosted/qwen3.6-35b-a3b"
    assert config["provider"]["self-hosted"]["options"]["baseURL"] == "http://10.0.0.50:8000/v1"
    # The built-in provider entries are untouched.
    assert "anthropic" in config["provider"]


async def test_missing_secrets_keep_the_placeholder_and_warn(tmp_path, monkeypatch):
    config, warnings = await _start(tmp_path, monkeypatch, {})

    assert config["model"] == "self-hosted/default"
    assert "self-hosted" not in config["provider"]
    assert [w["scope"] for w in warnings] == ["model"]
    assert "SELF_HOSTED_BASE_URL and SELF_HOSTED_MODEL" in warnings[0]["message"]


async def test_other_providers_ignore_self_hosted_secrets(tmp_path, monkeypatch):
    config, warnings = await _start(tmp_path, monkeypatch, SPARK, session={})

    assert warnings == []
    assert config["model"] == "anthropic/claude-sonnet-4-6"
    assert "self-hosted" not in config["provider"]
