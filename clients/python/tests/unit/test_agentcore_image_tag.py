"""La etiqueta de imagen por contenido (`testdata/agentcore-deploy/image-tags.json`)
y el zip determinista del contexto de build."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from typing import Any

import pytest

from rayito._agentcore._domain import image_tag_for, source_zip
from tests.unit.fake_agentcore import DATA

VECTORS: dict[str, Any] = json.loads((DATA / "image-tags.json").read_text("utf-8"))


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_image_tag_vectors(case: dict[str, Any]) -> None:
    tree = {path: text.encode("utf-8") for path, text in case["files"].items()}
    assert image_tag_for(tree) == case["expected"]


def test_the_source_zip_is_deterministic_and_complete() -> None:
    tree = {"b.txt": b"2", "a/x.py": b"print()\n", "Dockerfile": b"FROM scratch\n"}
    first = source_zip(tree)
    assert first == source_zip(dict(reversed(list(tree.items()))))
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == sorted(tree)
        assert {name: archive.read(name) for name in archive.namelist()} == tree
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert {info.external_attr >> 16 for info in archive.infolist()} == {0o644}
