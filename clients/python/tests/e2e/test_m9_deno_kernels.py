"""M9 `m9-deno-kernels` contra AWS real (design D12): la variante
`rayito-base-poly` (`RAYITO_TEMPLATE_POLY`, fixture `poly_sandbox` de
`conftest.py`) sirve `javascript` y `typescript` con el kernel Jupyter de
Deno, arrancado en la primera celda de cada lenguaje y nunca antes de
`/ready`: resultados sin escapes ANSI, streams, `Deno.jupyter.html`, errores,
`await` de nivel superior, `envs` de contexto, timeout, reinicio y un estado
JS que sobrevive a `pause()`/`resume()`. El shim E2B acepta `js`/`ts` en la
variante; `rayito-base` (`RAYITO_TEMPLATE`) responde `UNIMPLEMENTED` nombrando
`rayito-base-poly` y la capa de Deno queda inerte. `test_deno_snapshot_sizes`
compara los `snapshotBuild` de las dos publicaciones con la pareja Q57 sin
crear ningún MicroVM (`RAYITO_POLY_SIZES`, `RAYITO_BASE_SIZES`,
`RAYITO_BASE_PREVIOUS_SIZES`, `memoria,code,disco` en bytes, y
`RAYITO_RAYD_BYTES_DELTA`). Las bandas son las medidas de Q77 con margen:
`codeInstallSizeInBytes` no es la suma de bytes de los ficheros (el binario
de Deno de ≈ 85 MB cuesta 132-137 MB de code install), la memoria por
pares suma el ruido de cuatro builds (18-32 MB medidos) y el code install de
`rayito-base` frente a una versión anterior a M9 incluye git-core (Q76,
≈ 38-41 MB netos de `rayd`).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import TypeVar

import grpc
import pytest

import rayito.e2b
from rayito import Execution, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import CommandExitException, InvalidArgumentException

from .test_m7_poly_kernels import BASE_SIZES_VAR, POLY_SIZES_VAR, RAYD_DELTA_VAR, parse_sizes

pytestmark = pytest.mark.e2e

POLY_IMAGE = "rayito-base-poly"
DENO_BINARY = "/opt/rayito/deno/deno"
DENO_LAYER = "/opt/rayito/deno"
ANSI_ESCAPE = "\x1b"
DENO_LANGUAGES = ("javascript", "typescript")
BASE_PREVIOUS_SIZES_VAR = "RAYITO_BASE_PREVIOUS_SIZES"
POLY_3_0 = (921_780_224, 1_323_397_120, 37_466_112)
BASE_18_0 = (938_098_688, 1_320_202_240, 36_401_152)
DENO_CODE_MIN_BYTES = 110_000_000
DENO_CODE_MAX_BYTES = 160_000_000
DENO_MEMORY_BAND_BYTES = 40_000_000
MEMORY_BAND_BYTES = 20_000_000
BASE_CODE_BAND_BYTES = 50_000_000
ENDLESS_LOOP_TIMEOUT_SECONDS = 2
PROBE_TIMEOUT_SECONDS = 30

T = TypeVar("T")


def report(label: str, value: object) -> None:
    print(f"\n[m9] {label}: {value}", flush=True)


def timed(label: str, action: Callable[[], T]) -> T:
    started = time.perf_counter()
    outcome = action()
    report(label, f"{time.perf_counter() - started:.2f}")
    return outcome


def assert_plain_text(execution: Execution, expected: str) -> None:
    assert execution.error is None, execution.error
    assert execution.text == expected
    assert ANSI_ESCAPE not in (execution.text or "")


def deno_rss_kib(sandbox: Sandbox) -> list[str]:
    return sandbox.commands.run("ps -o rss= -C deno", timeout=PROBE_TIMEOUT_SECONDS).stdout.split()


def test_typescript_cell(poly_sandbox: Sandbox) -> None:
    poly_sandbox.commands.run(f"test -x {DENO_BINARY}", timeout=PROBE_TIMEOUT_SECONDS)
    first = timed(
        "typescript first cell incl. lazy Deno start (s)",
        lambda: poly_sandbox.run_code("const x: number = 40 + 2; x", language="typescript"),
    )
    assert_plain_text(first, "42")
    second = timed(
        "typescript second cell (s)",
        lambda: poly_sandbox.run_code("x + 1", language="typescript"),
    )
    assert_plain_text(second, "43")
    listed = {item.id: item for item in poly_sandbox.list_code_contexts()}
    assert next(iter(listed)) == "default"
    assert listed["default-typescript"].language == "typescript"
    report("deno kernel RSS (KiB per process)", deno_rss_kib(poly_sandbox))


def test_javascript_cell(poly_sandbox: Sandbox) -> None:
    first = timed(
        "javascript first cell incl. lazy Deno start (s)",
        lambda: poly_sandbox.run_code("let y = 40 + 2; y", language="javascript"),
    )
    assert_plain_text(first, "42")
    second = timed(
        "javascript second cell, alias js (s)",
        lambda: poly_sandbox.run_code("y", language="js"),
    )
    assert_plain_text(second, "42")
    listed = {item.id: item.language for item in poly_sandbox.list_code_contexts()}
    assert listed["default-javascript"] == "javascript"


def test_deno_outputs(poly_sandbox: Sandbox) -> None:
    logged = poly_sandbox.run_code("console.log('out')", language="typescript")
    assert "out" in "".join(logged.logs.stdout)
    errored = poly_sandbox.run_code("console.error('err')", language="typescript")
    assert "err" in "".join(errored.logs.stderr)
    html = poly_sandbox.run_code("Deno.jupyter.html`<b>hi</b>`", language="typescript")
    assert html.error is None, html.error
    assert html.results[0].html == "<b>hi</b>"
    thrown = poly_sandbox.run_code("throw new Error('boom')", language="typescript")
    assert thrown.error is not None
    assert (thrown.error.name, thrown.error.value) == ("Error", "boom")
    awaited = poly_sandbox.run_code(
        "await new Promise((r) => setTimeout(() => r(7), 200))", language="typescript"
    )
    assert_plain_text(awaited, "7")


def timeout_path(sandbox: Sandbox, context_id: str) -> str:
    """`z` sigue vivo si el `interrupt_request` paró el bucle; un
    `ReferenceError` delata el reinicio de respaldo de rayd a los 5 s."""
    survived = sandbox.run_code("z", context=context_id)
    if survived.text == "5":
        return "message interrupt (state kept)"
    name = survived.error.name if survived.error is not None else survived.text
    return f"rayd restart fallback (state lost: {name})"


def test_typescript_context_envs_timeout_restart(poly_sandbox: Sandbox) -> None:
    context = poly_sandbox.create_code_context(language="typescript", envs={"K": "1"})
    assert context.language == "typescript"
    assert_plain_text(poly_sandbox.run_code("Deno.env.get('K') === '1'", context=context), "true")
    assert poly_sandbox.run_code("let z = 5", context=context).error is None
    timed_out = timed(
        f"typescript endless loop with timeout={ENDLESS_LOOP_TIMEOUT_SECONDS} returned after (s)",
        lambda: poly_sandbox.run_code(
            "while (true) {}", context=context, timeout=ENDLESS_LOOP_TIMEOUT_SECONDS
        ),
    )
    assert timed_out.error is not None
    assert timed_out.error.name == "ExecutionTimeout"
    assert_plain_text(poly_sandbox.run_code("1 + 1", context=context), "2")
    report("typescript timeout path", timeout_path(poly_sandbox, context.id))
    assert poly_sandbox.run_code("let w = 9", context=context).error is None
    poly_sandbox.restart_code_context(context)
    cleared = poly_sandbox.run_code("w", context=context)
    assert cleared.error is not None
    assert cleared.error.name == "ReferenceError"
    assert context.id in [item.id for item in poly_sandbox.list_code_contexts()]
    poly_sandbox.remove_code_context(context)


def test_javascript_survives_pause_resume(poly_sandbox: Sandbox) -> None:
    assert poly_sandbox.run_code("globalThis.kept = 42", language="javascript").error is None
    generation = poly_sandbox.get_health().resume_generation
    assert timed("pause() -> SUSPENDED (s)", poly_sandbox.pause) is True
    timed("resume() -> kernel_ready (s)", poly_sandbox.resume)
    assert_plain_text(poly_sandbox.run_code("kept", language="javascript"), "42")
    health = poly_sandbox.get_health()
    report("kernel_state_lost after resume", health.kernel_state_lost)
    assert health.kernel_state_lost is False
    assert health.resume_generation > generation


async def async_shim_typescript_cell(
    sandbox_id: str, access_token: str, control_plane: LambdaMicrovmsControlPlane
) -> str | None:
    shim = await rayito.e2b.AsyncSandbox.connect(
        sandbox_id, access_token=access_token, control_plane=control_plane
    )
    try:
        return (await shim.run_code("const m: number = 4; m", language="ts")).text
    finally:
        await shim.native.close()


def test_e2b_shim_js_ts_on_poly(
    poly_sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane
) -> None:
    shim = rayito.e2b.Sandbox.connect(
        poly_sandbox.sandbox_id,
        access_token=poly_sandbox.access_token,
        control_plane=control_plane,
    )
    try:
        assert shim.run_code("1 + 1", language="js").text == "2"
        assert shim.run_code("const n: number = 3; n", language="ts").text == "3"
    finally:
        shim.native.close()
    typed = asyncio.run(
        async_shim_typescript_cell(
            poly_sandbox.sandbox_id, poly_sandbox.access_token, control_plane
        )
    )
    assert typed == "4"


def assert_unimplemented_naming_poly(error: InvalidArgumentException) -> None:
    assert error.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    assert POLY_IMAGE in str(error)


def test_js_ts_unimplemented_on_base(
    sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane
) -> None:
    for language in DENO_LANGUAGES:
        with pytest.raises(InvalidArgumentException) as executed:
            sandbox.run_code("1", language=language)
        assert_unimplemented_naming_poly(executed.value)
        with pytest.raises(InvalidArgumentException) as created:
            sandbox.create_code_context(language=language)
        assert_unimplemented_naming_poly(created.value)
    report("typescript on rayito-base", str(executed.value))
    assert [item.id for item in sandbox.list_code_contexts()] == ["default"]
    with pytest.raises(CommandExitException) as probe:
        sandbox.commands.run(f"test -e {DENO_LAYER}", timeout=PROBE_TIMEOUT_SECONDS)
    report("rayito-base: `test -e /opt/rayito/deno` (the Deno layer stayed inert)", probe.value)
    shim = rayito.e2b.Sandbox.connect(
        sandbox.sandbox_id, access_token=sandbox.access_token, control_plane=control_plane
    )
    try:
        with pytest.raises(rayito.e2b.UnimplementedError, match=POLY_IMAGE):
            shim.run_code("1", language="ts")
    finally:
        shim.native.close()


def required_sizes(variable: str) -> tuple[int, int, int]:
    sizes = parse_sizes(variable)
    if sizes is None:
        pytest.skip(
            f"exporta {POLY_SIZES_VAR}, {BASE_SIZES_VAR}, {BASE_PREVIOUS_SIZES_VAR} "
            f"(memoria,code,disco en bytes) y {RAYD_DELTA_VAR}"
        )
    return sizes


def pairwise_delta(new: tuple[int, int, int], old: tuple[int, int, int], field: int) -> int:
    """La diferencia poly - base de un campo del `snapshotBuild`, menos la
    misma diferencia en la pareja Q57: el binario `rayd`, común a las dos
    imágenes, se cancela y queda el coste de la capa de Deno."""
    return (new[field] - old[field]) - (POLY_3_0[field] - BASE_18_0[field])


def test_deno_snapshot_sizes() -> None:
    poly = required_sizes(POLY_SIZES_VAR)
    base = required_sizes(BASE_SIZES_VAR)
    previous = required_sizes(BASE_PREVIOUS_SIZES_VAR)
    raw_delta = os.environ.get(RAYD_DELTA_VAR)
    if not raw_delta:
        pytest.skip(f"exporta {RAYD_DELTA_VAR} (bytes del rayd nuevo menos el anterior)")
    rayd_delta = int(raw_delta.replace("_", ""))
    deno_memory = pairwise_delta(poly, base, 0)
    deno_code = pairwise_delta(poly, base, 1)
    base_memory_delta = base[0] - previous[0]
    base_code_delta = base[1] - previous[1] - rayd_delta
    report("rayito-base-poly memory/code/disk", poly)
    report("rayito-base memory/code/disk", base)
    report("previous rayito-base memory/code/disk", previous)
    report("deno_code (bytes)", deno_code)
    report("deno_memory (bytes)", deno_memory)
    report("rayito-base memory delta vs previous (bytes)", base_memory_delta)
    report("rayito-base code delta vs previous net of rayd (bytes)", base_code_delta)
    assert DENO_CODE_MIN_BYTES <= deno_code <= DENO_CODE_MAX_BYTES
    assert abs(deno_memory) <= DENO_MEMORY_BAND_BYTES
    assert abs(base_memory_delta) <= MEMORY_BAND_BYTES
    assert abs(base_code_delta) <= BASE_CODE_BAND_BYTES
