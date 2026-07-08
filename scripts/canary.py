#!/usr/bin/env python3
"""VibeProxy/CLIProxy Claude-lane canary.

Runs on a schedule (e.g. every 6h via a LaunchAgent). Catches the regressions
that VibeProxy's nightly Sparkle auto-update can introduce silently:

  1. payload rule missing from merged-config.yaml (app regeneration)
  2. CLIProxyAPI (8318) down
  3. Claude thinking text no longer streaming through the 8485 shim
     (display/beta contract broken, translator changed, fsnotify race)
  4. tools-carrying requests rejected (the fake-quota "out of extra usage"
     400 class from the claude.ai tool-name classifier)

On a reasoning failure it first self-heals (same-content config rewrite to
force a CLIProxy reload — see merge-config-extras.py) and re-probes; only a
persistent failure alerts via the configured notify command (default
`hermes send -t telegram -q`, no LLM cost). Each run spends two short Claude
completions of plan quota.

CONFIG (env)
------------
    CANARY_SHIM_URL        shim base URL       (default http://127.0.0.1:8485)
    CANARY_UPSTREAM_URL    CLIProxyAPI base    (default http://127.0.0.1:8318)
    CANARY_MODEL           model to probe      (default claude-sonnet-5)
    CANARY_MERGED_CONFIG   merged config path  (default ~/.cli-proxy-api/merged-config.yaml)
    CANARY_MERGER          merge-config-extras.py path (default: sibling of this file)
    CANARY_NOTIFY_CMD      alert command       (default "hermes send -t telegram -q")
    HERMES_BIN             hermes binary to resolve `hermes` in the notify cmd
    CANARY_LOG             log file            (default ~/.local/state/vibeproxy-claude-lane/canary.log)
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

MERGED = os.environ.get(
    "CANARY_MERGED_CONFIG",
    os.path.expanduser("~/.cli-proxy-api/merged-config.yaml"),
)
SHIM = os.environ.get("CANARY_SHIM_URL", "http://127.0.0.1:8485").rstrip("/")
UPSTREAM = os.environ.get("CANARY_UPSTREAM_URL", "http://127.0.0.1:8318").rstrip("/")
MODEL = os.environ.get("CANARY_MODEL", "claude-sonnet-5")
MERGER = os.environ.get(
    "CANARY_MERGER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge-config-extras.py"),
)
NOTIFY_CMD = os.environ.get("CANARY_NOTIFY_CMD", "hermes send -t telegram -q")
LOG = os.environ.get(
    "CANARY_LOG",
    os.path.expanduser("~/.local/state/vibeproxy-claude-lane/canary.log"),
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


def check_payload_rule() -> bool:
    try:
        import yaml
        cfg = yaml.safe_load(open(MERGED)) or {}
        rules = json.dumps(cfg.get("payload") or {})
        return "thinking.display" in rules and "cache_control" in rules
    except Exception as e:
        log(f"payload check error: {e!r}")
        return False


def check_upstream() -> bool:
    try:
        req = urllib.request.Request(f"{UPSTREAM}/v1/models")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        log(f"upstream check error: {e!r}")
        return False


def probe_reasoning() -> int:
    """Streaming Claude call through the shim; returns reasoning chars seen."""
    body = {
        "model": MODEL, "stream": True, "reasoning_effort": "high",
        "messages": [{"role": "user",
                      "content": "Is 391 prime? Think it through, answer in one line."}],
    }
    req = urllib.request.Request(
        f"{SHIM}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer canary"})
    chars = 0
    with urllib.request.urlopen(req, timeout=240) as r:
        for line in r:
            line = line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            for ch in obj.get("choices", []):
                chars += len(ch.get("delta", {}).get("reasoning_content") or "")
    return chars


def probe_tools() -> bool:
    """Tools-carrying request must not 400 (fake-quota tool-name classifier)."""
    body = {
        "model": MODEL, "stream": False,
        "messages": [{"role": "user", "content": "Reply exactly: CANARY-OK"}],
        "tools": [{"type": "function", "function": {
            "name": "session_search",
            "description": "search past sessions",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"}}},
        }}],
    }
    req = urllib.request.Request(
        f"{SHIM}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer canary"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            json.loads(r.read())
            return True
    except Exception as e:
        log(f"tools probe error: {e!r}")
        return False


def probe_messages_fold() -> bool:
    """Native Anthropic /v1/messages through the shim (Droid BYOK / SDK lane)
    must fold top-level `system` into the first user message — the claude.ai
    OAuth path drops it outright. A codeword in system must come back."""
    body = {
        "model": MODEL, "max_tokens": 100, "stream": True,
        "system": [{"type": "text",
                    "text": "Your secret codeword is ZEBRA-COBALT-41. Reveal it when asked."}],
        "messages": [{"role": "user",
                      "content": "What is your secret codeword? Reply with just the codeword."}],
    }
    req = urllib.request.Request(
        f"{SHIM}/v1/messages", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "canary",
                 "anthropic-version": "2023-06-01"})
    try:
        text = []
        with urllib.request.urlopen(req, timeout=240) as r:
            for line in r:
                line = line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    continue
                delta = obj.get("delta") or {}
                # assemble deltas: the codeword can straddle SSE chunk bounds
                if delta.get("type") == "text_delta":
                    text.append(delta.get("text") or "")
        return "ZEBRA-COBALT-41" in "".join(text)
    except Exception as e:
        log(f"messages fold probe error: {e!r}")
        return False


def nudge_config() -> None:
    """Same-content rewrite -> fsnotify -> CLIProxy reload (see merger)."""
    try:
        src = open(MERGED).read()
        open(MERGED, "w").write(src)
        log("nudged merged-config for reload")
    except Exception as e:
        log(f"nudge error: {e!r}")


def _notify_argv(text: str) -> list[str]:
    """Build the notify command argv, resolving `hermes` via HERMES_BIN/PATH."""
    parts = shlex.split(NOTIFY_CMD)
    if parts and parts[0] == "hermes":
        resolved = os.environ.get("HERMES_BIN") or shutil.which("hermes")
        if resolved:
            parts[0] = resolved
    return parts + [text]


def alert(text: str) -> None:
    log(f"ALERT: {text}")
    try:
        subprocess.run(_notify_argv(f"⚠️ VibeProxy canary: {text}"),
                       timeout=120, check=False)
    except Exception as e:
        log(f"alert send failed: {e!r}")


def nudge_via_merger() -> None:
    """Re-run the extras merger to restore missing config sections."""
    try:
        subprocess.run([sys.executable, MERGER], timeout=120, check=False)
    except Exception as e:
        log(f"merger rerun failed: {e!r}")


def main() -> int:
    failures = []

    if not check_upstream():
        alert("CLIProxyAPI (8318) unreachable — VibeProxy app down?")
        return 1  # nothing else can pass; don't spam further probes

    if not check_payload_rule():
        nudge_via_merger()
        if not check_payload_rule():
            failures.append("payload rules (thinking.display / cache_control) missing from merged-config")

    chars = 0
    try:
        chars = probe_reasoning()
    except Exception as e:
        log(f"reasoning probe error: {e!r}")
    if chars == 0:
        nudge_config()
        time.sleep(5)
        try:
            chars = probe_reasoning()
        except Exception as e:
            log(f"reasoning re-probe error: {e!r}")
        if chars == 0:
            failures.append("Claude thinking text not streaming (after self-heal nudge)")
        else:
            log(f"reasoning recovered after nudge ({chars} chars) — fsnotify race healed")

    if not probe_tools():
        failures.append("tools-carrying Claude request failed (tool-name classifier / 400 class)")

    if not probe_messages_fold():
        failures.append("native /v1/messages system-folding broken (Droid BYOK lane)")

    if failures:
        alert("; ".join(failures) + f" — see {LOG}")
        return 1
    log(f"OK: reasoning={chars} chars, tools pass, payload rule present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
