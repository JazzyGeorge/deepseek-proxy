#!/usr/bin/env python3
"""
Local reverse proxy that fixes DeepSeek V4 ↔ Claude Code protocol mismatches.

Fixes:
  1. Non-streaming timeout     — inject thinking:disabled → TTFB 30s→2s
  2. SSE thinking/signature    — strip DeepSeek-only events that break Claude Code
  3. thinking.type=adaptive    — remap to "enabled" (DeepSeek rejects "adaptive")
  4. Missing thinking blocks   — inject empty thinking before tool_use in responses
  5. reasoning_effort conflict — strip when thinking=disabled (DeepSeek 400s)

Architecture:  Claude Code → http://localhost:16889 → https://api.deepseek.com/anthropic
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

if not sys.stdin.isatty():
    os.environ.setdefault("PYTHON_BASIC_REPL", "1")

from aiohttp import ClientSession, web
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientPayloadError,
)

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("THINKING_PROXY_PORT", "16889"))
UPSTREAM_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
if UPSTREAM_URL.endswith("/v1"):
    UPSTREAM_URL = UPSTREAM_URL[:-3]

RETRY_DELAY = 1.5
MAX_RETRIES = 1
TRANSIENT_ERRORS = (
    ClientConnectorError,
    ClientPayloadError,
    ConnectionResetError,
    TimeoutError,
)

HOP_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade", "accept-encoding", "content-encoding",
}

DEEPSEEK_ONLY_SSE_EVENTS = {"thinking", "signature_delta"}

LOG_DIR = None
if sys.platform == "darwin":
    LOG_DIR = Path.home() / ".local" / "state" / "thinking-proxy"
elif sys.platform == "win32":
    LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "thinking-proxy" / "logs"

if LOG_DIR:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_DIR / "proxy.log")) if LOG_DIR else logging.NullHandler(),
    ],
)
log = logging.getLogger("thinking-proxy")


def clean_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}


def mask_key(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) < 12:
        return value[:3] + "***"
    return value[:7] + "..." + value[-4:]


def extract_auth_headers(headers: dict) -> dict:
    auth = {}
    for k, v in headers.items():
        if k.lower() in ("x-api-key", "authorization"):
            auth[k] = v
    return auth


# ---------------------------------------------------------------------------
# Fix 1: Request normalization
# ---------------------------------------------------------------------------

def normalize_request_body(body_bytes: bytes) -> bytes:
    if not body_bytes:
        return body_bytes
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return body_bytes
    if not isinstance(data, dict):
        return body_bytes

    modified = False

    if "thinking" in data:
        thinking = data["thinking"]
        if isinstance(thinking, dict):
            ttype = thinking.get("type")
            # Fix 1a: adaptive -> enabled
            if ttype == "adaptive":
                thinking["type"] = "enabled"
                modified = True
                log.info("remapped thinking.type adaptive -> enabled")
            # Fix 1b: strip reasoning_effort when thinking=disabled
            if ttype == "disabled" and "reasoning_effort" in data:
                del data["reasoning_effort"]
                modified = True
                log.info("stripped reasoning_effort (incompatible with thinking=disabled)")
    elif data.get("stream") is not True:
        # Fix 1c: inject thinking:disabled for non-streaming
        data["thinking"] = {"type": "disabled"}
        modified = True
        log.info("injected thinking:disabled into non-streaming request")

    if modified:
        return json.dumps(data).encode("utf-8")
    return body_bytes


# ---------------------------------------------------------------------------
# Fix 2: SSE event filter
# ---------------------------------------------------------------------------

class SseFilter:
    def __init__(self):
        self._buffer = b""
        self._skip_until_blank = False

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        output = bytearray()
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line_str = line.decode("utf-8", errors="replace")
            if line_str.strip() == "":
                self._skip_until_blank = False
                output.extend(b"\n")
                continue
            if line_str.startswith("event: "):
                event_type = line_str[7:].strip()
                if event_type in DEEPSEEK_ONLY_SSE_EVENTS:
                    self._skip_until_blank = True
                    continue
            if not self._skip_until_blank:
                output.extend(line)  # line is already bytes
                output.extend(b"\n")
        return bytes(output)

    def flush(self) -> bytes:
        if self._buffer and not self._skip_until_blank:
            remaining = self._buffer
            self._buffer = b""
            return remaining
        self._buffer = b""
        return b""


# ---------------------------------------------------------------------------
# Fix 3: Missing thinking block injection
# ---------------------------------------------------------------------------

def inject_missing_thinking_blocks(resp_body: bytes) -> bytes:
    if not resp_body:
        return resp_body
    try:
        data = json.loads(resp_body.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return resp_body
    if not isinstance(data, dict):
        return resp_body

    modified = False
    if "content" in data and isinstance(data["content"], list):
        modified = _fix_content_blocks(data["content"]) or modified
    if "message" in data and isinstance(data["message"], dict):
        msg = data["message"]
        if "content" in msg and isinstance(msg["content"], list):
            modified = _fix_content_blocks(msg["content"]) or modified

    if modified:
        log.info("injected missing thinking block(s) into response")
        return json.dumps(data).encode("utf-8")
    return resp_body


def _fix_content_blocks(blocks: list) -> bool:
    modified = False
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if isinstance(block, dict) and block.get("type") == "tool_use":
            prev_is_thinking = (
                i > 0
                and isinstance(blocks[i - 1], dict)
                and blocks[i - 1].get("type") == "thinking"
            )
            if not prev_is_thinking:
                blocks.insert(i, {"type": "thinking", "thinking": ""})
                modified = True
                i += 1
        i += 1
    return modified


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

MODELS_RESPONSE = {
    "data": [
        {
            "type": "model",
            "id": "claude-opus-4-6",
            "display_name": "Claude Opus 4.6",
            "created_at": "2025-05-14T00:00:00Z",
        }
    ],
    "has_more": False,
    "first_id": "claude-opus-4-6",
    "last_id": "claude-opus-4-6",
}


async def handle_models(request: web.Request) -> web.Response:
    return web.json_response(MODELS_RESPONSE)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "upstream": UPSTREAM_URL})


# ---------------------------------------------------------------------------
# Main proxy handler
# ---------------------------------------------------------------------------

async def proxy_handler(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    body = await request.read()

    if method == "POST" and path.startswith("/v1/messages") and body:
        body = normalize_request_body(body)

    fwd_headers = clean_headers(dict(request.headers))
    fwd_headers["host"] = UPSTREAM_URL.split("://", 1)[1].split("/", 1)[0]

    upstream_url = f"{UPSTREAM_URL}{path}"
    if request.query_string:
        upstream_url = f"{upstream_url}?{request.query_string}"

    auth_headers = extract_auth_headers(dict(request.headers))
    auth_info = ",".join(f"{k}:{mask_key(v)}" for k, v in auth_headers.items()) or "none"
    log.info("-> %s %s [auth: %s] [body: %d bytes]", method, path, auth_info, len(body))

    last_exc = None
    stream_started = False
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with ClientSession() as session:
                async with session.request(
                    method=method, url=upstream_url,
                    headers=fwd_headers, data=body,
                ) as upstream:
                    content_type = upstream.headers.get("Content-Type", "")

                    if "text/event-stream" in content_type:
                        resp = web.StreamResponse(
                            status=upstream.status,
                            headers={k: v for k, v in upstream.headers.items()
                                     if k.lower() not in HOP_HEADERS},
                        )
                        resp.headers["Cache-Control"] = "no-cache"
                        resp.headers["X-Accel-Buffering"] = "no"
                        await resp.prepare(request)
                        stream_started = True

                        sse_filter = SseFilter()
                        byte_count = 0
                        async for chunk in upstream.content.iter_any():
                            if chunk:
                                filtered = sse_filter.feed(chunk)
                                if filtered:
                                    await resp.write(filtered)
                                    byte_count += len(filtered)

                        remaining = sse_filter.flush()
                        if remaining:
                            await resp.write(remaining)
                            byte_count += len(remaining)

                        await resp.write_eof()
                        log.info("<- %d (SSE filtered, %d bytes)", upstream.status, byte_count)
                        return resp
                    else:
                        resp_body = await upstream.read()

                        if upstream.status == 200 and b"tool_use" in resp_body:
                            fixed_body = inject_missing_thinking_blocks(resp_body)
                            if fixed_body != resp_body:
                                resp_body = fixed_body

                        log.info("<- %d (%d bytes)", upstream.status, len(resp_body))
                        return web.Response(
                            status=upstream.status,
                            headers={k: v for k, v in upstream.headers.items()
                                     if k.lower() not in HOP_HEADERS},
                            body=resp_body,
                        )
        except TRANSIENT_ERRORS as exc:
            last_exc = exc
            if stream_started:
                # The client stream has already begun — a clean 502 is impossible
                # and retrying would only fetch data for a dead connection.
                break
            if attempt < MAX_RETRIES:
                log.warning(
                    "upstream transient error (attempt %d/%d): %s: %s — retrying in %.1fs",
                    attempt + 1, MAX_RETRIES + 1, type(exc).__name__, exc, RETRY_DELAY,
                )
                await asyncio.sleep(RETRY_DELAY)
        except Exception as exc:
            last_exc = exc
            log.exception("non-transient upstream error on %s %s", method, path)
            break

    if stream_started:
        # Can't send a 502 once the response is streaming — abort so the client
        # sees a clean connection failure instead of a 502 embedded in the
        # chunked body.
        raise last_exc

    log.error(
        "upstream request failed after %d attempt(s): %s %s — %s: %s",
        MAX_RETRIES + 1, method, path, type(last_exc).__name__, last_exc,
    )
    return web.json_response(
        {"error": "upstream request failed", "detail": str(last_exc)},
        status=502,
    )


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    return app


def main() -> None:
    log.info("starting thinking-proxy on %s:%s -> %s", LISTEN_HOST, LISTEN_PORT, UPSTREAM_URL)
    app = create_app()
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=log.info)


if __name__ == "__main__":
    main()