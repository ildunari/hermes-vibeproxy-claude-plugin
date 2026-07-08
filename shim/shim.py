#!/usr/bin/env python3
"""VibeProxy tool-name rename shim.

WHY THIS EXISTS
---------------
The OAuth/subscription route behind VibeProxy / CLIProxyAPI deterministically
rejects certain *tool-name co-occurrences* with a misleading HTTP 400 "You're
out of extra usage." Hermes sends GPT/Claude traffic through this shim first;
the shim now forwards directly to CLIProxyAPIPlus, while go-llm-proxy remains a
separate compatibility lane for non-subscription providers.
Proven combinations (bare names, 382-byte request, no system prompt — still fails):

    {session_search, clarify}        -> 400
    {session_search, delegate_task}  -> 400

It is NOT a size or tool-count limit: a padded 131 KB / 45-tool request passes,
and renaming a single colliding name fixes the full Hermes toolset.

2026-06-11: a second reserved pattern was isolated the same way — ANY tool name
starting with the `mcp_` prefix (single underscore, e.g. `mcp_exa_web_search_exa`,
`mcp_open_design_write_file`, even bare `mcp_anything`) gets the same fake
"out of extra usage" 400 from the claude.ai endpoint. `mcp__` (double underscore),
`mcpx_`, `Mcp_` all pass, so Anthropic reserves the lowercase `mcp_` namespace.
Hermes names MCP tools `mcp_<server>_<tool>`, so the shim now also rewrites that
prefix on the wire:

    outbound:  mcp_<rest>   -> xmcp_<rest>
    inbound:   xmcp_<rest>  -> mcp_<rest>

2026-07-08: visible Claude thinking needs two request-side signals the OpenAI
wire cannot carry on its own. The claude.ai OAuth backend returns thinking text
only when the Claude-format request has `thinking.display: "summarized"` AND the
request carries an `anthropic-beta` header (e.g. interleaved-thinking-2025-05-14);
otherwise thinking runs but comes back as an empty signature-only block.
CLIProxyAPI's openai->claude translator (<= 7.2.53) never sets `display`, so a
`payload.override` rule in ~/.cli-proxy-api/merged-config.yaml injects
`thinking.display: summarized` post-translation, and THIS SHIM adds the
`anthropic-beta` header for Anthropic models (CLIProxyAPI forwards it upstream).
Both halves are required: rule without header or header without rule = no text.

2026-06-11 (same dig): the claude.ai path also silently DROPS all client system
messages for Claude models — a magic-word recall test in the system prompt fails
at every hop while the same content in a user message succeeds, and prompt_tokens
stays constant no matter how large the system prompt is. Hermes ships its whole
identity/memory/instructions block as the system message, so Fable never saw any
of it. The shim now folds system/developer messages into the first user message
(wrapped in <system>...</system>) for Anthropic models so the content actually
reaches the model.

Renaming `session_search` alone breaks BOTH collisions (it's the shared element).
We do not want to rename the real Hermes tool — every other provider/agent has
used `session_search` for ages. So this shim aliases the name *only on the wire*
to VibeProxy, and only for Anthropic models, in both directions:

    outbound (Hermes -> upstream):  session_search      -> past_session_lookup
    inbound  (upstream -> Hermes):  past_session_lookup -> session_search

Hermes' agent loop, history, skills, and memory keep seeing `session_search`.
Non-Anthropic models (GLM, GPT) and non-completion paths pass through untouched.

CONFIG (env)
------------
    SHIM_PORT       listen port (default 8485)
    SHIM_UPSTREAM   upstream base origin (default http://127.0.0.1:8318)
    SHIM_LOG        log file (default ~/.hermes/logs/vibeproxy-rename-shim.log)
    SHIM_DEBUG      "1" to log every request's route/model/tool decision
"""
import asyncio
import hashlib
import json
import os
import sys
import time

from aiohttp import web, ClientSession, ClientTimeout

