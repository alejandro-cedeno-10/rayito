"""Borra el secreto del laboratorio.

Por defecto programa el borrado con ventana de recuperación (7 días, se puede
deshacer con `aws secretsmanager restore-secret`). Con --force lo borra sin
recuperación (`ForceDeleteWithoutRecovery`): úsalo sólo con secretos creados para
esta prueba. Los mensajes son fijos: no se imprime nada leído del entorno."""

import os
import sys

import boto3
from botocore.exceptions import ClientError
from rayito import SecretStore

NAME = os.environ.get("MODEL_SECRET", "bedrock-key")
REGION = os.environ.get("AWS_REGION", "us-east-1")
FORCE_FLAG = "--force"
RECOVERY_WINDOW_DAYS = 7
TOLERATED_ERRORS = ("ResourceNotFoundException", "InvalidRequestException")
SECRET_LABEL = "rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)"


def force_delete() -> None:
    """Borra el secreto sin ventana de recuperación."""
    deleted = SecretStore(region=REGION).destroy(NAME)
    print(f"{SECRET_LABEL}: borrado sin recuperación" if deleted else f"{SECRET_LABEL}: no existía")


def schedule_delete() -> None:
    """Programa el borrado con la ventana de recuperación."""
    client = boto3.client("secretsmanager", region_name=REGION)
    try:
        client.delete_secret(SecretId=f"rayito/{NAME}", RecoveryWindowInDays=RECOVERY_WINDOW_DAYS)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in TOLERATED_ERRORS:
            raise
        print(f"{SECRET_LABEL}: no existía o ya tenía el borrado programado")
        return
    print(f"{SECRET_LABEL}: borrado programado en {RECOVERY_WINDOW_DAYS} días (recuperable con restore-secret)")


def main() -> None:
    """Borra con recuperación o, con --force, sin ella."""
    if FORCE_FLAG in sys.argv:
        force_delete()
    else:
        schedule_delete()


if __name__ == "__main__":
    main()
