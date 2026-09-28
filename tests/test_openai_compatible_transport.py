"""Mocked transport checks; no request reaches a real AI provider."""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
import aiohttp

from utils import openai_compatible, openrouter


class FakeResponse:
    def __init__(self, status: int, body: bytes, *, chunks: list[bytes] | None = None):
        self.status = status
        self.read_count = 0
        self.content = self
        self.body = body
        self.chunks = chunks

    async def iter_chunked(self, _limit: int):
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            self.read_count += 1
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, response: FakeResponse, calls: list, *, timeout, trust_env, connector):
        self.response = response
        self.calls = calls
        assert timeout is openai_compatible.DEFAULT_TIMEOUT
        assert trust_env is False
        assert isinstance(connector._resolver, openai_compatible._ValidatedResolver)
        assert connector._use_dns_cache is False
        self.connector = connector

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.connector.close()
        return None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.parametrize("url", [
    "http://provider.example/v1", "https://user:pass@provider.example/v1",
    "https://provider.example/v1?key=secret", "https://provider.example/v1#fragment",
    "https://127.0.0.1/v1", "https://192.168.1.10/v1", "https://224.0.0.1/v1",
    "https://foo.localhost/v1", "https://internal/v1",
    "https://provider.example/v1/chat/completions", "https://provider.example:invalid/v1",
    "https://provider.example/v1?", "https://provider.example/v1#",
])
def test_rejects_unsafe_provider_base_urls(url):
    with pytest.raises(ValueError):
        openai_compatible.validate_base_url(url)


def test_normalizes_remote_url_and_explicit_local_http():
    assert openai_compatible.validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert openai_compatible.validate_base_url(
        "http://127.0.0.1:8188/v1/", allow_loopback_http=True,
    ) == "http://127.0.0.1:8188/v1"
    with pytest.raises(ValueError):
        openai_compatible.validate_base_url("http://127.0.0.1:8188/v1")
    with pytest.raises(ValueError):
        openai_compatible.validate_base_url("http://127.0.0.2:8188/v1", allow_loopback_http=True)


@pytest.mark.parametrize("addresses", [
    ["10.0.0.3"], ["127.0.0.1"], ["169.254.1.2"],
    ["192.168.1.3"], ["203.0.113.7"], ["224.0.0.1"], ["::1"],
    ["fc00::1"], ["fe80::1"], ["8.8.8.8", "10.0.0.3"],
])
def test_remote_resolver_rejects_nonpublic_and_mixed_dns_answers(monkeypatch, addresses):
    class FakeLookup:
        async def resolve(self, host, port, family):
            return [{"hostname": host, "host": value, "port": port,
                     "family": family, "proto": 0, "flags": 0} for value in addresses]

        async def close(self):
            pass

    monkeypatch.setattr(openai_compatible, "DefaultResolver", FakeLookup)

    async def resolve():
        resolver = openai_compatible._ValidatedResolver(local_only=False)
        try:
            await resolver.resolve("ai.example.com", 443, socket.AF_INET)
        finally:
            await resolver.close()

    with pytest.raises(OSError, match="disallowed address"):
        asyncio.run(resolve())


def test_public_dns_answers_and_explicit_loopback_http(monkeypatch):
    class FakeLookup:
        def __init__(self):
            self.addresses = ["8.8.8.8", "2606:4700:4700::1111"]

        async def resolve(self, host, port, family):
            return [{"hostname": host, "host": value, "port": port,
                     "family": family, "proto": 0, "flags": 0} for value in self.addresses]

        async def close(self):
            pass

    lookup = FakeLookup()
    monkeypatch.setattr(openai_compatible, "DefaultResolver", lambda: lookup)

    async def resolve():
        remote = openai_compatible._ValidatedResolver(local_only=False)
        local = openai_compatible._ValidatedResolver(local_only=True)
        try:
            assert len(await remote.resolve("ai.example.com", 443, socket.AF_UNSPEC)) == 2
            lookup.addresses = ["127.0.0.1", "::1"]
            assert len(await local.resolve("localhost", 8188, socket.AF_UNSPEC)) == 2
            lookup.addresses = ["127.0.0.2"]
            with pytest.raises(OSError, match="disallowed address"):
                await local.resolve("localhost", 8188, socket.AF_INET)
        finally:
            await remote.close()
            await local.close()

    asyncio.run(resolve())


def test_private_dns_answer_blocks_real_connector_before_socket(monkeypatch):
    class FakeLookup:
        async def resolve(self, host, port, family):
            return [{"hostname": host, "host": "10.2.3.4", "port": port,
                     "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    monkeypatch.setattr(openai_compatible, "DefaultResolver", FakeLookup)

    async def no_socket(*_args, **_kwargs):
        raise AssertionError("socket connection must not be attempted")

    monkeypatch.setattr(openai_compatible.aiohttp.TCPConnector, "_wrap_create_connection", no_socket)
    result = asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="safe-key",
    ))
    assert result[0] is None and result[2] is None
    assert "safe-key" not in repr(result)


def _mock_session(monkeypatch, response: FakeResponse):
    calls = []
    monkeypatch.setattr(
        openai_compatible.aiohttp, "ClientSession",
        lambda **kwargs: FakeSession(response, calls, **kwargs),
    )
    return calls


