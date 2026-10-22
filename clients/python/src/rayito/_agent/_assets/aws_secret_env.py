"""Valores de Secrets Manager para el host del agente en AgentCore (ADR-033 D4).

El host lo ejecuta una vez al arrancar, con el Python del venv de
`requirements-aws-credentials.txt`, antes de cargar el código del usuario, como
`python aws_secret_env.py --values-fd <fd>`. Lee por stdin
`{"secrets": [{"secret", "version_stage"?}, ...]}` y escribe `{"values": [...]}`,
un `SecretString` por petición y en el mismo orden, en el descriptor `<fd>`
(el extremo de escritura de una tubería del host), nunca en stdout: el host
manda su stdout y su stderr a `/dev/null`.
Sólo llama a `GetSecretValue` con `SecretId` y, si se pide, `VersionStage`
(`AWS_API_NOTES.md` §19), con la cadena de credenciales por defecto de
botocore (el rol del runtime, por el servicio de metadatos de la microVM) y la
región de `AWS_REGION`.

Nunca escribe un nombre de secreto ni un valor en stderr, ni el mensaje de
AWS: un fallo es sólo su código de salida (`EXIT_DENIED`, `EXIT_INVALID` o
`EXIT_UNAVAILABLE`), que el host traduce a su `detail_code`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from typing import IO, Any, Final

import botocore.session
from botocore.exceptions import BotoCoreError, ClientError

SERVICE: Final = "secretsmanager"
EXIT_OK: Final = 0
EXIT_UNAVAILABLE: Final = 1
EXIT_DENIED: Final = 3
EXIT_INVALID: Final = 4
#: Códigos de `GetSecretValue` (`AWS_API_NOTES.md` §19) que significan que el
#: rol no puede leer el secreto: sin permiso o inexistente para él.
DENIED_CODES: Final = frozenset({"AccessDeniedException", "ResourceNotFoundException"})
#: Argumento con el número del descriptor de los valores (`VALUES_FD_ARGUMENT`
#: del host, `agent_host/secret_env.py`).
VALUES_FD_ARGUMENT: Final = "--values-fd"


class _InvalidSecret(Exception):
    """Un secreto sin `SecretString` (binario) o una petición mal formada."""


def _requests(text: str) -> list[dict[str, Any]]:
    try:
        document = json.loads(text)
    except ValueError:
        raise _InvalidSecret from None
    secrets = document.get("secrets") if isinstance(document, dict) else None
    if not isinstance(secrets, list) or not all(
        isinstance(item, dict) and isinstance(item.get("secret"), str) for item in secrets
    ):
        raise _InvalidSecret
    return secrets


def _secret_string(client: Any, request: dict[str, Any]) -> str:
    parameters = {"SecretId": request["secret"]}
    stage = request.get("version_stage")
    if isinstance(stage, str):
        parameters["VersionStage"] = stage
    value = client.get_secret_value(**parameters).get("SecretString")
    if not isinstance(value, str):
        raise _InvalidSecret
    return value


def _exit_code(error: ClientError) -> int:
    code = error.response.get("Error", {}).get("Code")
    return EXIT_DENIED if code in DENIED_CODES else EXIT_UNAVAILABLE


def _write_all(fd: int, data: bytes) -> None:
    """Escribe `data` entero en `fd`: `os.write` puede escribir menos bytes
    de los pedidos en una tubería."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def values_fd(argv: Sequence[str]) -> int | None:
    """El descriptor de `VALUES_FD_ARGUMENT <fd>` en `argv`, o `None`."""
    if len(argv) != 2 or argv[0] != VALUES_FD_ARGUMENT or not argv[1].isdigit():
        return None
    return int(argv[1])


def main(stdin: IO[str], fd: int) -> int:
    """Lee las peticiones de `stdin` y escribe los valores en `fd`, que cierra
    siempre. Devuelve el código de salida."""
    try:
        try:
            requests = _requests(stdin.read())
            client = botocore.session.get_session().create_client(SERVICE)
            values = [_secret_string(client, request) for request in requests]
        except _InvalidSecret:
            return EXIT_INVALID
        except ClientError as error:
            return _exit_code(error)
        except BotoCoreError:
            return EXIT_UNAVAILABLE
        document = json.dumps({"values": values}, separators=(",", ":")).encode("utf-8")
        try:
            _write_all(fd, document)
        except OSError:
            return EXIT_UNAVAILABLE
        return EXIT_OK
    finally:
        os.close(fd)


def run(argv: Sequence[str]) -> int:
    """`main` con el descriptor de `argv`; sin él, `EXIT_UNAVAILABLE`."""
    fd = values_fd(argv)
    if fd is None:
        return EXIT_UNAVAILABLE
    return main(sys.stdin, fd)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
