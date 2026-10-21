"""Valores de Secrets Manager para el host del agente en AgentCore (ADR-033 D4).

El host lo ejecuta una vez al arrancar, con el Python del venv de
`requirements-aws-credentials.txt`, antes de cargar el código del usuario. Lee
por stdin `{"secrets": [{"secret", "version_stage"?}, ...]}` y escribe por
stdout `{"values": [...]}`, un `SecretString` por petición y en el mismo orden.
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
import sys
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


def main(stdin: IO[str], stdout: IO[str]) -> int:
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
    stdout.write(json.dumps({"values": values}, separators=(",", ":")))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.stdin, sys.stdout))
