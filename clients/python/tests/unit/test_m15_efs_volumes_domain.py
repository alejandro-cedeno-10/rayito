"""Dominio puro de `m15-efs-volumes` (ADR-018, experimental):
validación de `EfsVolume` y de los identificadores de EFS."""

from __future__ import annotations

import pytest

from rayito._volumes._domain import EfsVolume, validate_volume_name
from rayito.exceptions import InvalidArgumentException

FS = "fs-0123abcd"
AP = "fsap-0123abcd"


def test_a_well_formed_volume_constructs() -> None:
    volume = EfsVolume(file_system_id=FS, access_point_id=AP, name="datos-7")
    assert volume.read_only is False
    assert volume.name == "datos-7"


def test_a_malformed_file_system_id_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        EfsVolume(file_system_id="not-an-id", access_point_id=AP)


def test_a_malformed_access_point_id_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        EfsVolume(file_system_id=FS, access_point_id="fs-0123abcd")


def test_an_access_point_id_with_uppercase_hex_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        EfsVolume(file_system_id=FS, access_point_id="fsap-0123ABCD")


def test_a_volume_name_with_a_slash_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException):
        validate_volume_name("not/valid")


def test_a_volume_name_with_letters_numbers_and_hyphens_is_accepted() -> None:
    assert validate_volume_name("datos-agente-7") == "datos-agente-7"