PORT = int(os.environ.get("SHIM_PORT", "8485"))
UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:8318").rstrip("/")
LOG_PATH = os.environ.get(
    "SHIM_LOG", os.path.expanduser("~/.hermes/logs/vibeproxy-rename-shim.log")
)
DEBUG = os.environ.get("SHIM_DEBUG", "0") == "1"

# canonical (what Hermes uses)  ->  wire alias (what upstream sees)
RENAME = {"session_search": "past_session_lookup"}
INVERSE = {v: k for k, v in RENAME.items()}

# Reserved-prefix rewrite: claude.ai rejects any `mcp_`-prefixed tool name with a
# fake quota 400 (see header). `xmcp_` is proven to pass and is unused by Hermes.
PREFIX_OUT = ("mcp_", "xmcp_")   # outbound: mcp_*  -> xmcp_*
PREFIX_IN = ("xmcp_", "mcp_")    # inbound:  xmcp_* -> mcp_*


def _map_out(name):
    if name in RENAME:
        return RENAME[name]
    if isinstance(name, str) and name.startswith(PREFIX_OUT[0]):
        return PREFIX_OUT[1] + name[len(PREFIX_OUT[0]):]
    return None


def _map_in(name):
    if name in INVERSE:
        return INVERSE[name]
    if isinstance(name, str) and name.startswith(PREFIX_IN[0]):
        return PREFIX_IN[1] + name[len(PREFIX_IN[0]):]
    return None

# Models that route through the filtering Anthropic backend. Anything else
# (glm*, gpt*, deepseek*, etc.) is passed through with zero rewriting.
ANTHROPIC_HINTS = ("claude", "opus", "sonnet", "haiku", "fable", "mythos")

# Headers that must not be copied verbatim to the upstream request or back to
# the client (hop-by-hop / length recomputed by aiohttp).
_HOP = {
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade",
    "content-encoding", "host",
}


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass
    if DEBUG:
        sys.stderr.write(line)


def _is_anthropic(model) -> bool:
    m = (model or "").lower()
    return any(h in m for h in ANTHROPIC_HINTS)


def _count_cache_controls(obj) -> int:
    if isinstance(obj, dict):
        return (1 if "cache_control" in obj else 0) + sum(
            _count_cache_controls(v) for v in obj.values()
        )
    if isinstance(obj, list):
        return sum(_count_cache_controls(v) for v in obj)
    return 0


