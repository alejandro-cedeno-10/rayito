"""El contexto de Docker que se exporta de cada agente de ejemplo
(`testdata/agent-samples/expected/export/`): el `Dockerfile` y el `host.json`
son los mismos en Python y en TypeScript. Los ficheros del host y el README
cambian con cada versión y los cubre `testdata/agent/export/`."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import AgentProject

ROOT: Final = Path(__file__).parents[4] / "testdata" / "agent-samples"
MANIFEST: Final = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
SAMPLES: Final[list[dict[str, Any]]] = MANIFEST["samples"]
EXPECTED: Final = ROOT / "expected" / "export"
REGION: Final = "us-east-1"
COMPARED: Final = ("Dockerfile", "host.json")


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["id"])
def test_the_exported_context_matches_the_shared_files(
    sample: dict[str, Any], tmp_path: Path
) -> None:
    project, _ = AgentProject.detect(ROOT / sample["dir"], region=REGION)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        project.export(target="docker", out_dir=tmp_path / "context")
    for name in COMPARED:
        written = (tmp_path / "context" / name).read_text(encoding="utf-8")
        assert written == (EXPECTED / sample["id"] / name).read_text(encoding="utf-8"), name
