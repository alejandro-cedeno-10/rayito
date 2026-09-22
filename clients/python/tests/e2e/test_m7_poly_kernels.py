"""M7 `m7-poly-kernels` contra AWS real (design D8): un sandbox de la variante
`rayito-base-poly` (`RAYITO_TEMPLATE_POLY`, ARN o nombre) corre celdas bash
con arranque perezoso del kernel, `javascript` es un lenguaje conocido que
ninguna imagen trae (`ijavascript` necesita compilador en al2023 ARM64,
AWS_API_NOTES.md Q57), el timeout sigue la regla genérica, Python no cambia
en la variante y `rayito-base` responde `UNIMPLEMENTED` nombrando
`rayito-base-poly`. `test_snapshot_sizes` compara los `snapshotBuild` de las
dos publicaciones (`RAYITO_BASE_SIZES`/`RAYITO_POLY_SIZES`, `memoria,code,
disco` en bytes) con la banda de D5 sin crear ningún MicroVM.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator

import grpc
import pytest

from rayito import Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import (
    CommandExitException,
    InvalidArgumentException,
    SandboxNotFoundException,
)

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

POLY_TEMPLATE_VAR = "RAYITO_TEMPLATE_POLY"
BASE_SIZES_VAR = "RAYITO_BASE_SIZES"
POLY_SIZES_VAR = "RAYITO_POLY_SIZES"
RAYD_DELTA_VAR = "RAYITO_RAYD_BYTES_DELTA"
POLY_IMAGE = "rayito-base-poly"
BASELINE_17_0 = (928_100_352, 1_305_825_280, 37_998_592)
MEMORY_BAND_BYTES = 20 * 1_000_000
CODE_BAND_BYTES = 10 * 1_000_000
BASH_TIMEOUT_SECONDS = 2


def report(label: str, value: object) -> None:
    print(f"\n[m7] {label}: {value}", flush=True)


def poly_template() -> str | None:
    return os.environ.get(POLY_TEMPLATE_VAR) or None


@pytest.fixture
def poly_sandbox(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> Iterator[Sandbox]:
    template = poly_template()
    if not template:
        pytest.skip(f"exporta {POLY_TEMPLATE_VAR}=<arn|nombre> para probar los kernels poly")
    started = time.perf_counter()
    created = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=900,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    report("poly image kernel_ready_s", f"{time.perf_counter() - started:.2f}")
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def test_bash_cell(poly_sandbox: Sandbox) -> None:
    started = time.perf_counter()
    execution = poly_sandbox.run_code("echo hi", language="bash")
    report("first bash cell incl. lazy kernel start (s)", f"{time.perf_counter() - started:.2f}")
    assert "hi" in "".join(execution.logs.stdout)
    assert execution.error is None
    assert execution.execution_count is not None
    started = time.perf_counter()
    again = poly_sandbox.run_code("echo again", language="Bash")
    report("second bash cell (s)", f"{time.perf_counter() - started:.2f}")
    assert "again" in "".join(again.logs.stdout)
    listed = {item.id: item for item in poly_sandbox.list_code_contexts()}
    assert "default-bash" in listed
    assert listed["default-bash"].language == "bash"
    assert next(iter(listed)) == "default"
    context = poly_sandbox.create_code_context(language="bash", envs={"M7_CTX": "1"})
    assert context.language == "bash"
    in_context = poly_sandbox.run_code("echo $M7_CTX", context=context)
    assert "1" in "".join(in_context.logs.stdout)
    with pytest.raises(InvalidArgumentException):
        poly_sandbox.run_code("echo $A", language="bash", envs={"A": "1"})
    with pytest.raises(InvalidArgumentException):
        poly_sandbox.run_code("echo x", language="bash", context=context)
    failed = poly_sandbox.run_code("false", language="bash")
    report("bash non-zero exit error", failed.error)
    poly_sandbox.remove_code_context(context)


def test_javascript_is_known_but_not_shipped(poly_sandbox: Sandbox) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        poly_sandbox.run_code("1 + 1", language="javascript")
    assert excinfo.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert POLY_IMAGE in str(excinfo.value)
    report("javascript on the poly image", str(excinfo.value))
    assert "default-javascript" not in [item.id for item in poly_sandbox.list_code_contexts()]


def test_bash_timeout(poly_sandbox: Sandbox) -> None:
    started = time.perf_counter()
    timed_out = poly_sandbox.run_code("sleep 30", language="bash", timeout=BASH_TIMEOUT_SECONDS)
    elapsed = time.perf_counter() - started
    report("bash timeout=2 returned after (s)", f"{elapsed:.2f}")
    assert timed_out.error is not None
    assert timed_out.error.name == "ExecutionTimeout"
    back = poly_sandbox.run_code("echo back", language="bash")
    assert "back" in "".join(back.logs.stdout)
    report("bash context after the timeout", "alive or restarted, `echo back` printed back")


def test_python_unchanged_on_poly(poly_sandbox: Sandbox) -> None:
    assert poly_sandbox.get_health().kernel_ready
    poly_sandbox.run_code("x = 42")
    assert poly_sandbox.run_code("x").text == "42"
    assert poly_sandbox.run_code("x", language="python").text == "42"
    plot = poly_sandbox.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
    assert plot.results[0].chart is not None


def test_language_unimplemented_on_base(sandbox: Sandbox) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        sandbox.run_code("echo hi", language="bash")
    assert excinfo.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert POLY_IMAGE in str(excinfo.value)
    assert [item.id for item in sandbox.list_code_contexts()] == ["default"]
    with pytest.raises(InvalidArgumentException) as created:
        sandbox.create_code_context(language="bash")
    assert created.value.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert sandbox.run_code("1 + 1").text == "2"
    with pytest.raises(CommandExitException) as probe:
        sandbox.commands.run("python3 -c 'import bash_kernel'", timeout=30)
    report("rayito-base: `import bash_kernel` (the poly layer stayed inert)", probe.value)


def parse_sizes(variable: str) -> tuple[int, int, int] | None:
    raw = os.environ.get(variable)
    if not raw:
        return None
    parts = [int(part.strip().replace("_", "")) for part in raw.split(",")]
    if len(parts) != 3:
        pytest.fail(f"{variable} debe ser `memoria,code,disco` en bytes, recibido {raw!r}")
    return (parts[0], parts[1], parts[2])


def test_snapshot_sizes() -> None:
    """La banda de D5 sobre la memoria y sobre el `codeInstallSizeInBytes`
    neto del cambio de tamaño del binario `rayd` (`RAYITO_RAYD_BYTES_DELTA`,
    bytes del `rayd` nuevo menos el de 17.0): el binario lo mueven otros
    cambios del hito, no la capa condicional."""
    base = parse_sizes(BASE_SIZES_VAR)
    poly = parse_sizes(POLY_SIZES_VAR)
    if base is None or poly is None:
        pytest.skip(f"exporta {BASE_SIZES_VAR} y {POLY_SIZES_VAR} (memoria,code,disco en bytes)")
    rayd_delta = int(os.environ.get(RAYD_DELTA_VAR, "0").replace("_", ""))
    memory_delta = base[0] - BASELINE_17_0[0]
    code_delta = base[1] - BASELINE_17_0[1] - rayd_delta
    report("rayito-base memory/code/disk", base)
    report("rayito-base-poly memory/code/disk", poly)
    report("rayito-base memory delta vs 17.0 (bytes)", memory_delta)
    report("rayd binary delta vs 17.0 (bytes, subtracted)", rayd_delta)
    report("rayito-base code install delta vs 17.0 net of rayd (bytes)", code_delta)
    report("poly minus base code install (bytes)", poly[1] - base[1])
    assert abs(memory_delta) <= MEMORY_BAND_BYTES
    assert abs(code_delta) <= CODE_BAND_BYTES
    assert poly[1] >= base[1], "the poly layer installs bash_kernel on top of the base pins"
