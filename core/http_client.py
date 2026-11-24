"""Centralized async HTTP client with SSRF protection, DNS rebinding defense, cookie isolation, and size caps."""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from config import DEFAULT_REQUEST_TIMEOUT, DEFAULT_RETRY_LIMIT, MAX_RETRY_LIMIT, USER_AGENT
from core.url_security import (
    DnsResolver,
    UrlSecurityError,
    resolve_and_validate_host,
    validate_public_url,
)

logger = logging.getLogger("social_id_finder.http")

_SAFE_RESPONSE_HEADERS = ("content-type", "retry-after", "location", "x-rate-limit-reset")
_TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_RETRY_AFTER_SLEEP = 5.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB cap (L-01)


class _NoOpCookieJar(http.cookiejar.CookieJar):
    """Stateless cookie jar that never stores or replays cookies across concurrent requests (M-01)."""

    def extract_cookies(self, response: Any, request: Any) -> None:  # noqa: ARG002
        return None

    def add_cookie_header(self, request: Any) -> None:  # noqa: ARG002
        return None

    def set_cookie(self, cookie: Any) -> None:  # noqa: ARG002
        return None

    def set_cookie_if_ok(self, cookie: Any, request: Any) -> None:  # noqa: ARG002
        return None


class _NoOpCookies(httpx.Cookies):
    """httpx.Cookies wrapper backed by _NoOpCookieJar that drops all Set-Cookie headers (M-01)."""

    def __init__(self) -> None:
        self.jar = _NoOpCookieJar()

    def extract_cookies(self, response: httpx.Response) -> None:  # noqa: ARG002
        return None

    def set_cookie_header(self, request: httpx.Request) -> None:  # noqa: ARG002
        return None

    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:  # noqa: ARG002
        return None


