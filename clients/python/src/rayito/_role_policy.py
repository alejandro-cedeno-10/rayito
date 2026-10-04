"""Puerta de política de roles para las funciones 0.6 que necesitan las
credenciales del execution role dentro del guest (M15 foundations):
s3-mounts, efs-volumes y rayd-otlp con `OtlpAuth.execution_role()`. Esas
credenciales sólo llegan al guest sobre `rayito-base-caps` (o una variante
derivada de ella, por ejemplo con un sufijo de tamaño,
`rayito-base-caps-4gb`): la imagen base no concede el capability mask que
el patrón de ADR-012 exige.

`require_caps_for` corre antes de lanzar cuando el nombre de la imagen ya
permite decidirlo (una plantilla con el prefijo `rayito-` de este proyecto);
sobre cualquier otro nombre (una imagen propia, o un ARN opaco) la decisión
no puede tomarse aquí, así que se difiere: el llamante lanza la VM y,
después de `/run`, decide por `Health.features` (ausente o con el flag en
`false` según la función) — ahí si falta la caps exige terminar el sandbox
(salvo `keep_on_failure`) y lanzar `UnimplementedError`, nunca aquí.
"""

from __future__ import annotations

import re
from typing import Final

from rayito.exceptions import UnimplementedError

RAYITO_IMAGE_PATTERN: Final = re.compile(
    r"^rayito-(?P<variant>[a-z]+(?:-[a-z]+)*?)(?:-\d+[a-z]+)?$"
)
CAPS_VARIANT: Final = "base-caps"
#: `rayito image publish --with-efs` (m15-efs-volumes): la imagen caps con la
#: capa de `amazon-efs-utils` (`rayito-base-caps-efs`); concede lo mismo que
#: la caps y además deja montar `volumes=`.
EFS_CAPS_VARIANT: Final = f"{CAPS_VARIANT}-efs"
#: Las variantes que conceden el capability mask de ADR-012.
CAPS_VARIANTS: Final = frozenset({CAPS_VARIANT, EFS_CAPS_VARIANT})


def resolve_image_variant(template: str | None) -> str | None:
    """`"rayito-base-caps-4gb"` -> `"base-caps"`; `"rayito-base"` -> `"base"`;
    `None`, un ARN o un nombre fuera de la convención `rayito-<variant>[-<size>]`
    devuelven `None` (variante desconocida: la decisión se difiere al agente).
    """
    if not template or template.startswith("arn:"):
        return None
    match = RAYITO_IMAGE_PATTERN.match(template)
    return match.group("variant") if match else None


def require_caps_for(feature: str, image_variant: str | None) -> None:
    """Lanza `UnimplementedError` si `image_variant` se conoce y no es una
    variante caps (`CAPS_VARIANTS`: la caps o la caps con `amazon-efs-utils`);
    no hace nada (ni cuando lo es, ni cuando es `None`, es decir
    desconocida) para no bloquear un lanzamiento que el agente podría aún
    aceptar.
    """
    if image_variant is None or image_variant in CAPS_VARIANTS:
        return
    raise UnimplementedError(
        feature,
        f"necesita una imagen de la variante '{CAPS_VARIANT}' (o derivada, por tamaño); "
        f"la imagen pedida es la variante '{image_variant}'",
    )
