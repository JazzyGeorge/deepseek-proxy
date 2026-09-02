# DeepSeek Thinking Proxy

Local reverse proxy that fixes protocol mismatches between DeepSeek V4 and
Claude Code on DeepSeek's Anthropic-compatible endpoint.

**Architecture:** `Claude Code → http://localhost:16889 → https://api.deepseek.com/anthropic`

## What It Fixes

| # | Problem | Fix |
|---|---------|-----|
| 1 | **Classifier timeout** — non-streaming requests without a `thinking` field make DeepSeek V4 reason first, so TTFB ≈ full reasoning time (~30s) and Claude Code's security classifier times out | Inject `thinking: {type: "disabled"}` into non-streaming requests → TTFB drops to 2–3s |
| 2 | **DeepSeek-only SSE events** — thinking events in streams confused Claude Code ("Tool result missing due to internal error") | `SseFilter` strips `event: thinking` / `event: signature_delta` lines from streams. Today DeepSeek emits thinking via standard Anthropic event names, so the filter is a pass-through safety net — but it must not crash, see History |
| 3 | **`thinking: {type: "adaptive"}`** — Claude Code sends this by default; DeepSeek rejected it with 400 | Remap `adaptive` → `enabled` (DeepSeek currently accepts `adaptive` too — the remap is harmless insurance) |
| 4 | **`reasoning_effort` + `thinking: disabled`** — DeepSeek rejects the combination | Strip `reasoning_effort` when thinking is disabled |
| 5 | **Missing thinking blocks** — DeepSeek's non-streaming tool-use responses lack the `thinking` block Claude Code requires before every `tool_use` | Inject an empty `thinking` block before each `tool_use` in responses |

Transport hardening (added 2026-08-13):

- **Stream-abort guard** — once the client stream has started, any error aborts
  the connection cleanly instead of embedding a 502 inside the chunked body
- **Real exception logging** — a previous `log.exception()` call sat outside any
  `except` block and printed `NoneType: None`, hiding the crash described below
- **Transient retry** — one retry after 1.5s on connection resets, truncated
  payloads, and timeouts (skipped once a stream has started — retrying only
  fetches data for a dead connection)

## History

- **v1** — the original proxy fixed only #1 (non-streaming timeout) and retried
  transient errors. It worked fine on machines where the client and API never
  hit the other mismatches.
- **2026-08-11** — fixes #2–#5 were added for a Windows machine where Claude
  Code was hitting all of them.
- **2026-08-13** — fix #2 was found to contain a bug: `SseFilter.feed()` called
  `.encode()` on a `bytes` object, crashing on the first line of **every**
  streaming request. The proxy would send 200 headers, die mid-stream, and
  corrupt the body with a literal `HTTP/1.1 502 Bad Gateway`. Fixed, plus the
  hardening above; all five fixes verified live against DeepSeek.

Also observed 2026-08-13 (DeepSeek API is a moving target):

- DeepSeek now **accepts** `thinking.type=adaptive` directly (raw 200).
- DeepSeek no longer sends custom `event: thinking` / `event: signature_delta`
  event names — its thinking content uses standard Anthropic event names with
  Anthropic-compatible shapes (`content_block_start` →
  `{"type": "thinking", "thinking": "", "signature": ""}`,
  `delta.type=thinking_delta`/`signature_delta`).
- With `thinking: {"type": "disabled"}` DeepSeek sends text-only streams.

## Quick Start

### 1. Clone

```bash
git clone https://github.com/JazzyGeorge/deepseek-proxy
cd deepseek-proxy
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

# Install as a launchd service (auto-start at login, restarts on crash)
./install_macos.sh
```

The installer resolves your username and the real repo path itself (no
placeholders to replace), writes
`~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist`
(`KeepAlive` + `RunAtLoad`, logs into `<repo>/logs/`), bootstraps the
service into your per-user GUI launchd domain, and verifies `/health`.

Managing the service (per-user GUI domain, **no `sudo`** — run from a
Terminal.app login session, not over SSH):