class _SSRFSafeNetworkBackend:
    """httpcore network backend wrapper preventing DNS rebinding at TCP connect time (H-01).

    Resolves DNS right before opening TCP, validates every IP is public/global,
    and connects directly to the pinned public IP while httpcore preserves TLS SNI
    and certificate hostname verification for the original host.
    """

    def __init__(self, inner: Any, dns_resolver: DnsResolver | None = None) -> None:
        self._inner = inner
        self._dns_resolver = dns_resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        ips = await asyncio.to_thread(
            resolve_and_validate_host,
            host,
            int(port),
            resolver=self._dns_resolver,
            url=f"{host}:{port}",
        )
        pinned_ip = ips[0]
        return await self._inner.connect_tcp(
            host=pinned_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> Any:
        raise UrlSecurityError("Unix domain sockets are blocked.", url=path)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


@dataclass
class FetchResponse:
    """Normalized HTTP response wrapper (never exposes session cookies or auth tokens)."""

    url: str
    status_code: int = 0
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[str] = field(default_factory=list)
    error_type: str = ""
    error_message: str = ""
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and not self.error_type


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After header (delta-seconds or HTTP-date) into non-negative seconds."""
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        secs = float(stripped)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(stripped)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


class HttpClient:
    """Async HTTP client with SSRF/redirect/DNS-rebinding protection, stateless cookies, and bounded responses."""

    def __init__(
        self,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        user_agent: str = USER_AGENT,
        max_redirects: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        dns_resolver: DnsResolver | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.timeout = max(0.05, float(timeout))
        self.retry_limit = max(0, min(int(retry_limit), MAX_RETRY_LIMIT))
        self.user_agent = user_agent
        self.max_redirects = max(0, min(int(max_redirects), 10))
        self._transport = transport
        self._dns_resolver = dns_resolver
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._client: httpx.AsyncClient | None = None

    def _default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        }

    def _build_safe_transport(self, limits: httpx.Limits) -> httpx.AsyncBaseTransport:
        if self._transport is not None:
            return self._transport
        transport = httpx.AsyncHTTPTransport(limits=limits)
        pool = getattr(transport, "_pool", None)
        inner_backend = getattr(pool, "_network_backend", None)
        if pool is not None and inner_backend is not None:
            pool._network_backend = _SSRFSafeNetworkBackend(inner_backend, dns_resolver=self._dns_resolver)
        return transport

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
            transport = self._build_safe_transport(limits)
            self._client = httpx.AsyncClient(
                headers=self._default_headers(),
                follow_redirects=False,  # Manual hop-by-hop SSRF validation (H-01)
                limits=limits,
                timeout=httpx.Timeout(self.timeout),
                transport=transport,
            )
            # Replace httpx's internal cookie store with a stateless no-op jar (M-01)
            self._client._cookies = _NoOpCookies()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "HttpClient":
        self._get_client()
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    def _extract_safe_headers(self, response: httpx.Response) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in _SAFE_RESPONSE_HEADERS:
            if key in response.headers:
                out[key] = response.headers[key]
        return out

    async def _validate_hop(self, target_url: str) -> None:
        """Validate URL scheme, hostname, IP literals, and DNS (when real network or resolver supplied)."""
        should_resolve = (self._transport is None) or (self._dns_resolver is not None)
        if should_resolve:
            await asyncio.to_thread(
                validate_public_url,
                target_url,
                resolve_dns=True,
                resolver=self._dns_resolver,
            )
        else:
            validate_public_url(target_url, resolve_dns=False)

    async def _stream_bounded_text(self, response: httpx.Response) -> tuple[str, bool]:
        """Read response body up to max_response_bytes. Returns (text, exceeded_limit)."""
        cl_header = response.headers.get("content-length")
        if cl_header:
            try:
                if int(cl_header.strip()) > self.max_response_bytes:
                    return "", True
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                return "", True
            chunks.append(chunk)

        raw = b"".join(chunks)
        encoding = response.encoding or "utf-8"
        try:
            return raw.decode(encoding, errors="replace"), False
        except LookupError:
            return raw.decode("utf-8", errors="replace"), False

    async def _fetch_with_validated_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None,
        req_timeout: float,
    ) -> tuple[int, str, str, dict[str, str], list[str], str, str]:
        """Perform GET with manual hop-by-hop SSRF/redirect validation and bounded response size.

        Returns (status_code, final_url, text, safe_headers, redirect_chain, error_type, error_message).
        """
        current_url = url
        redirect_chain: list[str] = []

        for hop in range(self.max_redirects + 1):
            await self._validate_hop(current_url)

            async with client.stream(
                "GET",
                current_url,
                headers=headers,
                timeout=httpx.Timeout(req_timeout),
            ) as response:
                status = response.status_code
                safe_headers = self._extract_safe_headers(response)
                resp_url = str(response.url)

                if status in _REDIRECT_STATUS_CODES and "location" in response.headers:
                    raw_location = response.headers["location"].strip()
                    if not raw_location:
                        return status, resp_url, "", safe_headers, redirect_chain, "HTTP_ERROR", "Empty redirect Location."
                    next_url = urljoin(resp_url, raw_location)
                    if hop >= self.max_redirects:
                        return (
                            0,
                            resp_url,
                            "",
                            safe_headers,
                            redirect_chain,
                            "TOO_MANY_REDIRECTS",
                            f"Exceeded redirect limit ({self.max_redirects}).",
                        )
                    # Validate target before recording/following
                    await self._validate_hop(next_url)
                    redirect_chain.append(resp_url)
                    if next_url not in redirect_chain:
                        redirect_chain.append(next_url)
                    current_url = next_url
                    continue

                text, too_large = await self._stream_bounded_text(response)
                if too_large:
                    return (
                        status,
                        resp_url,
                        "",
                        safe_headers,
                        redirect_chain,
                        "RESPONSE_TOO_LARGE",
                        f"Response exceeded maximum allowed size ({self.max_response_bytes} bytes).",
                    )
                return status, resp_url, text, safe_headers, redirect_chain, "", ""

        return 0, current_url, "", {}, redirect_chain, "TOO_MANY_REDIRECTS", f"Exceeded redirect limit ({self.max_redirects})."

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retry_limit: int | None = None,
    ) -> FetchResponse:
        """Fetch a public URL with SSRF protection, bounded retries (max 1), and Retry-After compliance."""
        req_timeout = max(0.05, float(timeout)) if timeout is not None else self.timeout
        max_retries = (
            max(0, min(int(retry_limit), MAX_RETRY_LIMIT))
            if retry_limit is not None
            else self.retry_limit
        )
        client = self._get_client()

        for attempt in range(max_retries + 1):
            try:
                (
                    status,
                    final_url,
                    text,
                    safe_headers,
                    redirect_chain,
                    early_err_type,
                    early_err_msg,
                ) = await self._fetch_with_validated_redirects(
                    client,
                    url,
                    headers=headers,
                    req_timeout=req_timeout,
                )
            except asyncio.CancelledError:
                raise
            except (UrlSecurityError, httpx.InvalidURL) as exc:
                logger.warning("Blocked unsafe URL/redirect for %s: %s", url, exc)
                err_url = exc.url if isinstance(exc, UrlSecurityError) and exc.url else url
                return FetchResponse(
                    url=err_url,
                    error_type="SSRF_BLOCKED",
                    error_message=str(exc),
                )
            except httpx.TimeoutException:
                logger.warning("Request timed out (attempt %d/%d) for %s", attempt + 1, max_retries + 1, url)
                if attempt < max_retries:
                    await asyncio.sleep(0.25)
                    continue
                return FetchResponse(
                    url=url,
                    error_type="TIMEOUT",
                    error_message=f"Request timed out after {req_timeout:.1f}s.",
                )
            except httpx.TooManyRedirects:
                return FetchResponse(
                    url=url,
                    error_type="TOO_MANY_REDIRECTS",
                    error_message=f"Exceeded redirect limit ({self.max_redirects}).",
                )
            except httpx.RequestError as exc:
                logger.warning("Network error (attempt %d/%d) for %s: %s", attempt + 1, max_retries + 1, url, exc)
                if attempt < max_retries:
                    await asyncio.sleep(0.25)
                    continue
                return FetchResponse(
                    url=url,
                    error_type="NETWORK_ERROR",
                    error_message=f"Network error: {type(exc).__name__}",
                )

            if early_err_type:
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type=early_err_type,
                    error_message=early_err_msg,
                )

            if status == 429:
                retry_after = parse_retry_after(safe_headers.get("retry-after"))
                if (
                    attempt < max_retries
                    and retry_after is not None
                    and retry_after <= min(_MAX_RETRY_AFTER_SLEEP, req_timeout / 2.0)
                ):
                    logger.info("HTTP 429 for %s — sleeping Retry-After %.2fs before retry.", url, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                ra_note = f" (Retry-After: {retry_after:.0f}s)" if retry_after is not None else ""
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type="RATE_LIMITED",
                    error_message=f"HTTP 429 Too Many Requests{ra_note}.",
                    retry_after=retry_after,
                )

            if status in _TRANSIENT_STATUS_CODES:
                logger.warning("HTTP %d (attempt %d/%d) for %s", status, attempt + 1, max_retries + 1, url)
                if attempt < max_retries:
                    await asyncio.sleep(0.3)
                    continue
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type="SERVER_ERROR",
                    error_message=f"HTTP {status} server error.",
                )

            if status == 403:
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    text=text,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type="FORBIDDEN",
                    error_message="HTTP 403 Forbidden — profile access restricted.",
                )

            if status == 404:
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    text=text,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type="NOT_FOUND",
                    error_message="HTTP 404 Not Found — profile does not exist or is unavailable.",
                )

            if status >= 400:
                return FetchResponse(
                    url=final_url,
                    status_code=status,
                    text=text,
                    headers=safe_headers,
                    redirect_chain=redirect_chain,
                    error_type="HTTP_ERROR",
                    error_message=f"HTTP {status} error.",
                )

            return FetchResponse(
                url=final_url,
                status_code=status,
                text=text,
                headers=safe_headers,
                redirect_chain=redirect_chain,
            )

        return FetchResponse(
            url=url,
            error_type="NETWORK_ERROR",
            error_message="Request failed after retries.",
        )
