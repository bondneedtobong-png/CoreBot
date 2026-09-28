"""Small OpenAI-compatible chat completions transport for configured providers."""
from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from utils.logger import log

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=30)
MAX_RESPONSE_BYTES = 2_000_000
_OPTIONAL_HEADERS = {
    "http-referer": "HTTP-Referer",
    "x-title": "X-Title",
    "openai-organization": "OpenAI-Organization",
    "openai-project": "OpenAI-Project",
}


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


class _ValidatedResolver(AbstractResolver):
    """Check every DNS answer used by aiohttp before it opens a socket."""

    def __init__(self, *, local_only: bool):
        self._delegate = DefaultResolver()
        self._local_only = local_only

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        records = await self._delegate.resolve(host, port, family)
        if not records:
            raise OSError("provider DNS returned no addresses")
        for record in records:
            try:
                address = ipaddress.ip_address(record["host"])
            except (KeyError, ValueError) as exc:
                raise OSError("provider DNS returned an invalid address") from exc
            if self._local_only:
                allowed = str(address) in {"127.0.0.1", "::1"}
            else:
                allowed = _is_public_address(address)
            if not allowed:
                raise OSError("provider DNS returned a disallowed address")
        return records

    async def close(self) -> None:
        await self._delegate.close()


def validate_base_url(base_url: str, *, allow_loopback_http: bool = False) -> str:
    """Return a normalized API root; never accept credentials or URL parameters.

    HTTP is opt-in for a provider running on this machine. Remote providers
    must use HTTPS. Literal non-public IP addresses cannot be remote targets.
    """
    value = (base_url or "").strip()
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid provider base URL")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid provider base URL") from exc
    if (
        parts.scheme not in {"https", "http"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or "?" in value
        or "#" in value
        or parts.netloc.endswith(":")
        or "\\" in value
    ):
        raise ValueError("invalid provider base URL")

    host = hostname.lower().rstrip(".")
    is_loopback_host = host in {"localhost", "127.0.0.1", "::1"}
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if parts.scheme == "http":
        if not (allow_loopback_http and is_loopback_host):
            raise ValueError("provider base URL requires HTTPS")
    elif (
        is_loopback_host
        or (address is not None and not _is_public_address(address))
        or (address is None and (
            "." not in host or host.endswith((".localhost", ".local"))
        ))
    ):
        raise ValueError("provider base URL must be remote")

    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        raise ValueError("provider base URL must be the API root")
    return f"{parts.scheme}://{authority}{path}"


def _headers(api_key: str, extra_headers: Mapping[str, str] | None) -> dict[str, str]:
    if not api_key or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise ValueError("invalid provider API key")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for name, value in (extra_headers or {}).items():
        canonical = _OPTIONAL_HEADERS.get(str(name).lower())
        if canonical is None or not isinstance(value, str) or len(value) > 256:
            raise ValueError("unsupported provider header")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid provider header")
        if value:
            headers[canonical] = value
    return headers


def _extract_text(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    fragments: list[str] = []
    for part in content:
        if isinstance(part, str):
            fragments.append(part)
        elif isinstance(part, dict) and part.get("type", "text") == "text":
            text = part.get("text")
            if isinstance(text, str):
                fragments.append(text)
            elif isinstance(text, dict) and isinstance(text.get("value"), str):
                fragments.append(text["value"])
    return "".join(fragments).strip() or None


async def chat_completion_verbose(
    messages: list[dict[str, str]],
    model: str,
    *,
    base_url: str,
    api_key: str,
    generation: Mapping[str, Any] | None = None,
    extra_headers: Mapping[str, str] | None = None,
    allow_loopback_http: bool = False,
) -> tuple[str | None, str | None, int | None]:
    """POST chat/completions; return reply, safe error, and HTTP status."""
    try:
        root = validate_base_url(base_url, allow_loopback_http=allow_loopback_http)
        headers = _headers(api_key.strip(), extra_headers)
    except ValueError as exc:
        return None, str(exc), None
    if not model or not model.strip():
        return None, "model is required", None
    params = dict(generation) if generation is not None else {}
    if "model" in params or "messages" in params:
        return None, "generation cannot override model or messages", None
    payload: dict[str, Any] = {"model": model.strip(), "messages": messages, **params}
    resolver: _ValidatedResolver | None = None
    try:
        local_only = root.startswith("http://")
        resolver = _ValidatedResolver(local_only=local_only)
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with aiohttp.ClientSession(
            timeout=DEFAULT_TIMEOUT, trust_env=False, connector=connector,
        ) as session:
            async with session.post(
                f"{root}/chat/completions", headers=headers, json=payload,
                allow_redirects=False,
            ) as response:
                status = response.status
                if status != 200:
                    log.warning("AI provider HTTP status {}", status)
                    return None, f"Provider HTTP {status}", status
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        return None, "Provider response too large", status
                    body.extend(chunk)
                try:
                    data = json.loads(bytes(body))
                except (ValueError, UnicodeDecodeError):
                    return None, "Invalid provider JSON", status
                reply = _extract_text(data)
                if reply is None:
                    return None, "Provider response has no text content", status
                return reply, None, status
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        log.warning("AI provider transport failure: {}", type(exc).__name__)
        return None, f"Provider transport failure: {type(exc).__name__}", None
    finally:
        if resolver is not None:
            await resolver.close()
