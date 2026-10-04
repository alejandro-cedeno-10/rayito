"""Adaptador puro `mounts=` -> `ConfigureRequest.s3_mounts` (`m15-s3-mounts`):
sólo construye y lee mensajes de proto ya generados, nunca llama a `grpc`.
El RPC en sí lo hacen los adaptadores de M15 foundations
(`sandbox_sync/configure.py` / `sandbox_async/configure.py`) sobre el
`ConfigureRequest` que `S3MountsSection.fill` rellena — estructuralmente
compatible con el Protocol `ConfigureSection` de `_configure_base.py`
(`section`, `required_flag`, `fill`, `check_result`, `settle_timeout_s`,
`check_status`), sin heredar de él (Protocol).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from rayito._mount_path import validate_mount_paths
from rayito.exceptions import MountException, UnimplementedError
from rayito.v1 import configure_pb2, s3_mounts_pb2

from ._domain import (
    MOUNT_ERROR_CLASSES,
    TIMEOUT_ERROR_CLASS,
    UNKNOWN_ERROR_CLASS,
    MountPhase,
    MountStatus,
    S3Mount,
)

SECTION_NAME: Final = "s3_mounts"
REQUIRED_FLAG: Final = "s3_mounts"

#: Cuánto espera `create()` a que cada montaje pase de `"pending"` a
#: `"mounted"`/`"failed"`. `rayd` ya acota attach + arranque + primera
#: respuesta de cada montaje a 10 s (`MOUNT_READY_TIMEOUT`,
#: `crates/rayd/src/features/s3_mounts.rs`) y lo marca `failed`/`timeout`
#: él mismo; 5 s más cubren la planificación de su tarea y el último sondeo,
#: para que el SDK informe del veredicto del agente en vez de adivinarlo.
MOUNT_SETTLE_TIMEOUT_S: Final = 15.0

_PHASE_TO_STATE: Final[dict[int, MountPhase]] = {
    s3_mounts_pb2.S3_MOUNT_PHASE_PENDING: "pending",
    s3_mounts_pb2.S3_MOUNT_PHASE_MOUNTED: "mounted",
    s3_mounts_pb2.S3_MOUNT_PHASE_FAILED: "failed",
}


@dataclass(frozen=True)
class S3MountsSection:
    """Una sección de `mounts=` lista para enviar en la siguiente
    `Configure`; `plan_s3_mounts` es la única forma de construir una."""

    mounts: Mapping[str, S3Mount]

    @property
    def section(self) -> str:
        return SECTION_NAME

    @property
    def required_flag(self) -> str:
        return REQUIRED_FLAG

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        request.s3_mounts.CopyFrom(to_proto(self.mounts))

    def check_result(self, code: int, error_class: str) -> None:
        check_section_result(code, error_class)

    @property
    def settle_timeout_s(self) -> float:
        return MOUNT_SETTLE_TIMEOUT_S

    def check_status(self, status: configure_pb2.ConfigureStatusResponse, *, final: bool) -> bool:
        return check_mounts_settled(from_proto_status(status.s3_mounts), self.mounts, final=final)


def plan_s3_mounts(mounts: Mapping[str, S3Mount] | None) -> S3MountsSection | None:
    """`None` si `mounts` es `None`: ninguna sección, ninguna llamada a
    `ConfigureSandbox`. Valida las rutas (`_mount_path`, compartida con
    `volumes=`) antes de construir nada; cada valor ya se validó a sí
    mismo al construirse (`S3Mount.__post_init__`).
    """
    if mounts is None:
        return None
    validate_mount_paths(mounts.keys())
    return S3MountsSection(mounts=dict(mounts))


def to_proto(mounts: Mapping[str, S3Mount]) -> s3_mounts_pb2.S3MountsConfig:
    return s3_mounts_pb2.S3MountsConfig(
        mounts=[
            s3_mounts_pb2.S3Mount(
                mount_path=path,
                bucket=mount.bucket,
                prefix=mount.prefix,
                read_only=mount.read_only,
                allow_overwrite=mount.allow_overwrite,
                allow_delete=mount.allow_delete,
            )
            for path, mount in mounts.items()
        ]
    )


def from_proto_status(status: s3_mounts_pb2.S3MountsStatus) -> dict[str, MountStatus]:
    return {
        state.mount_path: MountStatus(
            state=_PHASE_TO_STATE.get(state.phase, "pending"),
            last_error_class=state.error_class or None,
        )
        for state in status.mounts
    }


def check_section_result(code: int, error_class: str) -> None:
    """`None` si la sección se aplicó o sigue asentándose (`PENDING`: no es
    un error todavía; `create()` sondea entonces `ConfigureStatus` hasta que
    cada montaje se asiente, `check_mounts_settled`); en otro caso, la
    excepción que explica por qué.
    `code` es el entero de `SectionCode` tal cual lo manda `rayd`, nunca una
    cadena comparada a mano: compararlo contra las constantes generadas
    (`configure_pb2.SECTION_CODE_*`) es lo que haría fallar una comprobación
    `mypy`/`pyright` si el `.proto` cambiara algún día el valor de un
    miembro del enum.
    """
    if code in (configure_pb2.SECTION_CODE_APPLIED, configure_pb2.SECTION_CODE_PENDING):
        return
    if code == configure_pb2.SECTION_CODE_UNSUPPORTED:
        raise UnimplementedError(
            "mounts=",
            "necesita una imagen 0.6.0 o posterior con el agente de m15-s3-mounts",
        )
    code_name = configure_pb2.SectionCode.Name(code)
    raise MountException(
        f"mounts=: la sección se rechazó ({code_name})",
        code=error_class if error_class in MOUNT_ERROR_CLASSES else UNKNOWN_ERROR_CLASS,
    )


def check_mounts_settled(
    states: Mapping[str, MountStatus], wanted: Mapping[str, S3Mount], *, final: bool
) -> bool:
    """`True` si cada ruta de `wanted` ya está `"mounted"`. El primer
    montaje `"failed"` lanza `MountException` con su `last_error_class`
    (así `create()` termina el sandbox en vez de devolver uno con el
    montaje roto); con `final`, uno que siga `"pending"` (o que el agente
    no reporte) lanza `MountException(code="timeout")`.
    """
    settled = True
    for path in wanted:
        state = states.get(path)
        if state is not None and state.state == "failed":
            code = state.last_error_class or UNKNOWN_ERROR_CLASS
            raise MountException(
                f"mounts=: {path} no se pudo montar ({code})",
                code=code if code in MOUNT_ERROR_CLASSES else UNKNOWN_ERROR_CLASS,
            )
        if state is None or state.state != "mounted":
            settled = False
    if not settled and final:
        raise MountException(
            f"mounts=: algún montaje sigue sin asentarse tras {MOUNT_SETTLE_TIMEOUT_S:g} s",
            code=TIMEOUT_ERROR_CLASS,
        )
    return settled
