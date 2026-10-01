"""`Sandbox.create()`/`AsyncSandbox.create()`: las siete opciones 0.6 lanzan
`UnimplementedError` antes de resolver ningún plano de control (y por tanto
antes de cualquier llamada a AWS: `plan_features` corre antes de
`resolve_control_plane`); `pool=` junto con cualquiera de ellas es
`InvalidArgumentException` por el mismo mecanismo que el resto de los
kwargs de plano."""

from __future__ import annotations

import pytest

from rayito import AsyncSandbox, AsyncSandboxPool, OtlpAuth, Sandbox, SandboxPool, TelemetryExport
from rayito.exceptions import InvalidArgumentException, UnimplementedError


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("mounts", {"/mnt/d": object()}),
        ("volumes", {"/mnt/v": object()}),
        ("size", "4gb"),
        ("events", object()),
        # `telemetry=` (m15-rayd-otlp) validates for real now; a
        # `TelemetryExport(auth=OtlpAuth.execution_role())` against
        # `rayito-base` (not the caps variant) still raises
        # `UnimplementedError` before any control plane is resolved, just
        # via `_role_policy.require_caps_for` instead of an unconditional
        # stub raise.
        ("telemetry", TelemetryExport(auth=OtlpAuth.execution_role())),
        ("gateways", {"anthropic": object()}),
        ("domain", object()),
    ],
)
def test_sync_create_rejects_each_0_6_option_before_resolving_a_control_plane(
    option: str, value: object
) -> None:
    with pytest.raises(UnimplementedError):
        Sandbox.create("rayito-base", **{option: value})  # type: ignore[arg-type]


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
