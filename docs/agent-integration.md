# Plugging any agent into your local CLIProxy / VibeProxy gateway

How to point **any** coding agent at your local subscription gateway and get
**full thinking, tool calling, prompt caching, and correct reasoning-effort
passthrough** — so the effort levels and toggles you pick inside an agent
actually reach the real provider.

This is the general recipe. The Hermes plugin, the Droid BYOK config, and the
Claude-Code-as-anything trick are all just instances of it.

## The mental model

```
                 ┌───────────────────────────────────────────────┐
   your agent →  │  8485 rename shim   (Claude compatibility)     │
                 │        │  adds anthropic-beta, folds system,    │
                 │        │  mcp_→xmcp_, thinking-shape translate   │
                 │        ▼                                        │
                 │  8318 CLIProxyAPI-Plus   (the real multiplexer) │
                 │        holds every sub: Claude OAuth, Codex     │
                 │        OAuth, GLM/DeepSeek/… keys. Does the      │
                 │        Claude Code fingerprint + OAuth itself.  │
                 └───────────────────────────────────────────────┘
                          │              │               │
                    claude.ai OAuth   OpenAI OAuth    z.ai / DeepSeek …
```

- **8318 (CLIProxyAPI-Plus)** is the hub. It owns the OAuth tokens and provider
  keys, translates between wire formats, and — for Claude — injects the full
  Claude Code fingerprint (identity system block, `claude-cli/…` UA, OAuth
  scopes, billing header). You do **not** re-implement any of that per agent.
- **8485 (the shim)** wraps 8318 with the residual Claude-OAuth compatibility
  fixes that CLIProxyAPI alone doesn't cover for arbitrary clients (see
  gotchas). Point Claude traffic here; point non-Claude traffic straight at 8318.

## Which endpoint / protocol to use

| Your agent speaks | For Claude models | For GPT/Codex | For GLM/DeepSeek/etc |
|---|---|---|---|
| **Native Anthropic** `/v1/messages` | `http://127.0.0.1:8485` (shim) | — | — |
| **OpenAI Chat Completions** `/v1/chat/completions` | `http://127.0.0.1:8485/v1` | `http://127.0.0.1:8318/v1` | `http://127.0.0.1:8318/v1` |
| **OpenAI Responses** | — | `http://127.0.0.1:8318/v1` | — |

Rule of thumb: **anything Claude → 8485; everything else → 8318 directly.** The
shim only helps Anthropic models; it's a transparent passthrough for the rest,
so routing non-Claude through it is harmless but pointless.

## Per-agent recipes

**Factory Droid** — `~/.factory/settings.json` `customModels[]`:
```json
{ "model": "claude-sonnet-5", "baseUrl": "http://127.0.0.1:8485",
  "apiKey": "dummy-not-used", "provider": "anthropic",
  "maxContextLimit": 200000, "maxOutputTokens": 32000 }
{ "model": "glm-5.2", "baseUrl": "http://127.0.0.1:8318/v1",
  "apiKey": "dummy-not-used", "provider": "generic-chat-completion-api" }
{ "model": "gpt-5.5", "baseUrl": "http://127.0.0.1:8318/v1",
  "apiKey": "dummy-not-used", "provider": "openai" }
```
Reasoning effort: Droid's `-r`/selector maps to the Anthropic thinking shape →
the shim translates it (see below). Caching: Droid stamps its own
`cache_control`; they pass through the native path unchanged.

**Claude Code, or any Anthropic SDK app** — just set the base URL:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8485
# auth: the app sends its own OAuth/token; CLIProxyAPI uses ITS stored sub,
# so a dummy or the app's own token both work — the sub is server-side.
```
Anything built on `@anthropic-ai/sdk` (Claude Code, Claude Agent SDK, custom
scripts) inherits full thinking + tools + caching with zero code changes.

**Codex CLI** — `~/.codex/config.toml` provider:
```toml
[model_providers.vibe]
base_url = "http://127.0.0.1:8318/v1"
# select models as `glm-5.2(high)`, `claude-sonnet-4-6-vibe`, etc.
```
Reasoning effort via the **`model(effort)` suffix** (Codex doesn't send
top-level `reasoning_effort` reliably).

**Cursor / Cline / Zed / any OpenAI-compat client** — set the OpenAI base URL to
`http://127.0.0.1:8485/v1` for Claude (or `8318/v1` for the rest), any dummy API
key. Pick the model by name; add `(high)` etc. for effort where the client
can't send `reasoning_effort`.

**Hermes** — the `vibeproxy` provider plugin
(`plugins/model-providers/vibeproxy/`) does this automatically: base_url 8485,
splits reasoning into both top-level `reasoning_effort` and a `model(effort)`
suffix, applies the Claude-OAuth cloak.

## Reasoning-effort passthrough — the part that silently breaks

