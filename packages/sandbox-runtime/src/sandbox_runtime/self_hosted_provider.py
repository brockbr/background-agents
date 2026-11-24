"""OpenCode provider for the deployment-configured OpenAI-compatible model.

The catalog exposes one fixed model, ``self-hosted/default``
(``packages/shared/src/models.ts``). What it points at is read from
``SELF_HOSTED_*`` environment variables, which reach the sandbox as global or
repository secrets, and becomes an ``@ai-sdk/openai-compatible`` provider in
the OpenCode config. The API key is referenced as ``{env:...}`` so its value
never appears in ``OPENCODE_CONFIG_CONTENT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

SELF_HOSTED_PROVIDER_ID = "self-hosted"

BASE_URL_ENV = "SELF_HOSTED_BASE_URL"
MODEL_ENV = "SELF_HOSTED_MODEL"
API_KEY_ENV = "SELF_HOSTED_API_KEY"
CONTEXT_LIMIT_ENV = "SELF_HOSTED_CONTEXT_LIMIT"
OUTPUT_LIMIT_ENV = "SELF_HOSTED_OUTPUT_LIMIT"

DEFAULT_CONTEXT_LIMIT = 131_072
DEFAULT_OUTPUT_LIMIT = 32_768


@dataclass(frozen=True)
class SelfHostedProvider:
    """Result of reading the ``SELF_HOSTED_*`` variables.

    ``entry`` is ``None`` when a required variable is missing; ``warnings``
    lists everything a user should be told at boot either way.
    """

    model: str | None
    entry: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)


def _limit(env: Mapping[str, str], name: str, default: int, warnings: list[str]) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        warnings.append(f"{name}={raw!r} is not a positive integer; using {default}")
        return default
    return value


def served_model(env: Mapping[str, str]) -> str | None:
    """The model name the server expects; what ``self-hosted/default`` stands for."""
    return env.get(MODEL_ENV, "").strip() or None


def build_self_hosted_provider(env: Mapping[str, str]) -> SelfHostedProvider:
    """Build the OpenCode provider entry for the self-hosted model."""
    base_url = env.get(BASE_URL_ENV, "").strip().rstrip("/")
    model = served_model(env) or ""
    missing = [name for name, value in ((BASE_URL_ENV, base_url), (MODEL_ENV, model)) if not value]
    if missing:
        return SelfHostedProvider(
            model=None,
            entry=None,
            warnings=[
                "Self-hosted model is selected but "
                + " and ".join(missing)
                + (" is" if len(missing) == 1 else " are")
                + " not set. Add them as global or repository secrets."
            ],
        )

    warnings: list[str] = []
    if not base_url.startswith(("http://", "https://")):
        warnings.append(f"{BASE_URL_ENV} should start with http:// or https:// (got {base_url!r})")

    options: dict[str, Any] = {"baseURL": base_url}
    if env.get(API_KEY_ENV, "").strip():
        options["apiKey"] = "{env:" + API_KEY_ENV + "}"

    entry = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Self-hosted",
        "options": options,
        "models": {
            model: {
                "name": model,
                "tool_call": True,
                "limit": {
                    "context": _limit(env, CONTEXT_LIMIT_ENV, DEFAULT_CONTEXT_LIMIT, warnings),
                    "output": _limit(env, OUTPUT_LIMIT_ENV, DEFAULT_OUTPUT_LIMIT, warnings),
                },
            }
        },
    }
    return SelfHostedProvider(model=model, entry=entry, warnings=warnings)
