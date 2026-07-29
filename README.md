# DeepSeek Thinking Proxy

Fixes Claude Code's security-classifier timeout when using DeepSeek V4 via the
Anthropic-compatible API. A local loopback proxy that disables reasoning on
non-streaming requests and retries on transient connection errors.

**Architecture:** `Claude Code → http://localhost:16889 → https://api.deepseek.com/anthropic`

## The Problem

Claude Code's built-in security classifier sends **non-streaming** requests to
DeepSeek's Anthropic-compatible endpoint. DeepSeek V4 is a reasoning model — when
no `thinking` field is present in a non-streaming request, it defaults to thinking
ON. The time-to-first-byte equals the full reasoning duration.

At realistic payload sizes (~2.9K input tokens), response time hits **28–32 seconds**,
exceeding Claude Code's ~30s classifier timeout. The command is blocked and retried
endlessly.

Additionally, DeepSeek sporadically drops connections on large payloads
(`ConnectionResetError`, `TransferEncodingError`), returning malformed responses.

**Upstream issue:** [deepseek-ai/DeepSeek-V3#1464](https://github.com/deepseek-ai/DeepSeek-V3/issues/1464)

## The Fix

This proxy sits between Claude Code and DeepSeek. For every non-streaming request:

1. **Injects `thinking: {type: "disabled"}`** — TTFB drops from 30s → 2-3s
2. **Retries once on transient errors** — catches connection resets, truncated
   responses, and connection timeouts with a 1.5s delay

Streaming requests pass through unchanged (thinking stays enabled for normal chat,
which is correct).

## Quick Start

### 1. Clone and set up the proxy

```bash
git clone https://github.com/JazzyGeorge/deepseek-proxy
cd deepseek-thinking-proxy
```

### 2. Install

#### macOS

```bash
# Create venv
python3 -m venv thinking_proxy_venv
thinking_proxy_venv/bin/pip install -r requirements.txt

# Create env file with your DeepSeek API key
mkdir -p ~/Secrets
echo 'export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key' > ~/Secrets/Anthropic_DeepSeek.env

# Test manually
source ~/Secrets/Anthropic_DeepSeek.env
./thinking_proxy_venv/bin/python3 thinking_proxy.py &
sleep 2
curl http://localhost:16889/health
# → {"status": "ok", "upstream": "https://api.deepseek.com/anthropic"}
kill %1

# Install as a launchd service (auto-start at login)
# Replace <username> with your macOS username (run `whoami` to find it)
cat > ~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.deepseek.thinking-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/<username>/deepseek-thinking-proxy/thinking_proxy_launcher.sh</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/<username>/deepseek-thinking-proxy/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/<username>/deepseek-thinking-proxy/logs/stderr.log</string>
</dict>
</plist>
EOF

mkdir -p logs
launchctl load -w ~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist
```

#### Windows

**Prerequisites:**
- Python 3.10+ from [python.org](https://www.python.org/downloads/) (NOT the Microsoft Store version — Store Python runs from a sandboxed `WindowsApps` directory that NSSM cannot launch)
- Administrator PowerShell

```powershell
$PROXY_DIR = "$env:APPDATA\thinking-proxy"
New-Item -ItemType Directory -Force -Path $PROXY_DIR, "$PROXY_DIR\logs" | Out-Null

# Create venv using py (the python.org launcher — avoids Store Python)
py -m venv "$PROXY_DIR\venv"
& "$PROXY_DIR\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$PROXY_DIR\venv\Scripts\pip.exe" install -r requirements.txt

# Copy proxy script and launcher from the repo to the service directory
Copy-Item thinking_proxy.py "$PROXY_DIR\"
Copy-Item launcher.bat "$PROXY_DIR\"

# Create env file with your DeepSeek API key
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\Secrets" | Out-Null
Set-Content -Path "$env:USERPROFILE\Secrets\Anthropic_DeepSeek_env" -Value "export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key"

# Test manually before installing the service
& "$PROXY_DIR\venv\Scripts\python.exe" "$PROXY_DIR\thinking_proxy.py"
# Should print: starting thinking-proxy on 127.0.0.1:16889 → ...
# Press Ctrl+C to stop

# Download NSSM and create Windows Service
$NSSM_DIR = "$PROXY_DIR\nssm"
New-Item -ItemType Directory -Force -Path $NSSM_DIR | Out-Null
Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile "$env:TEMP\nssm.zip"
Expand-Archive -Path "$env:TEMP\nssm.zip" -DestinationPath $NSSM_DIR -Force
$found = Get-ChildItem -Path $NSSM_DIR -Recurse -Filter "nssm.exe" | Select-Object -First 1
Copy-Item $found.FullName "$NSSM_DIR\nssm.exe"

# Install service
& "$NSSM_DIR\nssm.exe" install DeepSeekThinkingProxy "$PROXY_DIR\venv\Scripts\python.exe" "$PROXY_DIR\thinking_proxy.py"
& "$NSSM_DIR\nssm.exe" set DeepSeekThinkingProxy AppDirectory "$PROXY_DIR"
& "$NSSM_DIR\nssm.exe" set DeepSeekThinkingProxy AppStdout "$PROXY_DIR\logs\stdout.log"
& "$NSSM_DIR\nssm.exe" set DeepSeekThinkingProxy AppStderr "$PROXY_DIR\logs\stderr.log"
& "$NSSM_DIR\nssm.exe" set DeepSeekThinkingProxy AppRestartDelay 10000
& "$NSSM_DIR\nssm.exe" set DeepSeekThinkingProxy AppEnvironmentExtra "PYTHON_BASIC_REPL=1"
& "$NSSM_DIR\nssm.exe" start DeepSeekThinkingProxy

# Verify
Start-Sleep -Seconds 3
Invoke-RestMethod -Uri "http://localhost:16889/health" -TimeoutSec 5
```

### 3. Configure Claude Code

Add to `~/.claude/settings.json` (macOS) or set as permanent environment
variables (Windows):

```json
"env": {
  "ANTHROPIC_BASE_URL": "http://localhost:16889",
  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
  "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS": "1"
}
```

For Windows, persist these via PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "http://localhost:16889", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS", "1", "User")
```

> **Important:** `ANTHROPIC_AUTH_TOKEN` stays in the secrets file — do NOT copy it
> into `settings.json` or set it as a system-wide env var.

### 4. Restart Claude Code

Quit and restart. Run any Bash command — the security classifier should work
without timeout.

## Verifying It Works

```bash
# Health check
curl http://localhost:16889/health
# → {"status": "ok", "upstream": "https://api.deepseek.com/anthropic"}

# Non-streaming classifier test (should return in ~2-4s, not 30s)
curl -s http://localhost:16889/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: $ANTHROPIC_AUTH_TOKEN" \
  -d '{
    "model": "deepseek-v4-pro",
    "max_tokens": 256,
    "stream": false,
    "messages": [{
      "role": "user",
      "content": "Classify this bash command as SAFE or UNSAFE with a brief reason. Command: rm -rf ~/Downloads/temp-folder/*"
    }]
  }'
# Should return SAFE/UNSAFE verdict in ~2-4 seconds
```

## Files

| File | Purpose |
|------|---------|
| `thinking_proxy.py` | The proxy — cross-platform Python + aiohttp |
| `requirements.txt` | Python dependencies (`aiohttp`) |
| `thinking_proxy_launcher.sh` | macOS launcher — sources env file, starts proxy |
| `launcher.bat` | Windows launcher — reads env file, starts proxy |
| `LICENSE` | MIT |

## How It Works

### Non-streaming timeout fix

Claude Code's security classifier sends `POST /v1/messages` with `stream: false`
and no `thinking` field. DeepSeek V4 defaults to thinking ON → TTFB = full
reasoning time → 30s timeout. The proxy detects these requests and injects
`{"thinking": {"type": "disabled"}}` into the JSON body.

### Transient error retry

DeepSeek occasionally drops connections mid-response on large payloads (150KB+).
The proxy catches `ConnectionResetError`, `ClientPayloadError`, `ClientConnectorError`,
and `TimeoutError`, then retries once after a 1.5s delay.

### Python 3.13+ `_pyrepl` guard

Python 3.13+ ships a new REPL (`_pyrepl`) that queries console dimensions on
startup. When running as a Windows Service (or under launchd), there is no
console → `GetConsoleScreenBufferInfo()` fails → recursive traceback cascade.
The proxy sets `PYTHON_BASIC_REPL=1` before importing anything that touches the
exception display machinery.

### What the proxy does NOT touch

- **Streaming requests** (`stream: true`) pass through unchanged
- **Requests that already have a `thinking` field** — never overridden
- **Auth headers** — forwarded verbatim, never logged in full (only masked)

## Troubleshooting

### macOS

| Issue | Check |
|-------|-------|
| Proxy not running | `launchctl list \| grep thinking` |
| Connection refused | `curl http://localhost:16889/health` |
| Auth errors | `ls ~/Secrets/Anthropic_DeepSeek.env` |
| Logs | `tail ~/deepseek-thinking-proxy/logs/proxy.log` |

### Windows

| Issue | Check |
|-------|-------|
| Service not running | `& "$env:APPDATA\thinking-proxy\nssm\nssm.exe" status DeepSeekThinkingProxy` |
| `stderr.log` growing fast | Verify `PYTHON_BASIC_REPL=1` is set on the service |
| Store Python error | Make sure venv was created with `py -m venv`, not `python -m venv` |
| Logs | `%APPDATA%\thinking-proxy\logs\` |

## Uninstall

### macOS

```bash
launchctl unload ~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist
rm ~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist
```

### Windows

```powershell
$NSSM = "$env:APPDATA\thinking-proxy\nssm\nssm.exe"
& $NSSM stop DeepSeekThinkingProxy 2>$null
& $NSSM remove DeepSeekThinkingProxy confirm 2>$null
Remove-Item -Recurse -Force "$env:APPDATA\thinking-proxy"
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")
```

## License

MIT — see [LICENSE](LICENSE) file.

## References

- [deepseek-ai/DeepSeek-V3#1464](https://github.com/deepseek-ai/DeepSeek-V3/issues/1464) — Original issue
- [deepseek.com API docs](https://api-docs.deepseek.com/) — Anthropic-compatible endpoint