This is where "I set effort=high but it didn't think harder" comes from. The
rule depends on the wire protocol:

- **Native `/v1/messages`:** send `thinking: {type: "enabled", budget_tokens: N}`
  (standard Anthropic) OR `{type: "adaptive"}`. The claude.ai OAuth backend
  **ignores `type: enabled` outright** (zero thinking tokens, no error) — the
  8485 shim rewrites `enabled → adaptive` + `output_config.effort` (bucketed from
  `budget_tokens`) + `display: summarized` so effort actually lands. So: pick any
  effort in your agent; the shim makes it real. Nothing to configure.

- **OpenAI Chat Completions:** CLIProxyAPI's openai→claude translator reads
  **only** a **top-level `reasoning_effort`** field **or** a **`model(effort)`
  suffix** (suffix wins). It **ignores nested `reasoning.effort` / `extra_body`.**
  So if your agent buries effort in `extra_body.reasoning`, it's dropped — send
  top-level `reasoning_effort`, or name the model `claude-sonnet-5(high)`.

- **GLM / DeepSeek:** use the `model(effort)` suffix (e.g. `glm-5.2(high)`), or
  configure per-model `thinking.levels` in CLIProxyAPI's `openai-compatibility`
  block.

**Visible thinking TEXT** (not just "it thought") additionally needs, for Claude:
`thinking.display: summarized` (the CLIProxyAPI payload rule injects it) **and**
an `anthropic-beta` header (the shim injects it). Both ship in this package;
without them Claude thinks but returns a signature-only empty block.

## Prompt caching passthrough

- **Native `/v1/messages`:** client `cache_control` markers survive end-to-end.
  Stamp breakpoints as you normally would; verify with the agent's own cost view
  (`/cost` in Droid) — expect a large `cache_read` on the repeat turn.
- **Claude via Chat Completions / Hermes:** CLIProxyAPI's translator **drops all
  client `cache_control` markers**, so a payload rule re-stamps the first content
  block (the folded stable system/memory prefix). That rule **must** be
  match-guarded on `messages.0.content.0.type: text` or it 400s native
  string-content requests. Both are in `config/extras.template.yaml`.
- **Non-Claude:** provider-dependent; CLIProxyAPI can't guarantee it.

## The sharp edges (all handled by this package, listed so you can debug)

| Symptom | Cause | Fix (already in the lane) |
|---|---|---|
| Fake `400 "out of extra usage"` on a tiny request | tool name starts with single-underscore `mcp_` | shim `mcp_→xmcp_`; or name tools `mcp__` |
| Model never sees your system prompt / identity | claude.ai OAuth path drops client `system` (both message-role and native top-level) | shim folds system → first user message |
| Effort set but no harder thinking | nested `reasoning.effort`, or native `type:enabled` | top-level `reasoning_effort` / `model(effort)`; shim's `enabled→adaptive` |
| Thinking runs (latency) but no visible text | missing `display:summarized` + beta header | payload rule + shim header |
| Big prompt re-bills every turn | translator drops `cache_control` | payload rule (match-guarded) |
| Native request 400 `content.0.type: Field required` | unguarded cache rule hit a string-content message | `match: messages.0.content.0.type=text` guard |

## Verify any new agent in 30 seconds

```bash
# 1. reasoning visible? (expect >0 chars)
curl -s http://127.0.0.1:8485/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-5","stream":true,"reasoning_effort":"high",
       "messages":[{"role":"user","content":"Is 391 prime? think, one line."}]}' \
  | grep -c reasoning_content    # >0 == thinking works

# 2. system survives? (native path — expect the codeword back)
curl -s http://127.0.0.1:8485/v1/messages -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"claude-sonnet-5","max_tokens":100,"stream":true,
       "system":[{"type":"text","text":"Codeword is ZEBRA-41. Say it when asked."}],
       "messages":[{"role":"user","content":"codeword?"}]}' | grep -o ZEBRA-41

# 3. caching — run any repeat turn, check the agent's /cost for a cache_read hit
```

The bundled `scripts/canary.py` runs 1+2 (plus a tools probe) every 6h and
alerts on regression, so once an agent is wired it stays verified.

## Note on the Claude Code fingerprint

You do **not** need to spoof Claude Code yourself in any agent. CLIProxyAPI-Plus
already sends a byte-accurate Claude Code fingerprint (OAuth client, `user:inference`
scope, `claude-cli/…` UA, "You are Claude Code" identity, the body-embedded
`x-anthropic-billing-header`). That's why subscription routing works regardless
of which agent is in front. Don't re-inject any of it at the agent or shim layer
— it can only desync the fingerprint. See `CLAUDE-FINGERPRINT-ANALYSIS.md` in the
shim repo for the full teardown (incl. a real-CC capture showing the billing
header is body-embedded and the `cch` attestation isn't server-validated).
