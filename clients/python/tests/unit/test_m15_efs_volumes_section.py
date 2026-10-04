"""`require_volume_support` (`m15-efs-volumes`, ADR-018, experimental): la
puerta de `volumes=` en `Sandbox.create()`. Ver también
`test_m15_feature_options.py` (las otras seis opciones 0.6)."""

from __future__ import annotations

import pytest

from rayito._volumes._domain import EfsVolume
from rayito._volumes._section import VPC_GUIDE, require_volume_support
from rayito.exceptions import InvalidArgumentException, UnimplementedError

VOLUME = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")
#: Marcador de documentación (cuenta y región ficticias).
CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"
INTERNET_ARN = (
    "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
)


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
    with pytest.raises(UnimplementedError, match="amazon-efs-utils"):
        require_volume_support({"/mnt/v": VOLUME}, image_variant=image_variant, egress=[CONNECTOR])


@pytest.mark.parametrize("egress", [None, []])
def test_a_volume_without_its_connector_is_rejected_naming_the_alternative(
    egress: list[str] | None,
) -> None:
    """Without `egress=` the MicroVM inherits INTERNET_EGRESS (Q60) and
    never reaches the mount target."""
    with pytest.raises(InvalidArgumentException, match="egress=") as caught:
        require_volume_support({"/mnt/v": VOLUME}, image_variant="base-caps", egress=egress)
    assert VPC_GUIDE in str(caught.value)


@pytest.mark.parametrize("internet", ["INTERNET_EGRESS", INTERNET_ARN])
def test_a_volume_with_internet_egress_is_rejected_before_launch(internet: str) -> None:
    """Q131: `egressNetworkConnectors=[INTERNET_EGRESS, <VPC>]` is a
    ValidationException; the SDK says so first and names the way out
    (internet through the customer's VPC NAT or transit gateway)."""
    with pytest.raises(InvalidArgumentException, match="INTERNET_EGRESS") as caught:
        require_volume_support(
            {"/mnt/v": VOLUME}, image_variant="base-caps", egress=[CONNECTOR, internet]
        )
    message = str(caught.value)
    assert "NAT" in message and "transit gateway" in message


def test_a_volume_with_two_own_connectors_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="un solo conector"):
        require_volume_support(
            {"/mnt/v": VOLUME}, image_variant="base-caps", egress=[CONNECTOR, CONNECTOR + "-b"]
        )


def test_the_caps_check_comes_before_the_connector_check() -> None:
    with pytest.raises(UnimplementedError, match="base-caps"):
        require_volume_support({"/mnt/v": VOLUME}, image_variant="base", egress=None)
