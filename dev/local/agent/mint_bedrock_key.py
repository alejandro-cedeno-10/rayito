"""Clave de API de Amazon Bedrock de corta duración, para el spike de
agentes (docs/research/2026-10-agent-spike.md), escrita en stdout para
pasarla por una tubería a `spike.py`; nunca la guardes en un fichero.

    AWS_PROFILE=<tu-perfil> python3 mint_bedrock_key.py | docker compose ... exec -T runner ...

No llama a AWS ni crea ningún recurso: es una URL de
`bedrock:CallWithBearerToken` prefirmada en local con SigV4 por las
credenciales que ya tienes (el mismo algoritmo que el paquete
`aws-bedrock-token-generator` de AWS), en base64 tras el prefijo
`bedrock-api-key-`. Vale lo que dure la sesión de esas credenciales, como
mucho 12 h, y Bedrock la acepta como `Authorization: Bearer <clave>`. Una
clave de larga duración, en cambio, crea credenciales específicas de
servicio sobre un usuario IAM: el spike no la necesita. Requiere botocore
(cualquier entorno con boto3).
"""

from __future__ import annotations

import base64
import os
import sys
from typing import Final

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session

TOKEN_HOST: Final = "bedrock.amazonaws.com"
TOKEN_PREFIX: Final = "bedrock-api-key-"
TOKEN_VERSION_SUFFIX: Final = "&Version=1"
#: El máximo que acepta Bedrock para una clave de corta duración.
TOKEN_MAX_SECONDS: Final = 43200


def mint(region: str) -> str:
    credentials = get_session().get_credentials()
    if credentials is None:
        raise SystemExit("sin credenciales de AWS en este entorno")
    request = AWSRequest(
        method="POST",
        url=f"https://{TOKEN_HOST}/",
        headers={"host": TOKEN_HOST},
        params={"Action": "CallWithBearerToken"},
    )
    SigV4QueryAuth(
        credentials.get_frozen_credentials(),
        "bedrock",
        region,
        expires=TOKEN_MAX_SECONDS,
    ).add_auth(request)
    presigned = request.url.removeprefix("https://") + TOKEN_VERSION_SUFFIX
    return TOKEN_PREFIX + base64.b64encode(presigned.encode()).decode()


if __name__ == "__main__":
    sys.stdout.write(mint(os.environ.get("AWS_REGION", "us-east-1")))
