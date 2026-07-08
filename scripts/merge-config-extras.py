#!/usr/bin/env python3
"""Re-inject user-owned extras into VibeProxy's generated merged-config.yaml.

The VibeProxy menu-bar app regenerates ~/.cli-proxy-api/merged-config.yaml on
every launch (and Sparkle auto-updates nightly), wiping any manual additions.
This script deep-merges the user-owned extras from the extras yaml back in:

  - `payload:` rules (e.g. thinking.display=summarized for Claude reasoning
    visibility through the claude.ai OAuth backend — see the rename-shim docs)
  - extra `openai-compatibility` providers and extra models on existing
    providers (optional)

Idempotent: writes only when something is missing, so the launchd WatchPaths
trigger on merged-config.yaml does not loop. CLIProxyAPI hot-reloads the file.

CONFIG (env)
------------
    VIBEPROXY_MERGED_CONFIG   app-generated merged config to patch
                              (default ~/.cli-proxy-api/merged-config.yaml)
    VIBEPROXY_EXTRAS          user-owned extras yaml to merge in
                              (default ~/.cli-proxy-api/extras.yaml)
    VIBEPROXY_EXTRAS_LOG      log file
                              (default ~/.local/state/vibeproxy-claude-lane/config-extras.log)
"""
import os
import sys
import time

import yaml

MERGED = os.environ.get(
    "VIBEPROXY_MERGED_CONFIG",
    os.path.expanduser("~/.cli-proxy-api/merged-config.yaml"),
)
EXTRAS = os.environ.get(
    "VIBEPROXY_EXTRAS",
    os.path.expanduser("~/.cli-proxy-api/extras.yaml"),
)
LOG = os.environ.get(
    "VIBEPROXY_EXTRAS_LOG",
    os.path.expanduser("~/.local/state/vibeproxy-claude-lane/config-extras.log"),
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


def main() -> int:
    try:
        with open(EXTRAS) as f:
            extras = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log(f"extras file missing: {EXTRAS}")
        return 0

    with open(MERGED) as f:
        cfg = yaml.safe_load(f) or {}

    changed = False

    # payload rules: replace wholesale from extras (extras own this section)
    if "payload" in extras and cfg.get("payload") != extras["payload"]:
        cfg["payload"] = extras["payload"]
        changed = True

    # openai-compatibility: append missing providers; extend models by name
    for want in extras.get("openai-compatibility", []):
        oc = cfg.setdefault("openai-compatibility", [])
        existing = next((e for e in oc if e.get("name") == want.get("name")), None)
        if existing is None:
            oc.append(want)
            changed = True
            continue
        have = {m.get("name") for m in existing.get("models", [])}
        for model in want.get("models", []):
            if model.get("name") not in have:
                existing.setdefault("models", []).append(model)
                changed = True

    if changed:
        with open(MERGED, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        log("re-injected extras into merged-config.yaml")
        # CLIProxyAPI arms its config fsnotify watcher shortly after startup.
        # When the app has just relaunched, this re-inject can land BEFORE the
        # watcher is armed, and the running server silently keeps the payload-
        # less config (rule present in file, thinking invisible). Rewrite
        # identical content after a grace period so a post-arm fsnotify event
        # always fires; the second write re-triggers the WatchPaths agent,
        # which then no-ops here.
        time.sleep(20)
        with open(MERGED) as f:
            current = f.read()
        with open(MERGED, "w") as f:
            f.write(current)
        log("post-arm nudge rewrite done")
    else:
        log("no-op: extras already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