```bash
launchctl print gui/$(id -u)/com.deepseek.thinking-proxy    # state (look for "state = running")
launchctl kickstart -k gui/$(id -u)/com.deepseek.thinking-proxy  # restart
launchctl bootout gui/$(id -u)/com.deepseek.thinking-proxy  # stop/unload
```

> **KeepAlive gotcha:** `KeepAlive=true` makes launchd restart the proxy
> whenever the Python process dies — killing the process does **not** stop
> the service, and a manual `python3 thinking_proxy.py &` will then fight a
> relaunched instance for port 16889. Stop a managed instance with
> `launchctl bootout` above. Before any manual test run, check what already
> holds the port: `lsof -nP -iTCP:16889 -sTCP:LISTEN`.
>
> **Modern commands:** current macOS prefers `launchctl bootstrap` /
> `bootout` over the deprecated `launchctl load -w` / `unload`, which can
> fail with `Load failed: 5: Input/output error` (see Troubleshooting).
> `install_macos.sh` uses the modern commands; if you follow older guides,
> translate them.

#### Windows

**Prerequisites:**

- Python 3.10+ — `py -m venv` (the python.org launcher) is the safest route.
  Note: the newer Microsoft Store Python (`pythoncore-*`) installs under
  `%LOCALAPPDATA%\Python` and works with NSSM; older Store Python under
  `WindowsApps` does not.
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

> If the service crashes on startup with `OSError 10048` (address in use), an
> orphaned proxy process is still holding port 16889. Kill it before starting
> the service again:
>
> ```powershell
> $p = Get-NetTCPConnection -LocalPort 16889 -ErrorAction SilentlyContinue
> if ($p) { Stop-Process -Id $p.OwningProcess -Force }
> ```

### 3. Configure Claude Code

Add an `env` block to `~/.claude/settings.json` (macOS) or set as permanent
environment variables (Windows). If the file already contains keys (e.g.
`"model"`), **merge** the block into the existing object — don't replace the
whole file with this snippet:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:16889",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS": "1"
  }
}
```

For Windows, persist these via PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "http://localhost:16889", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS", "1", "User")
```

> **Important:** `ANTHROPIC_AUTH_TOKEN` stays in the secrets file — do NOT copy
> it into `settings.json` or set it as a system-wide env var.

Claude Code reads its auth token from its **process environment at startup**,
so the key must be exported in the shell before `claude` launches. On macOS
the reliable way is a wrapper in `~/.zshrc` that sources the secrets file.
This is zsh-only: Claude Code launched from the desktop app, VS Code, or a
non-login shell bypasses `.zshrc` — in those environments export the key in
that launcher's environment instead.

```bash
claude-deepseek() {
    source ~/Secrets/Anthropic_DeepSeek.env
    export ANTHROPIC_BASE_URL="http://localhost:16889"
    command claude "$@"
}
```

Then reload and launch with the wrapper (this avoids Claude Code falling back
to its normal Anthropic login flow):

```bash
source ~/.zshrc
claude-deepseek
```

### 4. Restart Claude Code

Quit and restart (or launch via the wrapper above).

## Verifying It Works

Health check first (no auth needed):

```bash
curl http://localhost:16889/health
# → {"status": "ok", "upstream": "https://api.deepseek.com/anthropic"}
```

macOS/Linux (loads the key from the secrets file first):

