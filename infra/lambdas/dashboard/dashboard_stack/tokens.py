"""Validación del token de acceso de Cognito antes de cualquier ruta o
llamada a AWS: formato, tamaño, algoritmo, firma contra el JWKS, emisor,
`token_use`, `client_id` (la audiencia de un token de acceso), caducidad,
`nbf` y `sub`. Las comprobaciones que no necesitan red van antes de la que lista los
clientes del pool (una llamada a AWS): un token inválido no la provoca.
Cualquier fallo es un `TokenError` con un código cerrado; el
mensaje al cliente es siempre el mismo (no revela qué comprobación falló) y
el código sólo se registra.

Se aceptan tokens de acceso (`token_use: access`), no de identidad: son los
que Cognito emite para llamar a una API, y llevan `cognito:groups`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from dashboard_stack.rs256 import b64url_decode, b64url_int, verify_rs256
from rayito._limits import DASHBOARD_STACK_JWT_LEEWAY_SECONDS, DASHBOARD_STACK_JWT_MAX_BYTES

ALGORITHM: Final = "RS256"
TOKEN_USE: Final = "access"
GROUPS_CLAIM: Final = "cognito:groups"
JWT_PARTS: Final = 3
#: Los códigos de `TokenError`: una lista cerrada que sólo va al log.
CODE_MALFORMED: Final = "malformed"
CODE_TOO_LARGE: Final = "too_large"
CODE_ALGORITHM: Final = "algorithm"
CODE_UNKNOWN_KEY: Final = "unknown_key"
CODE_SIGNATURE: Final = "signature"
CODE_ISSUER: Final = "issuer"
CODE_TOKEN_USE: Final = "token_use"
CODE_AUDIENCE: Final = "audience"
CODE_EXPIRED: Final = "expired"
CODE_NOT_YET_VALID: Final = "not_yet_valid"
CODE_SUBJECT: Final = "subject"


class TokenError(Exception):
    """El token no es válido; `code` dice por qué (sólo para el log)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuthUnavailable(Exception):
    """No se pudo obtener el JWKS o la lista de clientes del pool: no se
    puede decidir, así que se rechaza con un 503 y no con un 401."""


@dataclass(frozen=True)
class SigningKey:
    modulus: int
    exponent: int


class KeySource(Protocol):
    def key(self, kid: str) -> SigningKey | None: ...


class ClientIdSource(Protocol):
    def client_ids(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class Caller:
    """Quién llama: `subject` es el `sub` estable de Cognito (lo que se
    audita), `username` el nombre legible y `groups` los grupos del pool."""

    subject: str
    username: str
    groups: frozenset[str]


def _json_object(segment: str) -> dict[str, Any]:
    try:
        document = json.loads(b64url_decode(segment))
    except ValueError:
        raise TokenError(CODE_MALFORMED) from None
    if not isinstance(document, dict):
        raise TokenError(CODE_MALFORMED)
    return document


def _number(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TokenError(CODE_MALFORMED)
    return float(value)


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        keys: KeySource,
        clients: ClientIdSource,
        clock: Callable[[], float] = time.time,
        leeway_seconds: float = DASHBOARD_STACK_JWT_LEEWAY_SECONDS,
        max_bytes: int = DASHBOARD_STACK_JWT_MAX_BYTES,
    ) -> None:
        self._issuer = issuer
        self._keys = keys
        self._clients = clients
        self._clock = clock
        self._leeway = leeway_seconds
        self._max_bytes = max_bytes

    def verify(self, token: str) -> Caller:
        if len(token) > self._max_bytes:
            raise TokenError(CODE_TOO_LARGE)
        parts = token.split(".")
        if len(parts) != JWT_PARTS:
            raise TokenError(CODE_MALFORMED)
        header_segment, payload_segment, signature_segment = parts
        header = _json_object(header_segment)
        if header.get("alg") != ALGORITHM:
            raise TokenError(CODE_ALGORITHM)
        self._check_signature(header, f"{header_segment}.{payload_segment}", signature_segment)
        return self._caller(_json_object(payload_segment))

    def _check_signature(self, header: dict[str, Any], signing_input: str, signature: str) -> None:
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise TokenError(CODE_MALFORMED)
        key = self._keys.key(kid)
        if key is None:
            raise TokenError(CODE_UNKNOWN_KEY)
        try:
            raw_signature = b64url_decode(signature)
        except ValueError:
            raise TokenError(CODE_MALFORMED) from None
        if not verify_rs256(
            signing_input.encode("ascii"),
            raw_signature,
            modulus=key.modulus,
            exponent=key.exponent,
        ):
            raise TokenError(CODE_SIGNATURE)

    def _caller(self, claims: dict[str, Any]) -> Caller:
        if claims.get("iss") != self._issuer:
            raise TokenError(CODE_ISSUER)
        if claims.get("token_use") != TOKEN_USE:
            raise TokenError(CODE_TOKEN_USE)
        now = self._clock()
        if _number(claims, "exp") <= now - self._leeway:
            raise TokenError(CODE_EXPIRED)
        if "nbf" in claims and _number(claims, "nbf") > now + self._leeway:
            raise TokenError(CODE_NOT_YET_VALID)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenError(CODE_SUBJECT)
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or client_id not in self._clients.client_ids():
            raise TokenError(CODE_AUDIENCE)
        return Caller(subject, str(claims.get("username", subject)), _groups(claims))


def _groups(claims: dict[str, Any]) -> frozenset[str]:
    value = claims.get(GROUPS_CLAIM, [])
    items: Iterable[Any] = value if isinstance(value, list) else []
    return frozenset(item for item in items if isinstance(item, str))


def signing_key_from_jwk(jwk: dict[str, Any]) -> SigningKey:
    """Un JWK RSA (`n`, `e`) a su clave; un JWK mal formado lanza `ValueError`."""
    return SigningKey(modulus=b64url_int(str(jwk["n"])), exponent=b64url_int(str(jwk["e"])))
