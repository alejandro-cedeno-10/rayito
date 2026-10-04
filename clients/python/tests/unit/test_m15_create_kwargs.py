"""`Sandbox.create()`/`AsyncSandbox.create()`: las opciones 0.6 que siguen
siendo un stub lanzan `UnimplementedError` antes de resolver ningún plano
de control (y por tanto antes de cualquier llamada a AWS: `plan_features`
corre antes de `resolve_control_plane`); `pool=` junto con cualquiera de
ellas (`size=` incluido, ya implementada) es `InvalidArgumentException` por
el mismo mecanismo que el resto de los kwargs de plano. `size=` por sí sola
ya no es un stub (m15-sizes-catalog): se prueba en
`test_m15_sizes_catalog_create.py`. `gateways=` (m15-secrets-gateway)
tampoco: con un valor mal formado lanza `InvalidArgumentException` en su
lugar, en el mismo punto (antes de `resolve_control_plane`)."""

from __future__ import annotations

import pytest

from rayito import (
    AsyncSandbox,
    AsyncSandboxPool,
    EfsVolume,
    OtlpAuth,
    Sandbox,
    SandboxPool,
    TelemetryExport,
)
from rayito.exceptions import InvalidArgumentException, UnimplementedError

#: Un valor bien formado: m15-efs-volumes valida la forma de `volumes=`
#: (tipo, rutas) antes de llegar a `UnimplementedError`, así que un
#: `object()` ahí ya no llegaría tan lejos (lanzaría InvalidArgumentException
#: por el tipo, no por ser un stub).
_VALID_VOLUME = {"/mnt/v": EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")}


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("mounts", {"/mnt/d": object()}),
        ("volumes", _VALID_VOLUME),
        # `events` validates its type and `logging` first: see
        # `test_m15_events_webhooks_feature_options.py`.
        # `telemetry=` (m15-rayd-otlp) validates for real now; a
        # `TelemetryExport(auth=OtlpAuth.execution_role())` against
        # `rayito-base` (not the caps variant) still raises
        # `UnimplementedError` before any control plane is resolved, just
        # via `_role_policy.require_caps_for` instead of an unconditional
        # stub raise.
        ("telemetry", TelemetryExport(auth=OtlpAuth.execution_role())),
        ("domain", object()),
    ],
)
def test_sync_create_rejects_each_remaining_stub_option_before_resolving_a_control_plane(
    option: str, value: object
) -> None:
    with pytest.raises(UnimplementedError):
        Sandbox.create("rayito-base", **{option: value})  # type: ignore[arg-type]


def test_sync_create_rejects_a_malformed_gateways_value_before_resolving_a_control_plane() -> None:
    with pytest.raises(InvalidArgumentException):
        Sandbox.create("rayito-base", gateways={"anthropic": object()})


@pytest.mark.asyncio
async def test_async_create_rejects_an_0_6_option_too() -> None:
    with pytest.raises(UnimplementedError):
        await AsyncSandbox.create(
            "rayito-base", telemetry=TelemetryExport(auth=OtlpAuth.execution_role())
        )


def test_pool_with_a_0_6_option_is_invalid_argument() -> None:
    pool = SandboxPool.__new__(SandboxPool)
    with pytest.raises(InvalidArgumentException, match="mounts"):
        Sandbox.create(pool=pool, mounts={"/mnt/d": object()})


@pytest.mark.asyncio
async def test_async_pool_with_a_0_6_option_is_invalid_argument() -> None:
    pool = AsyncSandboxPool.__new__(AsyncSandboxPool)
    with pytest.raises(InvalidArgumentException, match="telemetry"):
        await AsyncSandbox.create(pool=pool, telemetry=object())


def test_pool_with_size_is_invalid_argument_even_though_size_is_implemented() -> None:
    """m15-sizes-catalog: `size=` ya resuelve de verdad, pero sigue sin
    poder combinarse con `pool=` (architecture §7.3: "create(pool=,
    size=) es rechazado"): una plaza ya salió de una imagen fija."""
    pool = SandboxPool.__new__(SandboxPool)
    with pytest.raises(InvalidArgumentException, match="size"):
        Sandbox.create(pool=pool, size="4gb")


#: Marcador de documentación (cuenta ficticia).
_CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"


@pytest.mark.parametrize("egress", [None, ["INTERNET_EGRESS"], [_CONNECTOR, "INTERNET_EGRESS"]])
def test_sync_create_rejects_volumes_without_a_single_own_connector(
    egress: list[str] | None,
) -> None:
    """`create()` hands its `egress=` to the `volumes=` gate: a MicroVM takes
    one egress connector (Q131), so a volume never runs with
    INTERNET_EGRESS, and this fails before any control plane is resolved."""
    with pytest.raises(InvalidArgumentException, match="NAT"):
        Sandbox.create("rayito-base-caps", volumes=_VALID_VOLUME, egress=egress)


@pytest.mark.asyncio
async def test_async_create_rejects_volumes_with_internet_egress() -> None:
    with pytest.raises(InvalidArgumentException, match="INTERNET_EGRESS"):
        await AsyncSandbox.create(
            "rayito-base-caps", volumes=_VALID_VOLUME, egress=[_CONNECTOR, "INTERNET_EGRESS"]
        )


def test_sync_create_with_volumes_and_its_connector_is_still_unimplemented() -> None:
    with pytest.raises(UnimplementedError, match="amazon-efs-utils"):
        Sandbox.create("rayito-base-caps", volumes=_VALID_VOLUME, egress=[_CONNECTOR])
