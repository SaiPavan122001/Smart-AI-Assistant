"""URL policy for the browser: scheme allowlist and SSRF protection."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.utils.errors import UrlViolation

_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".corp", ".home.arpa")
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )  # fmt: skip


def check_url(
    url: str,
    *,
    allow_private_network: bool = False,
    resolve_dns: bool = False,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> str:
    """Validate and return a normalised URL, or raise ``UrlViolation``."""
    if not isinstance(url, str) or not url.strip():
        raise UrlViolation("URL must be a non-empty string")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlViolation(f"Malformed URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in allowed_schemes:
        raise UrlViolation(f"URL scheme '{scheme or '(none)'}' is not allowed (only http/https)")
    if parts.username or parts.password:
        raise UrlViolation("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlViolation("URL has no host")
    if allow_private_network:
        return url
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UrlViolation(f"Host '{host}' is a local/internal name and is blocked")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_blocked(ip):
            raise UrlViolation(f"Address {host} is private/loopback and is blocked")
    elif resolve_dns:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return url  # unresolvable: the browser will fail on its own
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if _ip_blocked(addr):
                raise UrlViolation(f"Host '{host}' resolves to a private address ({addr})")
    return url
