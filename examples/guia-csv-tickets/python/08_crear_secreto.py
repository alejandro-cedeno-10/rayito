"""Guarda la clave del modelo en Secrets Manager sin pasarla por la línea de comandos.

Lee AWS_BEARER_TOKEN_BEDROCK del entorno o, con --generar, firma una clave de
Bedrock de corta duración con tus credenciales actuales. Nunca imprime el valor.
Un secreto cuesta unos 0,40 USD al mes (prorrateado) hasta que lo borras."""

import os
import sys

from rayito import SecretStore

NAME = os.environ.get("MODEL_SECRET", "bedrock-key")  # queda como rayito/<NAME>
REGION = os.environ.get("AWS_REGION", "us-east-1")
# Mensaje fijo: no se imprime nada leído del entorno (ni el nombre ni, por supuesto, el valor).
SALIDA = "secreto {} en Secrets Manager: rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)"


def clave() -> str:
    if "--generar" in sys.argv:
        from datetime import timedelta

        from aws_bedrock_token_generator import provide_token

        return provide_token(region=REGION, expiry=timedelta(hours=1))
    valor = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if not valor:
        raise SystemExit("define AWS_BEARER_TOKEN_BEDROCK o usa --generar")
    return valor


def main() -> None:
    valor = clave()
    secreto = valor if valor.startswith("Bearer ") else f"Bearer {valor}"
    store = SecretStore(region=REGION)
    if store.exists(NAME):
        store.update(NAME, secreto)
        print(SALIDA.format("actualizado"))
    else:
        store.create(NAME, secreto)
        print(SALIDA.format("creado"))
    del valor, secreto


if __name__ == "__main__":
    main()
