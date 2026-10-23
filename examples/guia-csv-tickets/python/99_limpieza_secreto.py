"""Borra el secreto del laboratorio.

Por defecto programa el borrado con ventana de recuperación (7 días, se puede
deshacer con `aws secretsmanager restore-secret`). Con --forzar lo borra sin
recuperación: úsalo sólo con secretos creados para esta prueba."""

import os
import sys

import boto3
from rayito import SecretStore

NAME = os.environ.get("MODEL_SECRET", "bedrock-key")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def main() -> None:
    if "--forzar" in sys.argv:
        borrado = SecretStore(region=REGION).destroy(NAME)  # ForceDeleteWithoutRecovery
        print(f"rayito/{NAME}: {'borrado sin recuperación' if borrado else 'no existía'}")
        return
    sm = boto3.client("secretsmanager", region_name=REGION)
    sm.delete_secret(SecretId=f"rayito/{NAME}", RecoveryWindowInDays=7)
    print(f"rayito/{NAME}: borrado programado en 7 días (recuperable con restore-secret)")


if __name__ == "__main__":
    main()
