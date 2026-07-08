# VibeProxy Claude Lane

A verified, self-contained compatibility lane for **Hermes** (hermes-agent by Nous
Research) through a local VibeProxy / CLIProxyAPI OpenAI-compatible gateway. It focuses on
request-shape correctness for Claude-family routes: visible extended thinking, safe tool
calling, prompt caching, and provider-specific cleanup. It bundles a Hermes model-provider plugin, a
transparent wire shim, a CLIProxyAPI config rule, and two small macOS
LaunchAgents that keep the lane healthy across VibeProxy's nightly
self-updates. Everything here is drop-in: clone this one directory and run the
installer.

## Architecture

```
Hermes (provider=vibeproxy)
  │  OpenAI /v1/chat/completions, base_url = http://127.0.0.1:8485/v1
  ▼
rename shim  :8485   ── adds anthropic-beta header, folds system→user,
  │                     mcp_→xmcp_ + session_search alias (Anthropic models only)
  ▼
CLIProxyAPI  :8318   ── openai→claude translation; payload rule injects
  │                     thinking.display=summarized (via merged-config.yaml)
  ▼
Claude-family upstream route
```

The provider plugin does request-shape cleanup *before* the wire (identity
compatibility cleanup, tool-name normalization, sampling-param stripping, reasoning-effort
suffix). For Hermes requests it normalizes raw `mcp_` tool names to Anthropic-safe
`mcp__` names; the shim still catches any remaining raw `mcp_` names from other
clients and rewrites them to `xmcp_` on the wire. The shim does byte-level wire
rewrites the agent loop must not see. The
config rule + watcher own the upstream half of visible thinking. See
[`docs/root-cause.md`](docs/root-cause.md) for the full forensics.

## The five problems it solves

**1. Invisible extended thinking.** On the Claude OAuth path, thinking *runs*
but the text never comes back — you get a signature-only block and no
`reasoning_content`. The backend only returns thinking text when the request has
**both** `thinking.display: "summarized"` **and** an `anthropic-beta` header.
CLIProxyAPI's translator sets neither, so this lane supplies both: a
`payload.override` rule injects `display: summarized` post-translation, and the
shim injects the beta header on the wire. Either half alone yields empty
thinking. Worse, on native `/v1/messages` the backend **silently ignores the
standard API shape** `thinking: {type: enabled, budget_tokens: N}` — zero
thinking tokens, no error — so the shim also translates `enabled` →
`{type: adaptive, display: summarized}` + `output_config.effort` (bucketed
from `budget_tokens`) for native Anthropic clients.

**2. `mcp_` prefix → fake "out of extra usage" 400.** The claude.ai endpoint
rejects any tool whose name starts with the lowercase `mcp_` prefix (and certain
name co-occurrences like `session_search`+`clarify`) with a misleading quota
400. It's not a size or count limit — a 45-tool 131 KB request passes while a
382-byte 2-tool request fails. The shim rewrites `mcp_`→`xmcp_` and aliases
`session_search`→`past_session_lookup` on the wire (reversed inbound), only for
Anthropic models; the plugin normalizes `mcp_`→`mcp__` at assembly too. Your
agent keeps seeing canonical names.

**3. Dropped system prompts.** The claude.ai path silently drops client `system`
messages for Claude models — an agent that ships its identity/memory/
instructions as the system block has the model never see them (`prompt_tokens`
stays flat no matter how big the system prompt is). This applies to the
**native Anthropic `/v1/messages` top-level `system` field too** (codeword
probes never come back). The shim folds system/developer messages — and, on
the native protocol, the top-level `system` blocks — into the first user
message wrapped in `<system>...</system>` so the content actually reaches
Claude, carrying the client's `cache_control` marker onto the folded block.

**4. Dropped prompt-cache markers → full re-billing every turn.** The
translator also discards ALL client `cache_control` markers (message-level and
content-part), so only CLIProxyAPI's injected ~1.9k-token prefix ever caches —
a 100k-token agent prompt re-bills completely on every turn. A second
`payload.override` rule stamps a cache breakpoint on the first content block of
the first message; combined with the shim's system-folding (problem 3), that
block holds the agent's entire stable system/memory prefix. Measured A/A:
99.8% cache read on the repeat call.

