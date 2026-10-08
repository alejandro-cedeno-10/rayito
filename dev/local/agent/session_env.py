"""Ejecuta una orden con las credenciales temporales de tu sesión de AWS en
su entorno, para que `litellm.sh` cree el contenedor de LiteLLM con ellas
(`docker create -e AWS_ACCESS_KEY_ID ...`) sin que pasen por stdout, por
un fichero ni por la línea de órdenes:

    AWS_PROFILE=<tu-perfil> python3 session_env.py docker create -e AWS_ACCESS_KEY_ID ...

No llama a AWS ni crea nada: resuelve las credenciales que ya tiene la
cadena de botocore (SSO, rol asumido...) y exige que sean temporales (con
token de sesión), así un descuido nunca entrega una clave de larga duración
a un contenedor. Requiere botocore (cualquier entorno con boto3).
"""

from __future__ import annotations

import os
import sys

from botocore.session import get_session


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("uso: session_env.py <orden> [argumentos...]\n")
        return 2
    credentials = get_session().get_credentials()
    if credentials is None:
        sys.stderr.write("sin credenciales de AWS en este entorno\n")
        return 1
    frozen = credentials.get_frozen_credentials()
    if not frozen.token:
        sys.stderr.write("las credenciales no son temporales: usa una sesión de SSO o un rol\n")
        return 1
    environment = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": frozen.access_key,
        "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        "AWS_SESSION_TOKEN": frozen.token,
    }
    os.execvpe(argv[0], argv, environment)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
