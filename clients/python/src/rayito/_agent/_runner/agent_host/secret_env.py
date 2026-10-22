"""Secretos de Secrets Manager como variables de entorno (ADR-033 D4).

En AgentCore el runtime recibe sólo la especificación (`RAYITO_SECRET_ENV`:
nombres de variable, nombres o ARNs de secreto, claves JSON; nunca valores).
Antes de crear el driver, el host la saca de su entorno, pide los valores a
la herramienta `aws_secret_env.py` (el venv de botocore del export, con el rol
del runtime), los expande con el algoritmo de
`testdata/agent-secret-env/expand.json`, rechaza los nombres reservados y los
exporta. Cualquier fallo es cerrado: `SecretEnvError` con un `detail_code` de
`FAILURE_CODES` y el driver no arranca. Ningún mensaje, log ni código lleva el
nombre de un secreto, de una clave ni un valor.

El algoritmo de expansión (`expand_spec`, `expand_reference`,
`expand_source`) es puro: el SDK de Python lo reutiliza para MicroVMs, donde
lee los valores con las credenciales de quien despliega. Sólo biblioteca
estándar y nada de `rayito`.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from typing import Any, Final

from .protocol import AgentFailure

#: La variable del runtime con la especificación (`agentSecretEnvVariable`).
SPEC_VARIABLE: Final = "RAYITO_SECRET_ENV"
#: En MicroVMs el SDK ya pone los valores en el entorno del host y aquí sólo
#: manda sus nombres, separados por `NAMES_SEPARATOR`
#: (`agentSecretEnvNamesVariable`), para redactarlos igual que en AgentCore.
NAMES_VARIABLE: Final = "RAYITO_SECRET_ENV_NAMES"
NAMES_SEPARATOR: Final = ","
#: Versión del documento de la especificación (`agentSecretEnvSpecVersion`).
SPEC_VERSION: Final = 1
#: Tope de secretos por despliegue (`agentSecretEnvMaxSecrets`).
MAX_SECRETS: Final = 20
#: Tope de bytes UTF-8 de todos los valores exportados (`agentSecretEnvMaxTotalBytes`).
MAX_TOTAL_BYTES: Final = 262144
#: Plazo de la herramienta (`agentSecretEnvHelperTimeoutSeconds`, provisional:
#: se mide en la aceptación, `AWS_API_NOTES.md` Q188).
HELPER_TIMEOUT_SECONDS: Final = 15
#: Nombres y prefijos que un secreto no puede fijar
#: (`agentSecretEnvReservedNames`, `agentSecretEnvReservedPrefixes`).
RESERVED_NAMES: Final = frozenset({"HOME", "NODE_OPTIONS", "PATH", "PYTHONPATH"})
RESERVED_PREFIXES: Final = ("AWS_", "LD_", "RAYITO_")
#: Clave de `host.json` con la herramienta (`python`, `script`) y los nombres
#: que el runtime ya fija (`reserved`).
HOST_KEY: Final = "secret_env"
FAILURE_REASON: Final = "runtime_error"
UNAVAILABLE: Final = "secret_env_unavailable"
DENIED: Final = "secret_env_denied"
INVALID: Final = "secret_env_invalid"
INVALID_KEY: Final = "secret_env_invalid_key"
RESERVED: Final = "secret_env_reserved"
FAILURE_CODES: Final = (UNAVAILABLE, DENIED, INVALID, INVALID_KEY, RESERVED)
#: Códigos de salida de `aws_secret_env.py` que no son `UNAVAILABLE`.
HELPER_EXIT_DENIED: Final = 3
HELPER_EXIT_INVALID: Final = 4
HELPER_EXIT_CODES: Final[Mapping[int, str]] = {
    HELPER_EXIT_DENIED: DENIED,
    HELPER_EXIT_INVALID: INVALID,
}
ENV_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Argumento con el que la herramienta recibe el número del descriptor por el
#: que devuelve los valores. Su stdout y su stderr van a `/dev/null`: un valor
#: nunca pasa por la salida estándar de un proceso (`aws_secret_env.py`,
#: `VALUES_FD_ARGUMENT`).
VALUES_FD_ARGUMENT: Final = "--values-fd"
#: Bytes que se leen de una vez del descriptor de los valores.
READ_CHUNK_BYTES: Final = 65536
#: Marca de un documento que no es JSON (`_json_or_invalid`).
_NOT_JSON: Final = object()

#: Un secreto pedido a Secrets Manager: su nombre o ARN y la etiqueta de
#: versión (`None` es `AWSCURRENT`).
SecretRequest = tuple[str, str | None]
SecretFetcher = Callable[[Sequence[SecretRequest]], Sequence[str]]


class SecretEnvError(AgentFailure):
    """Un fallo al cargar los secretos: `detail_code` es uno de
    `FAILURE_CODES` y el mensaje es sólo ese código."""

    def __init__(self, code: str) -> None:
        super().__init__(FAILURE_REASON, code)


def is_reserved(name: str, extra: Collection[str] = ()) -> bool:
    """Si `name` es un nombre que un secreto no puede fijar: los de
    `RESERVED_NAMES`, los que empiezan por un prefijo reservado y los de
    `extra` (las variables que el runtime ya fija)."""
    return name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES) or name in extra


def _json_or_invalid(text: str | bytes) -> object:
    """`text` decodificado como JSON, o `_NOT_JSON`. El error de `json` lleva
    el documento entero (`JSONDecodeError.doc`, un valor de secreto); se
    descarta aquí para que ninguna excepción posterior lo tenga en su
    `__context__`."""
    try:
        return json.loads(text)
    except ValueError:
        return _NOT_JSON


def _json_object(secret_string: str) -> dict[str, Any]:
    document = _json_or_invalid(secret_string)
    if not isinstance(document, dict):
        raise SecretEnvError(INVALID)
    return document


def expand_reference(reference: Mapping[str, Any], secret_string: str) -> str:
    """El valor de una referencia de `secret_env`: la cadena entera o, con
    `key`, esa clave de tipo cadena del objeto JSON."""
    key = reference.get("key")
    if key is None:
        return secret_string
    document = _json_object(secret_string)
    if key not in document:
        raise SecretEnvError(INVALID_KEY)
    value = document[key]
    if not isinstance(value, str):
        raise SecretEnvError(INVALID)
    return value


def _allow_listed(document: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    missing = [key for key in keys if key not in document]
    if missing:
        raise SecretEnvError(INVALID_KEY)
    return {key: document[key] for key in keys}


def expand_source(
    source: Mapping[str, Any], secret_string: str, reserved: Collection[str] = ()
) -> dict[str, str]:
    """Las variables de un `secret_env_from`: cada clave del objeto JSON (o
    sólo las de `keys`, que deben existir), con el prefijo, como nombre de
    variable válido y no reservado. Ninguna clave se descarta en silencio."""
    document = _json_object(secret_string)
    keys = source.get("keys")
    chosen = document if keys is None else _allow_listed(document, keys)
    prefix = source.get("prefix", "")
    variables: dict[str, str] = {}
    for key, value in chosen.items():
        name = f"{prefix}{key}"
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise SecretEnvError(INVALID_KEY)
        if is_reserved(name, reserved):
            raise SecretEnvError(RESERVED)
        if not isinstance(value, str):
            raise SecretEnvError(INVALID)
        variables[name] = value
    return variables


def _valid_reference(reference: object) -> bool:
    return (
        isinstance(reference, dict)
        and isinstance(reference.get("secret"), str)
        and all(isinstance(reference.get(key, ""), str) for key in ("key", "version_stage"))
    )


def _valid_source(source: object) -> bool:
    keys = source.get("keys", []) if isinstance(source, dict) else None
    return (
        isinstance(source, dict)
        and isinstance(source.get("secret"), str)
        and isinstance(source.get("prefix", ""), str)
        and isinstance(keys, list)
        and all(isinstance(key, str) for key in keys)
    )


def parse_spec(text: str) -> dict[str, Any]:
    """El documento de `RAYITO_SECRET_ENV` validado, o `INVALID`."""
    spec = _json_or_invalid(text)
    valid = (
        isinstance(spec, dict)
        and spec.get("v") == SPEC_VERSION
        and isinstance(spec.get("env"), dict)
        and isinstance(spec.get("from"), list)
        and all(_valid_reference(reference) for reference in spec["env"].values())
        and all(_valid_source(source) for source in spec["from"])
    )
    if not valid or not isinstance(spec, dict):
        raise SecretEnvError(INVALID)
    return dict(spec)


def _request_of(item: Mapping[str, Any]) -> SecretRequest:
    return (str(item["secret"]), item.get("version_stage"))


def secret_requests(spec: Mapping[str, Any]) -> tuple[SecretRequest, ...]:
    """Los secretos a pedir, sin repetir: los de `env` por nombre de variable
    y luego los de `from` en orden. Más de `MAX_SECRETS` es `INVALID`."""
    references = [spec["env"][name] for name in sorted(spec["env"])]
    requests = tuple(dict.fromkeys(_request_of(item) for item in [*references, *spec["from"]]))
    if len(requests) > MAX_SECRETS:
        raise SecretEnvError(INVALID)
    return requests


def expand_spec(
    spec: Mapping[str, Any],
    values: Mapping[SecretRequest, str],
    reserved: Collection[str] = (),
) -> dict[str, str]:
    """Las variables de toda la especificación a partir de los valores de
    cada secreto (`secret_requests`). Un nombre repetido entre `env` y
    `from` es `INVALID_KEY`; pasar de `MAX_TOTAL_BYTES` es `INVALID`."""
    variables: dict[str, str] = {}
    for name in sorted(spec["env"]):
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise SecretEnvError(INVALID_KEY)
        if is_reserved(name, reserved):
            raise SecretEnvError(RESERVED)
        reference = spec["env"][name]
        variables[name] = expand_reference(reference, values[_request_of(reference)])
    for source in spec["from"]:
        expanded = expand_source(source, values[_request_of(source)], reserved)
        if set(expanded) & set(variables):
            raise SecretEnvError(INVALID_KEY)
        variables.update(expanded)
    if sum(len(value.encode("utf-8")) for value in variables.values()) > MAX_TOTAL_BYTES:
        raise SecretEnvError(INVALID)
    return variables


class SecretEnvHelper:
    """Ejecuta `python script VALUES_FD_ARGUMENT <fd>` con las peticiones por
    stdin y lee los valores, en el mismo orden, del extremo de lectura de un
    `os.pipe()` cuyo extremo de escritura sólo hereda la herramienta. Su
    stdout y su stderr van a `/dev/null`: nunca se leen ni se guardan."""

    def __init__(
        self,
        python: str,
        script: str,
        *,
        timeout_seconds: float = HELPER_TIMEOUT_SECONDS,
    ) -> None:
        self._argv = [python, script]
        self._timeout_seconds = timeout_seconds

    def __call__(self, requests: Sequence[SecretRequest]) -> Sequence[str]:
        payload = json.dumps({"secrets": [_request_document(item) for item in requests]})
        outcome = _run_helper(self._argv, payload.encode("utf-8"), self._timeout_seconds)
        if outcome is None:
            raise SecretEnvError(UNAVAILABLE)
        returncode, output = outcome
        if returncode != 0:
            raise SecretEnvError(HELPER_EXIT_CODES.get(returncode, UNAVAILABLE))
        return _helper_values(output, len(requests))


def _run_helper(
    argv: Sequence[str], payload: bytes, timeout_seconds: float
) -> tuple[int, bytes] | None:
    """El código de salida de la herramienta y lo que escribió en su
    descriptor, o `None` si no arrancó, no terminó en `timeout_seconds` (que
    cubre la lectura y la espera) o falló la tubería. El fallo se devuelve,
    no se lanza, para que ningún error posterior arrastre estos bytes."""
    deadline = time.monotonic() + timeout_seconds
    read_fd, write_fd = os.pipe()
    try:
        return _exchange(argv, payload, read_fd, write_fd, deadline)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        os.close(read_fd)


def _exchange(
    argv: Sequence[str], payload: bytes, read_fd: int, write_fd: int, deadline: float
) -> tuple[int, bytes]:
    try:
        process = subprocess.Popen(
            [*argv, VALUES_FD_ARGUMENT, str(write_fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
        )
    finally:
        os.close(write_fd)
    with process:
        try:
            _send(process, payload)
            output = _read_until_eof(read_fd, deadline)
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            raise
    return returncode, output


def _send(process: subprocess.Popen[bytes], payload: bytes) -> None:
    """Manda las peticiones y cierra stdin. Una herramienta que ya salió
    (tubería rota) se juzga por su código de salida, como `communicate`."""
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(payload)
        stdin.close()
    except BrokenPipeError:
        pass


def _read_until_eof(fd: int, deadline: float) -> bytes:
    """Todo lo que llega por `fd` hasta que la herramienta cierra su extremo,
    o `subprocess.TimeoutExpired` si vence `deadline` antes."""
    chunks: list[bytes] = []
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(VALUES_FD_ARGUMENT, 0)
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def _request_document(request: SecretRequest) -> dict[str, str]:
    secret, stage = request
    return {"secret": secret} if stage is None else {"secret": secret, "version_stage": stage}


def _helper_values(output: bytes, expected: int) -> list[str]:
    document = _json_or_invalid(output)
    values = document.get("values") if isinstance(document, dict) else None
    if (
        not isinstance(values, list)
        or len(values) != expected
        or not all(isinstance(value, str) for value in values)
    ):
        raise SecretEnvError(UNAVAILABLE)
    return values


def helper_of(config: object) -> SecretEnvHelper | None:
    """La herramienta de la clave `secret_env` de `host.json`, o `None`."""
    if not isinstance(config, dict):
        return None
    python, script = config.get("python"), config.get("script")
    if not isinstance(python, str) or not isinstance(script, str):
        return None
    return SecretEnvHelper(python, script)


def reserved_of(config: object) -> frozenset[str]:
    """Los nombres que el runtime ya fija (`reserved` de esa clave)."""
    names = config.get("reserved") if isinstance(config, dict) else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name for name in names if isinstance(name, str))


def injected_values(environ: MutableMapping[str, str]) -> tuple[str, ...]:
    """Los valores que el SDK ya exportó en MicroVMs: saca `NAMES_VARIABLE`
    de `environ` (sólo nombres) y devuelve el valor de cada una de esas
    variables, para redactarlo en un diagnóstico de arranque. Sin la
    variable, nada."""
    names = environ.pop(NAMES_VARIABLE, "")
    return tuple(environ[name] for name in names.split(NAMES_SEPARATOR) if name in environ)


def load_secret_env(
    environ: MutableMapping[str, str],
    fetch: SecretFetcher | None,
    reserved: Collection[str] = (),
) -> dict[str, str]:
    """Saca `SPEC_VARIABLE` de `environ`, lee los secretos con `fetch` y
    exporta sus variables en `environ`. Devuelve lo exportado (para redactar
    los valores en un diagnóstico); sin especificación, nada. Sin `fetch`
    (un `host.json` sin herramienta), `UNAVAILABLE`."""
    text = environ.pop(SPEC_VARIABLE, None)
    if text is None:
        return {}
    if fetch is None:
        raise SecretEnvError(UNAVAILABLE)
    spec = parse_spec(text)
    requests = secret_requests(spec)
    values = fetch(requests)
    variables = expand_spec(spec, dict(zip(requests, values, strict=True)), reserved)
    environ.update(variables)
    return variables
