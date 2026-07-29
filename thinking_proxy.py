#!/usr/bin/env python3
"""
Local reverse proxy that injects `thinking: {type: "disabled"}` into
non-streaming requests to DeepSeek's Anthropic-compatible endpoint.

Fixes the ~30s TTFB timeout when Claude Code's security classifier sends
non-streaming requests without a `thinking` field — DeepSeek V4 defaults to
thinking ON, causing the classifier to time out.

Usage:
    ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
    python3 thinking_proxy.py

The proxy reads the upstream URL from the ANTHROPIC_BASE_URL env var
(typically set by a launcher script that sources the user's secrets file).
Auth is forwarded from incoming requests — the proxy never stores or logs
API keys.

Endpoints:
    POST /v1/messages*  — inject thinking:disabled if non-streaming
    GET  /v1/models     — Anthropic-native model list
    GET  /health        — {"status": "ok"}
    ANY  /*             — transparent passthrough
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Prevent _pyrepl from loading in non-interactive contexts (launchd service).
# Python 3.13+'s new REPL calls terminal ioctls on missing console → OSError.
# PYTHON_BASIC_REPL tells site.py to skip _pyrepl entirely.
# Set BEFORE any other imports that might trigger exception formatting.
if not sys.stdin.isatty():
    os.environ.setdefault("PYTHON_BASIC_REPL", "1")

from aiohttp import ClientSession, web
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientPayloadError,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("THINKING_PROXY_PORT", "16889"))
UPSTREAM_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")

# Ensure we don't double up paths — strip trailing /v1 if present
if UPSTREAM_URL.endswith("/v1"):
    UPSTREAM_URL = UPSTREAM_URL[:-3]

# Retry settings for transient upstream errors
RETRY_DELAY = 1.5  # seconds between retries
MAX_RETRIES = 1    # one retry → two total attempts
TRANSIENT_ERRORS = (
    ClientConnectorError,    # cannot connect to upstream
    ClientPayloadError,      # truncated/incomplete response body
    ConnectionResetError,    # upstream reset the connection
    TimeoutError,            # request timed out
)

# Hop-by-hop headers that must be stripped before forwarding
HOP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
    "accept-encoding",  # Strip client Accept-Encoding
    "content-encoding",  # Strip response Content-Encoding
}

# Logging — stdout for launchd, stderr for errors. No API keys ever logged.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_headers(headers: dict) -> dict:
    """Strip hop-by-hop headers and return clean forwarding headers."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_HEADERS
    }


def mask_key(value: str) -> str:
    """Return a masked version of an auth header for logging."""
    if not value:
        return "<empty>"
    if len(value) < 12:
        return value[:3] + "***"
    return value[:7] + "..." + value[-4:]


def extract_auth_headers(headers: dict) -> dict:
    """Extract only auth-relevant headers from incoming request."""
    auth = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("x-api-key", "authorization"):
            auth[k] = v
    return auth


def inject_thinking_disabled(body_bytes: bytes) -> bytes:
    """Inject ``{"thinking": {"type": "disabled"}}`` into a JSON request body
    if it is non-streaming and has no existing ``thinking`` field.

    Returns the original bytes if no modification is needed.
    """
    if not body_bytes:
        return body_bytes

    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return body_bytes  # not valid JSON, pass through unchanged

    if not isinstance(data, dict):
        return body_bytes

    # Streaming requests should keep thinking enabled for normal chat
    if data.get("stream") is True:
        return body_bytes

    # Already has a thinking field — don't override
    if "thinking" in data:
        return body_bytes

    # Non-streaming, no thinking field → inject disabled
    data["thinking"] = {"type": "disabled"}
    log.info("injected thinking:disabled into non-streaming request")
    return json.dumps(data).encode("utf-8")


# ---------------------------------------------------------------------------
# Endpoint: GET /v1/models
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
    """Return Anthropic-native model list for Claude Code startup validation."""
    return web.json_response(MODELS_RESPONSE)


# ---------------------------------------------------------------------------
# Endpoint: GET /health
# ---------------------------------------------------------------------------


async def handle_health(request: web.Request) -> web.Response:
    """Health check — confirms proxy is alive."""
    return web.json_response({"status": "ok", "upstream": UPSTREAM_URL})


# ---------------------------------------------------------------------------
# Main proxy handler
# ---------------------------------------------------------------------------


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """Forward the request to upstream, injecting thinking:disabled if needed."""
    path = request.path
    method = request.method

    # Read and optionally modify request body
    body = await request.read()

    if method == "POST" and path.startswith("/v1/messages") and body:
        body = inject_thinking_disabled(body)

    # Build forwarding headers — include auth from the incoming request
    fwd_headers = clean_headers(dict(request.headers))
    fwd_headers["host"] = UPSTREAM_URL.split("://", 1)[1].split("/", 1)[0]

    upstream_url = f"{UPSTREAM_URL}{path}"
    if request.query_string:
        upstream_url = f"{upstream_url}?{request.query_string}"

    # Log the request (mask auth)
    auth_headers = extract_auth_headers(dict(request.headers))
    auth_info = ",".join(f"{k}:{mask_key(v)}" for k, v in auth_headers.items()) or "none"
    log.info("→ %s %s [auth: %s] [body: %d bytes]", method, path, auth_info, len(body))

    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with ClientSession() as session:
                async with session.request(
                    method=method,
                    url=upstream_url,
                    headers=fwd_headers,
                    data=body,
                ) as upstream:
                    content_type = upstream.headers.get("Content-Type", "")

                    if "text/event-stream" in content_type:
                        # --- Streaming SSE path ---
                        resp = web.StreamResponse(
                            status=upstream.status,
                            headers={k: v for k, v in upstream.headers.items()
                                     if k.lower() not in HOP_HEADERS},
                        )
                        resp.headers["Cache-Control"] = "no-cache"
                        resp.headers["X-Accel-Buffering"] = "no"
                        await resp.prepare(request)

                        byte_count = 0
                        async for chunk in upstream.content.iter_any():
                            if chunk:
                                await resp.write(chunk)
                                byte_count += len(chunk)

                        await resp.write_eof()
                        log.info("← %d (SSE streamed, %d bytes)", upstream.status, byte_count)
                        return resp
                    else:
                        # --- Non-streaming path ---
                        resp_body = await upstream.read()
                        log.info("← %d (%d bytes)", upstream.status, len(resp_body))
                        return web.Response(
                            status=upstream.status,
                            headers={k: v for k, v in upstream.headers.items()
                                     if k.lower() not in HOP_HEADERS},
                            body=resp_body,
                        )
        except TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                log.warning(
                    "upstream transient error (attempt %d/%d): %s: %s — retrying in %.1fs",
                    attempt + 1, MAX_RETRIES + 1,
                    type(exc).__name__, exc,
                    RETRY_DELAY,
                )
                await asyncio.sleep(RETRY_DELAY)
        except Exception as exc:
            last_exc = exc
            break  # non-transient — don't retry

    # All retries exhausted or non-transient error
    log.exception(
        "upstream request failed after %d attempt(s): %s %s",
        MAX_RETRIES + 1, method, path,
    )
    return web.json_response(
        {"error": "upstream request failed", "detail": str(last_exc)},
        status=502,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    app = web.Application()

    # Specific endpoints
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)

    # Catch-all proxy — must be last
    app.router.add_route("*", "/{tail:.*}", proxy_handler)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("starting thinking-proxy on %s:%s → %s", LISTEN_HOST, LISTEN_PORT, UPSTREAM_URL)
    app = create_app()
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=log.info)


if __name__ == "__main__":
    main()
