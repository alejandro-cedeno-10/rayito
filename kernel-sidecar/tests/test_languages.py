"""The kernel catalog and the availability rule of ``install_kernelspecs`` on
any host: templates copied into a ``tmp_path`` sidecar root, a fake ``PATH``
and fake absolute paths decide which specs are written."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from rayito_kernel_sidecar.kernels import (
    KERNEL_NAME,
    KernelPaths,
    install_kernelspecs,
    kernel_available,
    rewritten_argv,
)
from rayito_kernel_sidecar.languages import LANGUAGES, PYTHON, KernelLanguage, language_for

SIDECAR_ROOT = Path(__file__).resolve().parents[1]


def test_catalog_names_kernels_and_probe_cells() -> None:
    assert list(LANGUAGES) == ["python", "bash", "javascript"]
    assert LANGUAGES[PYTHON] == KernelLanguage(PYTHON, "rayito", "pass")
    assert LANGUAGES["bash"] == KernelLanguage("bash", "rayito-bash", ":")
    assert LANGUAGES["javascript"] == KernelLanguage("javascript", "rayito-javascript", "void 0")
    assert KERNEL_NAME == "rayito"
    assert language_for("bash") is LANGUAGES["bash"]
    assert language_for("r") is None
    assert language_for("") is None


SHIPPED_TEMPLATES = ("python", "bash")


def test_repository_ships_python_and_bash_templates_only() -> None:
    """``javascript`` keeps its catalog entry but no template: ``ijavascript``
    needs a compiler on the al2023 ARM64 builder (AWS_API_NOTES.md Q57)."""
    for name in SHIPPED_TEMPLATES:
        language = LANGUAGES[name]
        template = SIDECAR_ROOT / "jupyter" / "kernels" / language.kernel_name / "kernel.json"
        spec = json.loads(template.read_text(encoding="utf-8"))
        assert spec["language"] == language.name
        assert spec["interrupt_mode"] == "signal"
        assert "{connection_file}" in spec["argv"]
    assert not (SIDECAR_ROOT / "jupyter" / "kernels" / "rayito-javascript").exists()
    bash = json.loads(
        (SIDECAR_ROOT / "jupyter/kernels/rayito-bash/kernel.json").read_text(encoding="utf-8")
    )
    assert bash["argv"][:3] == ["python3", "-m", "bash_kernel"]
    assert not any(arg.startswith("--config") for arg in bash["argv"])


def sidecar_root_with_templates(destination: Path) -> Path:
    shutil.copytree(SIDECAR_ROOT / "jupyter", destination / "jupyter")
    shutil.copytree(SIDECAR_ROOT / "ipython", destination / "ipython")
    return destination


def written_languages(paths: KernelPaths) -> dict[str, list[str]]:
    written = install_kernelspecs(paths)
    return {
        name: json.loads(target.read_text(encoding="utf-8"))["argv"]
        for name, target in written.items()
    }


def test_python_is_always_written_with_the_rewrites(tmp_path: Path) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    argv = written_languages(paths)[PYTHON]
    assert argv[0] == sys.executable
    assert argv[-1] == f"--config={root}/ipython/ipython_kernel_config.py"
    assert (paths.kernelspec_dir_for("rayito") / "kernel.json").is_file()


def test_bash_needs_its_module_and_a_missing_template_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert list(written_languages(paths)) == [PYTHON]
    assert not paths.kernelspec_dir_for("rayito-bash").exists()
    assert not paths.kernelspec_dir_for("rayito-javascript").exists()


def test_bash_is_written_when_its_module_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: object() if name == "bash_kernel" else None
    )
    written = written_languages(paths)
    assert sorted(written) == ["bash", "python"]
    assert written["bash"][:3] == [sys.executable, "-m", "bash_kernel"]


def test_javascript_needs_node_and_the_kernel_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    node = tmp_path / "node-20"
    node.write_text("", encoding="utf-8")
    kernel_js = tmp_path / "kernel.js"
    kernel_js.write_text("", encoding="utf-8")
    template = root / "jupyter" / "kernels" / "rayito-javascript" / "kernel.json"
    template.parent.mkdir()
    spec = {
        "argv": [str(node), str(kernel_js), "--protocol=5.1", "{connection_file}"],
        "display_name": "rayito-javascript",
        "language": "javascript",
        "interrupt_mode": "signal",
    }
    template.write_text(json.dumps(spec), encoding="utf-8")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert sorted(written_languages(paths)) == ["javascript", "python"]
    kernel_js.unlink()
    shutil.rmtree(paths.kernelspecs_dir)
    assert sorted(written_languages(paths)) == ["python"]


def test_kernel_available_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = tmp_path / "prog"
    program.write_text("", encoding="utf-8")
    existing = tmp_path / "file"
    existing.write_text("", encoding="utf-8")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ok" else None)
    assert kernel_available([str(program), "{connection_file}"])
    assert kernel_available([str(program), f"--config={existing}", str(existing)])
    assert not kernel_available([str(program), str(tmp_path / "missing")])
    assert not kernel_available([str(program), f"--config={tmp_path / 'missing'}"])
    assert not kernel_available([str(tmp_path / "no-such-program")])
    assert not kernel_available(["definitely-not-on-path-rayito", "x"])
    assert kernel_available([sys.executable, "-m", "ok", "-f", "{connection_file}"])
    assert not kernel_available([sys.executable, "-m", "missing_module_rayito"])
    assert not kernel_available([])


def test_rewritten_argv_replaces_prefix_and_python(tmp_path: Path) -> None:
    argv = rewritten_argv(
        ["python3", "-m", "x", "--config=/opt/rayito/sidecar/ipython/c.py"], tmp_path
    )
    assert argv == [sys.executable, "-m", "x", f"--config={tmp_path}/ipython/c.py"]
    assert rewritten_argv(["/usr/bin/node-20", "k.js"], tmp_path) == ["/usr/bin/node-20", "k.js"]
