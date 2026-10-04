"""`rayito._sizing`: dominio puro de m15-sizes-catalog. `resolve_size`
redondea hacia arriba dentro del catálogo cerrado (Q87: 512/1024/2048/4096/
8192 MiB), `apply_size_suffix` nombra la imagen sufijada y `warn_if_rounded`
sólo avisa cuando hubo redondeo de verdad."""

from __future__ import annotations

import warnings

import pytest

from rayito._limits import SUPPORTED_MEMORY_MIB
from rayito._sizing import (
    BASELINE_MEMORY_MIB,
    MAX_SUPPORTED_MEMORY_MIB,
    SIZE_NAMES,
    ResolvedSize,
    SizeRequest,
    apply_size_suffix,
    baseline_cpu_for,
    resolve_size,
    warn_if_rounded,
)
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning


def test_size_names_align_one_to_one_with_supported_memory_mib() -> None:
    """El único acoplamiento entre ambas tuplas es el orden; si
    `limits.json` cambiara el catálogo sin tocar `SIZE_NAMES` esto lo
    detecta antes que cualquier otra cosa."""
    assert len(SIZE_NAMES) == len(SUPPORTED_MEMORY_MIB)


@pytest.mark.parametrize("name,mib", list(zip(SIZE_NAMES, SUPPORTED_MEMORY_MIB, strict=True)))
def test_resolve_size_by_exact_name(name: str, mib: int) -> None:
    resolved = resolve_size(name)
    assert resolved == ResolvedSize(name=name, memory_mib=mib, requested_mib=mib)
    assert not resolved.rounded_up


def test_resolve_size_rounds_a_size_request_up() -> None:
    resolved = resolve_size(SizeRequest(memory_mib=3000))
    assert resolved.memory_mib == 4096
    assert resolved.name == "4gb"
    assert resolved.requested_mib == 3000
    assert resolved.rounded_up


def test_resolve_size_exact_size_request_does_not_round() -> None:
    resolved = resolve_size(SizeRequest(memory_mib=2048))
    assert resolved == ResolvedSize(name="2gb", memory_mib=2048, requested_mib=2048)
    assert not resolved.rounded_up


def test_resolve_size_unknown_name_is_invalid_argument() -> None:
    with pytest.raises(InvalidArgumentException, match="512mb"):
        resolve_size("huge")


def test_resolve_size_non_positive_request_is_invalid_argument() -> None:
    with pytest.raises(InvalidArgumentException):
        resolve_size(SizeRequest(memory_mib=0))
    with pytest.raises(InvalidArgumentException):
        resolve_size(SizeRequest(memory_mib=-1))


def test_resolve_size_above_the_catalog_is_invalid_argument_before_any_aws_call() -> None:
    """Q87: 16384 MiB da `ValidationException` síncrona sobre AWS sin crear
    nada; el SDK no debería ni llegar a intentarlo."""
    with pytest.raises(InvalidArgumentException, match=str(MAX_SUPPORTED_MEMORY_MIB)):
        resolve_size(SizeRequest(memory_mib=16384))


def test_is_baseline_is_true_only_for_the_unsuffixed_memory() -> None:
    assert resolve_size("2gb").is_baseline
    assert not resolve_size("4gb").is_baseline
    assert BASELINE_MEMORY_MIB == 2048


def test_apply_size_suffix_baseline_keeps_the_name_unchanged() -> None:
    assert apply_size_suffix("rayito-base", resolve_size("2gb")) == "rayito-base"


def test_apply_size_suffix_appends_the_size_name() -> None:
    assert apply_size_suffix("rayito-base", resolve_size("4gb")) == "rayito-base-4gb"
    assert apply_size_suffix("mi-imagen", resolve_size("512mb")) == "mi-imagen-512mb"


def test_apply_size_suffix_rejects_an_arn() -> None:
    arn = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
    with pytest.raises(InvalidArgumentException, match="ARN"):
        apply_size_suffix(arn, resolve_size("4gb"))


def test_baseline_cpu_for_matches_the_q68_measurement_at_2048_mib() -> None:
    """Q68 midió 4 vCPU con `minimumMemoryInMiB` 2048; RES-2/Q88 (la
    campaña dedicada de sizes-catalog) confirma exactamente este punto y lo
    extiende, medido, a los otros cuatro tamaños."""
    assert baseline_cpu_for(2048) == 4


@pytest.mark.parametrize(
    "mib,expected",
    [(512, 1), (1024, 2), (2048, 4), (4096, 8), (8192, 16)],
)
def test_baseline_cpu_for_matches_the_q88_measurement_for_every_catalog_size(
    mib: int, expected: int
) -> None:
    """RES-2/Q88: medido para los cinco tamaños del catálogo, no
    extrapolado."""
    assert baseline_cpu_for(mib) == expected


def test_baseline_cpu_for_never_goes_below_one() -> None:
    assert baseline_cpu_for(1) == 1


def test_warn_if_rounded_warns_only_when_rounded() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_if_rounded(resolve_size("4gb"), stacklevel=1)
    assert caught == []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_if_rounded(resolve_size(SizeRequest(memory_mib=3000)), stacklevel=1)
    assert len(caught) == 1
    assert issubclass(caught[0].category, RayitoCompatWarning)
    assert "3000" in str(caught[0].message)
    assert "4096" in str(caught[0].message)