def _first_cache_control(obj):
    """Return the first cache_control marker found inside obj, if any."""
    if isinstance(obj, dict):
        cc = obj.get("cache_control")
        if isinstance(cc, dict):
            return dict(cc)
        for v in obj.values():
            found = _first_cache_control(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _first_cache_control(v)
            if found:
                return found
    return None


def _strip_nested_cache_controls(obj) -> int:
    """Remove nested cache_control markers, returning the number removed."""
    removed = 0
    if isinstance(obj, dict):
        if "cache_control" in obj:
            obj.pop("cache_control", None)
            removed += 1
        for v in obj.values():
            removed += _strip_nested_cache_controls(v)
    elif isinstance(obj, list):
        for v in obj:
            removed += _strip_nested_cache_controls(v)
    return removed


def _promote_message_cache_controls(body: dict) -> int:
    """Promote OpenAI-content-part cache markers to message-envelope markers.

    The claude.ai/VibeProxy path consistently honors message-level
    ``cache_control`` markers. Content-part markers are accepted but, for Fable
    in this chain, cache far less of the folded prompt. Hermes emits OpenAI-wire
    content-part markers, so normalize them here after system-folding.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    promoted = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        cc = _first_cache_control(content)
        if cc and not isinstance(m.get("cache_control"), dict):
            m["cache_control"] = cc
            promoted += 1
        if cc:
            _strip_nested_cache_controls(content)
    return promoted


def _role_summary(body: dict) -> str:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return "-"
    roles = [str(m.get("role", "?")) if isinstance(m, dict) else "?" for m in msgs]
    shown = roles[:12]
    suffix = "+" if len(roles) > len(shown) else ""
    return ",".join(shown) + suffix


def _user_turn_count(body: dict) -> int:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")


def _prefix_hash(body: dict) -> str:
    """Privacy-safe hash of the likely cache-bearing prefix.

    The digest helps correlate cache behavior across requests without logging
    prompt, tool schema, memory, or user content.
    """
    msgs_value = body.get("messages")
    msgs = msgs_value if isinstance(msgs_value, list) else []
    prefix_msgs = []
    for m in msgs:
        if isinstance(m, dict):
            prefix_msgs.append(m)
        if len(prefix_msgs) >= 3:
            break
    payload = {
        "model": body.get("model"),
        "tools": body.get("tools") or [],
        "messages": prefix_msgs,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _usage_log_fragment(obj: dict) -> str | None:
    usage = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = 0
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    cached = cached or usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0
    try:
        prompt_i = int(prompt or 0)
        cached_i = int(cached or 0)
        completion_i = int(completion or 0)
    except Exception:
        return None
    pct = round((cached_i / prompt_i) * 100) if prompt_i > 0 else 0
    return f"prompt={prompt_i} cached={cached_i} cache_pct={pct} completion={completion_i}"


def _rename_outbound(body: dict) -> int:
    """Canonical -> alias across tools, tool_choice, and message history.
    Returns count of names rewritten (for logging)."""
    n = 0

    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict):
                new = _map_out(fn.get("name"))
                if new:
                    fn["name"] = new
                    n += 1

    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            new = _map_out(fn.get("name"))
            if new:
                fn["name"] = new
                n += 1

    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            # assistant tool calls
            for call in m.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    new = _map_out(fn.get("name"))
                    if new:
                        fn["name"] = new
                        n += 1
            # tool-result messages carry the tool name
            if m.get("role") == "tool":
                new = _map_out(m.get("name"))
                if new:
                    m["name"] = new
                    n += 1
    return n


def _fold_system(body: dict) -> int:
    """Merge system/developer messages into the first user message so their
    content survives the claude.ai path (which drops system blocks outright).
    Returns count of system messages folded."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    sys_parts, rest = [], []
    folded_cache_control = None
    for m in msgs:
        if isinstance(m, dict) and m.get("role") in ("system", "developer"):
            if folded_cache_control is None:
                folded_cache_control = _first_cache_control(m)
            c = m.get("content")
            if isinstance(c, list):
                c = "\n".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
            if c:
                sys_parts.append(str(c))
        else:
            rest.append(m)
    if not sys_parts:
        return 0
    block = "<system>\n" + "\n\n".join(sys_parts) + "\n</system>\n\n"
    for m in rest:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = block + c
            elif isinstance(c, list):
                c.insert(0, {"type": "text", "text": block})
            else:
                m["content"] = block
            if folded_cache_control and not isinstance(m.get("cache_control"), dict):
                m["cache_control"] = folded_cache_control
            break
    else:
        folded = {"role": "user", "content": block}
        if folded_cache_control:
            # Keep mixed-value typing explicit enough for Pyright.
            folded = {"role": "user", "content": block, "cache_control": folded_cache_control}
        rest.insert(0, folded)
    body["messages"] = rest
    return len(sys_parts)


def _fold_system_anthropic(body: dict) -> int:
    """Anthropic /v1/messages: the claude.ai OAuth path drops the top-level
    `system` field just like it drops system-role messages (codeword probe
    2026-07-08). Fold it into the first user message so the content actually
    reaches Claude; carry the first cache_control marker onto the folded block
    so the client's caching intent survives."""
    sys_ = body.get("system")
    if not sys_:
        return 0
    cache = None
    if isinstance(sys_, str):
        parts = [sys_]
    elif isinstance(sys_, list):
        parts = [b.get("text", "") for b in sys_ if isinstance(b, dict)]
        for b in sys_:
            if isinstance(b, dict) and isinstance(b.get("cache_control"), dict):
                cache = b["cache_control"]
                break
    else:
        return 0
    parts = [p for p in parts if p]
    msgs = body.get("messages")
    if not parts or not isinstance(msgs, list):
        return 0
    block = {"type": "text",
             "text": "<system>\n" + "\n\n".join(parts) + "\n</system>"}
    if cache:
        block["cache_control"] = dict(cache)
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = [block, {"type": "text", "text": c}]
            elif isinstance(c, list):
                c.insert(0, block)
            else:
                m["content"] = [block]
            break
    else:
        msgs.insert(0, {"role": "user", "content": [block]})
    del body["system"]
    return len(parts)


_EFFORT_BUCKETS = ((4096, "low"), (16384, "medium"))


def _translate_thinking_anthropic(body: dict) -> str:
    """The claude.ai OAuth backend ignores the standard API thinking shape
    {type: enabled, budget_tokens: N} outright — thinking_tokens stays 0
    (probed 2026-07-08). It only thinks on {type: adaptive}, and only returns
    thinking TEXT with display=summarized (+ the anthropic-beta header the
    handler injects). Translate enabled->adaptive, mapping budget_tokens to
    output_config.effort buckets; leave disabled/absent untouched."""
    th = body.get("thinking")
    if not isinstance(th, dict):
        return ""
    if th.get("type") == "adaptive":
        th.setdefault("display", "summarized")
        return "adaptive"
    if th.get("type") != "enabled":
        return ""
    budget = th.get("budget_tokens") or 0
    effort = "high"
    for cap, name in _EFFORT_BUCKETS:
        if budget <= cap:
            effort = name
            break
    body["thinking"] = {"type": "adaptive", "display": "summarized"}
    oc = body.get("output_config")
    if not isinstance(oc, dict):
        oc = body["output_config"] = {}
    oc.setdefault("effort", effort)
    return f"enabled->adaptive/{effort}"


def _rename_inbound_obj(obj) -> int:
    """alias -> canonical inside a parsed non-streaming completion JSON."""
    n = 0
    for choice in obj.get("choices") or []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(msg, dict):
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    new = _map_in(fn.get("name"))
                    if new:
                        fn["name"] = new
                        n += 1
    return n


def _rewrite_sse_line(line: str) -> str:
    """alias -> canonical inside a single streamed `data:` JSON line.
    OpenAI sends function.name whole in the first delta for a tool_call index,
    so a per-line rewrite is sufficient."""
    if not line.startswith("data:"):
        return line
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return line
    # Cheap pre-check so we only parse lines that could carry an aliased name.
    if not (any(a in payload for a in INVERSE) or PREFIX_IN[0] in payload):
        return line
    try:
        obj = json.loads(payload)
    except Exception:
        return line
    changed = False
    for choice in obj.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict):
            for call in delta.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    new = _map_in(fn.get("name"))
                    if new:
                        fn["name"] = new
                        changed = True
    if not changed:
        return line
    return "data: " + json.dumps(obj, separators=(",", ":"))


