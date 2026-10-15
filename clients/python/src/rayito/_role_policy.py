"""Puerta de política de roles para las funciones 0.6 que necesitan las
credenciales del execution role dentro del guest (M15 foundations):
s3-mounts, efs-volumes y rayd-otlp con `OtlpAuth.execution_role()`. Esas
credenciales sólo llegan al guest sobre `rayito-base-caps` (o una variante
derivada de ella, por ejemplo con un sufijo de tamaño,
`rayito-base-caps-4gb`): la imagen base no concede el capability mask que
el patrón de ADR-012 exige.

`require_caps_for` y `require_efs_for` corren antes de lanzar cuando el
nombre de la imagen ya permite decidirlo: sólo los nombres oficiales
`rayito-<variante>[-<talla>]`, con la variante dentro del catálogo cerrado
`OFFICIAL_IMAGE_VARIANTS` y la talla dentro de `SIZE_NAMES`. Sobre cualquier
otro nombre (una imagen propia, aunque empiece por `rayito-`, o un ARN opaco)
la decisión
no puede tomarse aquí, así que se difiere: el llamante lanza la VM y,
después de `/run`, decide por `Health.features` (ausente o con el flag en
`false` según la función) — ahí si falta la caps exige terminar el sandbox
(salvo `keep_on_failure`) y lanzar `UnimplementedError`, nunca aquí.
"""

from __future__ import annotations

from typing import Final

from rayito._limits import OFFICIAL_IMAGE_VARIANTS
from rayito._sizing import SIZE_NAMES
from rayito.exceptions import UnimplementedError

IMAGE_NAME_PREFIX: Final = "rayito-"
ARN_PREFIX: Final = "arn:"
CAPS_VARIANT: Final = "base-caps"
#: `rayito image publish --with-efs` (m15-efs-volumes): la imagen caps con la
#: capa de `amazon-efs-utils` (`rayito-base-caps-efs`); concede lo mismo que la
#: caps y además deja montar `volumes=`.
EFS_CAPS_VARIANT: Final = f"{CAPS_VARIANT}-efs"
#: Las variantes que conceden el capability mask de ADR-012.
CAPS_VARIANTS: Final = frozenset({CAPS_VARIANT, EFS_CAPS_VARIANT})
#: Las variantes oficiales (catálogo cerrado de `limits.json`).
KNOWN_VARIANTS: Final = frozenset(OFFICIAL_IMAGE_VARIANTS)


def resolve_image_variant(template: str | None) -> str | None:
    """`"rayito-base-caps-4gb"` -> `"base-caps"`; `"rayito-base"` -> `"base"`.
    Sólo se reconoce `rayito-<variante>[-<talla>]` con la variante en
    `OFFICIAL_IMAGE_VARIANTS` y la talla en `SIZE_NAMES`. `None`, un ARN y
    cualquier otro nombre (también `rayito-mybuild` o
    `rayito-miapp-caps-efs`: una imagen propia) devuelven `None`, variante
    desconocida: la decisión se difiere a `Health.features` del agente.
    """
    if not template or template.startswith(ARN_PREFIX):
        return None
    if not template.startswith(IMAGE_NAME_PREFIX):
        return None
    candidate = template.removeprefix(IMAGE_NAME_PREFIX)
    if candidate in KNOWN_VARIANTS:
        return candidate
    for size in SIZE_NAMES:
        unsized = candidate.removesuffix(f"-{size}")
        if unsized != candidate and unsized in KNOWN_VARIANTS:
            return unsized
    return None


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


def require_efs_for(feature: str, image_variant: str | None) -> None:
    """Como `require_caps_for`, pero para `volumes=`: con la variante conocida
    exige `EFS_CAPS_VARIANT` (la caps sin `amazon-efs-utils` no puede montar
    EFS y fallaría después de pagar el lanzamiento). Una variante desconocida
    (`None`) no se rechaza aquí: `Health.features.efs_volumes` decide tras
    `/run`.
    """
    if image_variant is None or image_variant == EFS_CAPS_VARIANT:
        return
    raise UnimplementedError(
        feature,
        f"necesita una imagen de la variante '{EFS_CAPS_VARIANT}' (o derivada, por tamaño); "
        f"la imagen pedida es la variante '{image_variant}'",
    )
