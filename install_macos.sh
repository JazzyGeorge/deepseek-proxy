#!/bin/bash
# install_macos.sh — install the proxy as a macOS LaunchAgent (launchd service).
#
# Resolves the current user and the real repo path itself, so the generated
# plist never contains placeholder usernames or stale paths — the classic
# cause of "Load failed: 5: Input/output error" on a second machine.
#
# Uses the modern per-user GUI launchd domain (no sudo, no deprecated
# `launchctl load`). Re-runnable: any previously loaded instance (including
# one loaded the legacy way) is booted out first.
#
# Usage:
#   ./install_macos.sh
#
# Prerequisites (see README "Quick Start"):
#   - venv at <repo>/thinking_proxy_venv with requirements.txt installed
#   - ~/Secrets/Anthropic_DeepSeek.env containing:
#       export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key
#   - run from a Terminal.app login session, not over SSH

set -euo pipefail

LABEL="com.deepseek.thinking-proxy"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"
PORT="${THINKING_PROXY_PORT:-16889}"

# --- Prerequisites -----------------------------------------------------------

if [ ! -f "$REPO_DIR/thinking_proxy_launcher.sh" ]; then
    echo "ERROR: thinking_proxy_launcher.sh not found next to this script." >&2
    echo "       Run install_macos.sh from the repo root." >&2
    exit 1
fi

if [ ! -x "$REPO_DIR/thinking_proxy_venv/bin/python3" ]; then
    echo "ERROR: venv not found at $REPO_DIR/thinking_proxy_venv." >&2
    echo "       Create it first:" >&2
    echo "         python3 -m venv thinking_proxy_venv" >&2
    echo "         thinking_proxy_venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [ ! -f "$HOME/Secrets/Anthropic_DeepSeek.env" ]; then
    echo "ERROR: $HOME/Secrets/Anthropic_DeepSeek.env not found." >&2
    echo "       Create it with your DeepSeek API key:" >&2
    echo "         mkdir -p ~/Secrets" >&2
    echo "         echo 'export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key' > ~/Secrets/Anthropic_DeepSeek.env" >&2
    exit 1
fi

if ! launchctl print "$GUI_DOMAIN" >/dev/null 2>&1; then
    echo "ERROR: the per-user GUI domain ($GUI_DOMAIN) is not reachable." >&2
    echo "       Run this from a Terminal.app login session, not over SSH." >&2
    exit 1
fi

# --- Generate the plist ------------------------------------------------------

mkdir -p "$PLIST_DIR" "$REPO_DIR/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO_DIR/thinking_proxy_launcher.sh</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/stderr.log</string>
</dict>
</plist>
EOF

if ! plutil -lint "$PLIST"; then
    echo "ERROR: generated plist failed validation — not loading it." >&2
    exit 1
fi

# --- Load it -----------------------------------------------------------------

# Kick any previously loaded instance (loaded by this script earlier or the
# legacy way with `launchctl load -w`) so bootstrap does not fail with
# "service already loaded".
launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true

if ! launchctl bootstrap "$GUI_DOMAIN" "$PLIST"; then
    echo "ERROR: launchctl bootstrap failed." >&2
    echo "       Likely causes: running over SSH or outside a GUI session," >&2
    echo "       or a path problem in $PLIST." >&2
    echo "       Fix and re-run this script." >&2
    exit 1
fi

# --- Verify ------------------------------------------------------------------

echo "Waiting for the proxy to come up on port $PORT ..."
up=0
for _ in 1 2 3 4 5; do
    if curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; then
        up=1
        break
    fi
    sleep 1
done

if [ "$up" -eq 1 ]; then
    echo "OK — http://localhost:$PORT/health -> $(curl -fsS "http://localhost:$PORT/health")"
else
    echo "WARNING: the service is loaded but not answering yet." >&2
    echo "         Check:" >&2
    echo "           launchctl print $GUI_DOMAIN/$LABEL" >&2
    echo "           tail $REPO_DIR/logs/stderr.log   (launchd)" >&2
    echo "           tail ~/.local/state/thinking-proxy/proxy.log   (proxy)" >&2
fi

echo
echo "Installed $LABEL:"
echo "  plist:     $PLIST"
echo "  logs:      $REPO_DIR/logs/stdout.log, $REPO_DIR/logs/stderr.log (launchd)"
echo "             ~/.local/state/thinking-proxy/proxy.log (proxy)"
echo "  restart:   launchctl kickstart -k $GUI_DOMAIN/$LABEL"
echo "  stop:      launchctl bootout $GUI_DOMAIN/$LABEL"
echo "  inspect:   launchctl print $GUI_DOMAIN/$LABEL"
echo "  health:    curl http://localhost:$PORT/health"
echo "  uninstall: ./uninstall_macos.sh"
