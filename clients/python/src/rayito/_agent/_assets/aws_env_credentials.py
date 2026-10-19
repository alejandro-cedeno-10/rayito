"""Credenciales del rol de AgentCore Runtime para los adaptadores de Node que
sólo las leen de variables de entorno (agentcore-deploy, ADR-031).

El host del agente lo ejecuta con el Python del venv de
`requirements-aws-credentials.txt` antes de cada invocación de un adaptador de
`AGENTCORE_ENV_CREDENTIAL_ADAPTERS` y pasa el resultado al puente de Node, que
lo pone en el entorno de esa invocación. La cadena de credenciales por defecto
de botocore es la que las obtiene del servicio de metadatos de la microVM; este
script no conoce ninguna ruta ni cabecera del servicio.

Escribe por stdout un único objeto JSON con `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` y, si la credencial es temporal, `AWS_SESSION_TOKEN`.
Sin credenciales sale con `EXIT_NO_CREDENTIALS` y no escribe nada. Nunca
escribe las credenciales en stderr ni en un fichero.
"""

from __future__ import annotations

import json
import sys
from typing import Final

import botocore.session

ACCESS_KEY_VARIABLE: Final = "AWS_ACCESS_KEY_ID"
SECRET_KEY_VARIABLE: Final = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_VARIABLE: Final = "AWS_SESSION_TOKEN"
EXIT_OK: Final = 0
EXIT_NO_CREDENTIALS: Final = 1


def credential_variables() -> dict[str, str] | None:
    """Las variables de entorno de la credencial vigente, o `None` si la
    cadena por defecto no encuentra ninguna."""
    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        return None
    frozen = credentials.get_frozen_credentials()
    variables = {ACCESS_KEY_VARIABLE: frozen.access_key, SECRET_KEY_VARIABLE: frozen.secret_key}
    if frozen.token:
        variables[SESSION_TOKEN_VARIABLE] = frozen.token
    return variables


def main() -> int:
    variables = credential_variables()
    if variables is None:
        return EXIT_NO_CREDENTIALS
    sys.stdout.write(json.dumps(variables, separators=(",", ":")))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
