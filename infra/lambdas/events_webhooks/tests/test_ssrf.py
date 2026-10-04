"""SSRF classification (T22): loopback, private, link-local (incl. IMDS),
multicast, reserved and CGNAT are all blocked; a plain public address is not.
"""

from __future__ import annotations

import ipaddress

from domain.ssrf import first_safe_address, is_blocked

BLOCKED_ADDRESSES = [
    "127.0.0.1",  # loopback
    "10.0.0.5",  # private
    "192.168.1.1",  # private
    "169.254.169.254",  # link-local / IMDS
    "169.254.1.1",  # link-local
    "224.0.0.1",  # multicast
    "100.64.0.1",  # CGNAT
    "100.127.255.254",  # CGNAT (top of the range)
    "0.0.0.0",  # unspecified
    "::1",  # IPv6 loopback
    "fe80::1",  # IPv6 link-local
    "::ffff:100.64.0.1",  # CGNAT, IPv4-mapped (an AAAA answer)
    "::ffff:127.0.0.1",  # loopback, IPv4-mapped
    "::ffff:169.254.169.254",  # IMDS, IPv4-mapped
    "::ffff:10.0.0.5",  # private, IPv4-mapped
]

ALLOWED_ADDRESSES = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "100.63.255.255",
    "100.128.0.0",
    "::ffff:8.8.8.8",  # public, IPv4-mapped: same verdict as 8.8.8.8
]


def test_every_blocked_address_is_classified_as_blocked() -> None:
    for text in BLOCKED_ADDRESSES:
        assert is_blocked(ipaddress.ip_address(text)), text


def test_every_allowed_address_is_not_blocked() -> None:
    for text in ALLOWED_ADDRESSES:
        assert not is_blocked(ipaddress.ip_address(text)), text


def test_first_safe_address_skips_blocked_candidates() -> None:
    candidates = [ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("8.8.8.8")]
    assert first_safe_address(candidates) == ipaddress.ip_address("8.8.8.8")


def test_first_safe_address_is_none_when_everything_is_blocked() -> None:
    candidates = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("169.254.169.254")]
    assert first_safe_address(candidates) is None
