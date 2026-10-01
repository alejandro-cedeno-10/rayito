"""Dominio puro de `m15-custom-domain` (ADR-024): el formato de hostname
`{puerto}-{alias}.{dominio}`, las dos claves por ruta que guarda el
`KeyValueStore` de CloudFront y el contrato de su valor. Nada de aquí
importa `boto3`: el adaptador que de verdad lee y escribe el KVS vive en
`_kvs.py`, detrás del puerto `KeyValueStoreWriter`.

Por qué dos claves por ruta (`j:<label>` y `m:<label>`) en vez de una: un
JWE de `create-microvm-auth-token` mide 823 B (DOM-1,
`docs/research/2026-10-e2b-out-of-scope.md` §8); sumarle los metadatos de
enrutado (`endpoint`, hash del `traffic_token`, expiración) pasaría del
límite de 1 KiB por valor de CloudFront KeyValueStore
(`AWS_API_NOTES.md` §29). Separarlos dentro del mismo límite deja margen y
hace que el refresher opcional sólo tenga que reescribir `j:` al renovar el
JWE, sin tocar `m:`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from rayito._limits import RESERVED_PORTS
from rayito.exceptions import CustomDomainException, InvalidArgumentException

#: Límite real de un valor de CloudFront KeyValueStore (AWS_API_NOTES.md
#: §29); un JWE de 823 B (DOM-1) más los metadatos de abajo caben de sobra.
MAX_KVS_VALUE_BYTES: Final = 1024

#: Puerto más alto válido en TCP; `validate_route_port` lo usa como tope.
MAX_TCP_PORT: Final = 65535

#: Una etiqueta DNS (RFC 1035): empieza y termina en alfanumérico, el resto
#: alfanumérico o guion, 1-63 caracteres.
_DNS_LABEL_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: Un nombre de dominio completo: etiquetas DNS separadas por puntos, sin
#: etiqueta comodín (`*`) aquí — el comodín lo añade `CustomDomain.deploy()`
#: al alias de CloudFront (`*.<public_domain>`), nunca el dominio en sí.
_DOMAIN_LABEL_SEPARATOR: Final = "."

#: Un alias de ruta es el identificador del sandbox: minúsculas, dígitos y
#: guiones (como cualquier UUID en minúsculas, `SandboxInfo.sandbox_id`),
#: nunca empezando ni terminando en guion — la misma forma que una etiqueta
#: DNS, aunque el guion que añade `route_label` ante el puerto ya evitaría
#: un hostname inválido: es defensa en profundidad, no una necesidad del
#: formato en sí.
_ALIAS_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,53}[a-z0-9])?$")

#: Un hostname completo no pasa de 253 caracteres (RFC 1035 §3.1); 55 para
#: el alias deja sitio al puerto (hasta 5 dígitos), el guion separador y un
#: `public_domain` razonable sin acercarse al límite real de CloudFront.
MAX_HOST_LENGTH: Final = 253


def validate_public_domain(value: str) -> str:
    """Cada etiqueta separada por puntos cumple la regla DNS; no se resuelve
    ni se comprueba el certificado aquí (eso es `deploy(certificate_arn=)`)."""
    labels = value.split(_DOMAIN_LABEL_SEPARATOR)
    if not value or any(not _DNS_LABEL_RE.match(label) for label in labels):
        raise InvalidArgumentException(f"public_domain inválido: {value!r}")
    return value


def validate_alias(value: str) -> str:
    """El alias es el `sandbox_id`: ya es un UUID en minúsculas, pero se
    valida igual porque forma parte de un hostname público."""
    if not _ALIAS_RE.match(value):
        raise InvalidArgumentException(f"alias de ruta inválido: {value!r}")
    return value


def validate_route_port(port: int) -> int:
    """`1..65535`, y nunca uno de `RESERVED_PORTS` (ADR-006: el puerto de
    hooks; `limits.json` `reservedPorts`, M15 foundations)."""
    if not 1 <= port <= MAX_TCP_PORT:
        raise InvalidArgumentException(f"puerto de ruta fuera de rango: {port}")
    if port in RESERVED_PORTS:
        raise InvalidArgumentException(
            f"puerto de ruta reservado: {port} (reservedPorts: {list(RESERVED_PORTS)})"
        )
    return port


def route_label(alias: str, port: int) -> str:
    """La etiqueta DNS de una ruta: `{puerto}-{alias}`. Valida ambos antes."""
    validate_alias(alias)
    validate_route_port(port)
    label = f"{port}-{alias}"
    if not _DNS_LABEL_RE.match(label):
        raise InvalidArgumentException(f"etiqueta de ruta inválida: {label!r}")
    return label


def route_host(alias: str, port: int, public_domain: str) -> str:
    """El hostname público de una ruta: `{puerto}-{alias}.{public_domain}`."""
    label = route_label(alias, port)
    validate_public_domain(public_domain)
    host = f"{label}.{public_domain}"
    if len(host) > MAX_HOST_LENGTH:
        raise InvalidArgumentException(f"hostname de ruta demasiado largo ({len(host)} car.)")
    return host


def kvs_json_key(label: str) -> str:
    """La clave del JWE de la ruta (`j:<label>`); el refresher sólo reescribe ésta."""
    return f"j:{label}"


def kvs_meta_key(label: str) -> str:
    """La clave de metadatos de la ruta (`m:<label>`)."""
    return f"m:{label}"


def traffic_token_digest(traffic_token: str | None) -> str:
    """El sha256 hexadecimal de `traffic_token`, o cadena vacía si no hay
    uno (ruta pública, `public=True`): nunca se guarda el token en claro en
    el KVS, sólo su huella (SEC-10)."""
    if traffic_token is None:
        return ""
    return hashlib.sha256(traffic_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RouteMetadata:
    """El valor de `m:<label>`: a qué endpoint reenvía la Function, el
    sha256 de `traffic_token` (vacío en una ruta pública) y cuándo caduca
    el JWE de `j:<label>` (epoch, segundos)."""

    endpoint: str
    traffic_token_sha256: str
    expires_at: int

    def encode(self) -> str:
        # Claves de una letra (`e`/`t`/`x`) a propósito: cada byte cuenta
        # dentro de MAX_KVS_VALUE_BYTES (DOM-1).
        payload = {"e": self.endpoint, "t": self.traffic_token_sha256, "x": self.expires_at}
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        check_kvs_value_size(encoded)
        return encoded

    @classmethod
    def decode(cls, raw: str) -> RouteMetadata:
        payload = json.loads(raw)
        return cls(
            endpoint=payload["e"], traffic_token_sha256=payload["t"], expires_at=payload["x"]
        )


def check_kvs_value_size(value: str) -> None:
    """`CustomDomainException` no tiene `code=` (a diferencia de
    `StackException`/`GatewayException`): es la única forma en que esta
    función puede fallar, así que el mensaje ya lo dice todo."""
    size = len(value.encode("utf-8"))
    if size > MAX_KVS_VALUE_BYTES:
        raise CustomDomainException(
            f"valor de KeyValueStore de {size} B supera el límite de {MAX_KVS_VALUE_BYTES} B "
            "(kvs_value_too_large)"
        )
