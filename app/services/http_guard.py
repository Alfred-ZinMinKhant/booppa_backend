"""SSRF guard for fetches of user-supplied URLs.

The PDPA free-scan and Deep Scan services fetch arbitrary customer-supplied
website URLs server-side. On ECS Fargate the task-role credential endpoint lives
at 169.254.170.2 and the EC2/instance metadata endpoint at 169.254.169.254, so an
unguarded fetch of an attacker-chosen URL (or a public URL that 30x-redirects to
one of those hosts) is a credential-theft SSRF vector.

This module resolves a URL's host to its IPs and refuses anything that lands in a
private / loopback / link-local / reserved range, and provides a redirect-following
`guarded_get` that re-validates every hop (httpx's own `follow_redirects` would
happily chase a redirect to the metadata endpoint without re-checking).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_REDIRECTS = 5


class BlockedURLError(ValueError):
    """Raised when a URL resolves to a non-public / disallowed address."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Reject anything that isn't a normal, routable public address. This covers
    # loopback (127/8, ::1), private (10/8, 172.16/12, 192.168/16, fc00::/7),
    # link-local (169.254/16 — the cloud metadata range — and fe80::/10),
    # multicast, reserved and unspecified (0.0.0.0).
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Raise :class:`BlockedURLError` unless *url* is http(s) to a public host.

    Resolves the hostname and rejects if *any* resolved address is non-public
    (defends against DNS records that return an internal IP)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"Disallowed URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURLError(f"Could not resolve host {host!r}: {e}") from e
    if not infos:
        raise BlockedURLError(f"Host {host!r} resolved to no addresses")
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise BlockedURLError(
                f"Host {host!r} resolves to non-public address {ip}"
            )


def guarded_get(
    client: httpx.Client, url: str, *, headers: dict | None = None
) -> httpx.Response:
    """Sync GET that validates the target (and every redirect hop) is public.

    The caller's client must have ``follow_redirects=False`` semantics honoured
    here — we follow redirects manually so each ``Location`` is re-validated
    before we connect to it."""
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_url(url)
        resp = client.get(url, headers=headers, follow_redirects=False)
        if resp.is_redirect and resp.next_request is not None:
            url = str(resp.next_request.url)
            continue
        return resp
    raise BlockedURLError(f"Too many redirects while fetching {url!r}")


async def guarded_get_async(
    client: httpx.AsyncClient, url: str, *, headers: dict | None = None
) -> httpx.Response:
    """Async counterpart of :func:`guarded_get`."""
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_url(url)
        resp = await client.get(url, headers=headers, follow_redirects=False)
        if resp.is_redirect and resp.next_request is not None:
            url = str(resp.next_request.url)
            continue
        return resp
    raise BlockedURLError(f"Too many redirects while fetching {url!r}")
