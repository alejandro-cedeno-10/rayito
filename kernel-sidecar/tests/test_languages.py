"""The kernel catalog, the transport each kernel binds, which contexts clean
the ``text/plain`` of their results and the availability rule of
``install_kernelspecs`` on any host: templates copied into a ``tmp_path``
sidecar root, a fake ``PATH`` and fake absolute paths decide which specs are
written."""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rayito_kernel_sidecar.executions import Emit, ExecutionOutcome, KernelIo
from rayito_kernel_sidecar.kernels import (
    KERNEL_NAME,
    LOOPBACK_IP,
    KernelContext,
    KernelEndpoint,
    KernelPaths,
    install_kernelspecs,
    kernel_available,
    kernel_endpoint,
    rewritten_argv,
)
from rayito_kernel_sidecar.languages import LANGUAGES, PYTHON, KernelLanguage, language_for
from rayito_kernel_sidecar.logging import SidecarLogger

SIDECAR_ROOT = Path(__file__).resolve().parents[1]
DENO_LANGUAGES = ("javascript", "typescript")
DENO_BINARY = "/opt/rayito/deno/deno"
DENO_ARGV = [DENO_BINARY, "jupyter", "--kernel", "--conn", "{connection_file}"]
DENO_ENV = {"NO_COLOR": "1", "DENO_DIR": "${HOME}/.cache/deno", "DENO_NO_UPDATE_CHECK": "1"}


def test_catalog_names_kernels_and_probe_cells() -> None:
    assert list(LANGUAGES) == ["python", "bash", "javascript", "typescript"]
    assert LANGUAGES[PYTHON] == KernelLanguage(PYTHON, "rayito", "pass")
    assert LANGUAGES["bash"] == KernelLanguage("bash", "rayito-bash", ":")
    assert LANGUAGES["javascript"] == KernelLanguage(
        "javascript", "rayito-javascript", "void 0", transport="tcp", strips_plain_text_ansi=True
    )
    assert LANGUAGES["typescript"] == KernelLanguage(
        "typescript", "rayito-typescript", "void 0", transport="tcp", strips_plain_text_ansi=True
    )
    assert LANGUAGES[PYTHON].transport == "ipc"
    assert LANGUAGES["bash"].transport == "ipc"
    assert not LANGUAGES[PYTHON].strips_plain_text_ansi
    assert not LANGUAGES["bash"].strips_plain_text_ansi
    assert KERNEL_NAME == "rayito"
    assert language_for("bash") is LANGUAGES["bash"]
    assert language_for("typescript") is LANGUAGES["typescript"]
    assert language_for("ts") is None
    assert language_for("r") is None
    assert language_for("") is None


def shipped_template(name: str) -> dict[str, Any]:
    template = SIDECAR_ROOT / "jupyter" / "kernels" / LANGUAGES[name].kernel_name / "kernel.json"
    spec: dict[str, Any] = json.loads(template.read_text(encoding="utf-8"))
    return spec


def test_repository_ships_the_deno_templates() -> None:
    """Deno 2.9.7 replaces ``ijavascript``, which needs a compiler on the al2023
    ARM64 builder (AWS_API_NOTES.md Q57, Q61); it handles ``interrupt_request``
    and installs no ``SIGINT`` handler, hence ``message``."""
    for name in DENO_LANGUAGES:
        spec = shipped_template(name)
        assert spec["argv"] == DENO_ARGV
        assert spec["language"] == name
        assert spec["display_name"] == LANGUAGES[name].kernel_name
        assert spec["interrupt_mode"] == "message"
        assert spec["env"] == DENO_ENV
    for name in (PYTHON, "bash"):
        spec = shipped_template(name)
        assert spec["language"] == name
        assert spec["interrupt_mode"] == "signal"
        assert "{connection_file}" in spec["argv"]
        assert "env" not in spec
    bash = shipped_template("bash")
    assert bash["argv"][:3] == ["python3", "-m", "bash_kernel"]
    assert not any(arg.startswith("--config") for arg in bash["argv"])


def sidecar_root_with_templates(destination: Path) -> Path:
    shutil.copytree(SIDECAR_ROOT / "jupyter", destination / "jupyter")
    shutil.copytree(SIDECAR_ROOT / "ipython", destination / "ipython")
    return destination


def sidecar_root_with_deno(destination: Path, deno: Path) -> Path:
    """A copy of the shipped templates whose Deno specs start ``deno``
    instead of the image path ``/opt/rayito/deno/deno``."""
    root = sidecar_root_with_templates(destination)
    for name in DENO_LANGUAGES:
        template = root / "jupyter" / "kernels" / LANGUAGES[name].kernel_name / "kernel.json"
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["argv"][0] = str(deno)
        template.write_text(json.dumps(spec), encoding="utf-8")
    return root


