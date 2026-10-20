"""Los agentes de ejemplo de la aceptación de 0.14 (`testdata/agent-samples/`):
dieciséis proyectos que ya existían, sin código de rayito, cada uno con su
lockfile, y el manifiesto que comparten los tests de Python y de TypeScript.
Todo es puro: sólo se leen ficheros."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

ROOT: Final = Path(__file__).parents[4] / "testdata" / "agent-samples"
MANIFEST: Final = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
SAMPLES: Final[list[dict[str, Any]]] = MANIFEST["samples"]
SAMPLE_COUNT: Final = 16
SKIPPED_DIRS: Final = frozenset({".venv", "node_modules", "__pycache__"})
LOCKFILE_NAMES: Final = frozenset({"uv.lock", "package-lock.json"})
#: Patrones con forma de credencial. Se arman por partes para que este fichero
#: no contenga ningún valor con esa forma.
SECRET_PATTERNS: Final = (
    re.compile("AK" + "IA[0-9A-Z]{16}"),
    re.compile("sk-" + "[A-Za-z0-9_-]{20,}"),
    re.compile("gh" + "p_[A-Za-z0-9]{20,}"),
    re.compile(r"\b[0-9]{12}\b"),
)


def _source_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and not SKIPPED_DIRS.intersection(path.relative_to(directory).parts)
    )


def _text_files(directory: Path) -> list[Path]:
    return [path for path in _source_files(directory) if path.name not in LOCKFILE_NAMES]


def _ids() -> list[str]:
    return [sample["id"] for sample in SAMPLES]


def test_manifest_lists_the_sixteen_samples_once() -> None:
    assert len(SAMPLES) == SAMPLE_COUNT
    assert len(set(_ids())) == SAMPLE_COUNT
    assert MANIFEST["schema"] == "rayito.agent-samples/1"


def test_every_directory_is_in_the_manifest() -> None:
    nested = {
        str(path.relative_to(ROOT))
        for language in ("python", "typescript")
        for path in (ROOT / language).iterdir()
        if path.is_dir() and path.name not in SKIPPED_DIRS
    }
    assert nested | {"opencode"} == {sample["dir"] for sample in SAMPLES}


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_sample_has_its_own_lockfile_or_says_why_not(sample: dict[str, Any]) -> None:
    directory = ROOT / sample["dir"]
    assert directory.is_dir()
    if sample["lockfile"] is None:
        assert sample["lockfileReason"]
        assert not any(path.name in LOCKFILE_NAMES for path in _source_files(directory))
    else:
        assert (directory / sample["lockfile"]).is_file()


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_sample_never_mentions_rayito(sample: dict[str, Any]) -> None:
    for path in _text_files(ROOT / sample["dir"]):
        assert "rayito" not in path.read_text(encoding="utf-8").lower(), path.name


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_sample_holds_no_secret_like_value_and_no_env_file(sample: dict[str, Any]) -> None:
    directory = ROOT / sample["dir"]
    assert not any(path.name == ".env" for path in _source_files(directory))
    for path in _text_files(directory):
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            assert pattern.search(text) is None, path.name


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_sample_declares_each_tool_in_its_source(sample: dict[str, Any]) -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _text_files(ROOT / sample["dir"])
        if path.suffix in {".py", ".js"}
    )
    for tool_name in sample["tools"]:
        assert re.search(rf"\b{re.escape(tool_name)}\b|name: \"{tool_name}\"", source), tool_name


@pytest.mark.parametrize("sample", SAMPLES, ids=_ids())
def test_sample_reads_its_model_from_the_environment(sample: dict[str, Any]) -> None:
    sources = [
        path.read_text(encoding="utf-8")
        for path in _text_files(ROOT / sample["dir"])
        if path.suffix in {".py", ".js", ".yaml", ".json"} and path.name != "manifest.json"
    ]
    reads_env = any("MODEL_ID" in text for text in sources)
    configured = any(("model" in text and "claude" in text) for text in sources)
    assert reads_env or configured


def test_python_samples_pin_their_project_name_in_the_lock() -> None:
    for sample in SAMPLES:
        if sample["lockfile"] != "uv.lock":
            continue
        directory = ROOT / sample["dir"]
        pyproject = (directory / "pyproject.toml").read_text(encoding="utf-8")
        name = re.search(r'^name = "([^"]+)"', pyproject, re.MULTILINE)
        assert name is not None
        assert f'name = "{name.group(1)}"' in (directory / "uv.lock").read_text(encoding="utf-8")


def test_node_samples_declare_the_node_floor_and_esm() -> None:
    for sample in SAMPLES:
        if sample["lockfile"] != "package-lock.json":
            continue
        package = json.loads((ROOT / sample["dir"] / "package.json").read_text(encoding="utf-8"))
        assert package["type"] == "module"
        assert package["engines"]["node"] == ">=22"


def test_custom_options_of_the_second_pass_are_the_documented_ones() -> None:
    assert MANIFEST["custom"] == {
        "memoryMib": 4096,
        "maxSessions": 2,
        "minVms": 1,
        "maxVms": 2,
        "warmVms": 1,
    }
