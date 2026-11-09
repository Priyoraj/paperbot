"""Guard for links that come from request emails.

While the VPN is up this PC sits inside the institute network, so a link such as
http://10.0.0.5/ or http://intranet.local/ must never be fetched on someone's behalf.
Only http(s) URLs whose host resolves exclusively to public addresses are allowed.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _resolve(host: str) -> list[str]:
    return list({info[4][0] for info in socket.getaddrinfo(host, None)})


def is_public_http_url(url: str, resolver=_resolve) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    host = parts.hostname
    if host.endswith((".local", ".internal", ".lan", ".localhost")) or host == "localhost":
        return False
    try:
        addrs = [host] if _is_ip(host) else resolver(host)
    except OSError:
        return False
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            return False
    return True


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