**5. VibeProxy app regenerates its config.** The VibeProxy menu-bar app rewrites
`merged-config.yaml` on every launch and nightly Sparkle update, wiping the
thinking-display rule — and even a re-inject can lose an `fsnotify` race and
leave the running server on the payload-less config. A `WatchPaths` LaunchAgent
re-injects the extras idempotently and does a post-arm nudge; a 6h canary probes
reasoning + tools, self-heals, and alerts on persistent failure.

## Requirements

- **Either** the VibeProxy menu-bar app **or** a plain CLIProxyAPI install,
  authenticated against a Claude-family local gateway route. The lane
  assumes CLIProxyAPI listens on `127.0.0.1:8318` (adjust `SHIM_UPSTREAM` /
  `CANARY_UPSTREAM_URL` otherwise).
- **hermes-agent** (Nous Research) for the provider plugin. Plain CLIProxyAPI
  users who just want visible thinking on any OpenAI-compat client can skip the
  plugin and use only the payload rule + shim.
- **macOS** for the launchd parts (shim KeepAlive, config watcher, canary).
  Non-macOS users can run `shim/shim.py` under any process supervisor and add
  the payload rule manually.
- A Python with **aiohttp** (shim) and **PyYAML** (merger/canary). A Hermes venv
  python typically has both; the installer verifies and warns rather than
  auto-installing.

## Install

### Mode A — VibeProxy app (full lane, macOS)

```bash
# from this directory
PYTHON=/path/to/python-with-aiohttp-and-pyyaml \
HERMES_HOME="$HOME/.hermes" \
  bash scripts/install.sh
```

The installer copies the shim + scripts into
`~/.local/share/vibeproxy-claude-lane/`, renders the plist templates
(substituting your `$HOME` and python), installs and bootstraps the three
LaunchAgents, and optionally symlinks the provider plugin into `$HERMES_HOME`.
Then:

```bash
# user-owned config extras (holds no secrets in the template)
cp config/extras.template.yaml ~/.cli-proxy-api/extras.yaml
chmod 600 ~/.cli-proxy-api/extras.yaml   # required if you add provider API keys

# point Hermes at the shim (env or provider config)
export VIBEPROXY_BASE_URL=http://127.0.0.1:8485/v1
```

Set your Hermes `vibeproxy` provider `base_url` / `api` to
`http://127.0.0.1:8485/v1` (see the plugin's `WIRING.md` for the full provider
config block).

### Mode B — plain CLIProxyAPI (no app, minimal)

You don't need the watcher or the canary — CLIProxyAPI won't wipe your config.

1. Paste the `payload:` block from `config/extras.template.yaml` into your
   CLIProxyAPI `config.yaml` **once** and reload.
2. Run the shim under any supervisor (it only needs `aiohttp`):
   ```bash
   SHIM_UPSTREAM=http://127.0.0.1:8318 python3 shim/shim.py
   ```
3. Point your client's base URL at `http://127.0.0.1:8485/v1`.

That's the whole lane for a non-Hermes OpenAI-compatible client: the payload
rule gives `display: summarized`, the shim adds the beta header, folds system
messages, and fixes the `mcp_` tool-name class.

### Bonus — Factory Droid BYOK (native Anthropic protocol)

