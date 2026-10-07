"""Pasarela de secretos en loopback (`gateways=`, m15-secrets-gateway,
ADR-023). Ver `_domain.py` (puro: `SecretGateway`, `GatewayStatus`,
validación) y `_section.py` (adaptador: `GatewaySection`, `GatewayHandle`,
sobre `ConfigureSandbox`)."""

from __future__ import annotations

from rayito._secret_gateway._domain import (
    DEFAULT_RATE_PER_MINUTE,
    MAX_ALLOW_RULES_PER_ROUTE,
    MAX_HEADERS_PER_ROUTE,
    MAX_RATE_PER_MINUTE,
    MAX_ROUTE_NAME_LEN,
    MAX_ROUTES_PER_GATEWAY,
    MIN_RATE_PER_MINUTE,
    GatewayStatus,
    SecretGateway,
    validate_gateways,
)
from rayito._secret_gateway._section import (
    EMPTY_GATEWAYS,
    SECTION_NAME,
    GatewayHandle,
    GatewaySection,
    GatewaySectionFactory,
    gateway_statuses_from_proto,
    gateways_recoverable,
    owns_gateways,
    recovered_gateways,
)

__all__ = [
    "DEFAULT_RATE_PER_MINUTE",
    "EMPTY_GATEWAYS",
    "MAX_ALLOW_RULES_PER_ROUTE",
    "MAX_HEADERS_PER_ROUTE",
    "MAX_RATE_PER_MINUTE",
    "MAX_ROUTES_PER_GATEWAY",
    "MAX_ROUTE_NAME_LEN",
    "MIN_RATE_PER_MINUTE",
    "SECTION_NAME",
    "GatewayHandle",
    "GatewaySection",
    "GatewaySectionFactory",
    "GatewayStatus",
    "SecretGateway",
    "gateway_statuses_from_proto",
    "gateways_recoverable",
    "owns_gateways",
    "recovered_gateways",
    "validate_gateways",
]
