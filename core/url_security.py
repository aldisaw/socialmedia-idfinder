"""Centralized URL, IP, DNS, and SSRF validation helpers (H-01, H-02, M-06)."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlsplit

DnsResolver = Callable[[str, int], list[str]]

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})
_RE_CONTROL_OR_WS = re.compile(r"[\s\x00-\x1f\x7f]")
_BLOCKED_HOST_EXACT: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
        "metadata.google.internal",
        "metadata",
    }
)
_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".localdomain",
    ".local",
    ".internal",
    ".home.arpa",
)
# Detect ambiguous numeric/octal/hex IPv4 representations (e.g. 2130706433, 0x7f000001, 0177.0.0.1)
_RE_ALL_NUMERIC_OR_HEX_LABEL = re.compile(r"^(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)$", re.I)


class UrlSecurityError(ValueError):
    """Raised when a URL, redirect target, or resolved IP violates SSRF/URL security policy."""

    def __init__(self, message: str, url: str = "") -> None:
        self.url = url
        super().__init__(message)


def is_blocked_ip(ip_str: str) -> bool:
    """Return True if ip_str is non-global, private, loopback, link-local, multicast, reserved, or unspecified."""
    raw = (ip_str or "").strip()
    if not raw:
        return True
    # Strip IPv6 zone index if present (e.g. fe80::1%eth0)
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return True

    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1, ::ffff:169.254.169.254)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped

    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    ):
        return True
    return False


def _try_parse_ip_literal(host: str) -> str | None:
    """Return canonical IP string if host is an IPv4/IPv6/numeric-obfuscated IP literal, else None."""
    cleaned = host.strip("[]")
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(cleaned))
    except ValueError:
        pass

    # Catch obfuscated numeric/octal/hex IPv4 forms like 2130706433, 0x7f000001, 0177.0.0.1
    parts = cleaned.split(".")
    if 1 <= len(parts) <= 4 and all(_RE_ALL_NUMERIC_OR_HEX_LABEL.match(p) for p in parts):
        try:
            packed = socket.inet_aton(cleaned)
            return socket.inet_ntoa(packed)
        except OSError:
            return cleaned  # Unparseable all-numeric host is still invalid/blocked
    return None


def is_ip_literal(host: str) -> bool:
    return _try_parse_ip_literal(host) is not None


def default_dns_resolver(host: str, port: int) -> list[str]:
    """Resolve host via OS DNS and return unique IP strings."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if ip not in ips:
            ips.append(ip)
    return ips


def resolve_and_validate_host(
    host: str,
    port: int,
    *,
    resolver: DnsResolver | None = None,
    url: str = "",
) -> list[str]:
    """Resolve hostname to IPs and ensure every resolved address is public/global."""
    ip_lit = _try_parse_ip_literal(host)
    if ip_lit is not None:
        if is_blocked_ip(ip_lit):
            raise UrlSecurityError(
                f"Blocked non-public IP address '{ip_lit}'.",
                url=url,
            )
        return [ip_lit]

    resolve_fn = resolver or default_dns_resolver
    try:
        ips = resolve_fn(host, port)
    except UrlSecurityError:
        raise
    except Exception as exc:
        raise UrlSecurityError(f"DNS resolution failed for '{host}': {type(exc).__name__}", url=url) from exc

    if not ips:
        raise UrlSecurityError(f"DNS returned no IP addresses for '{host}'.", url=url)

    for ip in ips:
        if is_blocked_ip(ip):
            raise UrlSecurityError(
                f"Blocked resolved non-public IP '{ip}' for host '{host}'.",
                url=url,
            )
    return ips


def validate_public_url(
    url: str,
    *,
    resolve_dns: bool = True,
    resolver: DnsResolver | None = None,
    allow_custom_ports: bool = False,
) -> tuple[str, list[str]]:
    """Validate URL scheme, credentials, hostname, port, IP literals, and optional DNS resolution.

    Returns (validated_url, resolved_public_ips). Raises UrlSecurityError on any violation.
    """
    if not url or not isinstance(url, str):
        raise UrlSecurityError("Empty URL is not allowed.", url="")

    if _RE_CONTROL_OR_WS.search(url):
        raise UrlSecurityError("URL contains whitespace or control characters.", url=url)

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UrlSecurityError("Malformed URL.", url=url) from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlSecurityError(
            f"Blocked non-HTTP(S) URL scheme '{scheme or 'missing'}'.",
            url=url,
        )

    if parsed.username is not None or parsed.password is not None:
        raise UrlSecurityError("URLs containing embedded credentials are not allowed.", url=url)

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlSecurityError("URL is missing a hostname.", url=url)

    try:
        port = parsed.port
    except ValueError as exc:
        raise UrlSecurityError("URL contains an invalid port.", url=url) from exc

    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if not allow_custom_ports and effective_port not in _ALLOWED_PORTS:
        raise UrlSecurityError(
            f"Blocked non-standard port {effective_port}; only ports 80 and 443 are allowed.",
            url=url,
        )

    if host in _BLOCKED_HOST_EXACT or any(host.endswith(sfx) for sfx in _BLOCKED_HOST_SUFFIXES):
        raise UrlSecurityError(f"Blocked internal/local hostname '{host}'.", url=url)

    ip_lit = _try_parse_ip_literal(host)
    if ip_lit is not None:
        if is_blocked_ip(ip_lit):
            raise UrlSecurityError(f"Blocked non-public IP literal '{ip_lit}'.", url=url)
        return url, [ip_lit]

    # Single-label hostnames (no dot) cannot be public internet domains
    if "." not in host:
        raise UrlSecurityError(f"Blocked non-public single-label hostname '{host}'.", url=url)

    if resolve_dns:
        ips = resolve_and_validate_host(host, effective_port, resolver=resolver, url=url)
        return url, ips

    return url, []
