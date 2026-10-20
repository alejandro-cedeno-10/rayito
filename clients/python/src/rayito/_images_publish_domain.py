"""Dominio puro de la publicación de una imagen de MicroVM (`rayito image
publish` y `RayitoMicrovmImage` de `rayito.cdk`, ADR-032 D9.4): las
variantes y sus nombres, los hooks, la combinación de EFS, los tamaños
adicionales y la configuración deseada de `create`/`update-microvm-image`.

Extraído de `cli/_publish.py` sin cambiar su comportamiento
(`testdata/images/publish-vectors.json` lo fija en los dos SDK): la CLI lo
reexporta y `rayito.cdk` lo usa sin importar la CLI. Nada de red, ficheros ni
AWS; sólo la biblioteca estándar y módulos puros del SDK.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol, TypeVar

from rayito._images import DEFAULT_BASE_IMAGE_NAME, LOG_GROUP_PREFIX
from rayito._sizing import BASELINE_MEMORY_MIB, NAME_TO_MEMORY_MIB, SIZE_NAMES
from rayito.cli._artifact import VARIANTS

DEFAULT_IMAGE_NAME: Final = DEFAULT_BASE_IMAGE_NAME
DEFAULT_IMAGE_NAMES: Final = {
    "full": DEFAULT_IMAGE_NAME,
    "slim": f"{DEFAULT_IMAGE_NAME}-slim",
    "poly": f"{DEFAULT_IMAGE_NAME}-poly",
}
#: `--with-efs` (m15-efs-volumes): la imagen caps con `amazon-efs-utils`;
#: `_role_policy.EFS_CAPS_VARIANT` la cuenta como una variante caps.
EFS_IMAGE_NAME: Final = f"{DEFAULT_IMAGE_NAME}-caps-efs"
#: La única variante con la que `--with-efs` se combina: es la imagen caps (el
#: artefacto `full`) más una capa, nunca slim ni poly.
EFS_BASE_VARIANT: Final = "full"
#: `additionalOsCapabilities` (el único valor que acepta el modelo del
#: servicio): `--with-efs` lo exige, porque `rayd` sólo monta con
#: `CAP_SYS_ADMIN`.
ALL_OS_CAPABILITIES: Final = "ALL"
HOOKS_PORT: Final = 9000
MANAGED_BASE_IMAGE_NAME: Final = "al2023-1"
MANAGED_BASE_IMAGE_ARN_TEMPLATE: Final = "arn:aws:lambda:{region}:aws:microvm-image:{name}"
CPU_ARCHITECTURE: Final = "ARM_64"
#: Variable de imagen que `--sizes` hornea: información declarada para que el
#: código del usuario sepa el baseline de la imagen (Q88) sin llamar a AWS;
#: `rayd` no la lee.
BASELINE_MEMORY_ENV_VAR: Final = "RAYITO_BASELINE_MEMORY_MIB"
#: La lista de buckets que la imagen deja montar (m15-s3-mounts), separados
#: por comas (`make image-publish-caps MOUNT_BUCKETS=b1,b2`).
ALLOWED_MOUNT_BUCKETS_ENV_VAR: Final = "RAYITO_ALLOWED_MOUNT_BUCKETS"
MOUNT_BUCKETS_SEPARATOR: Final = ","
#: `--env KEY=VALUE`: el patrón de nombre de variable de entorno de POSIX
#: (letra o `_` inicial, luego alfanuméricos o `_`).
ENV_KEY_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: El tamaño que ya es la imagen sin sufijo (m15-sizes-catalog).
BASELINE_SIZE_NAME: Final = next(
    name for name, mib in NAME_TO_MEMORY_MIB.items() if mib == BASELINE_MEMORY_MIB
)

IMAGE_HOOKS: dict[str, Any] = {
    "port": HOOKS_PORT,
    "microvmImageHooks": {
        "ready": "ENABLED",
        "readyTimeoutInSeconds": 600,
        "validate": "ENABLED",
        "validateTimeoutInSeconds": 600,
    },
    "microvmHooks": {
        "run": "ENABLED",
        "runTimeoutInSeconds": 30,
        "resume": "ENABLED",
        "resumeTimeoutInSeconds": 30,
        "suspend": "ENABLED",
        "suspendTimeoutInSeconds": 30,
        "terminate": "ENABLED",
        "terminateTimeoutInSeconds": 10,
    },
}


class ImageSettings(Protocol):
    """Lo que la configuración deseada lee de unos ajustes de publicación:
    `cli._publish.PublishSettings` y los de `RayitoMicrovmImage` lo cumplen."""

    @property
    def image_name(self) -> str: ...

    @property
    def base_image_version(self) -> str: ...

    @property
    def memory_mib(self) -> int: ...

    @property
    def os_capabilities(self) -> str | None: ...

    @property
    def environment_variables(self) -> Mapping[str, str]: ...


SettingsT = TypeVar("SettingsT", bound=ImageSettings)


@dataclasses.dataclass(frozen=True)
class ImageBuildSettings:
    """Unos ajustes de imagen sin nada de la CLI (bucket, pila, plazos): los
    de `RayitoMicrovmImage`."""

    image_name: str
    base_image_version: str
    memory_mib: int
    os_capabilities: str | None = None
    environment_variables: Mapping[str, str] = dataclasses.field(default_factory=dict)


def log_group_for(image_name: str) -> str:
    """El grupo de logs del build de una imagen: `/rayito/<nombre>`."""
    return f"{LOG_GROUP_PREFIX}/{image_name}"


def default_image_name(variant: str, *, with_efs: bool = False) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"variant desconocida: {variant!r} (admitidas: {', '.join(VARIANTS)})")
    if with_efs:
        require_efs_combination(variant, ALL_OS_CAPABILITIES)
        return EFS_IMAGE_NAME
    return DEFAULT_IMAGE_NAMES[variant]


def require_efs_combination(variant: str, os_capabilities: str | None) -> None:
    """`--with-efs` sólo con la variante `full` y `--os-capabilities ALL`: la
    imagen es `rayito-base-caps` más la capa `amazon-efs-utils`, y sin
    `CAP_SYS_ADMIN` `rayd` nunca anunciaría `Health.features.efs_volumes` (una
    imagen que paga la capa y nunca puede usarla)."""
    if variant != EFS_BASE_VARIANT:
        raise ValueError(f"--with-efs sólo con --variant {EFS_BASE_VARIANT}, no {variant!r}")
    if os_capabilities != ALL_OS_CAPABILITIES:
        raise ValueError(
            f"--with-efs necesita --os-capabilities {ALL_OS_CAPABILITIES}: rayd sólo monta "
            "EFS con CAP_SYS_ADMIN"
        )


def base_image_arn(region: str) -> str:
    """La imagen base gestionada de la región (`al2023-1`)."""
    return MANAGED_BASE_IMAGE_ARN_TEMPLATE.format(region=region, name=MANAGED_BASE_IMAGE_NAME)


def size_names_problem(names: Sequence[str]) -> str | None:
    """Por qué una lista de tamaños adicionales no vale (cada uno del catálogo
    cerrado `_sizing.SIZE_NAMES`, sin el baseline ni duplicados), o `None`."""
    for name in names:
        if name not in SIZE_NAMES:
            return f"{name!r}: admitidos {', '.join(SIZE_NAMES)}"
        if name == BASELINE_SIZE_NAME:
            return f"{BASELINE_SIZE_NAME!r} ya es el baseline sin sufijo; quítalo de --sizes"
    if len(set(names)) != len(names):
        return "tamaños repetidos"
    return None


def sized_settings(base: SettingsT, size_name: str) -> SettingsT:
    """`base` con el sufijo de tamaño aplicado (m15-sizes-catalog):
    `image_name` lleva `-<size_name>`, `memory_mib` es el valor del catálogo
    cerrado y `environment_variables` gana `BASELINE_MEMORY_ENV_VAR` horneado:
    información declarada para que el código del usuario (no `rayd`, que hoy
    no la lee) sepa el baseline de la imagen sin llamar a AWS (Q88: el guest
    ve hasta 4x más en `free -m`). `size_name` ya viene validado contra
    `_sizing.SIZE_NAMES`. `base` es un dataclass."""
    memory_mib = NAME_TO_MEMORY_MIB[size_name]
    replaced: SettingsT = dataclasses.replace(
        base,  # type: ignore[type-var]
        image_name=f"{base.image_name}-{size_name}",
        memory_mib=memory_mib,
        environment_variables={
            **base.environment_variables,
            BASELINE_MEMORY_ENV_VAR: str(memory_mib),
        },
    )
    return replaced


def desired_configuration(
    *, region: str, settings: ImageSettings, artifact_uri: str, build_role_arn: str
) -> dict[str, Any]:
    """El cuerpo de `create` y `update-microvm-image`, sin nombre ni
    descripción. `additionalOsCapabilities` y `environmentVariables` sólo
    aparecen si traen algo, como siempre."""
    configuration: dict[str, Any] = {
        "baseImageArn": base_image_arn(region),
        "baseImageVersion": settings.base_image_version,
        "buildRoleArn": build_role_arn,
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": settings.memory_mib}],
        "cpuConfigurations": [{"architecture": CPU_ARCHITECTURE}],
        "hooks": IMAGE_HOOKS,
        "logging": {"cloudWatch": {"logGroup": log_group_for(settings.image_name)}},
    }
    if settings.os_capabilities:
        configuration["additionalOsCapabilities"] = [settings.os_capabilities]
    if settings.environment_variables:
        configuration["environmentVariables"] = dict(settings.environment_variables)
    return configuration


__all__ = [
    "ALLOWED_MOUNT_BUCKETS_ENV_VAR",
    "ALL_OS_CAPABILITIES",
    "BASELINE_MEMORY_ENV_VAR",
    "BASELINE_SIZE_NAME",
    "DEFAULT_IMAGE_NAME",
    "DEFAULT_IMAGE_NAMES",
    "EFS_BASE_VARIANT",
    "EFS_IMAGE_NAME",
    "ENV_KEY_PATTERN",
    "HOOKS_PORT",
    "IMAGE_HOOKS",
    "MANAGED_BASE_IMAGE_NAME",
    "MOUNT_BUCKETS_SEPARATOR",
    "ImageBuildSettings",
    "ImageSettings",
    "base_image_arn",
    "default_image_name",
    "desired_configuration",
    "log_group_for",
    "require_efs_combination",
    "size_names_problem",
    "sized_settings",
]
