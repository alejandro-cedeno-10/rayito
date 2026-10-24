"""Guarda la clave del modelo en Secrets Manager sin pasarla por la línea de comandos.

Lee AWS_BEARER_TOKEN_BEDROCK del entorno o, con --generate, firma una clave de
Bedrock de corta duración con tus credenciales actuales. Nunca imprime el valor.
El secreto queda como `rayito/<MODEL_SECRET>`. El mensaje es fijo: no se imprime
nada leído del entorno (ni el nombre ni, por supuesto, el valor).
Un secreto cuesta unos 0,40 USD al mes (prorrateado) hasta que lo borras."""

import os
import sys
from datetime import timedelta

from rayito import SecretStore

NAME = os.environ.get("MODEL_SECRET", "bedrock-key")
REGION = os.environ.get("AWS_REGION", "us-east-1")
GENERATE_FLAG = "--generate"
GENERATED_KEY_LIFETIME = timedelta(hours=1)
BEARER_PREFIX = "Bearer "
OUTPUT = "secreto {} en Secrets Manager: rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)"


def model_key() -> str:
    """La clave del modelo, del entorno o firmada con `aws-bedrock-token-generator`."""
    if GENERATE_FLAG in sys.argv:
        from aws_bedrock_token_generator import provide_token

        return str(provide_token(region=REGION, expiry=GENERATED_KEY_LIFETIME))
    value = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if not value:
        raise SystemExit(f"define AWS_BEARER_TOKEN_BEDROCK o usa {GENERATE_FLAG}")
    return value


def main() -> None:
    """Crea el secreto o lo actualiza si ya existe."""
    value = model_key()
    secret = value if value.startswith(BEARER_PREFIX) else f"{BEARER_PREFIX}{value}"
    store = SecretStore(region=REGION)
    if store.exists(NAME):
        store.update(NAME, secret)
        print(OUTPUT.format("actualizado"))
    else:
        store.create(NAME, secret)
        print(OUTPUT.format("creado"))


if __name__ == "__main__":
    main()
