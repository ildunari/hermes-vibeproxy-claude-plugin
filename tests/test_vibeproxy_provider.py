import json

from hermes_cli import runtime_provider as rp


def test_vibeproxy_provider_resolves_without_real_api_key(monkeypatch):
    from hermes_cli import auth

    monkeypatch.delenv("VIBEPROXY_API_KEY", raising=False)
    monkeypatch.delenv("VIBEPROXY_BASE_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name: "")

    assert auth.resolve_provider("vibe") == "vibeproxy"
    creds = auth.resolve_api_key_provider_credentials("vibeproxy")

    assert creds["provider"] == "vibeproxy"
    assert creds["api_key"] == auth.VIBEPROXY_NOAUTH_PLACEHOLDER
    assert creds["base_url"] == auth.DEFAULT_VIBEPROXY_BASE_URL


def test_vibeproxy_runtime_stays_openai_chat_for_anthropic_and_codex_models(monkeypatch):
    class _EmptyPool:
        def has_credentials(self):
            return False

    monkeypatch.delenv("VIBEPROXY_API_KEY", raising=False)
    monkeypatch.delenv("VIBEPROXY_BASE_URL", raising=False)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name: "")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())
    monkeypatch.setattr(
        rp,
        "_get_model_config",
        lambda: {
            "provider": "vibeproxy",
            "default": "claude-sonnet-4-6",
            "api_mode": "codex_responses",
        },
    )

    resolved = rp.resolve_runtime_provider(
        requested="vibeproxy",
        target_model="claude-sonnet-4-6",
    )

    assert resolved["provider"] == "vibeproxy"
    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == "http://127.0.0.1:8485/v1"
    assert resolved["api_key"]


def test_vibeproxy_fable_models_honor_medium_reasoning():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    extra_body, top_level = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "medium"},
        model="claude-fable-5",
    )

    assert top_level["model"] == "claude-fable-5(medium)"
    assert extra_body == {"reasoning": {"enabled": True, "effort": "medium"}}
    assert top_level["reasoning_effort"] == "medium"


def test_vibeproxy_non_fable_claude_models_preserve_medium_reasoning():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    extra_body, top_level = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "medium"},
        model="claude-sonnet-4-6",
    )

    assert top_level["model"] == "claude-sonnet-4-6(medium)"
    assert extra_body == {"reasoning": {"enabled": True, "effort": "medium"}}
    assert top_level["reasoning_effort"] == "medium"


def test_vibeproxy_claude_all_supported_reasoning_modes_are_preserved():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        extra_body, top_level = profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="claude-opus-4-8",
        )
        expected = "low" if effort == "minimal" else effort

        assert top_level["model"] == f"claude-opus-4-8({expected})"
        assert extra_body == {"reasoning": {"enabled": True, "effort": expected}}
        assert top_level["reasoning_effort"] == expected


def test_vibeproxy_claude_xhigh_reasoning_is_preserved():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    extra_body, top_level = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "xhigh"},
        model="claude-opus-4-8",
    )

    assert top_level["model"] == "claude-opus-4-8(xhigh)"
    assert extra_body == {"reasoning": {"enabled": True, "effort": "xhigh"}}
    assert top_level["reasoning_effort"] == "xhigh"


def test_vibeproxy_model_catalog_uses_openai_compatible_models_endpoint(monkeypatch):
    from hermes_cli.models import provider_model_ids

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "data": [
                    {"id": "claude-sonnet-4-6"},
                    {"id": "gpt-5.3-codex"},
                    {"id": "glm-5.1"},
                ]
            }).encode()

    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name: "")
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        lambda provider: {
            "provider": provider,
            "api_key": "dummy-vibeproxy-api-key",
            "base_url": "http://127.0.0.1:8485/v1",
            "source": "default",
        },
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=8.0: _Response())

    models = provider_model_ids("vibeproxy", force_refresh=True)
    assert "claude-sonnet-4-6" in models
    assert "claude-opus-4-8" in models
    assert "gpt-5.3-codex" in models
    assert "glm-5.1" in models


def test_vibeproxy_claude_finalizer_strips_unsafe_openai_fields_but_preserves_cache_markers():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    messages = [
        {
            "role": "system",
            "content": "You are Hermes Agent by Nous Research. Use hermes-agent tools.",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "mcp_linear_get_issue", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "function",
            "function": {"name": "mcp_linear_get_issue", "parameters": {"type": "object"}},
        },
    ]
    api_kwargs = {
        "model": "claude-opus-4-8",
        "messages": messages,
        "tools": tools,
        "temperature": 0,
        "top_p": 0.9,
        "top_k": 1,
        "cache_control": {"type": "ephemeral"},
        "prompt_cache_retention": "24h",
        "response_format": {"type": "json_object"},
        "n": 2,
    }

    cleaned = profile.finalize_api_kwargs(api_kwargs, model="claude-opus-4-8")

    for key in (
        "temperature",
        "top_p",
        "top_k",
        "cache_control",
        "prompt_cache_retention",
        "response_format",
        "n",
    ):
        assert key not in cleaned
    assert cleaned["messages"][0]["content"].startswith(
        "You are Claude Code, Anthropic's official CLI for Claude."
    )
    assert "Hermes Agent" not in cleaned["messages"][0]["content"]
    assert "Nous Research" not in cleaned["messages"][0]["content"]
    assert "Claude Code" in cleaned["messages"][0]["content"]
    assert "Anthropic" in cleaned["messages"][0]["content"]
    assert cleaned["messages"][1]["tool_calls"][0]["function"]["name"] == "mcp__linear_get_issue"
    assert cleaned["tools"][0]["function"]["name"] == "mcp__lookup"
    assert cleaned["tools"][1]["function"]["name"] == "mcp__linear_get_issue"
    assert cleaned["messages"][2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert cleaned["tools"][0]["cache_control"] == {"type": "ephemeral"}
    # Defensive-copy contract: finalizer must not mutate conversation history
    # or tool schemas owned by the agent loop.
    assert messages[0]["content"].startswith("You are Hermes Agent")
    assert messages[1]["tool_calls"][0]["function"]["name"] == "mcp_linear_get_issue"
    assert tools[0]["function"]["name"] == "lookup"
    assert "cache_control" in messages[2]["content"][0]
    assert "cache_control" in tools[0]


def test_vibeproxy_finalizer_leaves_non_claude_models_alone():
    from providers import get_provider_profile

    profile = get_provider_profile("vibeproxy")
    assert profile is not None
    api_kwargs = {"model": "gpt-5.5", "messages": [], "temperature": 0.2, "n": 2}

    assert profile.finalize_api_kwargs(api_kwargs, model="gpt-5.5") is api_kwargs
