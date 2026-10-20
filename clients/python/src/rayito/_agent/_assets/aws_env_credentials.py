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

Con `--credential-process` escribe, en cambio, el documento que los SDK de AWS
esperan de un `credential_process` de un perfil (`Version` 1, `AccessKeyId`,
`SecretAccessKey`, `SessionToken` y `Expiration`): así un proceso de larga vida
que sólo arranca su proveedor de Bedrock con un perfil (OpenCode,
`AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES`) obtiene credenciales que el SDK
refresca al caducar. Antes de abrir la sesión de botocore quita
`AWS_PROFILE` y `AWS_CONFIG_FILE`: el perfil que lo invoca apunta a este mismo
script y, si no, se llamaría a sí mismo.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Final

import botocore.session

ACCESS_KEY_VARIABLE: Final = "AWS_ACCESS_KEY_ID"
SECRET_KEY_VARIABLE: Final = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_VARIABLE: Final = "AWS_SESSION_TOKEN"
CREDENTIAL_PROCESS_FLAG: Final = "--credential-process"
#: Versión del documento de `credential_process` que definen los SDK de AWS.
CREDENTIAL_PROCESS_VERSION: Final = 1
#: Variables que hacen que botocore lea el perfil de `credential_process`.
PROFILE_VARIABLES: Final = ("AWS_PROFILE", "AWS_CONFIG_FILE")
EXIT_OK: Final = 0
EXIT_NO_CREDENTIALS: Final = 1


def _current_credentials() -> Any:
    """La credencial vigente de la cadena por defecto de botocore, o `None`."""
    for variable in PROFILE_VARIABLES:
        os.environ.pop(variable, None)
    return botocore.session.get_session().get_credentials()


def credential_variables() -> dict[str, str] | None:
    """Las variables de entorno de la credencial vigente, o `None` si la
    cadena por defecto no encuentra ninguna."""
    credentials = _current_credentials()
    if credentials is None:
        return None
    frozen = credentials.get_frozen_credentials()
    variables = {ACCESS_KEY_VARIABLE: frozen.access_key, SECRET_KEY_VARIABLE: frozen.secret_key}
    if frozen.token:
        variables[SESSION_TOKEN_VARIABLE] = frozen.token
    return variables


def credential_process_document() -> dict[str, str | int] | None:
    """El documento de `credential_process` de la credencial vigente, o `None`
    si no hay ninguna. `Expiration` sale del instante de caducidad que botocore
    guarda en las credenciales renovables (la versión está fijada en
    `requirements-aws-credentials.txt`); sin él el SDK las tomaría por
    permanentes."""
    credentials = _current_credentials()
    if credentials is None:
        return None
    frozen = credentials.get_frozen_credentials()
    document: dict[str, str | int] = {
        "Version": CREDENTIAL_PROCESS_VERSION,
        "AccessKeyId": frozen.access_key,
        "SecretAccessKey": frozen.secret_key,
    }
    if frozen.token:
        document["SessionToken"] = frozen.token
    expiry = getattr(credentials, "_expiry_time", None)
    if expiry is not None:
        document["Expiration"] = expiry.isoformat()
    return document


def main(argv: list[str]) -> int:
    document = (
        credential_process_document() if CREDENTIAL_PROCESS_FLAG in argv else credential_variables()
    )
    if document is None:
        return EXIT_NO_CREDENTIALS
    sys.stdout.write(json.dumps(document, separators=(",", ":")))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
