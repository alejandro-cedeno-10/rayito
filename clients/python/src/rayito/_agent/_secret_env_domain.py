"""Secretos de Secrets Manager como variables de entorno del agente
(ADR-033 D4): los valores `SecretEnvRef` y `SecretEnvSource`, la
especificación que viaja en `RAYITO_SECRET_ENV` (sólo nombres y ARNs), los
secretos que el rol del runtime debe poder leer y la expansión de los valores
leídos a variables.

La expansión es la del host (`_runner/agent_host/secret_env.py`, sólo
biblioteca estándar): una sola implementación del algoritmo de
`testdata/agent-secret-env/expand.json` para AgentCore (la ejecuta el host
con el rol del runtime) y para MicroVMs (la ejecuta el SDK con las
credenciales de quien despliega). Puro: no llama a AWS ni crea clientes; la
lectura de cada secreto llega como una función.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rayito._agent._events import failure_message
from rayito._agent._runner.agent_host import secret_env as host_secret_env
from rayito._limits import (
    AGENT_SECRET_ENV_NAMES_VARIABLE,
    AGENT_SECRET_ENV_RESERVED_NAMES,
    AGENT_SECRET_ENV_RESERVED_PREFIXES,
    AGENT_SECRET_ENV_SPEC_VERSION,
)
from rayito.exceptions import AgentException, InvalidArgumentException

#: La herramienta que lee los secretos en AgentCore y la clave de `host.json`
#: que la apunta (`contract.json`, `constants.secretEnv`).
SCRIPT_NAME: Final = "aws_secret_env.py"
HOST_KEY: Final = host_secret_env.HOST_KEY
#: Lo que separa la clave JSON en `--secret-env NOMBRE=<id>#clave`: Secrets
#: Manager no lo admite en nombres ni en ARNs.
KEY_SEPARATOR: Final = "#"
#: Un nombre o ARN de secreto: los caracteres de `NameType` de Secrets Manager
#: (`AWS_API_NOTES.md` §19) más `:` de un ARN.
SECRET_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9/_+=.@:-]+")
#: Una clave JSON o una etiqueta de versión: ASCII imprimible sin espacios,
#: para que la especificación sea la misma en los dos SDK.
SECRET_ENV_KEY_PATTERN: Final = re.compile(r"[!-~]+")
ENV_NAME_PATTERN: Final = host_secret_env.ENV_NAME_PATTERN
FAILURE_REASON: Final = host_secret_env.FAILURE_REASON

SecretRequest = host_secret_env.SecretRequest
SecretReader = Callable[[str, str | None], str]
AsyncSecretReader = Callable[[str, str | None], Awaitable[str]]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidArgumentException(f"{label} debe ser una cadena no vacía")
    return value


@dataclass(frozen=True)
class SecretEnvRef:
    """Una variable de entorno del agente con el valor de un secreto de
    Secrets Manager: `secret` es su nombre completo (sin el prefijo `rayito/`
    de `SecretStore`) o su ARN; `key`, una clave de un secreto JSON (sin ella,
    la cadena entera); `version_stage`, la etiqueta de versión (sin ella,
    `AWSCURRENT`). Nunca lleva el valor.

    Coste y activación
    -------------------
    Activa: `AgentProject(secret_env={"NOMBRE": SecretEnvRef(...)})`,
        `[secret_env]` en el fichero de proyecto o `--secret-env`; sin ellos no
        se lee ningún secreto ni se crea ningún cliente de Secrets Manager.
    Recursos y llamadas AWS: un `GetSecretValue` por secreto al arrancar cada
        sesión (AgentCore, con el rol del runtime) o cada VM (MicroVMs, con tus
        credenciales). No crea nada.
    Coste aproximado: $0,05 por cada 10 000 llamadas a la API (us-east-1,
        consultado 2026-09-30 en https://aws.amazon.com/secrets-manager/pricing/);
        el secreto ya existe y ya lo pagas.
    IAM: en AgentCore, `secretsmanager:GetSecretValue` sólo sobre esos
        secretos, que Rayito añade al rol del runtime; en MicroVMs, el mismo
        permiso en tus credenciales.
    Cómo apagarla: quita la variable de `secret_env`.
    Ejemplo:
        AgentProject(..., secret_env={"OPENAI_API_KEY": SecretEnvRef("llm", key="api_key")})
    """

    secret: str
    key: str | None = None
    version_stage: str | None = None

    def __post_init__(self) -> None:
        _text(self.secret, "SecretEnvRef.secret")
        for label in ("key", "version_stage"):
            value = getattr(self, label)
            if value is not None:
                _text(value, f"SecretEnvRef.{label}")


@dataclass(frozen=True)
class SecretEnvSource:
    """Cada clave de un secreto JSON de Secrets Manager como una variable de
    entorno del agente: `secret` es su nombre completo o su ARN; `prefix` se
    antepone a cada clave; `keys`, si se da, son las únicas claves que se
    toman (y deben existir). Sin `keys`, cada clave debe ser un nombre de
    variable válido: ninguna se descarta en silencio.

    Coste y activación
    -------------------
    Activa: `AgentProject(secret_env_from=[SecretEnvSource(...)])`,
        `[[secret_env_from]]` en el fichero de proyecto o `--secret-env-from`;
        sin ellos no se lee ningún secreto.
    Recursos y llamadas AWS: un `GetSecretValue` por secreto al arrancar cada
        sesión (AgentCore) o cada VM (MicroVMs). No crea nada.
    Coste aproximado: $0,05 por cada 10 000 llamadas a la API (us-east-1,
        consultado 2026-09-30 en https://aws.amazon.com/secrets-manager/pricing/).
    IAM: `secretsmanager:GetSecretValue` sólo sobre ese secreto (el rol del
        runtime en AgentCore; tus credenciales en MicroVMs).
    Cómo apagarla: quita la entrada de `secret_env_from`.
    Ejemplo:
        SecretEnvSource("app/config", prefix="APP_", keys=("DB_HOST", "DB_USER"))
    """

    secret: str
    prefix: str = ""
    keys: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _text(self.secret, "SecretEnvSource.secret")
        if not isinstance(self.prefix, str):
            raise InvalidArgumentException("SecretEnvSource.prefix debe ser una cadena")
        keys: object = self.keys
        if keys is None:
            return
        if isinstance(keys, str) or not isinstance(keys, Sequence):
            raise InvalidArgumentException("SecretEnvSource.keys debe ser una lista de nombres")
        object.__setattr__(self, "keys", tuple(_text(key, "SecretEnvSource.keys") for key in keys))


SecretEnvValue = str | SecretEnvRef


def reference_of(value: SecretEnvValue) -> SecretEnvRef:
    """La referencia de un valor de `secret_env`: una cadena es el secreto."""
    return value if isinstance(value, SecretEnvRef) else SecretEnvRef(value)


def is_reserved_name(name: str, extra: Sequence[str] = ()) -> bool:
    """Si `name` es un nombre que un secreto no puede fijar
    (`testdata/agent-secret-env/reserved.json` más `extra`)."""
    return (
        name in AGENT_SECRET_ENV_RESERVED_NAMES
        or name.startswith(AGENT_SECRET_ENV_RESERVED_PREFIXES)
        or name in extra
    )


def has_reserved_prefix(prefix: str) -> bool:
    """Si todo nombre con `prefix` queda reservado."""
    return bool(prefix) and prefix.startswith(AGENT_SECRET_ENV_RESERVED_PREFIXES)


def uses_secret_env(
    secret_env: Mapping[str, SecretEnvValue], secret_env_from: Sequence[SecretEnvSource]
) -> bool:
    return bool(secret_env) or bool(secret_env_from)


def _reference_document(reference: SecretEnvRef) -> dict[str, str]:
    document = {"secret": reference.secret}
    if reference.key is not None:
        document["key"] = reference.key
    if reference.version_stage is not None:
        document["version_stage"] = reference.version_stage
    return document


def _source_document(source: SecretEnvSource) -> dict[str, object]:
    document: dict[str, object] = {"secret": source.secret}
    if source.prefix:
        document["prefix"] = source.prefix
    if source.keys is not None:
        document["keys"] = list(source.keys)
    return document


def spec_document(
    secret_env: Mapping[str, SecretEnvValue], secret_env_from: Sequence[SecretEnvSource]
) -> dict[str, Any] | None:
    """La especificación (`testdata/agent-secret-env/spec.json`): sólo
    nombres y ARNs; `None` sin secretos."""
    if not uses_secret_env(secret_env, secret_env_from):
        return None
    return {
        "env": {
            name: _reference_document(reference_of(value))
            for name, value in sorted(secret_env.items())
        },
        "from": [_source_document(source) for source in secret_env_from],
        "v": AGENT_SECRET_ENV_SPEC_VERSION,
    }


def spec_variable(
    secret_env: Mapping[str, SecretEnvValue], secret_env_from: Sequence[SecretEnvSource]
) -> str | None:
    """El valor de `RAYITO_SECRET_ENV`: JSON compacto con las claves
    ordenadas, o `None` sin secretos."""
    document = spec_document(secret_env, secret_env_from)
    if document is None:
        return None
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def injected_names_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Lo que acompaña a los valores que el SDK exporta en MicroVMs:
    `RAYITO_SECRET_ENV_NAMES` con sus nombres (nunca valores), para que el
    host los redacte en un diagnóstico de arranque como en AgentCore; nada
    sin secretos."""
    if not values:
        return {}
    names = host_secret_env.NAMES_SEPARATOR.join(sorted(values))
    return {AGENT_SECRET_ENV_NAMES_VARIABLE: names}


def secret_ids(
    secret_env: Mapping[str, SecretEnvValue], secret_env_from: Sequence[SecretEnvSource]
) -> tuple[str, ...]:
    """Los secretos referenciados, sin repetir: los de `secret_env` por nombre
    de variable y luego los de `secret_env_from` en orden."""
    references = [reference_of(secret_env[name]).secret for name in sorted(secret_env)]
    return tuple(dict.fromkeys([*references, *(source.secret for source in secret_env_from)]))


def secret_env_names(
    secret_env: Mapping[str, SecretEnvValue], secret_env_from: Sequence[SecretEnvSource]
) -> tuple[str, ...]:
    """Los nombres de variable que se sabe que se fijan sin leer ningún
    secreto: los de `secret_env` y las claves de una lista `keys` con su
    prefijo. Ordenados."""
    listed = [
        f"{source.prefix}{key}"
        for source in secret_env_from
        if source.keys is not None
        for key in source.keys
    ]
    return tuple(sorted({*secret_env, *listed}))


def unlisted_prefixes(secret_env_from: Sequence[SecretEnvSource]) -> tuple[str, ...]:
    """Los prefijos de las fuentes sin lista `keys`: sus variables sólo se
    conocen al leer el secreto."""
    return tuple(source.prefix for source in secret_env_from if source.keys is None)


def _failure(error: host_secret_env.SecretEnvError) -> AgentException:
    code = error.detail_code
    return AgentException(
        failure_message(FAILURE_REASON, code), reason=FAILURE_REASON, detail_code=code
    )


def _expanded(
    spec: Mapping[str, Any],
    requests: Sequence[SecretRequest],
    values: Sequence[str],
    reserved: Sequence[str],
) -> dict[str, str]:
    try:
        return host_secret_env.expand_spec(spec, dict(zip(requests, values, strict=True)), reserved)
    except host_secret_env.SecretEnvError as error:
        raise _failure(error) from None


def _requests_of(spec: Mapping[str, Any]) -> tuple[SecretRequest, ...]:
    try:
        return host_secret_env.secret_requests(spec)
    except host_secret_env.SecretEnvError as error:
        raise _failure(error) from None


def resolve_secret_env(
    spec: Mapping[str, Any], read: SecretReader, reserved: Sequence[str] = ()
) -> dict[str, str]:
    """Las variables de `spec`, leyendo cada secreto una vez con `read`. Un
    fallo de expansión es `AgentException(reason="runtime_error")` con el
    `detail_code` del host; nunca nombra un secreto ni un valor."""
    requests = _requests_of(spec)
    values = [read(secret, stage) for secret, stage in requests]
    return _expanded(spec, requests, values, reserved)


async def aresolve_secret_env(
    spec: Mapping[str, Any], read: AsyncSecretReader, reserved: Sequence[str] = ()
) -> dict[str, str]:
    """`resolve_secret_env` para asyncio."""
    requests = _requests_of(spec)
    values = [await read(secret, stage) for secret, stage in requests]
    return _expanded(spec, requests, values, reserved)


# ------------------------------------------------------------------ .env


#: Una línea `[export ]NOMBRE=valor` de un `.env`.
DOTENV_ASSIGNMENT: Final = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
DOTENV_COMMENT: Final = "#"
DOTENV_QUOTES: Final = ("'", '"')
#: Un comentario al final de un valor sin comillas: `#` tras un espacio.
DOTENV_INLINE_COMMENT: Final = re.compile(r"\s#")


def _dotenv_value(raw: str) -> str:
    """El valor de una asignación: entre comillas, tal cual; sin ellas, hasta
    un comentario precedido de espacio. Sin interpolación ni escapes."""
    text = raw.strip()
    quote = text[:1]
    if quote in DOTENV_QUOTES:
        closing = text.find(quote, 1)
        if closing > 0:
            return text[1:closing]
    comment = DOTENV_INLINE_COMMENT.search(text)
    return (text if comment is None else text[: comment.start()]).strip()


def _dotenv_assignments(text: str) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.strip().startswith(DOTENV_COMMENT):
            continue
        match = DOTENV_ASSIGNMENT.fullmatch(line)
        if match is not None:
            assignments.append((match.group(1), match.group(2)))
    return assignments


def dotenv_names(text: str) -> tuple[str, ...]:
    """Los nombres de variable de un `.env` (o `.env.example`), en orden y sin
    repetir; nunca interpreta un valor."""
    return tuple(dict.fromkeys(name for name, _ in _dotenv_assignments(text)))


def parse_dotenv(text: str) -> dict[str, str]:
    """Las variables de un `.env` (`testdata/agent-secret-env/dotenv.json`):
    `NOMBRE=valor`, `export`, comillas simples o dobles, comentarios; sin
    interpolación. Una variable repetida gana la última."""
    return {name: _dotenv_value(raw) for name, raw in _dotenv_assignments(text)}
