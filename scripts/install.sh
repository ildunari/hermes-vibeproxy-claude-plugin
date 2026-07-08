#!/usr/bin/env bash
#
# install.sh — set up the VibeProxy Claude lane (macOS / launchd).
#
# What it does (and nothing else):
#   1. Copies the shim + scripts into ~/.local/share/vibeproxy-claude-lane/
#   2. Verifies your chosen Python has PyYAML (merger/canary) and aiohttp (shim)
#   3. Substitutes __HOME__ / __PYTHON__ into the plist templates
#   4. Installs the plists into ~/Library/LaunchAgents and bootstraps them
#   5. (optional) symlinks the provider plugin into your Hermes home
#
# It NEVER installs Python packages, edits anything outside your home dir, or
# touches your CLIProxyAPI config. Re-runnable (idempotent).
#
# Config via env:
#   PYTHON        python interpreter to run the shim/scripts (default: python3)
#   HERMES_HOME   if set, offer to symlink the provider plugin there
#   ASSUME_YES=1  skip the confirmation prompt
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
DEST="$HOME/.local/share/vibeproxy-claude-lane"
STATE="$HOME/.local/state/vibeproxy-claude-lane"
AGENTS="$HOME/Library/LaunchAgents"

SHIM_LABEL="com.example.vibeproxy-rename-shim"
EXTRAS_LABEL="com.example.vibeproxy-config-extras"
CANARY_LABEL="com.example.vibeproxy-canary"

