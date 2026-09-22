"""Constructor del `runHookPayload` (ADR-004).

Es el único canal per-VM de `run-microvm`. Lleva el sha256 del access token
del agente, nunca el token: AWS puede registrar el payload en CloudTrail.

Contrato con `rayd` (ARCHITECTURE.md "Auth interna"): el access token es
`base64url(secret)` sin padding; `token_sha256 = sha256(secret).hex()`, es
decir, el hash de los **bytes decodificados**, no de la cadena. `rayd`
decodifica `x-access-token`, tolera padding y compara en tiempo constante.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Final

from rayito._limits import RUN_HOOK_PAYLOAD_MAX_CHARS
from rayito.exceptions import InvalidArgumentException

PAYLOAD_VERSION: Final = 1
DEFAULT_USER: Final = "user"
DEFAULT_WORKDIR: Final = "/home/user"
ACCESS_TOKEN_BYTES: Final = 32
CPU_TIME_LIMIT_MIN_SECONDS: Final = 1
CPU_TIME_LIMIT_MAX_SECONDS: Final = 28_800


def generate_access_token() -> str:
    return encode_access_token(secrets.token_bytes(ACCESS_TOKEN_BYTES))


def encode_access_token(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


def decode_access_token(access_token: str) -> bytes:
    """Los bytes que `rayd` hashea. Falla si el token no es base64url canónico
    (alfabeto estricto, bits sobrantes a cero), igual que el decoder de `rayd`."""
    stripped = access_token.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        secret = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidArgumentException("access_token no es base64url") from exc
    if not secret or encode_access_token(secret) != stripped:
        raise InvalidArgumentException("access_token no es base64url")
    return secret


def validate_access_token(access_token: str) -> str:
    decode_access_token(access_token)
    return access_token


def access_token_sha256(access_token: str) -> str:
    return hashlib.sha256(decode_access_token(access_token)).hexdigest()


def build_run_hook_payload(
    *,
    access_token: str,
    envs: Mapping[str, str] | None = None,
    metadata: Mapping[str, str] | None = None,
    user: str = DEFAULT_USER,
    workdir: str = DEFAULT_WORKDIR,
    cpu_time_limit: int | None = None,
) -> str:
    """Serializa el payload y falla si supera el límite del modelo (4096 chars).

    `envs` y `metadata` se omiten del JSON cuando están vacíos para no gastar
    caracteres. `metadata` son etiquetas no secretas que `rayd` devuelve en
    `Health` (viajan como `envs`: CloudTrail puede registrar el payload).
    `cpu_time_limit` (segundos de CPU, no de pared, `1..=28800`) viaja como
    `limits: {"cpu_seconds": N}` sólo cuando se pasa: `rayd` lo aplica como
    `RLIMIT_CPU` a cada proceso y PTY del sandbox, nunca al kernel.
    """
    if not access_token:
        raise InvalidArgumentException("access_token no puede estar vacío")
    payload: dict[str, object] = {
        "v": PAYLOAD_VERSION,
        "token_sha256": access_token_sha256(access_token),
        "user": user,
        "workdir": workdir,
    }
    if envs:
        payload["envs"] = validated_envs(envs)
    if metadata:
        payload["metadata"] = validated_metadata(metadata)
    if cpu_time_limit is not None:
        payload["limits"] = {"cpu_seconds": validated_cpu_time_limit(cpu_time_limit)}
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(text) > RUN_HOOK_PAYLOAD_MAX_CHARS:
        raise InvalidArgumentException(
            f"runHookPayload ocupa {len(text)} caracteres y el máximo es "
            f"{RUN_HOOK_PAYLOAD_MAX_CHARS}. Reduce `envs` o `metadata` en create(); las "
            "variables grandes van en `envs` de cada comando o en un fichero con "
            "files.write()."
        )
    return text


def validated_cpu_time_limit(cpu_time_limit: int) -> int:
    if (
        isinstance(cpu_time_limit, bool)
        or not isinstance(cpu_time_limit, int)
        or not CPU_TIME_LIMIT_MIN_SECONDS <= cpu_time_limit <= CPU_TIME_LIMIT_MAX_SECONDS
    ):
        raise InvalidArgumentException(
            f"cpu_time_limit debe ser un entero entre {CPU_TIME_LIMIT_MIN_SECONDS} y "
            f"{CPU_TIME_LIMIT_MAX_SECONDS} segundos de CPU, recibido {cpu_time_limit!r}"
        )
    return cpu_time_limit


def validated_envs(envs: Mapping[str, str]) -> dict[str, str]:
    return validated_string_map(envs, field="envs")


def validated_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    return validated_string_map(metadata, field="metadata")


def validated_string_map(values: Mapping[str, str], *, field: str) -> dict[str, str]:
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise InvalidArgumentException(f"clave de {field} inválida: {key!r}")
        if not isinstance(value, str):
            raise InvalidArgumentException(f"el valor de {field}[{key!r}] debe ser str")
    return dict(values)
