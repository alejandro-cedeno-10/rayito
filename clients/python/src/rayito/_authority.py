"""Autoridades HTTP (`host:puerto`) tal y como las escribe un cliente en
`Host` y `Origin`. Sólo biblioteca estándar: la comparten `rayito-mcp
--http` (extra `mcp`) y `rayito sandbox proxy` (extra `cli`), y ninguno de
los dos puede arrastrar las dependencias del otro.
"""

from __future__ import annotations

import ipaddress

IPV6_VERSION = 6


def is_ipv6_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == IPV6_VERSION
    except ValueError:
        return False


def http_authority(host: str, port: int) -> str:
    """`Host` y `Origin` tal y como los escribe un cliente: un literal IPv6
    va entre corchetes (RFC 3986 §3.2.2), que es la forma que compara el
    middleware de `mcp` y el proxy de la CLI."""
    return f"[{host}]:{port}" if is_ipv6_literal(host) else f"{host}:{port}"


def is_wildcard(host: str) -> bool:
    """`0.0.0.0` y `::` ligan todas las interfaces: son una interfaz válida y
    una autoridad imposible (ningún cliente escribe `Host: 0.0.0.0`)."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


__all__ = ["http_authority", "is_ipv6_literal", "is_wildcard"]
