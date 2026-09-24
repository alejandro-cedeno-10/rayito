"""Aceptación de `m9-e2b-v2-surface` contra AWS real: el corpus E2B 2.x
(`openspec/changes/m9-e2b-v2-surface/design.md` D23, tarea 11.2).

Cada programa de `e2b_corpus/{sync,async}/` es código de E2B tal cual lo
escribiría un usuario (cita su página de docs.e2b.dev y su única línea de
Rayito es el import de `rayito.e2b`) y corre como subproceso con
`sys.executable`. El entorno del subproceso lleva `RAYITO_TEMPLATE` (la
imagen `rayito-base` M9), un `RAYITO_ACCESS_TOKEN` recién generado (así
`Sandbox.connect(id)` funciona sin cambios), `AWS_REGION` y `AWS_PROFILE`.
El token nunca se imprime: la salida de cada programa se imprime y se
compara sin él y sin URLs.

Guardrails (`conftest.py`) sin tocar los programas:

- el subproceso arranca con un bootstrap (`python -c`) que acota la vida de
  plataforma por defecto del shim (`max_lifetime`, 3600 s sin el kwarg, que
  es sólo de Rayito) a `TEST_SANDBOX_TIMEOUT_SECONDS` = 900 s y luego
  ejecuta el programa con `runpy` como `__main__`; el plazo lógico sigue
  siendo el de E2B (300 s);
- cada programa tiene un presupuesto de pared (`PROGRAM_BUDGET_SECONDS`);
- al acabar cada programa (bien, mal o por timeout) se terminan los
  MicroVMs de la imagen que no estaban vivos antes de lanzarlo, y el
  sweeper de sesión de `conftest.py` termina lo que quede.

Además, `sbx.fork()` sobre un sandbox vivo lanza `UnimplementedError` antes
de cualquier llamada al agente. Imprime `kernel_ready_s`, los segundos de
cada programa y `corpus_ok=<n>`.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from rayito._aws import LambdaMicrovmsControlPlane
from rayito._payload import generate_access_token
from rayito.e2b import Sandbox, UnimplementedError
from rayito.exceptions import SandboxException

from .conftest import (
    TEMPLATE_VAR,
    TEST_SANDBOX_TIMEOUT_SECONDS,
    BootTimings,
    E2ESettings,
    live_sandbox_ids,
)

pytestmark = pytest.mark.e2e

CORPUS_DIR = Path(__file__).resolve().parent / "e2b_corpus"
FLAVOURS = ("sync", "async")
PROGRAMS = (
    "hello_run_code",
    "commands",
    "files",
    "watch",
    "pty",
    "contexts",
    "info_list",
    "pause_connect",
    "bound_client",
)
CORPUS = tuple(f"{flavour}/{program}" for flavour in FLAVOURS for program in PROGRAMS)
ACCESS_TOKEN_VAR = "RAYITO_ACCESS_TOKEN"
DOCS_PREFIX = "# Sigue https://docs.e2b.dev/"
ALLOWED_IMPORTS = frozenset({"asyncio", "contextlib", "time", "uuid", "rayito.e2b"})
PROGRAM_BUDGET_SECONDS = 600
OUTPUT_TAIL_LINES = 40
URL_PATTERN = re.compile(r"\b(?:https?|grpcs?)://\S+")
BOOTSTRAP = """\
import runpy
import sys

from rayito.e2b import _compat

