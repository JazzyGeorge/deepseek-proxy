#!/bin/bash
# uninstall_macos.sh — stop and remove the proxy LaunchAgent.
#
# Usage:
#   ./uninstall_macos.sh

set -euo pipefail

LABEL="com.deepseek.thinking-proxy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"

if launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Stopped and unloaded $LABEL."
else
    echo "$LABEL was not loaded — nothing to stop."
fi

if [ -f "$PLIST" ]; then
    rm "$PLIST"
    echo "Removed $PLIST"
else
    echo "No plist at $PLIST."
fi

echo
echo "Optional cleanup:"
echo "  rm -rf <repo>/thinking_proxy_venv <repo>/logs"
echo "  rm ~/Secrets/Anthropic_DeepSeek.env"
echo "  Remove the \"env\" block from ~/.claude/settings.json and the"
echo "  claude-deepseek wrapper from ~/.zshrc (see README)"
