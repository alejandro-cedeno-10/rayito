"""Comprobaciones tempranas de los valores de despliegue que comparten
`AgentProject` (`[deploy]`) y `AgentDeployOptions`: el nombre de un bucket y la
versión de la imagen base. Puro: no llama a AWS ni crea clientes.
"""

from __future__ import annotations

import re
from typing import Final

from rayito._limits import S3_BUCKET_NAME_MAX, S3_BUCKET_NAME_MIN
from rayito.exceptions import InvalidArgumentException

#: Valor de `base_image_version` que pide la última versión activa de la base.
BASE_IMAGE_VERSION_LATEST: Final = "latest"
#: Un nombre de bucket, de secreto o de versión de imagen: sin espacios ni saltos.
_TOKEN_PATTERN: Final = re.compile(r"\S+")


def validate_bucket_hint(bucket: object, label: str) -> str:
    """Comprobación temprana de un nombre de bucket: longitud y sin
    espacios ni `/`. Las reglas completas de S3 las aplica quien lo usa."""
    if (
        not isinstance(bucket, str)
        or not S3_BUCKET_NAME_MIN <= len(bucket) <= S3_BUCKET_NAME_MAX
        or not _TOKEN_PATTERN.fullmatch(bucket)
        or "/" in bucket
    ):
        raise InvalidArgumentException(
            f"{label} debe ser un nombre de bucket de {S3_BUCKET_NAME_MIN} a "
            f"{S3_BUCKET_NAME_MAX} caracteres, sin espacios ni '/'"
        )
    return bucket


def validate_image_version(version: object, label: str) -> str:
    """`"latest"` o el identificador de una versión de imagen."""
    if not isinstance(version, str) or not _TOKEN_PATTERN.fullmatch(version):
        raise InvalidArgumentException(f"{label} debe ser 'latest' o una versión, sin espacios")
    return version


def validate_secret_name(name: object, label: str) -> str:
    """El nombre de un secreto existente: texto sin espacios ni saltos. Las
    reglas completas las aplica Secrets Manager."""
    if not isinstance(name, str) or not _TOKEN_PATTERN.fullmatch(name):
        raise InvalidArgumentException(f"{label} debe ser el nombre de un secreto, sin espacios")
    return name


__all__ = [
    "BASE_IMAGE_VERSION_LATEST",
    "validate_bucket_hint",
    "validate_image_version",
    "validate_secret_name",
]
