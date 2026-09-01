"""Tests for thinking_proxy.py.

Redirects HOME/LOCALAPPDATA to a temp dir BEFORE importing the module so the
module's import-time logging FileHandler never touches the real proxy log.
"""

import json
import os
import tempfile
from contextlib import asynccontextmanager

# Must happen before `import thinking_proxy` — the module configures a
# FileHandler into ~/.local/state/thinking-proxy at import time.
_HOME = tempfile.mkdtemp(prefix="thinking-proxy-tests-")
os.environ["HOME"] = _HOME
os.environ["LOCALAPPDATA"] = os.path.join(_HOME, "Local")

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import thinking_proxy as tp  # noqa: E402


def _large_body(size: int = 1_300_000) -> bytes:
    return json.dumps(
        {"model": "x", "messages": [{"role": "user", "content": "y" * size}]}
    ).encode()


@asynccontextmanager
async def upstream_server(status: int = 200):
    """Fake upstream with a generous body cap so the proxy's own cap is the
    binding one (aiohttp's default 1 MB would 413 the forwarded body)."""
    app = web.Application(client_max_size=128 * 1024 * 1024)

    async def handler(request):
        await request.read()
        return web.Response(status=status, body=b'{"ok": true}')

    app.router.add_post("/v1/messages", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        await server.close()


@asynccontextmanager
async def proxy_client():
    app = tp.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Body-size cap (fix for the "Request too large (max 32MB)" session deaths)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_body_passes_with_default_cap(monkeypatch):
    """1.3 MB body must reach upstream with the default 64 MB cap.

    Break it catches: create_app() dropping client_max_size (aiohttp's
    default 1 MB would reject this body with a local 413 before the handler
    runs — the exact bug that killed Claude Code sessions).
    """
    monkeypatch.delenv("THINKING_PROXY_MAX_BODY_MB", raising=False)
    async with upstream_server() as url:
        monkeypatch.setattr(tp, "UPSTREAM_URL", url)
        async with proxy_client() as client:
            resp = await client.post("/v1/messages", data=_large_body())
            assert resp.status == 200


@pytest.mark.asyncio
async def test_large_body_passes_with_env_cap_override(monkeypatch):
    """THINKING_PROXY_MAX_BODY_MB=128 must raise the cap above the default.

    Break it catches: the env var being ignored — the default 1 MB cap would
    still 413 this body, so the test fails without the override working.
    """
    monkeypatch.setenv("THINKING_PROXY_MAX_BODY_MB", "128")
    async with upstream_server() as url:
        monkeypatch.setattr(tp, "UPSTREAM_URL", url)
        async with proxy_client() as client:
            resp = await client.post("/v1/messages", data=_large_body())
            assert resp.status == 200


# ---------------------------------------------------------------------------
# Upstream 413 must be distinguishable from a local rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upstream_413_forwarded_and_logged_with_source_hint(caplog, monkeypatch):
    """A 413 from upstream is forwarded, and a warning names the source.

    Break it catches: the proxy swallowing a 413, or logging it the same way
    as a success — making upstream vs. local rejection indistinguishable.
    """
    async with upstream_server(status=413) as url:
        monkeypatch.setattr(tp, "UPSTREAM_URL", url)
        async with proxy_client() as client:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "x", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            )
            assert resp.status == 413
            assert "413 from upstream" in caplog.text


# ---------------------------------------------------------------------------
# /v1/models model id (must be overridable for non-Claude model names)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_models_response_default_id(monkeypatch):
    monkeypatch.delenv("THINKING_PROXY_MODEL_ID", raising=False)
    async with proxy_client() as client:
        data = await (await client.get("/v1/models")).json()
        assert data["data"][0]["id"] == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_models_response_uses_env_model_id(monkeypatch):
    """THINKING_PROXY_MODEL_ID must change the advertised model id.

    Break it catches: handle_models ignoring the env var and always
    advertising the hardcoded Claude model id.
    """
    monkeypatch.setenv("THINKING_PROXY_MODEL_ID", "deepseek-chat")
    async with proxy_client() as client:
        data = await (await client.get("/v1/models")).json()
        assert data["data"][0]["id"] == "deepseek-chat"
        assert data["first_id"] == "deepseek-chat"


