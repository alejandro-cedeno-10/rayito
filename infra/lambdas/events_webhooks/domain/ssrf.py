"""SSRF guard for the deliverer (T22, §7.4): "applies an SSRF guard after
DNS resolution (loopback, private, link-local, IMDS and CGNAT are
rejected)". Pure classification only — `adapters/http_client.py` is the one
place that actually resolves a hostname and opens a socket; this module
just says yes or no about an already-resolved address, so it is unit
tested without a network.
"""

from __future__ import annotations

import ipaddress
from typing import Final

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: 100.64.0.0/10 (RFC 6598): shared/carrier-grade NAT space, where an AWS
#: account's own NAT gateways or internal load balancers can live —
#: `ipaddress` has no built-in predicate for this one, unlike loopback/
#: private/link-local/multicast/reserved.
CGNAT_BLOCK: Final = ipaddress.ip_network("100.64.0.0/10")

#: The IMDS address itself (also covered by `is_link_local`, called out by
#: name since this is the one SSRF target with a direct, well-known path to
#: credential theft).
IMDS_ADDRESS: Final = ipaddress.ip_address("169.254.169.254")


def is_blocked(address: IpAddress) -> bool:
    """`True` for any address class a webhook upstream must never reach:
    loopback, private, link-local (which includes IMDS), multicast,
    reserved/unspecified, or CGNAT. An IPv4-mapped IPv6 address
    (`::ffff:a.b.c.d`, e.g. from an AAAA record) is classified as the IPv4
    address it carries, so both paths agree on the same destination."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in CGNAT_BLOCK


def first_safe_address(addresses: list[IpAddress]) -> IpAddress | None:
    """The first address in `addresses` (as DNS resolution returned them)
    that is not blocked, or `None` if every candidate is — the caller must
    treat `None` as a hard failure, never fall through to an unchecked
    address."""
    for address in addresses:
        if not is_blocked(address):
            return address
    return None
