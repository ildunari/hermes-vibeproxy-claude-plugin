# Root cause: why the Claude lane needs a shim

This lane exists because the Claude **OAuth / subscription** backend (the
claude.ai path that VibeProxy / CLIProxyAPI authenticates against) behaves
differently from the pay-as-you-go Anthropic API in four ways that break a naive
OpenAI-compatible client. Each is a real, reproduced failure — not a guess.

## 1. Extended thinking runs but its text is invisible

**Symptom.** Through the Claude OAuth path, extended thinking *runs* (usage shows
`output_tokens_details.thinking_tokens > 0`, latency confirms it) but the text is
never returned. Non-streaming responses contain a thinking block with only a
`signature` and `"thinking": ""`; streams emit a single `signature_delta` and
**zero `thinking_delta` events**. On `/v1/chat/completions` this surfaces as
`delta.reasoning_content` never appearing for any Claude model. Tested on
CLIProxyAPI v7.2.53 / 7.2.50 with claude-sonnet-5, claude-fable-5,
claude-opus-4-8, claude-sonnet-4-6.

**Root cause.** The claude.ai OAuth backend only returns thinking text when the
Anthropic-format request carries **both**:

1. `thinking.display: "summarized"` — the field accepts exactly `summarized` or
   `omitted` (a 400 lists the valid values); when absent the backend behaves as
   `omitted`, i.e. signature-only thinking blocks; and
2. an `anthropic-beta` header (e.g. `interleaved-thinking-2025-05-14`).

Neither CLIProxyAPI translator ever sets `display`
(`claude_openai_request.go`, `claude_openai-responses_request.go`, and
`internal/thinking/provider/claude/apply.go` only write `thinking.type`,
`budget_tokens`, `output_config.effort`). A repo-wide search for `"display"`
under the claude translators returns nothing, so every OpenAI-format client gets
thinking-less-looking Claude responses on OAuth. Either half alone is
insufficient: rule without header, or header without rule, yields thinking with
no returned text.

**What this lane does.** Two coordinated halves:

- **Config half** — a `payload.override` rule (see `config/extras.template.yaml`)
  injects `thinking.display: summarized` post-translation into the Claude-format
  request. Because the VibeProxy app regenerates `merged-config.yaml` on every
  launch/auto-update, the `merge-config-extras.py` watcher re-injects it.
- **Wire half** — the shim (`shim/shim.py`) adds the `anthropic-beta` header on
  Anthropic-model requests, which CLIProxyAPI forwards upstream.

## 2. The `mcp_` tool-name prefix triggers a fake quota 400

**Symptom.** Any tool whose name starts with the lowercase `mcp_` prefix (single
underscore — e.g. `mcp_exa_web_search_exa`, even bare `mcp_anything`) makes the
claude.ai endpoint return `HTTP 400 "You're out of extra usage."` — a misleading
message that has nothing to do with quota. `mcp__` (double underscore), `mcpx_`,
and `Mcp_` all pass, so Anthropic effectively reserves the lowercase `mcp_`
namespace for its own MCP tools.

There is a related, older collision class: certain **tool-name co-occurrences**
also 400 regardless of size. Proven with bare names in a 382-byte request and no
system prompt: `{session_search, clarify}` → 400 and
`{session_search, delegate_task}` → 400, while a padded 131 KB / 45-tool request
passes. It is not a size or tool-count limit; `session_search` is the shared
element, so aliasing that one name on the wire fixes the whole toolset.

**What this lane does.** The shim rewrites, **only on the wire and only for
Anthropic models**, in both directions:

- `mcp_<rest>` → `xmcp_<rest>` outbound, reversed inbound; and
- `session_search` → `past_session_lookup` outbound, reversed inbound.

The provider plugin applies the same `mcp_` → `mcp__` normalization at request
assembly as belt-and-suspenders. Hermes' agent loop, history, skills, and memory
keep seeing the canonical names; non-Anthropic models pass through untouched.

## 3. System / developer messages are silently dropped

**Symptom.** The claude.ai path drops all client `system` messages for Claude
models. A magic-word recall test placed in the system prompt fails at every hop
while the same content in a user message succeeds, and `prompt_tokens` stays
constant no matter how large the system prompt is. An agent that ships its whole
identity / memory / instructions block as the system message therefore has the
model never see any of it.

**What this lane does.** For Anthropic models the shim folds `system` /
`developer` messages into the first `user` message, wrapped in
`<system>...</system>`, so the content actually reaches the model. The provider
plugin additionally guarantees a Claude Code identity system message and
sanitizes agent-identity strings (Hermes → Claude Code, Nous Research →
Anthropic) so the subscription route stays consistent with the Claude Code
client contract.

## 4. The VibeProxy app regenerates config on every launch

**Symptom.** The VibeProxy menu-bar app rewrites `~/.cli-proxy-api/merged-config.yaml`
from scratch on every launch, and Sparkle auto-updates it nightly. Any manual
addition — including the `thinking.display` payload rule from problem 1 — is
wiped, so visible thinking silently breaks after an update.

**Root cause / race.** Even when the rule is re-injected, timing can defeat it:
CLIProxyAPI arms its config `fsnotify` watcher shortly *after* startup. A
re-inject that lands before the watcher is armed leaves the running server on the
payload-less config (rule present in the file, thinking still invisible).

**What this lane does.** `merge-config-extras.py` runs from a `WatchPaths`
LaunchAgent on `merged-config.yaml`: it deep-merges the user's extras back in
(idempotently, so it doesn't loop), then does a **post-arm nudge** — a
same-content rewrite after a grace period so a second `fsnotify` event always
fires once the watcher is live. The 6h `canary.py` is the backstop: it probes
reasoning + tools, self-heals via a config nudge / merger rerun, and only alerts
(default `hermes send -t telegram -q`) on a persistent failure.

## Suggested upstream fix

When building the adaptive thinking config for Claude (the CLIProxyAPI
translators and/or `internal/thinking/provider/claude/apply.go`), set
`display: "summarized"`, and ensure OAuth Claude upstream requests carry an
`anthropic-beta` value that unlocks thinking output
(`interleaved-thinking-2025-05-14` works). With that upstream, problem 1's
config + header workaround becomes unnecessary; the tool-name and system-message
rewrites (problems 2–3) remain client-side wire concerns.

## Dropped `cache_control` markers (prompt caching)

The same openai→claude translation discards client prompt-caching markers in
both OpenAI shapes: message-level `cache_control` and Anthropic-style
content-part `cache_control` inside content arrays. A/A verification on
v7.2.53: a marked ~2.7k-token user block never caches (`cached_tokens` stays at
the injected-prefix size, ~1.9k), while the native `/v1/messages` passthrough
with a content-block marker caches correctly (cache_creation on run 1,
cache_read of prefix+block on run 2). The payload rule

```yaml
- models:
  - name: "claude-*"
    protocol: "claude"
  params:
    "messages.0.content.0.cache_control": {"type": "ephemeral"}
```

restores caching of the first message's first block — which, with the shim's
system folding, is the agent's entire stable prefix (99.8% measured cache read
on repeat calls). String-content messages are safe: the translator normalizes
content to blocks before payload rules run.
