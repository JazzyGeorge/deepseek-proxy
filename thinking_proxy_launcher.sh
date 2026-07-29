#!/bin/bash
# Launcher for thinking_proxy.py — sources secrets from env file before starting.
# Used by launchd (macOS) or manually for testing.
#
# No secrets are stored in this file — they all come from the env file.
#
# Usage:
#   ./thinking_proxy_launcher.sh
#
# The launcher expects an env file at ~/Secrets/Anthropic_DeepSeek.env
# containing: export ANTHROPIC_AUTH_TOKEN=sk-your-key

ENV_FILE="$HOME/Secrets/Anthropic_DeepSeek.env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "WARNING: env file not found at $ENV_FILE" >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/thinking_proxy_venv/bin/python3"

if [ -x "$VENV_PYTHON" ]; then
    exec "$VENV_PYTHON" "$SCRIPT_DIR/thinking_proxy.py"
else
    # Fall back to system python3 if no venv
    exec python3 "$SCRIPT_DIR/thinking_proxy.py"
fi
