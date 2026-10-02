"""`Sandbox.create(size=...)`/`AsyncSandbox.create(size=...)` de extremo a
extremo contra el plano de control falso (m15-sizes-catalog): el sufijo de
tamaño se resuelve en cliente antes de pedir el ARN, `get_info()` confirma
`baseline_memory_mib` con una única llamada cacheada a
`GetMicrovmImageVersion`, y `size=None` (el valor por defecto) no hace
ninguna de las dos cosas."""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

from rayito import AsyncSandbox, Sandbox
from rayito._sizing import SizeRequest
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning

from .conftest import ACCOUNT_ID, REGION, TrackingTransport
from .fake_control_plane import FakeControlPlane

BASE_TEMPLATE = "rayito-base"
BASE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{BASE_TEMPLATE}"
SIZED_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{BASE_TEMPLATE}-4gb"


@pytest.fixture
def plane() -> Iterator[FakeControlPlane]:
    fake = FakeControlPlane()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def transport() -> TrackingTransport:
    return TrackingTransport.for_loopback()


def test_size_appends_the_suffix_to_the_resolved_image_arn(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    sbx = Sandbox.create(
        BASE_TEMPLATE,
        size="4gb",
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
    )
    assert sbx.info.template == SIZED_ARN


def test_size_baseline_keeps_the_unsuffixed_image_name(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    sbx = Sandbox.create(
        BASE_TEMPLATE,
        size="2gb",
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
    )
    assert sbx.info.template == BASE_ARN


def test_without_size_the_control_plane_never_sees_get_microvm_image_version(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    sbx = Sandbox.create(
        BASE_TEMPLATE, idle=None, control_plane=plane, transport=transport, ready_timeout=10
    )
    info = sbx.get_info()
    assert info.size is None
    assert info.baseline_memory_mib is None
    assert info.baseline_cpu is None
    assert plane.calls_to("GetMicrovmImageVersion") == []


def test_get_info_confirms_baseline_memory_with_one_cached_call(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    plane.set_image_version_memory(SIZED_ARN, "1.0", 4096)
    sbx = Sandbox.create(
        BASE_TEMPLATE,
        size="4gb",
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
    )
    first = sbx.get_info()
    second = sbx.get_info()
    assert first.size == second.size == "4gb"
    assert first.baseline_memory_mib == second.baseline_memory_mib == 4096
    assert first.baseline_cpu == second.baseline_cpu == 8  # RES-2/Q88: 4096 MiB -> 8 vCPU
    assert len(plane.calls_to("GetMicrovmImageVersion")) == 1


def test_a_size_request_that_rounds_up_warns_once(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Sandbox.create(
            BASE_TEMPLATE,
            size=SizeRequest(memory_mib=3000),
            idle=None,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
    rounding_warnings = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert len(rounding_warnings) == 1


def test_the_rounding_warning_points_at_the_callers_create_call(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    """code review de PR #76: `stacklevel` tiene que señalar la línea de
    quien llama a `Sandbox.create(...)`, no el cuerpo de `create()` ni de
    `plan_size`/`warn_if_rounded` (`sandbox_sync/main.py`)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Sandbox.create(
            BASE_TEMPLATE,
            size=SizeRequest(memory_mib=3000),
            idle=None,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
    (warning,) = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert warning.filename == __file__


@pytest.mark.asyncio
async def test_the_rounding_warning_points_at_the_callers_create_call_async(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    """Misma regla que la versión sync, para `sandbox_async/main.py`."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await AsyncSandbox.create(
            BASE_TEMPLATE,
            size=SizeRequest(memory_mib=3000),
            idle=None,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
    (warning,) = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert warning.filename == __file__


def test_size_with_an_arn_template_is_invalid_argument_before_any_aws_call(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    with pytest.raises(InvalidArgumentException, match="ARN"):
        Sandbox.create(
            BASE_ARN,
            size="4gb",
            idle=None,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
    assert plane.calls == []


def test_impossible_size_is_rejected_before_any_aws_call(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    with pytest.raises(InvalidArgumentException):
        Sandbox.create(
            BASE_TEMPLATE,
            size=SizeRequest(memory_mib=16384),
            idle=None,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
    assert plane.calls == []


@pytest.mark.asyncio
async def test_async_create_resolves_size_the_same_way(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    plane.set_image_version_memory(SIZED_ARN, "1.0", 4096)
    sbx = await AsyncSandbox.create(
        BASE_TEMPLATE,
        size="4gb",
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
    )
    info = await sbx.get_info()
    assert info.template == SIZED_ARN
    assert info.baseline_memory_mib == 4096
    assert info.baseline_cpu == 8
