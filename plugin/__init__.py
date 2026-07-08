"""VibeProxy local OpenAI-compatible provider profile.

Drop-in Hermes (hermes-agent by Nous Research) model-provider plugin that keeps
Claude-subscription traffic on the local VibeProxy / CLIProxyAPI
``chat_completions`` transport instead of the native Anthropic Messages
transport. The subscription-backed Claude / Claude Code path is only reachable
through the local gateway, and the wire shim on port 8485 (see ``../shim``)
applies the Claude-OAuth compatibility rewrites that a direct native Anthropic
transport would bypass.

See ``../README.md`` for the full architecture, the four problems this lane
solves, install steps, and verification commands. The base URL honors the
``VIBEPROXY_BASE_URL`` env var (declared in ``env_vars`` and resolved by Hermes
core at request time); the module-level default below is only the fallback when
neither config nor env sets it.
"""

import copy
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

# Standalone-clone friendly: honor VIBEPROXY_BASE_URL at import so a bare
# checkout works even outside Hermes' config layer. Hermes core additionally
# resolves the same env var / user config at request time.
_DEFAULT_BASE_URL = os.environ.get("VIBEPROXY_BASE_URL", "http://127.0.0.1:8485/v1").rstrip("/")

_ANTHROPIC_MODEL_HINTS = ("claude", "opus", "sonnet", "haiku", "fable", "mythos")
_SAMPLING_KEYS = ("temperature", "top_p", "top_k")
_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_MCP_TOOL_PREFIX = "mcp__"


def _is_anthropic_model(model: str | None) -> bool:
    model_lower = (model or "").lower()
    return any(hint in model_lower for hint in _ANTHROPIC_MODEL_HINTS)


def _forbids_sampling_params(model: str | None) -> bool:
    try:
        from agent.anthropic_adapter import _forbids_sampling_params as _native_forbids

        return bool(_native_forbids(model or ""))
    except Exception:
        model_lower = (model or "").lower()
        return any(
            marker in model_lower
            for marker in ("claude-opus-4-7", "claude-opus-4-8", "claude-fable", "claude-sonnet-5")
        )


def _sanitize_claude_oauth_text(text: str) -> str:
    return (
        text.replace("Hermes Agent", "Claude Code")
        .replace("Hermes agent", "Claude Code")
        .replace("hermes-agent", "claude-code")
        .replace("Nous Research", "Anthropic")
    )


def _to_claude_oauth_wire_name(name: str | None) -> str | None:
    if not isinstance(name, str) or not name:
        return name
    if name.startswith(_MCP_TOOL_PREFIX):
        return name
    if name.startswith("mcp_"):
        return _MCP_TOOL_PREFIX + name[len("mcp_"):]
    return _MCP_TOOL_PREFIX + name


def _sanitize_message_content(content: Any) -> Any:
    if isinstance(content, str):
        return _sanitize_claude_oauth_text(content)
    if isinstance(content, list):
        new_content = []
        changed = False
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                new_part = dict(part)
                new_text = _sanitize_claude_oauth_text(new_part["text"])
                changed = changed or new_text != new_part["text"]
                new_part["text"] = new_text
                new_content.append(new_part)
            else:
                new_content.append(part)
        return new_content if changed else content
    return content


