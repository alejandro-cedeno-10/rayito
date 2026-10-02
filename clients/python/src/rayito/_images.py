"""Núcleo compartido de build de imágenes (`ImageBuildGateway`,
arquitectura §1(g) de la investigación): el envío de
`create`/`update-microvm-image`, la puerta de tres estados (imagen
`CREATED|UPDATED`, versión `SUCCESSFUL`, estado `ACTIVE`), el reuso de una
versión ya construida con la misma configuración y la subida
direccionada por contenido del artefacto. Lo consumen `rayito image
publish` (`cli/_publish.py`) y `Template.build()` (`_templates/_build.py`).

Extraído de `cli/_publish.py` sin cambiar su comportamiento (revisión de
m15-templates: `_templates/_build.py` tenía su propia copia que ya había
divergido — sin `FAILED_IMAGE_STATES`, sin la normalización Q52 de
`baseImageVersion`, sin comprobar `status == ACTIVE` al decidir éxito).
El puerto es `ImageBuildClients` (estructural: `cli._session.Clients` y
`_templates._build._Clients` lo cumplen); el adaptador son las funciones
de este módulo sobre clientes boto3. Cada nombre de parámetro AWS usado
aquí aparece literalmente en `AWS_API_NOTES.md` §4/§27.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from botocore.exceptions import ClientError

#: Cadencia de sondeo del gate de tres estados; igual en ambos llamantes
#: desde que este módulo existe (antes, dos constantes idénticas).
POLL_INTERVAL_SECONDS: Final = 10.0
#: Plazo por defecto de `rayito image publish`/`Template.build()` antes de
#: dar el build por parado (no fallido: ver `wait_for_gate`'s `timed_out`).
DEFAULT_BUILD_TIMEOUT_SECONDS: Final = 1800.0
#: `minimumMemoryInMiB` por defecto cuando el llamante no pide uno
#: (`rayito image publish --memory-mib`/`Template.build(memory_mb=)`);
#: `_limits.SUPPORTED_MEMORY_MIB` fija el catálogo cerrado (RES-1).
DEFAULT_MEMORY_MIB: Final = 2048
#: Imagen lista para `run-microvm` tras un build (`publish`/`Template.build`).
LAUNCHABLE_IMAGE_STATES: Final = frozenset({"CREATED", "UPDATED"})
#: Imagen que ya no va a terminar: un `update-microvm-image` sobre una
#: imagen en uno de estos estados nunca llega a `LAUNCHABLE_IMAGE_STATES`,
#: así que sondearla hasta el timeout sería esperar 30 min por nada.
FAILED_IMAGE_STATES: Final = frozenset({"CREATE_FAILED", "UPDATE_FAILED"})
#: Estados finales de `version_state` (`GetMicrovmImageVersion`); cualquier
#: otro valor significa que el build sigue en marcha.
SETTLED_VERSION_STATES: Final = frozenset({"SUCCESSFUL", "FAILED"})
#: Códigos de `ClientError` que `HeadObject` da por un objeto ausente: un
#: 403 cuenta como ausente porque sin `s3:ListBucket` S3 contesta 403 en
#: vez de 404 a un HEAD, y un permiso de verdad ausente aparece en el
#: `PutObject` que sigue.
MISSING_OBJECT_CODES: Final = frozenset({"404", "NoSuchKey", "NotFound", "403"})
ACTIVE_VERSION_STATUS: Final = "ACTIVE"
SUCCESSFUL_VERSION_STATE: Final = "SUCCESSFUL"
#: Clave cuyo valor se compara con normalización Q52 (`1` == `1.0`), no con
#: `==` puro: ver `base_image_version_matches`.
BASE_IMAGE_VERSION_KEY: Final = "baseImageVersion"
#: Prefijo del grupo de logs de cada imagen (`/rayito/<nombre>`), el mismo
#: para `rayito image publish` y `Template.build()` (`AWS_API_NOTES.md` §27;
#: `infra/templates.yaml` `ImageLogGroupPrefix` lo repite como default).
LOG_GROUP_PREFIX: Final = "/rayito"
#: Nombre por defecto de la imagen base publicada por `rayito image publish`
#: (variante `full`); `from_base_image()` sin nombre compone sobre ella.
DEFAULT_BASE_IMAGE_NAME: Final = "rayito-base"
#: Código de error que `create`/`update-microvm-image` devuelven al pasar
#: de 10 builds simultáneos por cuenta (Q83, `AWS_API_NOTES.md` §27).
BUILD_QUOTA_ERROR_CODE: Final = "ServiceQuotaExceededException"
#: Claves de configuración de `CreateMicrovmImageRequest` (`AWS_API_NOTES.md`
#: §4) que una imagen compuesta hereda de la versión base: todas menos
#: `name`/`description`/`tags`/`clientToken` (identidad de la imagen nueva)
#: y `codeArtifact`/`resources`/`logging` (que el llamante fija). Heredar
#: `additionalOsCapabilities` es lo que mantiene una composición sobre
#: `rayito-base-caps` con capabilities.
INHERITED_CONFIGURATION_KEYS: Final = (
    "baseImageArn",
    BASE_IMAGE_VERSION_KEY,
    "buildRoleArn",
    "cpuConfigurations",
    "environmentVariables",
    "additionalOsCapabilities",
    "hooks",
    "egressNetworkConnectors",
)

Sleeper = Callable[[float], None]


class ImageBuildClients(Protocol):
    """Lo mínimo que esta puerta necesita; `cli._session.Clients` y
    `_templates._build._Clients` lo satisfacen ambos por estructura."""

    @property
    def microvms(self) -> Any: ...


class ArtifactStoreClients(Protocol):
    """Lo que `upload_if_absent` necesita: sólo S3."""

    @property
    def s3(self) -> Any: ...


@dataclass(frozen=True)
class SubmittedBuild:
    """Respuesta de `create`/`update-microvm-image`: `created` distingue
    los dos caminos para el mensaje de progreso de `rayito image publish`."""

    arn: str
    version: str
    created: bool


def client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def image_exists(clients: ImageBuildClients, arn: str) -> bool:
    try:
        clients.microvms.get_microvm_image(imageIdentifier=arn)
    except ClientError as exc:
        if client_error_code(exc) == "ResourceNotFoundException":
            return False
        raise
    return True


def account_image_arn(region: str, account_id: str, name: str) -> str:
    """ARN de una imagen propia de la cuenta (`AWS_API_NOTES.md` §10); un
    `name` que ya es un ARN se devuelve tal cual."""
    if name.startswith("arn:"):
        return name
    return f"arn:aws:lambda:{region}:{account_id}:microvm-image:{name}"


def object_exists(clients: ArtifactStoreClients, bucket: str, key: str) -> bool:
    """Un 403 también cuenta como ausente (ver `MISSING_OBJECT_CODES`)."""
    try:
        clients.s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if client_error_code(exc) in MISSING_OBJECT_CODES:
            return False
        raise
    return True


def upload_if_absent(clients: ArtifactStoreClients, bucket: str, key: str, payload: bytes) -> bool:
    """Sube `payload` a `s3://bucket/key` salvo que ya exista (la clave es
    direccionada por contenido); `True` si hubo subida."""
    if object_exists(clients, bucket, key):
        return False
    clients.s3.put_object(Bucket=bucket, Key=key, Body=payload)
    return True


def inherited_configuration(base_version: dict[str, Any]) -> dict[str, Any]:
    """Las claves de `INHERITED_CONFIGURATION_KEYS` que la versión base
    declara (las ausentes no se inventan: AWS aplica su propio default)."""
    configuration = {
        key: base_version[key]
        for key in INHERITED_CONFIGURATION_KEYS
        if base_version.get(key) is not None
    }
    if BASE_IMAGE_VERSION_KEY in configuration:
        configuration[BASE_IMAGE_VERSION_KEY] = requestable_base_image_version(
            configuration[BASE_IMAGE_VERSION_KEY]
        )
    return configuration


def requestable_base_image_version(echoed: Any) -> str:
    """La grafía de `baseImageVersion` que aceptan `create`/
    `update-microvm-image`: la versión gestionada mayor (`1`), no la
    normalizada (`1.0`) que devuelve `get-microvm-image-version` (Q52).
    Reenviar el eco tal cual da `ValidationException` "Expected a single
    major version number" (Q115). Una grafía no numérica o con parte
    fraccionaria se deja intacta: la validación es de AWS."""
    text = str(echoed)
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite() or number != number.to_integral_value():
        return text
    return str(int(number))


def submit_image_build(
    clients: ImageBuildClients, name: str, arn: str, request: dict[str, Any]
) -> SubmittedBuild:
    """`update-microvm-image` si la imagen ya existe, si no
    `create-microvm-image`; los `ClientError` (incluido
    `BUILD_QUOTA_ERROR_CODE`) suben tal cual para que cada llamante los
    traduzca a su propio error."""
    if image_exists(clients, arn):
        response = clients.microvms.update_microvm_image(imageIdentifier=arn, **request)
        created = False
    else:
        response = clients.microvms.create_microvm_image(name=name, **request)
        created = True
    return SubmittedBuild(
        arn=str(response["imageArn"]), version=str(response["imageVersion"]), created=created
    )


def base_image_version_matches(echoed: Any, desired: Any) -> bool:
    """``1`` y ``1.0`` nombran la misma versión gestionada: la API acepta la
    forma de ``list-managed-microvm-image-versions`` pero devuelve la
    normalizada (``AWS_API_NOTES.md`` Q52). Cualquier otra forma sólo
    coincide exactamente."""
    if not isinstance(echoed, str) or not isinstance(desired, str):
        return bool(echoed == desired)
    if echoed == desired:
        return True
    try:
        return Decimal(echoed) == Decimal(desired)
    except InvalidOperation:
        return False


def value_matches(key: str, echoed: Any, desired: Any) -> bool:
    if key == BASE_IMAGE_VERSION_KEY:
        return base_image_version_matches(echoed, desired)
    return bool(echoed == desired)


def configuration_matches(version: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(value_matches(key, version.get(key), value) for key, value in desired.items())


def find_reusable_version(
    clients: ImageBuildClients, arn: str, desired: dict[str, Any]
) -> str | None:
    """La versión lanzable más reciente que ya tiene exactamente `desired`
    (artefacto y configuración): reusarla evita un build y una semana más
    de almacenamiento de snapshot. `None` si no hay ninguna."""
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    matches = [
        item
        for page in paginator.paginate(imageIdentifier=arn)
        for item in page["items"]
        if item["state"] == SUCCESSFUL_VERSION_STATE
        and item["status"] == ACTIVE_VERSION_STATUS
        and configuration_matches(item, desired)
    ]
    if not matches:
        return None
    return str(max(matches, key=lambda item: item["createdAt"])["imageVersion"])


@dataclass(frozen=True)
class VersionGate:
    """Los tres estados independientes que deben pasar antes de
    `run-microvm`."""

    image_state: str
    version_state: str
    version_status: str
    state_reason: str | None

    @property
    def launchable(self) -> bool:
        return (
            self.image_state in LAUNCHABLE_IMAGE_STATES
            and self.version_state == SUCCESSFUL_VERSION_STATE
            and self.version_status == ACTIVE_VERSION_STATUS
        )

    @property
    def settled(self) -> bool:
        """Terminó de resolverse, bien o mal: una versión `FAILED` ya es
        definitiva aunque la imagen (que puede llevar otras versiones
        vivas) nunca llegue a `FAILED_IMAGE_STATES`; una imagen que sí cae
        en `FAILED_IMAGE_STATES` (p. ej. `UPDATE_FAILED`) también es
        definitiva aunque esta versión concreta quedara `SUCCESSFUL`."""
        version_done = self.version_state in SETTLED_VERSION_STATES
        image_done = self.image_state in LAUNCHABLE_IMAGE_STATES | FAILED_IMAGE_STATES
        return (version_done and image_done) or self.version_state == "FAILED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "imageState": self.image_state,
            "versionState": self.version_state,
            "versionStatus": self.version_status,
            "stateReason": self.state_reason,
            "launchable": self.launchable,
        }


def read_gate(clients: ImageBuildClients, arn: str, version: str) -> VersionGate:
    image = clients.microvms.get_microvm_image(imageIdentifier=arn)
    detail = clients.microvms.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
    return VersionGate(
        image_state=str(image["state"]),
        version_state=str(detail["state"]),
        version_status=str(detail["status"]),
        state_reason=detail.get("stateReason"),
    )


def wait_for_gate(
    clients: ImageBuildClients,
    arn: str,
    version: str,
    timeout_seconds: float,
    sleep: Sleeper = time.sleep,
    *,
    on_tick: Callable[[VersionGate, float], None] | None = None,
) -> tuple[VersionGate, bool]:
    """Sondea hasta que `settled` o se agote `timeout_seconds`. Devuelve
    `(gate, timed_out)`: el llamante decide qué excepción lanzar en cada
    caso (un timeout no es lo mismo que un build que de verdad falló)."""
    started = time.monotonic()
    while True:
        gate = read_gate(clients, arn, version)
        elapsed = time.monotonic() - started
        if on_tick is not None:
            on_tick(gate, elapsed)
        if gate.settled:
            return gate, False
        if elapsed >= timeout_seconds:
            return gate, True
        sleep(POLL_INTERVAL_SECONDS)