info()  { printf '  %s\n' "$*"; }
warn()  { printf '  WARNING: %s\n' "$*" >&2; }
die()   { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- python resolution -------------------------------------------------------
command -v "$PYTHON" >/dev/null 2>&1 || die "python interpreter '$PYTHON' not found (set PYTHON=/path/to/python)"
PYTHON_ABS="$(command -v "$PYTHON")"

echo "VibeProxy Claude lane installer"
echo
echo "This will install into your home directory:"
echo "  files : $DEST/"
echo "  state : $STATE/"
echo "  agents: $AGENTS/{$SHIM_LABEL,$EXTRAS_LABEL,$CANARY_LABEL}.plist"
echo "  python: $PYTHON_ABS"
echo
echo "It will bootstrap the shim (KeepAlive) and the config-extras watcher now,"
echo "and register the 6h canary. It does NOT install Python packages or edit"
echo "your CLIProxyAPI config."
echo

if [[ "${ASSUME_YES:-0}" != "1" ]]; then
  read -r -p "Proceed? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

# --- dependency checks --------------------------------------------------------
echo
echo "Checking Python dependencies for $PYTHON_ABS ..."
missing_pkgs=()
if "$PYTHON_ABS" -c "import yaml" >/dev/null 2>&1; then
  info "PyYAML: OK"
else
  warn "PyYAML NOT found — the config-extras merger and the canary need it."
  missing_pkgs+=("pyyaml")
fi
if "$PYTHON_ABS" -c "import aiohttp" >/dev/null 2>&1; then
  info "aiohttp: OK"
else
  warn "aiohttp NOT found — the rename shim needs it."
  missing_pkgs+=("aiohttp")
fi
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
  echo
  warn "Missing: ${missing_pkgs[*]}. This installer does NOT auto-install packages."
  warn "Install them into that interpreter, e.g.:"
  warn "    $PYTHON_ABS -m pip install --user ${missing_pkgs[*]}"
  warn "Or point PYTHON= at an interpreter that already has them (a Hermes venv"
  warn "python typically has both). Continuing to lay down files/plists anyway;"
  warn "the services will fail to start until the deps are present."
fi

# --- copy files ---------------------------------------------------------------
echo
echo "Copying files ..."
mkdir -p "$DEST/shim" "$DEST/scripts" "$STATE" "$AGENTS"
cp "$SRC_DIR/shim/shim.py"                    "$DEST/shim/shim.py"
cp "$SRC_DIR/scripts/merge-config-extras.py"  "$DEST/scripts/merge-config-extras.py"
cp "$SRC_DIR/scripts/canary.py"               "$DEST/scripts/canary.py"
chmod +x "$DEST/scripts/merge-config-extras.py" "$DEST/scripts/canary.py"
info "-> $DEST"

# --- render plists ------------------------------------------------------------
render() {
  # render <template> <dest-plist>
  sed -e "s#__HOME__#$HOME#g" -e "s#__PYTHON__#$PYTHON_ABS#g" "$1" > "$2"
}
echo
echo "Rendering + installing LaunchAgents ..."
render "$SRC_DIR/shim/com.example.vibeproxy-rename-shim.plist.template"      "$AGENTS/$SHIM_LABEL.plist"
render "$SRC_DIR/launchd/com.example.vibeproxy-config-extras.plist.template" "$AGENTS/$EXTRAS_LABEL.plist"
render "$SRC_DIR/launchd/com.example.vibeproxy-canary.plist.template"        "$AGENTS/$CANARY_LABEL.plist"
info "-> $AGENTS/$SHIM_LABEL.plist"
info "-> $AGENTS/$EXTRAS_LABEL.plist"
info "-> $AGENTS/$CANARY_LABEL.plist"

# --- bootstrap ----------------------------------------------------------------
uid="$(id -u)"
domain="gui/$uid"
boot() {
  # boot <label>: idempotent bootout+bootstrap
  local label="$1"
  launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "$domain" "$AGENTS/$label.plist"
}
echo
echo "Bootstrapping services in $domain ..."
boot "$SHIM_LABEL";   info "shim bootstrapped (KeepAlive)"
boot "$EXTRAS_LABEL"; info "config-extras watcher bootstrapped (RunAtLoad + WatchPaths)"
boot "$CANARY_LABEL"; info "canary registered (6h StartInterval)"
launchctl kickstart -k "$domain/$SHIM_LABEL" >/dev/null 2>&1 || true

# --- optional plugin symlink --------------------------------------------------
if [[ -n "${HERMES_HOME:-}" ]]; then
  echo
  target="$HERMES_HOME/plugins/model-providers/vibeproxy"
  echo "HERMES_HOME set — link the provider plugin?"
  echo "  $target -> $SRC_DIR/plugin"
  if [[ "${ASSUME_YES:-0}" == "1" ]]; then
    do_link=y
  else
    read -r -p "Create/replace this symlink? [y/N] " do_link
  fi
  case "$do_link" in
    y|Y|yes|YES)
      mkdir -p "$HERMES_HOME/plugins/model-providers"
      ln -sfn "$SRC_DIR/plugin" "$target"
      info "linked provider plugin"
      ;;
    *) info "skipped plugin symlink" ;;
  esac
else
  echo
  info "HERMES_HOME not set — skipping provider plugin symlink."
  info "To link it later: ln -sfn '$SRC_DIR/plugin' \"\$HERMES_HOME/plugins/model-providers/vibeproxy\""
fi

# --- next steps ---------------------------------------------------------------
echo
echo "Done."
echo
echo "Next:"
echo "  1. Copy config/extras.template.yaml to ~/.cli-proxy-api/extras.yaml and"
echo "     chmod 600 it. (Plain CLIProxyAPI users: paste its payload: block into"
echo "     config.yaml once and skip the watcher — you can bootout the extras +"
echo "     canary agents.)"
echo "  2. Point your Hermes vibeproxy provider base_url at http://127.0.0.1:8485/v1"
echo "     (VIBEPROXY_BASE_URL env or provider config)."
echo "  3. Verify:"
echo "       curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8485/v1/models"
echo "     Health/logs: tail -f $STATE/*.log"
echo
echo "Uninstall:"
echo "  for L in $SHIM_LABEL $EXTRAS_LABEL $CANARY_LABEL; do"
echo "    launchctl bootout $domain/\$L 2>/dev/null; rm -f $AGENTS/\$L.plist; done"
echo "  rm -rf $DEST"
