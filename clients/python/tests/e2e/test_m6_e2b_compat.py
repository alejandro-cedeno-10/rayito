"""M6 track C "E2B compat + metadatos": el corpus del cookbook de E2B a
través de `rayito.e2b` contra un MicroVM real, los metadatos de `Health`
(instancia, variante de clase y `list(metadata=)`), un ciclo
`beta_pause()` + `connect()` con el kernel y los metadatos intactos, y la
medición Q44 (un MicroVM sin conector de egress, ¿sale a internet?).

Sigue `openspec/changes/m6-e2b-compat/design.md` D13 en el mismo orden; cada
bloque es una función para que un fallo diga qué contrato se rompió. Dos
sandboxes (900 s y 300 s) más el que termina la compuerta de egress (tope
900 s), un ciclo suspend/resume, ≈ $0.03. Los tiempos se
imprimen con su nombre de `AWS_API_NOTES.md` §16 (`kernel_ready_s`,
`get_info_metadata_s`, `list_metadata_s`, `list_metadata_n`).

Ajustes de M9 (`m9-e2b-v2-surface`, tarea 11.6), sólo donde la semántica
2.x cambió: `timeout` es el plazo lógico de `rayd` bajo `max_lifetime`
(840 s bajo un tope de 900 s, el guardrail de `conftest.py`), así que
`end_at` es ese plazo con una tolerancia de arranque; `pause()` devuelve un
bool; `get_metrics()` es una serie; `set_timeout` está mapeado (se acorta
a 300 s, dentro del tope); `upload_url` firma en S3 (lo cubre
`test_m9_transfer.py`), así que el miembro sin equivalente que se comprueba
es `create_snapshot`; y un kernel `js` que la imagen base no trae es
`UnimplementedError` nombrando `rayito-base-poly`. En `test_no_egress_connector`
(`m9-egress-policy` D1 y plan de migración) `allow_internet_access=False` ya
no se rechaza antes de AWS: en `rayito-base`, sin aplicación de egress en el
guest, la compuerta del SDK termina el MicroVM y lanza `UnimplementedError`
(falla cerrado; nunca queda un sandbox con la red abierta en silencio).
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable

import pytest

import rayito
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.e2b import (
    CommandExitException,
    NotFoundException,
    PtySize,
    Sandbox,
    SandboxInfo,
    SandboxQuery,
    SandboxState,
    UnimplementedError,
    WriteEntry,
)
from rayito.exceptions import InvalidArgumentException, SandboxException, TimeoutException

from .conftest import BootTimings, E2ESettings
from .test_m5_pty_suspend_resume import output_line, read_until

COOKBOOK_MAX_LIFETIME_SECONDS = 900
COOKBOOK_SANDBOX_TIMEOUT_SECONDS = 840
END_AT_TOLERANCE_SECONDS = 60
SET_TIMEOUT_SECONDS = 300
EGRESS_SANDBOX_TIMEOUT_SECONDS = 300
EGRESS_PROBE_TIMEOUT_SECONDS = 15
PTY_ECHO_BUDGET_SECONDS = 10.0
PAUSE_BUDGET_SECONDS = 30.0
TERMINATE_BUDGET_SECONDS = 60.0
POLL_SECONDS = 0.5
PNG_SIGNATURE_BASE64_PREFIX = "iVBORw0KGgo"
EGRESS_PROBE = (
    "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\""
)


def report(label: str, seconds: float) -> None:
    print(f"\n[m6-e2b] {label}: {seconds:.2f} s", flush=True)


def timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    report(label, elapsed)
    return elapsed


def wait_until(predicate: Callable[[], bool], budget: float, what: str) -> float:
    started = time.perf_counter()
    while not predicate():
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{what} no ocurrió en {budget:g} s"
        time.sleep(POLL_SECONDS)
    return time.perf_counter() - started


def running_count(control_plane: LambdaMicrovmsControlPlane, template_arn: str) -> int:
    return sum(1 for _ in control_plane.list_microvms(image_arn=template_arn, states=("RUNNING",)))


@pytest.mark.e2e
def test_e2b_shim_cookbook(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    run_id = uuid.uuid4().hex
    metadata = {"track": "m6", "run": run_id}
    started = time.perf_counter()
    sbx = Sandbox(
        template_arn,
        timeout=COOKBOOK_SANDBOX_TIMEOUT_SECONDS,
        max_lifetime=COOKBOOK_MAX_LIFETIME_SECONDS,
        metadata=metadata,
        envs={"E2B_TEST": "1"},
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    kernel_ready_s = time.perf_counter() - started
    boot_timings[sbx.sandbox_id] = kernel_ready_s
    report(f"{sbx.sandbox_id}: run-microvm -> kernel_ready (kernel_ready_s)", kernel_ready_s)
    try:
        step_2_run_code(sbx)
        step_3_commands(sbx)
        step_4_files(sbx)
        step_5_pty(sbx)
        step_6_instance_get_info(sbx, metadata, template_arn)
        step_7_class_get_info(sbx, metadata, control_plane)
        step_8_list_by_metadata(sbx, run_id, control_plane, template_arn)
        step_9_native_list(sbx, run_id, control_plane, template_arn)
        step_10_metrics_and_unimplemented(sbx)
        again = step_11_pause_then_connect(sbx, metadata, control_plane)
    except BaseException:
        with contextlib.suppress(Exception):
            sbx.kill()
        raise
    sbx.native.close()
    step_12_kill_then_not_found(again, control_plane)


def step_2_run_code(sbx: Sandbox) -> None:
    assert sbx.run_code("x = 40; x + 2").text == "42"
    assert "hi\n" in sbx.run_code("print('hi')").logs.stdout
    failed = sbx.run_code("1/0")
    assert failed.error is not None and failed.error.name == "ZeroDivisionError"
    plot = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
    assert plot.results and plot.results[0].png
    assert plot.results[0].png.startswith(PNG_SIGNATURE_BASE64_PREFIX)


def step_3_commands(sbx: Sandbox) -> None:
    assert sbx.commands.run("echo $E2B_TEST").stdout == "1\n"
    with pytest.raises(CommandExitException) as excinfo:
        sbx.commands.run("exit 3")
    assert excinfo.value.exit_code == 3


def step_4_files(sbx: Sandbox) -> None:
    info = sbx.files.write("/home/user/m6.txt", "hola")
    assert info.path == "/home/user/m6.txt"
    assert sbx.files.read("/home/user/m6.txt") == "hola"
    written = sbx.files.write(
        [WriteEntry("/home/user/m6-a.txt", "a"), WriteEntry("/home/user/m6-b.txt", b"b")]
    )
    assert [entry.name for entry in written] == ["m6-a.txt", "m6-b.txt"]
    assert sbx.files.read("/home/user/m6-b.txt", format="bytes") == b"b"


def step_5_pty(sbx: Sandbox) -> None:
    chunks: list[bytes] = []
    terminal = sbx.pty.create(PtySize(rows=24, cols=80), on_data=chunks.append, timeout=None)
    sbx.pty.send_stdin(terminal.pid, b"echo hola\n")
    started = time.perf_counter()
    read_until(terminal, output_line("hola"), PTY_ECHO_BUDGET_SECONDS)
    report("echo hola por la PTY del shim (pty_echo_s)", time.perf_counter() - started)
    assert any(b"hola" in chunk for chunk in chunks)
    assert sbx.pty.kill(terminal.pid) is True


def step_6_instance_get_info(sbx: Sandbox, metadata: dict[str, str], template_arn: str) -> None:
    info = sbx.get_info()
    assert isinstance(info, SandboxInfo)
    assert info.metadata == metadata
    assert info.state is SandboxState.RUNNING
    assert info.template_id == template_arn
    assert info.name == template_arn.rsplit(":", 1)[-1]
    assert info.end_at is not None
    deadline_s = (info.end_at - info.started_at).total_seconds()
    assert abs(deadline_s - COOKBOOK_SANDBOX_TIMEOUT_SECONDS) <= END_AT_TOLERANCE_SECONDS, (
        deadline_s
    )


def step_7_class_get_info(
    sbx: Sandbox, metadata: dict[str, str], control_plane: LambdaMicrovmsControlPlane
) -> None:
    started = time.perf_counter()
    info = Sandbox.get_info(sbx.sandbox_id, control_plane=control_plane)
    report(
        "Sandbox.get_info(id): get-microvm + JWE + Health (get_info_metadata_s)",
        time.perf_counter() - started,
    )
    assert info.metadata == metadata
    assert info.state is SandboxState.RUNNING


def step_8_list_by_metadata(
    sbx: Sandbox, run_id: str, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    probed = running_count(control_plane, template_arn)
    started = time.perf_counter()
    paginator = Sandbox.list(
        query=SandboxQuery(metadata={"run": run_id}),
        template=template_arn,
        control_plane=control_plane,
    )
    items = paginator.next_items()
    elapsed = time.perf_counter() - started
    print(
        f"\n[m6-e2b] Sandbox.list(query=metadata) sobre {probed} sandboxes RUNNING de la imagen "
        f"(list_metadata_n={probed}): {elapsed:.2f} s (list_metadata_s)",
        flush=True,
    )
    assert [item.sandbox_id for item in items] == [sbx.sandbox_id]
    assert items[0].metadata == {"track": "m6", "run": run_id}
    assert paginator.has_next is False
    none = Sandbox.list(
        query=SandboxQuery(metadata={"run": "other"}),
        template=template_arn,
        control_plane=control_plane,
    )
    assert none.next_items() == []


def step_9_native_list(
    sbx: Sandbox, run_id: str, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    listed = list(
        rayito.Sandbox.list(
            metadata={"run": run_id}, template=template_arn, control_plane=control_plane
        )
    )
    assert [item.sandbox_id for item in listed] == [sbx.sandbox_id]
    assert listed[0].metadata == {"track": "m6", "run": run_id}
    with pytest.raises(InvalidArgumentException):
        rayito.Sandbox.list(
            metadata={"run": run_id}, states=["SUSPENDED"], control_plane=control_plane
        )


def step_10_metrics_and_unimplemented(sbx: Sandbox) -> None:
    metrics = sbx.get_metrics()
    assert len(metrics) >= 1 and metrics[-1].mem_total > 0 and metrics[-1].cpu_count >= 1
    sbx.set_timeout(SET_TIMEOUT_SECONDS)
    with pytest.raises(UnimplementedError):
        sbx.create_snapshot()
    with pytest.raises(UnimplementedError):
        sbx.run_code("1", language="r")
    with pytest.raises(UnimplementedError, match="rayito-base-poly"):
        sbx.run_code("1", language="js")


def step_11_pause_then_connect(
    sbx: Sandbox, metadata: dict[str, str], control_plane: LambdaMicrovmsControlPlane
) -> Sandbox:
    paused: list[bool] = []
    pause_s = timed("beta_pause() -> SUSPENDED (pause_s)", lambda: paused.append(sbx.beta_pause()))
    assert paused == [True]
    assert pause_s < PAUSE_BUDGET_SECONDS
    assert sbx.native.info.state == "SUSPENDED"
    started = time.perf_counter()
    again = Sandbox.connect(
        sbx.sandbox_id, access_token=sbx.native.access_token, control_plane=control_plane
    )
    report(
        "Sandbox.connect(id) sobre un sandbox pausado -> kernel_ready (resume_s)",
        time.perf_counter() - started,
    )
    assert again.run_code("x").text == "40"
    assert again.get_info().metadata == metadata
    assert again.native.get_health().resume_generation == 1
    assert again.native.get_health().metadata == metadata
    return again


def step_12_kill_then_not_found(again: Sandbox, control_plane: LambdaMicrovmsControlPlane) -> None:
    sandbox_id = again.sandbox_id
    assert again.kill() is True

    def gone() -> bool:
        try:
            Sandbox.get_info(sandbox_id, control_plane=control_plane)
        except NotFoundException:
            return True
        except SandboxException:
            return False
        return False

    seconds = wait_until(gone, TERMINATE_BUDGET_SECONDS, "get_info -> NotFoundException")
    report("kill() -> Sandbox.get_info(id) NotFoundException", seconds)


@pytest.mark.e2e
def test_no_egress_connector(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    """Q44 (medido 2026-09-16): un MicroVM lanzado sin `egressNetworkConnectors`
    hereda el conector de egress de la versión de imagen (`INTERNET_EGRESS`
    en `rayito-base`) y **sigue saliendo a internet**. Desde `m9-egress-policy`
    (D1, plan de migración) el shim mapea `allow_internet_access=False` a
    `deny_out=[ALL_TRAFFIC]`: en `rayito-base-caps` se aplica en el guest y en
    `rayito-base` (la plantilla de este e2e, sin `CAP_NET_ADMIN`) la compuerta
    del SDK termina el MicroVM y lanza `UnimplementedError`. Aquí se comprueba
    ese fallo cerrado (error y ningún MicroVM nuevo vivo) con el tope de 900 s,
    y la medición Q44 se repite con el SDK nativo, para que un cambio de
    comportamiento de AWS se note en la primera pasada."""
    step_internet_off_fails_closed(e2e_settings, control_plane, template_arn)

    started = time.perf_counter()
    with rayito.Sandbox.create(
        template_arn,
        timeout=EGRESS_SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        egress=None,
        ingress=["ALL_INGRESS"],
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
    ) as native:
        boot_timings[native.sandbox_id] = time.perf_counter() - started
        report(
            f"{native.sandbox_id}: sin egressNetworkConnectors, run-microvm -> kernel_ready",
            boot_timings[native.sandbox_id],
        )
        probe_started = time.perf_counter()
        outcome = egress_probe_outcome(native)
        print(
            f"\n[m6-e2b] Q44 sin conector de egress en run-microvm: {outcome} "
            f"({time.perf_counter() - probe_started:.2f} s)",
            flush=True,
        )
    assert outcome.startswith("internet reachable"), (
        f"Q44 cambió ({outcome}): sin conector de egress ya no hay salida a internet; "
        "revisa AWS_API_NOTES.md Q44 y vuelve a mapear allow_internet_access=False"
    )


def live_ids(control_plane: LambdaMicrovmsControlPlane, template_arn: str) -> set[str]:
    """Los MicroVMs de la imagen que no están `TERMINATING`/`TERMINATED`."""
    return {item.sandbox_id for item in control_plane.list_microvms(image_arn=template_arn)}


def step_internet_off_fails_closed(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> None:
    """`allow_internet_access=False` en una imagen sin aplicación de egress:
    `UnimplementedError` y el MicroVM lanzado queda terminado."""
    before = live_ids(control_plane, template_arn)
    started = time.perf_counter()
    try:
        leaked = Sandbox.create(
            template_arn,
            timeout=EGRESS_SANDBOX_TIMEOUT_SECONDS,
            allow_internet_access=False,
            max_lifetime=COOKBOOK_MAX_LIFETIME_SECONDS,
            execution_role_arn=e2e_settings.execution_role_arn,
            logging=e2e_settings.logging,
            control_plane=control_plane,
        )
    except UnimplementedError as error:
        gate = error
    else:
        with contextlib.suppress(Exception):
            leaked.kill()
        pytest.fail(
            f"allow_internet_access=False en {e2e_settings.template} devolvió un sandbox: "
            "la imagen aplica egress en el guest o la compuerta de m9-egress-policy no saltó"
        )
    report(
        "allow_internet_access=False -> UnimplementedError (compuerta)",
        time.perf_counter() - started,
    )
    assert gate.feature == "allow_internet_access=False", gate
    assert "rayito-base-caps" in gate.reason, gate
    assert "el sandbox se ha terminado" in gate.reason, gate

    def no_new_live() -> bool:
        return not (live_ids(control_plane, template_arn) - before)

    seconds = wait_until(
        no_new_live, TERMINATE_BUDGET_SECONDS, "terminar el MicroVM de la compuerta"
    )
    report("compuerta -> ningún MicroVM nuevo vivo", seconds)


def egress_probe_outcome(sbx: rayito.Sandbox) -> str:
    try:
        result = sbx.commands.run(EGRESS_PROBE, timeout=EGRESS_PROBE_TIMEOUT_SECONDS)
    except CommandExitException as error:
        return f"blocked: exit {error.exit_code} ({error.stderr.strip().splitlines()[-1:]})"
    except TimeoutException:
        return f"blocked: timeout tras {EGRESS_PROBE_TIMEOUT_SECONDS} s"
    return f"internet reachable (exit {result.exit_code})"
