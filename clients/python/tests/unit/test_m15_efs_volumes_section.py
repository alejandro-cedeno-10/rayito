"""`require_volume_support` (`m15-efs-volumes`, ADR-018, experimental): la
puerta de `volumes=` en `Sandbox.create()`. Ver también
`test_m15_feature_options.py` (las otras seis opciones 0.6)."""

from __future__ import annotations

import pytest

from rayito._volumes._domain import EfsVolume
from rayito._volumes._section import require_volume_support
from rayito.exceptions import InvalidArgumentException, UnimplementedError

VOLUME = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")


def test_an_empty_map_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        require_volume_support({}, image_variant=None)


def test_a_non_efs_volume_value_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        require_volume_support({"/mnt/v": object()}, image_variant=None)  # type: ignore[dict-item]


def test_an_overlapping_path_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        require_volume_support(
            {"/mnt/v": VOLUME, "/mnt/v/sub": VOLUME},
            image_variant=None,
        )


def test_a_non_caps_image_variant_is_rejected_before_the_generic_message() -> None:
    with pytest.raises(UnimplementedError, match="base-caps"):
        require_volume_support({"/mnt/v": VOLUME}, image_variant="base")


@pytest.mark.parametrize("image_variant", ["base-caps", None])
def test_a_well_formed_request_still_raises_unimplemented(image_variant: str | None) -> None:
    with pytest.raises(UnimplementedError, match=r"EFS-1\.\.EFS-20"):
        require_volume_support({"/mnt/v": VOLUME}, image_variant=image_variant)