def test_sends_bearer_headers_and_parses_text_parts(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": [
        {"type": "text", "text": "Hello "}, {"type": "text", "text": {"value": "world"}},
        {"type": "image", "image_url": "ignored"},
    ]}}]}).encode()
    calls = _mock_session(monkeypatch, FakeResponse(200, body))
    reply, error, status = asyncio.run(openai_compatible.chat_completion_verbose(
        [{"role": "user", "content": "Hi"}], "vendor/model",
        base_url="https://ai.example.com/v1/", api_key="super-secret",
        generation={"temperature": 0.3}, extra_headers={"OpenAI-Organization": "org-test"},
    ))
    assert (reply, error, status) == ("Hello world", None, 200)
    url, kwargs = calls[0]
    assert url == "https://ai.example.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer super-secret"
    assert kwargs["headers"]["OpenAI-Organization"] == "org-test"
    assert kwargs["json"] == {
        "model": "vendor/model", "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0.3,
    }
    assert kwargs["allow_redirects"] is False


def test_reads_all_chunks_before_parsing_provider_json(monkeypatch):
    body = b'{"choices":[{"message":{"content":"Ready"}}]}'
    response = FakeResponse(200, body, chunks=[body[:9], body[9:23], body[23:]])
    _mock_session(monkeypatch, response)
    assert asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="secret",
    )) == ("Ready", None, 200)
    assert response.read_count == 3


def test_oversized_chunked_response_stops_at_limit(monkeypatch):
    response = FakeResponse(200, b"", chunks=[b"x" * 100, b"y" * openai_compatible.MAX_RESPONSE_BYTES])
    _mock_session(monkeypatch, response)
    assert asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="secret",
    )) == (None, "Provider response too large", 200)
    assert response.read_count == 2


def test_error_body_is_not_read_logged_or_returned(monkeypatch):
    secret = "response-secret-key"
    response = FakeResponse(429, f"secret={secret}".encode())
    calls = _mock_session(monkeypatch, response)
    logged = []
    monkeypatch.setattr(openai_compatible.log, "warning", lambda message, *args: logged.append((message, args)))
    reply, error, status = asyncio.run(openai_compatible.chat_completion_verbose(
        [{"role": "user", "content": "Hi"}], "model",
        base_url="https://ai.example.com/v1", api_key="api-secret",
    ))
    assert (reply, error, status) == (None, "Provider HTTP 429", 429)
    assert len(calls) == 1
    assert response.read_count == 0
    assert secret not in repr(logged)
    assert "api-secret" not in repr(logged)


@pytest.mark.parametrize("body,error", [
    (b"not json", "Invalid provider JSON"),
    (b'{"choices":[]}', "Provider response has no text content"),
    (b'{"choices":[{"message":{"content":null}}]}', "Provider response has no text content"),
])
def test_invalid_provider_response_returns_safe_error(monkeypatch, body, error):
    _mock_session(monkeypatch, FakeResponse(200, body))
    assert asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="secret",
    )) == (None, error, 200)


def test_header_override_and_redirect_are_blocked(monkeypatch):
    calls = _mock_session(monkeypatch, FakeResponse(302, b"https://evil.example/"))
    result = asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="secret",
        extra_headers={"Authorization": "Bearer attacker"},
    ))
    assert result[0] is None and not calls
    result = asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="secret",
    ))
    assert result == (None, "Provider HTTP 302", 302)


def test_generation_cannot_replace_model_or_messages(monkeypatch):
    calls = _mock_session(monkeypatch, FakeResponse(200, b"{}"))
    result = asyncio.run(openai_compatible.chat_completion_verbose(
        [{"role": "user", "content": "Hi"}], "safe-model",
        base_url="https://ai.example.com/v1", api_key="secret",
        generation={"model": "other-model"},
    ))
    assert result == (None, "generation cannot override model or messages", None)
    assert not calls


def test_transport_exception_does_not_expose_exception_text(monkeypatch):
    secret = "secret-in-exception"
    logged = []

    class FailingSession(FakeSession):
        def post(self, _url, **_kwargs):
            raise aiohttp.ClientError(secret)

    monkeypatch.setattr(
        openai_compatible.aiohttp, "ClientSession",
        lambda **kwargs: FailingSession(FakeResponse(200, b"{}"), [], **kwargs),
    )
    monkeypatch.setattr(openai_compatible.log, "warning", lambda message, *args: logged.append((message, args)))
    result = asyncio.run(openai_compatible.chat_completion_verbose(
        [], "model", base_url="https://ai.example.com/v1", api_key="api-secret",
    ))
    assert result == (None, "Provider transport failure: ClientError", None)
    assert secret not in repr(logged) + repr(result)
    assert "api-secret" not in repr(logged) + repr(result)


def test_openrouter_wrapper_preserves_defaults_and_simple_call(monkeypatch):
    calls = []

    async def fake_transport(messages, model, **kwargs):
        calls.append((messages, model, kwargs))
        return "ok", None, 200

    monkeypatch.setattr(openrouter, "compatible_chat_completion_verbose", fake_transport)
    monkeypatch.setattr(openrouter, "OPENROUTER_API_KEY", "env-key")
    monkeypatch.setattr(openrouter, "OPENROUTER_HTTP_REFERER", "https://corebot.example")
    simple = asyncio.run(openrouter.chat_completion([{"role": "user", "content": "Hi"}], "openrouter/free"))
    assert simple == ("ok", None)
    assert calls[0][2]["base_url"] == openrouter.OPENROUTER_BASE_URL
    assert calls[0][2]["api_key"] == "env-key"
    assert calls[0][2]["allow_loopback_http"] is True
    assert calls[0][2]["extra_headers"] == {
        "X-Title": "CoreBot", "HTTP-Referer": "https://corebot.example",
    }
    assert calls[0][2]["generation"] == {"max_tokens": 1024, "temperature": 0.7}