cap = int(sys.argv[1])
default_max_lifetime = _compat.default_max_lifetime
_compat.default_max_lifetime = lambda timeout: min(default_max_lifetime(timeout), cap)
program = sys.argv[2]
sys.argv = [program]
runpy.run_path(program, run_name="__main__")
"""


@dataclass(frozen=True)
class CorpusEnvironment:
    env: Mapping[str, str]
    token: str


def report(label: str, value: object) -> None:
    print(f"\n[m9-e2b] {label}: {value}", flush=True)


def scrub(text: str, token: str) -> str:
    """La salida de un programa sin el access token ni URLs (endpoints,
    URLs firmadas), apta para imprimirse en el log de CI."""
    return URL_PATTERN.sub("<url>", text.replace(token, "<token>"))


def tail(text: str) -> str:
    return "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])


@pytest.fixture(scope="module")
def corpus_env(e2e_settings: E2ESettings) -> CorpusEnvironment:
    token = generate_access_token()
    env = dict(os.environ)
    env[TEMPLATE_VAR] = e2e_settings.template
    env[ACCESS_TOKEN_VAR] = token
    if e2e_settings.region:
        env["AWS_REGION"] = e2e_settings.region
    env["PYTHONUNBUFFERED"] = "1"
    return CorpusEnvironment(env=env, token=token)


@pytest.fixture(scope="module")
def corpus_seconds() -> dict[str, float]:
    return {}


def sweep_new(
    control_plane: LambdaMicrovmsControlPlane, template_arn: str, before: set[str]
) -> list[str]:
    """Termina (idempotente) los MicroVMs de la imagen que el programa dejó
    sin terminar: los vivos que no estaban antes de lanzarlo."""
    leftovers = [sid for sid in live_sandbox_ids(control_plane, template_arn) if sid not in before]
    for sandbox_id in leftovers:
        with contextlib.suppress(SandboxException):
            control_plane.terminate_microvm(sandbox_id)
    return leftovers


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_corpus_is_plain_e2b_code() -> None:
    """Cada programa cita su página de docs.e2b.dev en la primera línea y
    sólo importa `rayito.e2b` (más la biblioteca estándar que usan los
    ejemplos de E2B)."""
    found = sorted(
        f"{path.parent.name}/{path.stem}" for path in CORPUS_DIR.glob("*/*.py") if path.is_file()
    )
    assert found == sorted(CORPUS)
    for program in CORPUS:
        path = CORPUS_DIR / f"{program}.py"
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith(DOCS_PREFIX), f"{program}: {first_line!r}"
        modules = imported_modules(path)
        assert "rayito.e2b" in modules, program
        assert modules <= ALLOWED_IMPORTS, f"{program}: {sorted(modules - ALLOWED_IMPORTS)}"


def test_fork_is_unimplemented_on_a_live_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    started = time.perf_counter()
    sbx = Sandbox.create(
        template_arn,
        max_lifetime=TEST_SANDBOX_TIMEOUT_SECONDS,
        template_version=e2e_settings.template_version,
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    kernel_ready_s = time.perf_counter() - started
    boot_timings[sbx.sandbox_id] = kernel_ready_s
    report(f"{sbx.sandbox_id}: create -> kernel_ready (kernel_ready_s)", f"{kernel_ready_s:.2f} s")
    try:
        with pytest.raises(UnimplementedError, match="fork"):
            sbx.fork()
        assert sbx.run_code("1 + 1").text == "2"
    finally:
        with contextlib.suppress(SandboxException):
            sbx.kill()


@pytest.mark.parametrize("program", CORPUS)
def test_corpus_program(
    program: str,
    corpus_env: CorpusEnvironment,
    corpus_seconds: dict[str, float],
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    tmp_path: Path,
) -> None:
    path = CORPUS_DIR / f"{program}.py"
    before = set(live_sandbox_ids(control_plane, template_arn))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP, str(TEST_SANDBOX_TIMEOUT_SECONDS), str(path)],
            env=dict(corpus_env.env),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=PROGRAM_BUDGET_SECONDS,
            check=False,
        )
    finally:
        elapsed = time.perf_counter() - started
        leftovers = sweep_new(control_plane, template_arn, before)
    stdout = scrub(completed.stdout, corpus_env.token)
    stderr = scrub(completed.stderr, corpus_env.token)
    report(
        program,
        f"exit {completed.returncode} en {elapsed:.2f} s; sweep tras el programa: "
        f"{len(leftovers)} MicroVM(s)",
    )
    assert corpus_env.token not in completed.stdout + completed.stderr, "el token salió del SDK"
    assert completed.returncode == 0, f"{program} salió con {completed.returncode}:\n{tail(stderr)}"
    assert stdout.strip().endswith(f"{path.stem} ok"), tail(stdout)
    corpus_seconds[program] = elapsed


def test_corpus_summary(corpus_seconds: dict[str, float]) -> None:
    for program in CORPUS:
        seconds = corpus_seconds.get(program)
        report(program, "falló o no corrió" if seconds is None else f"{seconds:.2f} s")
    print(f"\ncorpus_ok={len(corpus_seconds)}/{len(CORPUS)}", flush=True)
    assert sorted(corpus_seconds) == sorted(CORPUS)
