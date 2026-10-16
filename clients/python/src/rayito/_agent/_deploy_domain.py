"""Valores puros del despliegue de un agente con una sola llamada
(`deploy_agent`, ADR-029): `DeployOptions` (dónde y cómo se despliega, no qué
es el agente) y los nombres de los valores por defecto que la fachada resuelve.
Nada aquí llama a AWS ni crea clientes; construir un `DeployOptions` valida y
nada más. La fachada que los usa (`_deploy_facade.py`) hace la I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from rayito._limits import ACCESS_TOKEN_MIN_BYTES, S3_BUCKET_NAME_MAX, S3_BUCKET_NAME_MIN
from rayito.exceptions import InvalidArgumentException

#: Valor de `base_image_version` que pide la última versión activa de la base.
BASE_IMAGE_VERSION_LATEST: Final = "latest"
#: Un nombre de proveedor de `secret_names` (un preset de pasarela).
_PROVIDER_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]*")
#: Un nombre de secreto o de versión de imagen: sin espacios ni saltos.
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


@dataclass(frozen=True)
class DeployOptions:
    """Los argumentos de `deploy_agent()` que describen dónde y cómo se
    despliega, no el agente: `bucket` del artefacto de la imagen,
    `base_image_version` de `rayito-base-caps` (`"latest"` por defecto),
    `secret_names` por proveedor, `create_secret_from_env` (crea el secreto del
    modelo desde la variable local del proveedor, apagado por defecto),
    `ensure_base_image` (publica la base si falta, tras confirmar),
    `access_token`, `keep_on_failure` y `dry_run` (muestra la detección, el plan
    y el coste sin llamar a AWS).

    Coste y activación
    -------------------
    Activa: nada; construirlo es puro. `create_secret_from_env=True` y
        `ensure_base_image=True` sólo cuentan cuando `deploy_agent()` los usa.
    Recursos y llamadas AWS: ninguno aquí.
    Coste aproximado: ninguno aquí; el de `deploy_agent()` está en su bloque.
    IAM: ninguna para este valor.
    Cómo apagarla: no lo pases; los valores por defecto no crean nada extra.
    Ejemplo:
        options = DeployOptions(bucket="amzn-s3-demo-bucket", dry_run=True)
    """

    bucket: str | None = None
    base_image_version: str = BASE_IMAGE_VERSION_LATEST
    secret_names: Mapping[str, str] = field(default_factory=dict)
    create_secret_from_env: bool = False
    ensure_base_image: bool = True
    access_token: str | None = None
    keep_on_failure: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.bucket is not None:
            validate_bucket_hint(self.bucket, "DeployOptions.bucket")
        validate_image_version(self.base_image_version, "DeployOptions.base_image_version")
        if not isinstance(self.secret_names, Mapping):
            raise InvalidArgumentException("DeployOptions.secret_names debe ser un dict")
        for provider, name in self.secret_names.items():
            if not isinstance(provider, str) or not _PROVIDER_PATTERN.fullmatch(provider):
                raise InvalidArgumentException(
                    "DeployOptions.secret_names: cada clave debe ser un proveedor "
                    "(por ejemplo 'openai')"
                )
            if not isinstance(name, str) or not _TOKEN_PATTERN.fullmatch(name):
                raise InvalidArgumentException(
                    "DeployOptions.secret_names: cada valor debe ser el nombre de un secreto"
                )
        object.__setattr__(self, "secret_names", MappingProxyType(dict(self.secret_names)))
        for label, value in (
            ("create_secret_from_env", self.create_secret_from_env),
            ("ensure_base_image", self.ensure_base_image),
            ("keep_on_failure", self.keep_on_failure),
            ("dry_run", self.dry_run),
        ):
            if not isinstance(value, bool):
                raise InvalidArgumentException(f"DeployOptions.{label} debe ser un bool")
        if self.access_token is not None and (
            not isinstance(self.access_token, str)
            or len(self.access_token.encode("utf-8")) < ACCESS_TOKEN_MIN_BYTES
        ):
            raise InvalidArgumentException(
                f"DeployOptions.access_token debe tener al menos {ACCESS_TOKEN_MIN_BYTES} bytes"
            )

    def __repr__(self) -> str:
        token = "<oculto>" if self.access_token is not None else None
        return (
            f"DeployOptions(bucket={self.bucket!r}, "
            f"base_image_version={self.base_image_version!r}, "
            f"secret_names={dict(self.secret_names)!r}, "
            f"create_secret_from_env={self.create_secret_from_env!r}, "
            f"ensure_base_image={self.ensure_base_image!r}, access_token={token!r}, "
            f"keep_on_failure={self.keep_on_failure!r}, dry_run={self.dry_run!r})"
        )


__all__ = [
    "BASE_IMAGE_VERSION_LATEST",
    "DeployOptions",
    "validate_bucket_hint",
    "validate_image_version",
]
