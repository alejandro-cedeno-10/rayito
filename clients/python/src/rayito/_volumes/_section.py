"""`volumes=` en `Sandbox.create()` (`m15-efs-volumes`, ADR-018,
experimental): la validación previa a cualquier llamada a AWS
(`plan_volumes`) y la sección `efs_volumes` de `ConfigureSandbox`
(`EfsVolumesSection`), que `create()` manda en su único `Configure` tras la
readiness, igual que `mounts=`/`gateways=`.

`rayd` sólo monta en una imagen con `amazon-efs-utils` y `CAP_SYS_ADMIN`
(la imagen opcional `rayito-base-caps-efs`, `rayito image publish
--with-efs`); en cualquier otra `Health.features.efs_volumes` es `false` y
`create()` termina el sandbox con `UnimplementedError` antes de construir el
`ConfigureRequest`.

El conector se comprueba antes de lanzar porque un MicroVM admite **un solo**
conector de egress (`AWS_API_NOTES.md` §16 Q131, EFS-4): un volumen necesita
el de `infra/efs-volumes.yaml`, así que el sandbox no puede usar además
`INTERNET_EGRESS`, y un `egress=` omitido lo hereda de la imagen y nunca
llega al mount target. `execution_role_arn=` también: `efs-utils` firma el
túnel TLS con las credenciales del execution role que sirve IMDS (Q128).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, NoReturn

from rayito._configure_base import AgentFeatures
from rayito._limits import EFS_VOLUMES_MAX_PER_SANDBOX
from rayito._models import has_internet_connector
from rayito._mount_path import validate_mount_paths
from rayito._role_policy import require_caps_for
from rayito._volumes._domain import EfsVolume, MountState, VolumeStatus
from rayito.exceptions import InvalidArgumentException, UnimplementedError, VolumeMountException
from rayito.v1 import configure_pb2, efs_volumes_pb2

SECTION_NAME: Final = "efs_volumes"
REQUIRED_FLAG: Final = "efs_volumes"
FEATURE: Final = "volumes="

#: La página de la función, enlazada desde cada `UnimplementedError`.
VOLUMES_DOC: Final = "docs/site/docs/funciones-opcionales/volumenes-efs.md"
#: La página que explica cómo dar internet a un sandbox con volumen.
VPC_GUIDE: Final = "funciones-opcionales/volumenes-efs-vpc.md"
#: Lo que un sandbox con volumen hace para tener también internet: salir
#: por la VPC, porque el MicroVM sólo admite un conector de egress (Q131).
INTERNET_THROUGH_VPC: Final = (
    "para tener internet además del volumen, sal por tu VPC: una NAT o un transit gateway "
    "en las subredes del conector y un conector cuyo grupo de seguridad permita esa salida "
    f"(el de efs-volumes sólo deja salir NFS); ver {VPC_GUIDE}"
)
#: Por qué una imagen no monta: le falta `amazon-efs-utils` o `CAP_SYS_ADMIN`.
IMAGE_REASON: Final = (
    "esta imagen no trae amazon-efs-utils (o no corre con additionalOsCapabilities ALL): "
    "publica la imagen opcional rayito-base-caps-efs con "
    "`rayito image publish --with-efs --os-capabilities ALL` y lanza el sandbox con ella"
)

#: `rayd` lanza `mount -t efs` con este plazo por volumen
#: (`MOUNT_HELPER_TIMEOUT`, `crates/rayd/src/adapters/efs_mount.rs`) y monta
#: los volúmenes de uno en uno dentro del propio `Configure`.
MOUNT_HELPER_TIMEOUT_SECONDS: Final = 15.0
#: Lo que se suma al peor caso de `rayd` para el *bind mount*, la red y la
#: planificación de la tarea.
VOLUME_APPLY_MARGIN_SECONDS: Final = 5.0
#: El plazo de la llamada `Configure` que lleva volúmenes (y de la espera si
#: un `rayd` futuro responde `PENDING`): el peor caso de `rayd` con el máximo
#: de volúmenes más el margen. Con un volumen, el montaje medido tarda
#: p50 313 ms, p95 589 ms (Q128).
VOLUME_APPLY_TIMEOUT_SECONDS: Final = (
    EFS_VOLUMES_MAX_PER_SANDBOX * MOUNT_HELPER_TIMEOUT_SECONDS + VOLUME_APPLY_MARGIN_SECONDS
)

#: Los `last_error_class` de un volumen `FAILED` que documenta
#: `efs_volumes.proto`; cualquier otro llega como `UNKNOWN_ERROR_CLASS`.
VOLUME_MOUNT_ERROR_CLASSES: Final = frozenset(
    {"network", "iam_denied", "not_found", "tls", "helper_missing", "timeout", "invalid_path"}
)
TIMEOUT_ERROR_CLASS: Final = "timeout"
UNKNOWN_ERROR_CLASS: Final = "unknown"

_STATE_NAMES: Final[dict[int, MountState]] = {
    efs_volumes_pb2.EFS_VOLUME_STATE_REQUESTED: "requested",
    efs_volumes_pb2.EFS_VOLUME_STATE_MOUNTING: "mounting",
    efs_volumes_pb2.EFS_VOLUME_STATE_MOUNTED: "mounted",
    efs_volumes_pb2.EFS_VOLUME_STATE_DEGRADED: "degraded",
    efs_volumes_pb2.EFS_VOLUME_STATE_REMOUNTING: "remounting",
    efs_volumes_pb2.EFS_VOLUME_STATE_UNMOUNTED: "unmounted",
    efs_volumes_pb2.EFS_VOLUME_STATE_FAILED: "failed",
}


def require_volume_connector(egress: Sequence[str] | None, *, feature: str = FEATURE) -> None:
    """Sin I/O: `egress` tiene que ser exactamente un conector propio (el
    `ConnectorArn` de `infra/efs-volumes.yaml` o uno de tu VPC que llegue
    al mount target). `InvalidArgumentException` si falta (el sandbox
    heredaría `INTERNET_EGRESS` de la imagen), si incluye `INTERNET_EGRESS`
    o si trae más de uno: `run-microvm` respondería `ValidationException`
    "Only one egress network connector can be provided" (Q131)."""
    connectors = tuple(egress or ())
    if not connectors:
        raise InvalidArgumentException(
            f"{feature} necesita egress=[<ConnectorArn de efs-volumes>]: sin él el sandbox hereda "
            f"INTERNET_EGRESS de la imagen y no llega al mount target; {INTERNET_THROUGH_VPC}"
        )
    if has_internet_connector(connectors):
        raise InvalidArgumentException(
            f"{feature} no se combina con INTERNET_EGRESS: un MicroVM admite un solo conector de "
            f"egress y el volumen necesita el de tu VPC; {INTERNET_THROUGH_VPC}"
        )
    if len(connectors) > 1:
        raise InvalidArgumentException(
            f"{feature} admite un solo conector en egress= (un MicroVM sólo acepta uno); "
            f"{INTERNET_THROUGH_VPC}"
        )


def require_execution_role(execution_role_arn: str | None, *, feature: str = FEATURE) -> None:
    """Sin I/O: `efs-utils` firma el túnel con las credenciales del
    execution role (IMDS, Q128); sin rol el montaje acabaría en
    `iam_denied` tras haber lanzado (y pagado) el MicroVM."""
    if not execution_role_arn:
        raise InvalidArgumentException(
            f"{feature} necesita execution_role_arn=: amazon-efs-utils firma el túnel TLS con "
            "las credenciales del execution role (IMDS), que necesita la política "
            "CallerPolicyArn de la pila efs-volumes"
        )


def validate_volume_values(values: Iterable[object], *, feature: str = FEATURE) -> None:
    """Cada valor es un `EfsVolume` (ya validado al construirse)."""
    for value in values:
        if not isinstance(value, EfsVolume):
            raise InvalidArgumentException(
                f"{feature} espera valores EfsVolume, se recibió {type(value).__name__}"
            )


def validate_volume_count(count: int, *, feature: str = FEATURE) -> None:
    """Entre 1 y `EFS_VOLUMES_MAX_PER_SANDBOX` volúmenes (el tope de
    `rayd_core::volume`, research doc §5)."""
    if count == 0:
        raise InvalidArgumentException(f"{feature} no admite un mapa vacío; omite el argumento")
    if count > EFS_VOLUMES_MAX_PER_SANDBOX:
        raise InvalidArgumentException(
            f"{feature} admite como mucho {EFS_VOLUMES_MAX_PER_SANDBOX} volúmenes por sandbox"
        )


@dataclass(frozen=True)
class VolumesRequest:
    """`volumes=` ya validado, todavía sin las IPs de mount target que
    `create()` resuelve justo antes de lanzar (`_feature_options.prepare_features`)."""

    volumes: Mapping[str, EfsVolume]


def plan_volumes(
    volumes: Mapping[str, EfsVolume],
    *,
    image_variant: str | None,
    egress: Sequence[str] | None,
    execution_role_arn: str | None,
) -> VolumesRequest:
    """Valida `volumes=` por completo, sin I/O. El orden importa (forma,
    rutas, caps, conector y rol) para que el primer error que vea el
    llamante sea siempre el que puede corregir."""
    validate_volume_count(len(volumes))
    validate_volume_values(volumes.values())
    validate_mount_paths(volumes.keys())
    require_caps_for(FEATURE, image_variant)
    require_volume_connector(egress)
    require_execution_role(execution_role_arn)
    return VolumesRequest(volumes=dict(volumes))


def require_volume_mounts(
    paths: Iterable[str], *, image_variant: str | None, feature: str = "volume_mounts"
) -> None:
    """La parte de `plan_volumes` que el shim de E2B comprueba sin I/O sobre
    `volume_mounts=` antes de resolver ningún nombre: número, rutas y
    variante caps."""
    resolved = tuple(paths)
    validate_volume_count(len(resolved), feature=feature)
    validate_mount_paths(resolved)
    require_caps_for(feature, image_variant)


def to_proto(volumes: Mapping[str, EfsVolume]) -> efs_volumes_pb2.EfsVolumesConfig:
    return efs_volumes_pb2.EfsVolumesConfig(
        mounts=[
            efs_volumes_pb2.EfsVolumeMount(
                mount_path=path,
                file_system_id=volume.file_system_id,
                access_point_id=volume.access_point_id,
                read_only=volume.read_only,
                mount_target_ip=volume.mount_target_ip or "",
            )
            for path, volume in volumes.items()
        ]
    )


def from_proto_status(status: efs_volumes_pb2.EfsVolumesStatus) -> dict[str, VolumeStatus]:
    return {
        volume.mount_path: VolumeStatus(
            state=_STATE_NAMES.get(volume.state, "requested"),
            last_error_class=volume.last_error_class or None,
        )
        for volume in status.volumes
    }


def mount_error(path: str | None, error_class: str) -> VolumeMountException:
    """La excepción de un volumen que no montó, con `code` de la lista
    cerrada (nunca el texto del helper ni un identificador)."""
    code = error_class if error_class in VOLUME_MOUNT_ERROR_CLASSES else UNKNOWN_ERROR_CLASS
    where = f"{path} " if path else ""
    return VolumeMountException(f"volumes=: {where}no se pudo montar ({code})", code=code)


def unsupported_image() -> UnimplementedError:
    return UnimplementedError(FEATURE, IMAGE_REASON, doc=VOLUMES_DOC)


@dataclass(frozen=True)
class EfsVolumesSection:
    """La sección `efs_volumes` del `Configure` de `create()`, con cada
    `mount_target_ip` ya resuelto. `rayd` monta dentro del propio
    `Configure` (de uno en uno, hasta el primer fallo), así que la llamada
    lleva el plazo `VOLUME_APPLY_TIMEOUT_SECONDS` (`apply_timeout_s`) y un
    `APPLIED` ya es «todos `mounted`»; si un `rayd` futuro respondiera
    `PENDING`, `check_status` espera a `mounted` con el mismo plazo."""

    volumes: Mapping[str, EfsVolume]

    @property
    def section(self) -> str:
        return SECTION_NAME

    @property
    def required_flag(self) -> str:
        return REQUIRED_FLAG

    @property
    def apply_timeout_s(self) -> float:
        return VOLUME_APPLY_TIMEOUT_SECONDS

    @property
    def settle_timeout_s(self) -> float:
        return VOLUME_APPLY_TIMEOUT_SECONDS

    def require_support(self, features: AgentFeatures) -> None:
        if not features.efs_volumes:
            raise unsupported_image()

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        request.efs_volumes.CopyFrom(to_proto(self.volumes))

    def check_result(self, code: int, error_class: str) -> None:
        if code in (configure_pb2.SECTION_CODE_APPLIED, configure_pb2.SECTION_CODE_PENDING):
            return
        if code == configure_pb2.SECTION_CODE_UNSUPPORTED:
            raise unsupported_image()
        raise mount_error(None, error_class)

    def check_status(self, status: configure_pb2.ConfigureStatusResponse, *, final: bool) -> bool:
        states = from_proto_status(status.efs_volumes)
        return check_volumes_settled(states, self.volumes, final=final)


def check_volumes_settled(
    states: Mapping[str, VolumeStatus], wanted: Mapping[str, EfsVolume], *, final: bool
) -> bool:
    """`True` si cada ruta de `wanted` ya está `"mounted"`; el primer
    volumen `"failed"` lanza `VolumeMountException` con su clase y, con
    `final`, uno que siga sin montar lanza `VolumeMountException("timeout")`."""
    settled = True
    for path in wanted:
        state = states.get(path)
        if state is not None and state.state == "failed":
            raise mount_error(path, state.last_error_class or UNKNOWN_ERROR_CLASS)
        if state is None or state.state != "mounted":
            settled = False
    if not settled and final:
        raise mount_error(None, TIMEOUT_ERROR_CLASS)
    return settled


def refuse_internet_with_volumes(feature: str) -> NoReturn:
    """El shim de E2B con `volume_mounts=` y `allow_internet_access=True`:
    un MicroVM sólo admite un conector de egress (Q131)."""
    raise InvalidArgumentException(
        f"{feature} no se combina con allow_internet_access=True: un MicroVM admite un solo "
        f"conector de egress y el volumen necesita el de tu VPC; {INTERNET_THROUGH_VPC}"
    )
