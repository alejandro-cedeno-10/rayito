"""`rayito._role_policy`: la puerta de caps para las funciones 0.6 que
necesitan el execution role dentro del guest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._limits import OFFICIAL_IMAGE_VARIANTS
from rayito._role_policy import (
    CAPS_VARIANTS,
    EFS_CAPS_VARIANT,
    require_caps_for,
    require_efs_for,
    resolve_image_variant,
)
from rayito.exceptions import UnimplementedError

#: Los mismos vectores que `clients/typescript/tests/unit/role-policy.test.ts`.
VECTORS: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).resolve().parents[4] / "testdata/image-variants/vectors.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("vector", VECTORS["resolve"], ids=lambda v: str(v["template"]))
def test_resolve_image_variant_matches_the_shared_vectors(vector: dict[str, Any]) -> None:
    assert resolve_image_variant(vector["template"]) == vector["variant"]


def test_the_catalogue_contains_the_caps_variants() -> None:
    assert CAPS_VARIANTS.issubset(OFFICIAL_IMAGE_VARIANTS)


@pytest.mark.parametrize("vector", VECTORS["requireCaps"], ids=lambda v: str(v["variant"]))
def test_require_caps_for_matches_the_shared_vectors(vector: dict[str, Any]) -> None:
    if vector["accepted"]:
        require_caps_for("mounts=", vector["variant"])
    else:
        with pytest.raises(UnimplementedError, match="base-caps"):
            require_caps_for("mounts=", vector["variant"])


@pytest.mark.parametrize("vector", VECTORS["requireEfs"], ids=lambda v: str(v["variant"]))
def test_require_efs_for_matches_the_shared_vectors(vector: dict[str, Any]) -> None:
    if vector["accepted"]:
        require_efs_for("volumes=", vector["variant"])
    else:
        with pytest.raises(UnimplementedError, match=EFS_CAPS_VARIANT) as excinfo:
            require_efs_for("volumes=", vector["variant"])
        assert excinfo.value.feature == "volumes="


@pytest.mark.parametrize("variant", ["base-caps", "base-caps-efs"])
def test_the_caps_variants_are_accepted(variant: str) -> None:
    require_caps_for("volumes=", variant)  # no debe lanzar


def test_the_efs_image_name_resolves_to_an_accepted_caps_variant() -> None:
    require_caps_for("mounts=", resolve_image_variant("rayito-base-caps-efs-4gb"))


def test_an_unknown_variant_is_deferred_not_rejected() -> None:
    require_caps_for("volumes=", None)  # no debe lanzar: se decide tras /run


def test_a_known_non_caps_variant_is_rejected_before_launch() -> None:
    with pytest.raises(UnimplementedError, match="base-caps") as excinfo:
        require_caps_for("volumes=", "base")
    assert excinfo.value.feature == "volumes="
