"""`rayito._mount_path.validate_mount_paths`: regla compartida por
`mounts=`/`volumes=`."""

from __future__ import annotations

import pytest

from rayito._mount_path import MAX_MOUNTS, validate_mount_paths
from rayito.exceptions import InvalidArgumentException


def test_valid_paths_are_returned_as_given() -> None:
    paths = ("/mnt/data", "/home/user/shared")
    assert validate_mount_paths(paths) == paths


def test_relative_paths_are_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="absoluta"):
        validate_mount_paths(["mnt/data"])


@pytest.mark.parametrize("path", ["/mnt/../etc", "/mnt//data", "/mnt/data/", "/mnt/./data"])
def test_non_canonical_paths_are_rejected(path: str) -> None:
    with pytest.raises(InvalidArgumentException, match="canónica"):
        validate_mount_paths([path])


@pytest.mark.parametrize("path", ["/etc/passwd", "/root/data", "/mnt", "/home/user"])
def test_paths_outside_the_allowed_roots_are_rejected(path: str) -> None:
    with pytest.raises(InvalidArgumentException, match="mnt"):
        validate_mount_paths([path])


def test_overlapping_paths_are_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="solapar"):
        validate_mount_paths(["/mnt/data", "/mnt/data/sub"])


def test_identical_paths_are_rejected_as_overlapping() -> None:
    with pytest.raises(InvalidArgumentException, match="solapar"):
        validate_mount_paths(["/mnt/data", "/mnt/data"])


def test_more_than_the_maximum_is_rejected() -> None:
    too_many = [f"/mnt/d{i}" for i in range(MAX_MOUNTS + 1)]
    with pytest.raises(InvalidArgumentException, match=str(MAX_MOUNTS)):
        validate_mount_paths(too_many)


def test_exactly_the_maximum_is_accepted() -> None:
    paths = [f"/mnt/d{i}" for i in range(MAX_MOUNTS)]
    assert validate_mount_paths(paths) == tuple(paths)


def test_no_paths_is_fine() -> None:
    assert validate_mount_paths([]) == ()