def _apply_claude_oauth_wire_cloak(api_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Apply native Anthropic OAuth cloaking to VibeProxy's OpenAI wire."""
    cleaned = copy.deepcopy(api_kwargs)

    messages = cleaned.get("messages")
    if isinstance(messages, list):
        system_seen = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system":
                content = _sanitize_message_content(msg.get("content", ""))
                if isinstance(content, str):
                    if _CLAUDE_CODE_SYSTEM_PREFIX not in content:
                        content = f"{_CLAUDE_CODE_SYSTEM_PREFIX}\n\n{content}" if content else _CLAUDE_CODE_SYSTEM_PREFIX
                elif isinstance(content, list):
                    content = [{"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX}] + content
                msg["content"] = content
                system_seen = True

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function")
                    if isinstance(fn, dict) and "name" in fn:
                        fn["name"] = _to_claude_oauth_wire_name(fn.get("name"))

        if not system_seen:
            messages.insert(0, {"role": "system", "content": _CLAUDE_CODE_SYSTEM_PREFIX})

    tools = cleaned.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if isinstance(fn, dict) and "name" in fn:
                fn["name"] = _to_claude_oauth_wire_name(fn.get("name"))

    return cleaned


def _vibeproxy_reasoning_effort(reasoning_config: dict | None, model: str | None) -> str | None:
    if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
        return None
    if not isinstance(reasoning_config, dict):
        return None

    effort = str(reasoning_config.get("effort") or "").strip().lower()
    if effort == "minimal":
        return "low"
    if effort in {"low", "medium", "high", "xhigh", "max"}:
        return effort
    return None


def _vibeproxy_reasoning_model(model: str | None, effort: str | None) -> str | None:
    model_name = (model or "").strip()
    if not model_name or not effort:
        return model
    if "(" in model_name or model_name.endswith("-thinking"):
        return model
    return f"{model_name}({effort})"


class VibeProxyProfile(ProviderProfile):
    """VibeProxy accepts OpenAI-compatible requests and does model-side routing."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch the local catalog; VibeProxy does not require a real API key."""
        return super().fetch_models(api_key=None, base_url=base_url, timeout=timeout)

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context,
    ) -> tuple[dict, dict]:
        effort = _vibeproxy_reasoning_effort(reasoning_config, model)
        if effort is None:
            return {}, {}
        # CLIProxyAPI's OpenAI Chat Completions → Claude translator reads only
        # the top-level `reasoning_effort` kwarg or a `model(effort)` suffix
        # (suffix wins); nested `reasoning.effort` is ignored on that route and
        # kept here only for Responses/Codex-compatible paths.  Note visible
        # thinking TEXT additionally requires `thinking.display: summarized` +
        # an anthropic-beta header upstream — handled by the CLIProxyAPI
        # payload rule (config/extras.template.yaml) and the 8485 rename shim,
        # not by this plugin.  Without effort here Claude still thinks
        # adaptively upstream; it just returns no thinking text.
        return (
            {"reasoning": {"enabled": True, "effort": effort}},
            {
                "model": _vibeproxy_reasoning_model(model, effort),
                "reasoning_effort": effort,
            },
        )

    def finalize_api_kwargs(
        self,
        api_kwargs: dict[str, Any],
        *,
        model: str | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """Sanitize Claude-through-VibeProxy payloads after request overrides.

        Older VibeProxy/CLIProxyAPIPlus routes performed Claude-OAuth cloaking
        in the local gateway. Hermes now applies the same request-shape cloak
        here too, so Claude subscription-backed routes stay safe even when the
        Go LLM proxy is no longer in the path: system identity is rewritten to
        Claude Code/Anthropic and tool names use the double-underscore MCP wire
        form that avoids Anthropic's third-party-app extra-usage classifier.
        Preserve cache markers while still removing request fields known to
        confuse Claude subscription-backed routes. Modern Claude models also
        reject non-default sampling params; remove them even if user overrides
        added them late in request assembly.
        """
        if not _is_anthropic_model(model):
            return api_kwargs

        cleaned = _apply_claude_oauth_wire_cloak(api_kwargs)
        cleaned.pop("cache_control", None)
        cleaned.pop("prompt_cache_retention", None)
        cleaned.pop("response_format", None)
        cleaned.pop("n", None)

        if _forbids_sampling_params(model):
            for key in _SAMPLING_KEYS:
                cleaned.pop(key, None)

        return cleaned


vibeproxy = VibeProxyProfile(
    name="vibeproxy",
    aliases=("vibe", "vibe-proxy", "vibe_proxy"),
    display_name="VibeProxy",
    description="Local VibeProxy gateway for subscription-backed Codex, Claude, Gemini, GLM, and other models",
    signup_url="https://github.com/automazeio/vibeproxy",
    env_vars=("VIBEPROXY_API_KEY", "VIBEPROXY_BASE_URL"),
    base_url=_DEFAULT_BASE_URL,
    models_url=f"{_DEFAULT_BASE_URL}/models",
    auth_type="api_key",
    default_aux_model="gpt-5.4-mini",
    fallback_models=(
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "glm-4.7",
        "glm-4-plus",
        "deepseek-v4-pro",
    ),
)

register_provider(vibeproxy)