```bash
source ~/Secrets/Anthropic_DeepSeek.env
AUTH="Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"
VER="anthropic-version: 2023-06-01"
CT="content-type: application/json"

# Fix #1 — non-streaming classifier test: returns in ~2-4s, not ~30s
curl -s -o /dev/null -w 'total: %{time_total}s\n' -X POST http://localhost:16889/v1/messages \
  -H "$AUTH" -H "$VER" -H "$CT" \
  -d '{"model": "claude-opus-4-6", "max_tokens": 256, "stream": false,
       "messages": [{"role": "user", "content": "Classify this bash command as SAFE or UNSAFE with a brief reason. Command: rm -rf ~/Downloads/temp-folder/*"}]}'

# Fix #3 — adaptive thinking: returns 200, not 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:16889/v1/messages \
  -H "$AUTH" -H "$VER" -H "$CT" \
  -d '{"model": "claude-opus-4-6", "max_tokens": 128, "stream": false, "thinking": {"type": "adaptive"},
       "messages": [{"role": "user", "content": "Say hello in 3 words"}]}'

# Fix #5 — tool_use: response blocks should be thinking, tool_use
curl -s -X POST http://localhost:16889/v1/messages -H "$AUTH" -H "$VER" -H "$CT" \
  -d '{"model": "claude-opus-4-6", "max_tokens": 256, "stream": false,
       "tools": [{"name": "get_weather", "description": "Get the current weather",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
       "messages": [{"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}]}' \
  | python3 -c 'import json, sys; print([b["type"] for b in json.load(sys.stdin)["content"]])'
# → ['thinking', 'tool_use']

# Fix #2 — streaming: must complete cleanly (no 502, no broken chunked body)
curl -sN -X POST http://localhost:16889/v1/messages -H "$AUTH" -H "$VER" -H "$CT" \
  -d '{"model": "claude-opus-4-6", "max_tokens": 256, "stream": true,
       "messages": [{"role": "user", "content": "Count from 1 to 5"}]}'
# Should stream message_start → content blocks → message_stop without errors
```

Inside Claude Code, `/status` should show `Auth token: ANTHROPIC_AUTH_TOKEN`
and `Anthropic base URL: http://localhost:16889`. (Claude Code may still
display Anthropic-style model names in its UI — model translation happens on
the DeepSeek compatibility layer behind the proxy, not in Claude Code.)

Windows PowerShell (reads the key from the secrets file):

```powershell
$env:ANTHROPIC_AUTH_TOKEN = (Get-Content "$env:USERPROFILE\Secrets\Anthropic_DeepSeek_env" | Select-String "ANTHROPIC_AUTH_TOKEN" | ForEach-Object { $_ -replace '.*ANTHROPIC_AUTH_TOKEN=([^ ]+).*', '$1' })

# Fix #1 — non-streaming classifier test: should return in ~2-4s, not 30s
$body = @{ model = "claude-opus-4-6"; max_tokens = 256; stream = $false;
           messages = @(@{ role = "user"; content = "Classify this bash command as SAFE or UNSAFE with a brief reason. Command: rm -rf ~/Downloads/temp-folder/*" }) } | ConvertTo-Json -Depth 4
Measure-Command { $r = Invoke-RestMethod -Uri "http://localhost:16889/v1/messages" -Method Post -Body $body -ContentType "application/json" -Headers @{"x-api-key"=$env:ANTHROPIC_AUTH_TOKEN} -TimeoutSec 60 } | Select-Object -ExpandProperty TotalSeconds

# Fix #3 — adaptive thinking: should return 200 (not 400)
$body = @{ model = "claude-opus-4-6"; max_tokens = 128; stream = $false; thinking = @{ type = "adaptive" };
           messages = @(@{ role = "user"; content = "Say hello in 3 words" }) } | ConvertTo-Json -Depth 5
$r = Invoke-RestMethod -Uri "http://localhost:16889/v1/messages" -Method Post -Body $body -ContentType "application/json" -Headers @{"x-api-key"=$env:ANTHROPIC_AUTH_TOKEN} -TimeoutSec 60
# Should be 200 with blocks: thinking, text

# Fix #5 — tool_use: response blocks should be thinking → tool_use
$body = @{ model = "claude-opus-4-6"; max_tokens = 256; stream = $false;
           tools = @(@{ name = "get_weather"; description = "Get the current weather"; input_schema = @{ type = "object"; properties = @{ city = @{ type = "string" } }; required = @("city") } });
           messages = @(@{ role = "user"; content = "What is the weather in Paris? Use the get_weather tool." }) } | ConvertTo-Json -Depth 6
$r = Invoke-RestMethod -Uri "http://localhost:16889/v1/messages" -Method Post -Body $body -ContentType "application/json" -Headers @{"x-api-key"=$env:ANTHROPIC_AUTH_TOKEN} -TimeoutSec 60
$r.content | ForEach-Object { $_.type }
# Should print: thinking, tool_use

# Fix #2 — streaming: must complete cleanly (no 502, no broken chunked body)
$body = @{ model = "claude-opus-4-6"; max_tokens = 256; stream = $true;
           messages = @(@{ role = "user"; content = "Count from 1 to 5" }) } | ConvertTo-Json -Depth 4
$body | curl.exe -sN -X POST http://localhost:16889/v1/messages -H "Content-Type: application/json" -H "x-api-key: $env:ANTHROPIC_AUTH_TOKEN" --data-binary "@-"
# Should stream message_start → content blocks → message_stop without errors
```