# ---------------------------------------------------------------------------
# Request normalization (existing behavior)
# ---------------------------------------------------------------------------

def test_normalize_injects_thinking_disabled_for_non_streaming():
    body = json.dumps({"model": "x", "messages": [{"role": "user", "content": "hi"}]}).encode()
    out = json.loads(tp.normalize_request_body(body))
    assert out["thinking"] == {"type": "disabled"}


def test_normalize_leaves_streaming_untouched():
    body = json.dumps({"model": "x", "stream": True, "messages": []}).encode()
    assert tp.normalize_request_body(body) == body


def test_normalize_leaves_existing_thinking_untouched():
    body = json.dumps({"model": "x", "thinking": {"type": "enabled"}, "messages": []}).encode()
    assert tp.normalize_request_body(body) == body


def test_normalize_remaps_adaptive_to_enabled():
    body = json.dumps({"model": "x", "thinking": {"type": "adaptive"}, "messages": []}).encode()
    assert json.loads(tp.normalize_request_body(body))["thinking"]["type"] == "enabled"


def test_normalize_strips_reasoning_effort_when_thinking_disabled():
    body = json.dumps({
        "model": "x", "thinking": {"type": "disabled"},
        "reasoning_effort": "high", "messages": [],
    }).encode()
    out = json.loads(tp.normalize_request_body(body))
    assert "reasoning_effort" not in out


def test_normalize_passthrough_on_invalid_json():
    body = b"not json"
    assert tp.normalize_request_body(body) is body


def test_normalize_passthrough_on_empty():
    assert tp.normalize_request_body(b"") == b""


# ---------------------------------------------------------------------------
# SSE filter (existing behavior)
# ---------------------------------------------------------------------------

def test_sse_filter_drops_deepseek_only_events():
    f = tp.SseFilter()
    chunk = (
        b'event: thinking\ndata: {"thinking": "hidden"}\n\n'
        b'event: content_block_delta\ndata: {"delta": "visible"}\n\n'
    )
    out = f.feed(chunk)
    assert b'"thinking": "hidden"' not in out
    assert b'"delta": "visible"' in out


def test_sse_filter_keeps_normal_events_verbatim():
    chunk = b'event: content_block_start\ndata: {"type": "text"}\n\n'
    assert tp.SseFilter().feed(chunk) == chunk


def test_sse_filter_buffers_partial_lines():
    f = tp.SseFilter()
    assert f.feed(b'event: thinking\nda') == b""
    out = f.feed(b'ta: x\n\nevent: message_delta\ndata: {}\n\n')
    assert b"data: x" not in out
    assert b"message_delta" in out


def test_sse_filter_flush_emits_buffered_data():
    f = tp.SseFilter()
    f.feed(b'data: tail')
    assert f.flush() == b'data: tail'


# ---------------------------------------------------------------------------
# Response thinking-block injection (existing behavior)
# ---------------------------------------------------------------------------

def test_response_gets_thinking_before_tool_use():
    body = json.dumps({
        "content": [{"type": "tool_use", "name": "x"}, {"type": "text", "text": "t"}],
    }).encode()
    out = json.loads(tp.inject_missing_thinking_blocks(body))
    assert out["content"][0] == {"type": "thinking", "thinking": ""}
    assert out["content"][1]["type"] == "tool_use"
    assert len(out["content"]) == 3


def test_response_keeps_existing_thinking_before_tool_use():
    body = json.dumps({
        "content": [{"type": "thinking", "thinking": "x"}, {"type": "tool_use", "name": "x"}],
    }).encode()
    assert tp.inject_missing_thinking_blocks(body) == body


def test_response_message_field_also_fixed():
    body = json.dumps({"message": {"content": [{"type": "tool_use", "name": "x"}]}}).encode()
    out = json.loads(tp.inject_missing_thinking_blocks(body))
    assert out["message"]["content"][0]["type"] == "thinking"


def test_response_passthrough_on_invalid_json():
    body = b"nope"
    assert tp.inject_missing_thinking_blocks(body) is body


# ---------------------------------------------------------------------------
# Auth masking (existing behavior)
# ---------------------------------------------------------------------------

def test_mask_key_long():
    assert tp.mask_key("sk-abcdefghijklmnop1234") == "sk-abcd...1234"


def test_mask_key_empty():
    assert tp.mask_key("") == "<empty>"
