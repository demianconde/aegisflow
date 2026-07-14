"""Validação de endpoints de provedor contra SSRF.

Resolve o host e rejeita endereços privados/loopback/link-local/reservados,
a menos que endpoints privados estejam explicitamente permitidos (self-host).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # não parseou → bloqueia por precaução
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def is_public_endpoint(base_url: str) -> bool:
    """True se o host da URL resolve apenas para IPs públicos."""
    host = urlparse(base_url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return all(not _is_blocked_ip(info[4][0]) for info in infos)


_SSRF_MSG = (
    "base_url aponta para rede privada/local, bloqueada por segurança (SSRF). "
    "Em self-host, defina AEGIS_ALLOW_PRIVATE_ENDPOINTS=true."
)


def validate_endpoint(base_url: str, allow_private: bool) -> None:
    """Levanta ValueError se a base_url apontar para rede privada (e não permitido)."""
    if allow_private:
        return
    if not is_public_endpoint(base_url):
        raise ValueError(_SSRF_MSG)


async def validate_endpoint_async(base_url: str, allow_private: bool) -> None:
    """Igual a validate_endpoint, mas resolve DNS fora do event loop (não bloqueia)."""
    if allow_private:
        return
    ok = await asyncio.to_thread(is_public_endpoint, base_url)
    if not ok:
        raise ValueError(_SSRF_MSG)