The streaming test logs a `<- 200 (SSE filtered, N bytes)` line — the proxy's
proof that a stream passed through the filter cleanly.

## Running the tests

```bash
python3 -m venv thinking_proxy_venv
thinking_proxy_venv/bin/pip install -r requirements.txt -r requirements-dev.txt
thinking_proxy_venv/bin/python -m pytest
```

The suite covers request normalization, the SSE filter, response fixups, the
body-size cap, and `/v1/models`. Tests redirect the log directory to a temp
dir — they never write to the real `proxy.log`.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_BASE_URL` | `https://api.deepseek.com/anthropic` | Upstream endpoint (`/v1` suffix is stripped) |
| `THINKING_PROXY_PORT` | `16889` | Local listen port (`127.0.0.1`) |
| `THINKING_PROXY_MAX_BODY_MB` | `64` | Max request body size in MB (aiohttp's default is 1 MB — too small for long sessions; see Troubleshooting) |
| `THINKING_PROXY_MODEL_ID` | `claude-opus-4-6` | Model id advertised by `GET /v1/models` — change it if you configure a different model in Claude Code |

| Endpoint | Behavior |
|----------|----------|
| `GET /health` | `{"status": "ok", "upstream": ...}` |
| `GET /v1/models` | Anthropic-native model list |
| `POST /v1/messages*` | Normalized + filtered + forwarded |
| `ANY /*` | Transparent passthrough |

## How It Works

### Request normalization (`normalize_request_body`)

- Non-streaming request without a `thinking` field → injects
  `{"thinking": {"type": "disabled"}}` (fix #1)
- `thinking.type == "adaptive"` → remaps to `"enabled"` (fix #3)
- `thinking.type == "disabled"` + `reasoning_effort` → strips
  `reasoning_effort` (fix #4)

### SSE filtering (`SseFilter`)

Parses the upstream byte stream line-by-line and drops DeepSeek-only
`event: thinking` / `event: signature_delta` events (fix #2). All other
events pass through unchanged. Since DeepSeek currently emits thinking via
standard Anthropic event names, this filter is normally a no-op safety net.

### Response fixup (`inject_missing_thinking_blocks`)

For non-streaming 200 responses containing `tool_use`, inserts an empty
`{"type": "thinking", "thinking": ""}` block before every `tool_use` that
isn't already preceded by one (fix #5).

### Stream-abort guard

If the upstream request fails after the client stream has already started,
the proxy aborts the connection instead of attempting to send a 502 inside
the chunked body. Transient-error retries are skipped in that state.

### What the proxy does NOT touch

- **Auth headers** — forwarded verbatim, never stored; logged only masked
- **Requests with a valid `thinking` config** — passed through (only the
  `adaptive` remap and the `reasoning_effort` strip apply)
- **Streamed content** — filtered line-by-line for the two event names, but
  never rewritten
- **API keys** — the proxy never reads or stores keys itself

## Files

| File | Purpose |
|------|---------|
| `thinking_proxy.py` | The proxy — cross-platform Python + aiohttp |
| `requirements.txt` | Python dependencies (`aiohttp`) |
| `requirements-dev.txt` | Test-only dependencies (`pytest`, `pytest-asyncio`) |
| `tests/test_proxy.py` | pytest suite — normalization, SSE filter, response fixups, body-size cap, `/v1/models` |
| `pytest.ini` | pytest configuration |
| `thinking_proxy_launcher.sh` | macOS launcher — sources env file, starts proxy |
| `install_macos.sh` | macOS installer — writes the LaunchAgent plist with real user/repo paths, bootstraps it (`gui/$(id -u)`), verifies `/health` |
| `uninstall_macos.sh` | macOS uninstaller — `launchctl bootout` + removes the plist |
| `launcher.bat` | Windows launcher — reads env file, starts proxy |
| `LICENSE` | MIT |

## Troubleshooting

### macOS

| Issue | Check |
|-------|-------|
| Service not running | `launchctl print gui/$(id -u)/com.deepseek.thinking-proxy` — look for `state = running` |
| Connection refused | `curl http://localhost:16889/health` |
| `launchctl load`/`bootstrap` fails with `5: Input/output error` | Use `launchctl bootstrap gui/$(id -u) <plist>` (the legacy `load -w` is deprecated), run from a **Terminal.app login session, not SSH**, and make sure the paths inside the plist exist — a stale/placeholder repo path is the classic cause. Re-running `./install_macos.sh` fixes all three |
| Killing the process doesn't stop the service | `KeepAlive=true` respawns it — stop with `launchctl bootout gui/$(id -u)/com.deepseek.thinking-proxy`, don't kill PIDs |
| Port 16889 already in use when testing manually | A launchd-managed instance may already be listening — check `lsof -nP -iTCP:16889 -sTCP:LISTEN` first |
| Auth errors | `ls ~/Secrets/Anthropic_DeepSeek.env`; the token must be exported in the shell *before* `claude` starts (see the wrapper in "Configure Claude Code") |
| Crash-loop in `<repo>/logs/stderr.log` | launchd respawns the proxy (KeepAlive) — read stderr.log first: it shows the real error. Fix, then `launchctl bootout` + re-run `./install_macos.sh` |
| Proxy log | `tail ~/.local/state/thinking-proxy/proxy.log` — request/upstream diagnostics. Distinct from launchd's `<repo>/logs/stdout.log` + `stderr.log`; check both when nothing comes up |
| «Request too large (max 32MB)» mid-session | The proxy's own body cap was hit — aiohttp's default is **1 MB**; the proxy raises it to 64 MB (`THINKING_PROXY_MAX_BODY_MB`). The CLI message names the *real* Anthropic API limit (32 MB); through a proxy it's misleading. Check `proxy.log`: a `->` forward line before the 413 means **upstream** (DeepSeek) rejected; no forward line means the proxy rejected it locally |
| `proxy.log` grows forever | Never rotated — truncate occasionally (`: > ~/.local/state/thinking-proxy/proxy.log`) or add a newsyslog config |

### Windows

| Issue | Check |
|-------|-------|
| Service not running | `& "$env:APPDATA\thinking-proxy\nssm\nssm.exe" status DeepSeekThinkingProxy` |
| Streaming requests fail / 502s | Logs should show `<- 200 (SSE filtered, N bytes)` for healthy streams and no `502` after the fix; check `%APPDATA%\thinking-proxy\logs\stdout.log` (NSSM stdout) — the Python-side log is `%LOCALAPPDATA%\thinking-proxy\logs\proxy.log` |
| `stderr.log` growing fast | Verify `PYTHON_BASIC_REPL=1` is set on the service |
| `OSError 10048` on startup | Another process holds port 16889 — kill it (see Quick Start note) |
| Store Python error | Make sure venv was created with `py -m venv`, not `python -m venv` |

## Uninstall

### macOS

```bash
./uninstall_macos.sh
```

(Manual alternative: `launchctl bootout gui/$(id -u)/com.deepseek.thinking-proxy`
then `rm ~/Library/LaunchAgents/com.deepseek.thinking-proxy.plist`.)

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
