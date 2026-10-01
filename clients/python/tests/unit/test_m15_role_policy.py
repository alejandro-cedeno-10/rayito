"""`rayito._role_policy`: la puerta de caps para las funciones 0.6 que
necesitan el execution role dentro del guest."""

from __future__ import annotations

import pytest

from rayito._role_policy import require_caps_for, resolve_image_variant
from rayito.exceptions import UnimplementedError


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("rayito-base-caps", "base-caps"),
        ("rayito-base-caps-4gb", "base-caps"),
        ("rayito-base", "base"),
        ("rayito-base-poly", "base-poly"),
        (None, None),
        ("arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-caps", None),
        ("mi-imagen-propia", None),
    ],
)
def test_resolve_image_variant(template: str | None, expected: str | None) -> None:
    assert resolve_image_variant(template) == expected


def test_the_caps_variant_is_accepted() -> None:
    require_caps_for("volumes=", "base-caps")  # no debe lanzar


def test_an_unknown_variant_is_deferred_not_rejected() -> None:
    require_caps_for("volumes=", None)  # no debe lanzar: se decide tras /run


def test_a_known_non_caps_variant_is_rejected_before_launch() -> None:
    with pytest.raises(UnimplementedError, match="base-caps") as excinfo:
        require_caps_for("volumes=", "base")
    assert excinfo.value.feature == "volumes="
