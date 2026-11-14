from __future__ import annotations

import ipaddress
import socket
import asyncio
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver


def validate_public_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Only standard HTTP(S) ports are allowed.")


class PublicResolver(AbstractResolver):
    """Resolve a hostname once and refuse every non-public address before connect."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        if not records:
            raise OSError("Hostname did not resolve.")
        resolved = []
        for record_family, _, proto, _, sockaddr in records:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global:
                raise OSError("Refusing to connect to a non-public IP address.")
            resolved.append({
                "hostname": host,
                "host": str(address),
                "port": int(port),
                "family": record_family,
                "proto": proto,
                "flags": 0,
            })
        return resolved

    async def close(self) -> None:
        return None
