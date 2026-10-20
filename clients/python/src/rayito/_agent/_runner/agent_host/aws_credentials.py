"""Las credenciales del rol de AgentCore Runtime para los adaptadores de Node
que sólo las leen de variables de entorno (agentcore-deploy, ADR-031). Antes
de cada invocación el host ejecuta `python script` (la herramienta
`aws_env_credentials.py` con el venv de botocore que instala el export), lee su
salida y la manda al puente de Node, que la pone en el entorno de esa
invocación. Nunca escribe las credenciales en un log, en un fichero ni en un
mensaje de error. Sólo biblioteca estándar y nada de `rayito`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from typing import Final

from .protocol import AgentFailure

#: Plazo de la herramienta (`hostCredentialsTimeoutSeconds` de `contract.json`).
HELPER_TIMEOUT_SECONDS: Final = 10
#: El `detail_code` con el que termina la invocación si no hay credenciales.
FAILURE_DETAIL_CODE: Final = "aws_credentials_unavailable"
#: Las únicas variables que la herramienta puede devolver (`awsEnvCredentialVariables`).
ALLOWED_VARIABLES: Final = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
REQUIRED_VARIABLES: Final = ALLOWED_VARIABLES[:2]


def _unavailable() -> AgentFailure:
    return AgentFailure("runtime_error", FAILURE_DETAIL_CODE)


class CredentialsHelper:
    """Lee las credenciales vigentes del rol ejecutando la herramienta; una
    llamada, una lectura (las credenciales del rol rotan)."""

    def __init__(self, python: str, script: str) -> None:
        self._argv = [python, script]

    def __call__(self) -> Mapping[str, str]:
        try:
            completed = subprocess.run(
                self._argv, capture_output=True, timeout=HELPER_TIMEOUT_SECONDS, check=False
            )
        except (OSError, subprocess.SubprocessError):
            raise _unavailable() from None
        if completed.returncode != 0:
            raise _unavailable()
        return _parse(completed.stdout)


def _parse(output: bytes) -> dict[str, str]:
    try:
        document = json.loads(output)
    except ValueError:
        raise _unavailable() from None
    valid = (
        isinstance(document, dict)
        and set(document) <= set(ALLOWED_VARIABLES)
        and all(
            isinstance(document.get(name), str) and document[name] for name in REQUIRED_VARIABLES
        )
        and all(isinstance(value, str) for value in document.values())
    )
    if not valid:
        raise _unavailable()
    return dict(document)
