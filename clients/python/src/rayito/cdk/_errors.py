"""Errores cerrados de los constructos CDK (`agentcore-cdk`, ADR-032): su
código y su mensaje en español, tal cual `testdata/agentcore-cdk/contract.json`
(`errors.messages`). Puro: sólo la biblioteca estándar, así que se importa sin
`aws-cdk-lib`. Los errores del plan de AgentCore salen como
`AgentCoreException`, no como éste.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

CdkErrorCode = Literal[
    "region_unresolved",
    "account_invalid",
    "image_name_invalid",
    "image_name_reserved",
    "artifact_not_found",
    "artifact_checksum_mismatch",
    "artifact_variant_mismatch",
    "artifact_efs_mismatch",
    "option_invalid",
    "release_download_failed",
]

ERROR_MESSAGES: Final[Mapping[CdkErrorCode, str]] = MappingProxyType(
    {
        "region_unresolved": (
            "el constructo {construct} necesita una región concreta en el stack (env.region): "
            "con un stack sin entorno la región es un token y no se puede validar"
        ),
        "account_invalid": "la cuenta {account} del stack no es un id de 12 dígitos",
        "image_name_invalid": (
            "el nombre de imagen {name} no vale: de 1 a {max} caracteres de letras, dígitos, - o _ "
            "(con el sufijo de tamaño incluido)"
        ),
        "image_name_reserved": (
            "el nombre de imagen {name} está reservado para rayito setup y rayito image publish "
            "({prefix}*): elige otro"
        ),
        "artifact_not_found": "no existe el artefacto {path}",
        "artifact_checksum_mismatch": "el sha256 de {path} no coincide con el esperado",
        "artifact_variant_mismatch": "el artefacto es de la variante {found} y se pidió {variant}",
        "artifact_efs_mismatch": "el artefacto {state} el marcador de EFS y with_efs es {withEfs}",
        "option_invalid": "{option}: {reason}",
        "release_download_failed": "no se pudo descargar {asset} de la release {release}: {reason}",
    }
)


class RayitoCdkException(Exception):
    """Un constructo de `rayito.cdk` no se puede sintetizar con lo que se le
    pasó: la región del stack es un token, el nombre de la imagen no vale o
    está reservado, el artefacto no existe o no casa, o una opción no vale.
    Se lanza en el constructor, antes de añadir ningún recurso. `code` es una
    lista cerrada y `fields` los valores que interpola el mensaje."""

    def __init__(self, code: CdkErrorCode, **fields: object) -> None:
        self.fields: dict[str, str] = {name: str(value) for name, value in fields.items()}
        super().__init__(ERROR_MESSAGES[code].format(**self.fields))
        self.code: CdkErrorCode = code


__all__ = ["ERROR_MESSAGES", "CdkErrorCode", "RayitoCdkException"]