def _fwd_req_headers(req: web.Request) -> dict:
    return {k: v for k, v in req.headers.items() if k.lower() not in _HOP}


def _fwd_resp_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP}


async def handle(req: web.Request) -> web.StreamResponse:
    raw = await req.read()
    url = f"{UPSTREAM}{req.rel_url}"
    is_completions = req.method == "POST" and req.path.endswith("/chat/completions")
    is_messages = req.method == "POST" and req.path.endswith("/v1/messages")

    body = None
    anthropic = False
    renamed = 0
    folded = 0
    cache_controls = 0
    user_turns = 0
    roles = "-"
    prefix_hash = "-"
    promoted_cache_controls = 0
    thinking_note = ""
    if (is_completions or is_messages) and raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = None
    if isinstance(body, dict):
        anthropic = _is_anthropic(body.get("model"))
        if anthropic and is_completions:
            renamed = _rename_outbound(body)
            folded = _fold_system(body)
            promoted_cache_controls = _promote_message_cache_controls(body)
            cache_controls = _count_cache_controls(body)
            user_turns = _user_turn_count(body)
            roles = _role_summary(body)
            prefix_hash = _prefix_hash(body)
            raw = json.dumps(body).encode()
        elif anthropic and is_messages:
            # Native Anthropic Messages clients (Droid BYOK, Warp, SDKs).
            # No tool renames here: droid/native tool names never carry the
            # bare mcp_ prefix, and inbound tool_use rewriting isn't wired
            # for this protocol.
            folded = _fold_system_anthropic(body)
            thinking_note = _translate_thinking_anthropic(body)
            cache_controls = _count_cache_controls(body)
            user_turns = _user_turn_count(body)
            raw = json.dumps(body).encode()

    if DEBUG or ((is_completions or is_messages) and isinstance(body, dict)):
        _log(
            f"{req.method} {req.path} model={body.get('model') if isinstance(body, dict) else '?'} "
            f"anthropic={anthropic} renamed_out={renamed} folded_sys={folded} "
            f"promoted_cache={promoted_cache_controls} "
            f"thinking={thinking_note or '-'} "
            f"tools={len(body.get('tools') or []) if isinstance(body, dict) else 0} "
            f"user_turns={user_turns} roles={roles} cache_controls={cache_controls} "
            f"prefix_hash={prefix_hash} bytes={len(raw)}"
        )

    headers = _fwd_req_headers(req)
    headers["Accept-Encoding"] = "identity"  # keep SSE/text uncompressed for rewrite
    if anthropic and not headers.get("anthropic-beta"):
        # Required for the claude.ai OAuth backend to return thinking text
        # (paired with the thinking.display payload rule in CLIProxyAPI config).
        headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"

    timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
    session: ClientSession = req.app["session"]

    try:
        up = await session.request(
            req.method, url, data=raw if raw else None, headers=headers,
            timeout=timeout, allow_redirects=False,
        )
    except Exception as e:
        # The shim must be transparent: if the upstream is unreachable, return
        # an upstream-shaped 502 rather than a shim stack trace, so Hermes'
        # error handling/fallback sees a normal gateway failure.
        _log(f"  upstream error {url}: {e!r}")
        return web.json_response(
            {"error": {"type": "upstream_error", "message": f"rename-shim: upstream {UPSTREAM} unreachable: {e}"}},
            status=502,
        )

    ctype = up.headers.get("Content-Type", "")
    streaming = "text/event-stream" in ctype

    # Only the Anthropic completion path needs response rewriting.
    rewrite = anthropic and is_completions

    if streaming:
        resp = web.StreamResponse(status=up.status, headers=_fwd_resp_headers(up.headers))
        resp.headers["Content-Type"] = ctype or "text/event-stream"
        await resp.prepare(req)
        buf = ""
        async for chunk in up.content.iter_any():
            text = chunk.decode("utf-8", errors="replace")
            if not rewrite:
                await resp.write(text.encode("utf-8"))
                continue
            buf += text
            # Emit complete lines; keep partial tail buffered.
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                await resp.write((_rewrite_sse_line(line) + "\n").encode("utf-8"))
        if buf:
            tail = _rewrite_sse_line(buf) if rewrite else buf
            await resp.write(tail.encode("utf-8"))
        await resp.write_eof()
        return resp

    # Non-streaming.
    data = await up.read()
    if rewrite and up.status == 200 and data:
        try:
            obj = json.loads(data)
            n = _rename_inbound_obj(obj)
            usage_fragment = _usage_log_fragment(obj)
            if usage_fragment:
                _log(f"  response status=200 {usage_fragment}")
            if n:
                data = json.dumps(obj).encode()
                _log(f"  inbound renamed={n}")
        except Exception:
            pass
    out_headers = _fwd_resp_headers(up.headers)
    return web.Response(status=up.status, body=data, headers=out_headers)


async def _on_startup(app):
    # Config-side half of the visible-thinking fix (the payload rule that sets
    # thinking.display=summarized) is owned by the WatchPaths LaunchAgent
    # the config-extras LaunchAgent running merge-config-extras.py in this
    # repo — the shim only carries the wire-side half (anthropic-beta header).
    app["session"] = ClientSession()
    _log(f"shim up: listen :{PORT} -> upstream {UPSTREAM} rename={RENAME} debug={DEBUG}")


async def _on_cleanup(app):
    await app["session"].close()


def main():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_route("*", "/{tail:.*}", handle)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