The shim also handles the native Anthropic Messages protocol, which makes it a
drop-in Claude-family backend for [Factory Droid](https://factory.ai)'s
BYOK custom models. Droid speaks `/v1/messages` directly (`provider:
"anthropic"`), stamps its own `cache_control` markers (preserved end-to-end —
prompt caching just works), and its reasoning-effort selector maps through the
shim's `enabled → adaptive` translation. Add to `~/.factory/settings.json`:

```json
{
  "customModels": [
    {
      "model": "claude-sonnet-5",
      "displayName": "Claude Sonnet 5 (subscription)",
      "baseUrl": "http://127.0.0.1:8485",
      "apiKey": "dummy-not-used",
      "provider": "anthropic",
      "maxContextLimit": 200000,
      "maxOutputTokens": 32000
    }
  ]
}
```

Droid ≥ mid-2026 builds apply the full reasoning-effort range to BYOK models
(older builds send no thinking config for custom models at all — update first).
Any other OpenAI-compat models CLIProxyAPI hosts (GLM, DeepSeek, ...) can point
at `http://127.0.0.1:8318/v1` with `provider: "generic-chat-completion-api"`;
API keys stay server-side in the CLIProxyAPI config.

## Verify

**Streaming reasoning probe** — expect a non-zero reasoning-char count. If it
prints `0`, the display-rule / beta-header contract is broken (see problem 1):

```bash
python3 - <<'PY'
import json, urllib.request
body = {"model": "claude-sonnet-5", "stream": True, "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "Is 391 prime? Think it through, one line."}]}
req = urllib.request.Request("http://127.0.0.1:8485/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer probe"})
chars = 0
with urllib.request.urlopen(req, timeout=240) as r:
    for line in r:
        line = line.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        try: obj = json.loads(line[5:].strip())
        except ValueError: continue
        for ch in obj.get("choices", []):
            chars += len(ch.get("delta", {}).get("reasoning_content") or "")
print("reasoning_content chars:", chars)   # >0 == visible thinking works
PY
```

**curl one-liner** — health check the shim:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8485/v1/models   # 200
```

**Hermes one-shot** — confirm the provider routes and thinks:

```bash
hermes -p gpt -m 'claude-sonnet-5(high)' -z "Is 391 prime? Think it through, answer in one line."
```

(You should see thinking text, then the answer. `391 = 17 × 23`, so: not prime.)

The bundled `scripts/canary.py` runs all of the above on a schedule and alerts
on failure.

## Tested with

- Hermes Agent local provider-plugin API (`ProviderProfile.build_api_kwargs_extras` and `finalize_api_kwargs`).
- VibeProxy / CLIProxyAPI local OpenAI-compatible gateway on `127.0.0.1:8318`.
- macOS launchd for the optional shim, config watcher, and canary services.
- Python 3 with `aiohttp` for the shim and `PyYAML` for config merge/canary scripts.

Upstream proxy behavior can change. Re-run the verification probes after VibeProxy / CLIProxyAPI updates.

## Security

- **Never commit an extras yaml that contains real API keys.** The shipped
  `config/extras.template.yaml` is redacted; keep your populated
  `~/.cli-proxy-api/extras.yaml` out of git and `chmod 600`.
- The shim's rewrites are **wire-only** — it changes the bytes sent upstream and
  restores canonical names inbound; your agent's history, skills, and memory are
  untouched. It logs counts and hashes only, never prompt content.
- The identity cloak (Claude Code / Anthropic) exists to keep the Claude Code
  subscription client contract consistent, not to defeat any control.

## Plug other agents into your local gateway

To point any *other* agent (Droid, Cursor, Cline, Zed, a raw Anthropic SDK
script, another Claude Code instance) at the same local gateway — with
correct thinking, tool calling, caching, and reasoning-effort passthrough — see
**[`docs/agent-integration.md`](docs/agent-integration.md)**. It covers which
endpoint/protocol to use per model family, per-agent config recipes, and the
exact rule for how reasoning-effort toggles must be sent so they actually reach
the provider (the #1 silent-failure: nested `reasoning.effort` is dropped; use
top-level `reasoning_effort` or a `model(effort)` suffix).

## Credits / upstream

The visible-thinking root cause (`thinking.display: summarized` + `anthropic-beta`
contract) and a proposed CLIProxyAPI fix are written up in
[`docs/root-cause.md`](docs/root-cause.md), suitable as the basis of an upstream
issue/PR against CLIProxyAPI. VibeProxy: <https://github.com/automazeio/vibeproxy>.
Built and verified against a local Hermes + VibeProxy / CLIProxyAPI stack, then generalized so anyone with a Claude OAuth CLIProxyAPI route can reuse it.
