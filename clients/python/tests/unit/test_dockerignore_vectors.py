"""Vectores compartidos con el SDK TypeScript
(`testdata/templates/dockerignore-vectors.json`): la semántica de
`.dockerignore` de Docker (`**/` también en la raíz, `*` sin cruzar `/`,
anclado en la raíz, padres que excluyen, la última coincidencia gana). Si
este test y `dockerignore-vectors.test.ts` pasan, los dos SDKs empaquetan
los mismos ficheros."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from rayito._templates._dockerignore import DockerIgnore, clean_pattern

VECTORS = json.loads(
    (Path(__file__).parents[4] / "testdata" / "templates" / "dockerignore-vectors.json").read_text(
        encoding="utf-8"
    )
)
#: Lo bastante largo para que un emparejado exponencial se note.
ADVERSARIAL_SEGMENTS = 2_000
#: `NAME_MAX` de Linux/macOS: ningún segmento de ruta real es más largo.
NAME_MAX = 255
#: Un emparejado acotado tarda milisegundos; el margen absorbe un CI lento.
LINEAR_BUDGET_SECONDS = 2.0


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_dockerignore_vectors(case: dict[str, Any]) -> None:
    ignore = DockerIgnore.from_text(case["dockerignore"])
    wrongly_kept = [path for path in case["excluded"] if not ignore.matches(path)]
    wrongly_excluded = [path for path in case["included"] if ignore.matches(path)]
    assert wrongly_kept == []
    assert wrongly_excluded == []


def test_clean_pattern_matches_filepath_clean() -> None:
    assert clean_pattern("./a//b/../c/") == "a/c"
    assert clean_pattern("/") == ""
    assert clean_pattern("../x") == "../x"


def test_a_hostile_pattern_cannot_trigger_exponential_matching() -> None:
    """`**/**/…/x`, `**/a/**/a/…/x` y `*a*a…b` contra rutas que no casan:
    el emparejado por segmentos es polinómico, nunca un retroceso
    exponencial."""
    globstars = "/".join(["**"] * ADVERSARIAL_SEGMENTS) + "/x"
    interleaved = "**/a/" * (ADVERSARIAL_SEGMENTS // 2) + "x"
    path = "/".join(["a"] * ADVERSARIAL_SEGMENTS) + "/y"
    stars = "*a" * ADVERSARIAL_SEGMENTS
    ignore = DockerIgnore.from_text(f"{globstars}\n{interleaved}\n{stars}b\n")
    started = time.perf_counter()
    assert ignore.matches(path) is False
    assert ignore.matches("a" * NAME_MAX) is False
    assert time.perf_counter() - started < LINEAR_BUDGET_SECONDS