def written_languages(paths: KernelPaths) -> dict[str, list[str]]:
    written = install_kernelspecs(paths)
    return {
        name: json.loads(target.read_text(encoding="utf-8"))["argv"]
        for name, target in written.items()
    }


def written_spec(paths: KernelPaths, name: str) -> dict[str, Any]:
    target = paths.kernelspec_dir_for(LANGUAGES[name].kernel_name) / "kernel.json"
    spec: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    return spec


def test_python_is_always_written_with_the_rewrites(tmp_path: Path) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    argv = written_languages(paths)[PYTHON]
    assert argv[0] == sys.executable
    assert argv[-1] == f"--config={root}/ipython/ipython_kernel_config.py"
    assert (paths.kernelspec_dir_for("rayito") / "kernel.json").is_file()


def test_bash_needs_its_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = sidecar_root_with_templates(tmp_path / "root")
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert list(written_languages(paths)) == [PYTHON]
    assert not paths.kernelspec_dir_for("rayito-bash").exists()
    assert not paths.kernelspec_dir_for("rayito-javascript").exists()
    assert not paths.kernelspec_dir_for("rayito-typescript").exists()


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


def test_deno_specs_need_the_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    real = KernelPaths(
        socket_root=tmp_path / "k-real",
        sidecar_root=sidecar_root_with_templates(tmp_path / "real"),
    )
    if not Path(DENO_BINARY).exists():
        assert sorted(written_languages(real)) == ["python"]
    deno = tmp_path / "deno"
    root = sidecar_root_with_deno(tmp_path / "root", deno)
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=root)
    assert sorted(written_languages(paths)) == ["python"]
    deno.write_text("", encoding="utf-8")
    written = written_languages(paths)
    assert sorted(written) == ["javascript", "python", "typescript"]
    for name in DENO_LANGUAGES:
        assert written[name] == [str(deno), *DENO_ARGV[1:]]
        spec = written_spec(paths, name)
        assert spec["interrupt_mode"] == "message"
        assert spec["env"] == DENO_ENV
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name: object() if name == "bash_kernel" else None
    )
    assert sorted(written_languages(paths)) == ["bash", "javascript", "python", "typescript"]
    shutil.rmtree(paths.kernelspecs_dir)
    shutil.rmtree(root / "jupyter" / "kernels" / "rayito-typescript")
    assert sorted(written_languages(paths)) == ["bash", "javascript", "python"]
    deno.unlink()
    shutil.rmtree(paths.kernelspecs_dir)
    assert sorted(written_languages(paths)) == ["bash", "python"]


def test_kernel_endpoint(tmp_path: Path) -> None:
    for name in (PYTHON, "bash"):
        assert kernel_endpoint(LANGUAGES[name], tmp_path) == KernelEndpoint(
            "ipc", str(tmp_path / "k")
        )
    for name in DENO_LANGUAGES:
        assert kernel_endpoint(LANGUAGES[name], tmp_path) == KernelEndpoint("tcp", "127.0.0.1")
    assert LOOPBACK_IP == "127.0.0.1"


class UnstartedKernelContext(KernelContext):
    """A ``KernelContext`` whose kernel never starts, so ``run`` reaches
    ``run_execution`` on any host."""

    async def _start_kernel(self) -> None:
        return None


async def test_only_deno_contexts_strip_plain_text_ansi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, bool] = {}

    async def record(
        kernel: KernelIo,
        request_id: int,
        execution_id: str,
        code: str,
        envs: Mapping[str, str],
        emit: Emit,
        *,
        strip_plain_text_ansi: bool = False,
    ) -> ExecutionOutcome:
        assert isinstance(kernel, KernelContext)
        received[kernel.language] = strip_plain_text_ansi
        return ExecutionOutcome(ended_by="kernel")

    async def discard(event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr("rayito_kernel_sidecar.kernels.run_execution", record)
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=tmp_path / "root")
    logger = SidecarLogger(io.StringIO())
    for name in LANGUAGES:
        context = UnstartedKernelContext(f"ctx-{name}", name, str(tmp_path), {}, logger, paths)
        await context.start()
        await context.run(1, "exec-1", "x", {}, discard)
    assert received == {"python": False, "bash": False, "javascript": True, "typescript": True}


def test_kernel_available_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = tmp_path / "prog"
    program.write_text("", encoding="utf-8")
    existing = tmp_path / "file"
    existing.write_text("", encoding="utf-8")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ok" else None)
    assert kernel_available([str(program), "{connection_file}"])
    assert kernel_available([str(program), f"--config={existing}", str(existing)])
    assert kernel_available([str(program), "jupyter", "--kernel", "--conn", "{connection_file}"])
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
    assert rewritten_argv(DENO_ARGV, tmp_path) == DENO_ARGV
