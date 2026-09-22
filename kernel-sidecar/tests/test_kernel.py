"""The real ``ipykernel`` with the pinned requirements (Linux only: ``ipc``
sockets). Every case of design D13 "Sidecar" that needs a kernel."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_server import Harness, MemoryTransport

from rayito_kernel_sidecar.kernels import (
    ContextBase,
    KernelContext,
    KernelPaths,
    install_kernelspecs,
)
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.server import ServerOptions, SidecarServer

pytestmark = [
    pytest.mark.kernel,
    pytest.mark.skipif(sys.platform == "win32", reason="ipc transport needs a Unix host"),
]

SIDECAR_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def harness() -> Any:
    with tempfile.TemporaryDirectory(prefix="rayito-k-") as socket_root:
        paths = KernelPaths(socket_root=Path(socket_root), sidecar_root=SIDECAR_ROOT)
        languages = tuple(install_kernelspecs(paths))
        transport = MemoryTransport()
        server: SidecarServer | None = None
        logger = SidecarLogger()

        def factory(
            context_id: str, language: str, cwd: str, envs: Mapping[str, str]
        ) -> ContextBase:
            assert server is not None
            return KernelContext(
                context_id,
                language,
                cwd,
                envs,
                logger,
                paths,
                on_kernel_died=server.on_kernel_died,
            )

        server = SidecarServer(
            transport,
            factory,
            logger,
            ServerOptions(default_cwd=socket_root, context_ready_timeout=60.0, languages=languages),
        )
        harness = Harness(transport, server)
        harness.task = asyncio.create_task(server.run())
        ready = await transport.next_event(timeout=180.0)
        assert ready["event"] == "ready"
        assert ready["kernel_pid"] > 0
        assert ready["warmup_ms"] >= 0
        assert "python" in ready["languages"]
        harness.ready = ready  # type: ignore[attr-defined]
        harness.paths = paths  # type: ignore[attr-defined]
        yield harness
        await harness.stop()


async def run(harness: Harness, request_id: int, code: str, **fields: Any) -> list[dict[str, Any]]:
    await harness.send(
        request_id,
        "execute",
        context_id=fields.pop("context_id", "default"),
        execution_id=f"exec-{request_id}",
        code=code,
        **fields,
    )
    return await harness.collect(request_id, timeout=60.0)


def main_text(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event["event"] == "result" and event["is_main_result"]:
            return str(event["mime"].get("text/plain"))
    return None


def error_of(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((e for e in events if e["event"] == "error"), None)


async def test_execute_result_stream_and_counts(harness: Harness) -> None:
    first = await run(harness, 1, "x = 42")
    assert [e["event"] for e in first] == ["started", "end"]
    assert first[0]["execution_count"] == 1
    assert first[-1]["execution_count"] == 1
    second = await run(harness, 2, "x")
    assert main_text(second) == "42"
    assert second[-1]["execution_count"] == 2
    third = await run(harness, 3, "print(x)")
    assert [e["event"] for e in third] == ["started", "stdout", "end"]
    assert third[1]["text"] == "42\n"
    assert third[1]["timestamp_unix_ns"] > 0
    assert main_text(third) is None


async def test_error_traceback_is_plain_text(harness: Harness) -> None:
    events = await run(harness, 4, "1/0")
    error = error_of(events)
    assert error is not None
    assert error["name"] == "ZeroDivisionError"
    assert "division by zero" in error["value"]
    assert any("ZeroDivisionError" in line for line in error["traceback"])
    assert not any("\x1b[" in line for line in error["traceback"])
    assert events[-1]["event"] == "end"
    assert events[-1]["execution_count"] >= 1


async def test_startup_scripts_produce_chart_data_and_png(harness: Harness) -> None:
    plot = await run(harness, 5, "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
    results = [e for e in plot if e["event"] == "result"]
    assert results, plot
    mime = results[0]["mime"]
    assert "image/png" in mime
    assert "e2b/chart" in mime
    assert results[0]["is_main_result"] is False
    chart = json.loads(mime["e2b/chart"])
    assert chart["type"] == "line"
    assert len(chart["elements"][0]["points"]) == 3
    frame = await run(harness, 6, "import pandas as pd; pd.DataFrame({'a': [1, 2]})")
    results = [e for e in frame if e["event"] == "result"]
    assert results[0]["is_main_result"] is True
    mime = results[0]["mime"]
    assert {"text/plain", "text/html", "e2b/data"} <= set(mime)
    assert json.loads(mime["e2b/data"]) == {"a": [1, 2]}
    image = await run(harness, 7, "from PIL import Image; Image.new('RGB', (2, 2))")
    results = [e for e in image if e["event"] == "result"]
    assert "image/png" in results[0]["mime"]


async def test_warmup_leaves_the_namespace_clean(harness: Harness) -> None:
    events = await run(harness, 8, "sorted(k for k in dir() if not k.startswith('_'))")
    names = main_text(events) or ""
    for forbidden in ("np", "pd", "plt", "numpy", "pandas", "matplotlib", "scipy", "sklearn"):
        assert f"'{forbidden}'" not in names
    modules = await run(
        harness, 9, "import sys; sorted(m for m in ('numpy', 'pandas') if m in sys.modules)"
    )
    assert main_text(modules) == "['numpy', 'pandas']"


async def test_per_execution_envs_are_restored(harness: Harness) -> None:
    with_env = await run(harness, 10, "import os; os.environ.get('M4_RUN')", envs={"M4_RUN": "yes"})
    assert main_text(with_env) == "'yes'"
    without = await run(harness, 11, "import os; repr(os.environ.get('M4_RUN'))")
    assert main_text(without) == "'None'"
    bare = await run(harness, 12, "import os; os.environ.get('M4_RUN')")
    assert main_text(bare) is None


async def test_interrupt_keeps_state(harness: Harness) -> None:
    await run(harness, 40, "y = 7")
    await harness.send(
        13,
        "execute",
        context_id="default",
        execution_id="exec-13",
        code="import time; time.sleep(10)",
    )
    await harness.transport.events_until(
        lambda e: e.get("id") == 13 and e["event"] == "started", 30.0
    )
    started = asyncio.get_running_loop().time()
    await harness.send(14, "interrupt", context_id="default", execution_id="exec-13")
    events = await harness.collect(13, timeout=10.0)
    assert asyncio.get_running_loop().time() - started < 2.0
    error = error_of(events)
    assert error is not None and error["name"] == "KeyboardInterrupt"
    assert main_text(await run(harness, 15, "y")) == "7"


async def test_restart_rotates_the_key_and_loses_state(harness: Harness) -> None:
    context = harness.server.contexts["default"]
    assert isinstance(context, KernelContext)
    previous_pid = context.kernel_pid
    previous_key = json.loads(context.connection_file.read_text())["key"]
    await run(harness, 16, "z = 1")
    await harness.send(17, "restart_context", context_id="default", envs={"M4_CTX": "ctx"})
    reply = await harness.reply(17, timeout=120.0)
    assert reply["ok"] and reply["payload"]["kernel_pid"] not in (None, previous_pid)
    assert json.loads(context.connection_file.read_text())["key"] != previous_key
    lost = await run(harness, 18, "z")
    assert error_of(lost) is not None and error_of(lost)["name"] == "NameError"
    assert main_text(await run(harness, 19, "import os; os.environ['M4_CTX']")) == "'ctx'"


async def test_reseed_changes_the_sequence_and_resume_reports_alive(harness: Harness) -> None:
    before = main_text(await run(harness, 20, "import random; random.seed(1); random.random()"))
    await harness.send(21, "reseed")
    reply = await harness.reply(21, timeout=30.0)
    assert reply["payload"] == {
        "reseeded": ["default"],
        "deferred": [],
        "failed": [],
        "skipped": [],
    }
    after = main_text(await run(harness, 22, "random.random()"))
    assert before != after
    await harness.send(23, "resume")
    assert (await harness.reply(23, timeout=30.0))["payload"] == {
        "contexts": [{"context_id": "default", "alive": True}]
    }


async def test_reseed_replies_immediately_while_a_cell_runs(harness: Harness) -> None:
    before = main_text(await run(harness, 30, "import random; random.seed(7); random.random()"))
    await harness.send(
        31,
        "execute",
        context_id="default",
        execution_id="exec-31",
        code="import time; time.sleep(3)",
    )
    await harness.transport.events_until(lambda e: e.get("event") == "started", 30.0)
    await asyncio.sleep(1.0)
    started = time.monotonic()
    await harness.send(32, "reseed")
    reply = await harness.reply(32, timeout=5.0)
    assert time.monotonic() - started < 1.0
    assert reply["payload"] == {
        "reseeded": [],
        "deferred": ["default"],
        "failed": [],
        "skipped": [],
    }
    await harness.transport.events_until(
        lambda e: e.get("event") == "end" and e.get("id") == 31, 30.0
    )
    after = main_text(await run(harness, 33, "random.seed(7); random.random()"))
    assert after == before, "seed(7) is deterministic: the kernel survived the cell"
    reseeded = main_text(await run(harness, 34, "random.random()"))
    assert reseeded != after


async def test_kernel_death_is_reported_and_the_context_recovers(harness: Harness) -> None:
    await harness.send(
        24, "execute", context_id="default", execution_id="exec-24", code="import os; os._exit(0)"
    )
    events = await harness.transport.events_until(lambda e: e.get("event") == "kernel_died", 60.0)
    execution = [e for e in events if e.get("id") == 24]
    assert [e["event"] for e in execution] == ["started", "error", "end"]
    assert execution[1]["name"] == "KernelDied"
    assert events[-1]["context_id"] == "default"
    assert events[-1]["execution_id"] == "exec-24"
    assert main_text(await run(harness, 25, "1+1")) == "2"


async def test_second_context_is_isolated(harness: Harness) -> None:
    await run(harness, 26, "w = 1")
    await harness.send(27, "create_context", context_id="ctx-a", cwd="/tmp", envs={"M4_ENV": "1"})
    reply = await harness.reply(27, timeout=180.0)
    assert reply["ok"]
    isolated = await run(harness, 28, "w", context_id="ctx-a")
    assert error_of(isolated) is not None and error_of(isolated)["name"] == "NameError"
    assert (
        main_text(await run(harness, 29, "import os; os.getcwd()", context_id="ctx-a")) == "'/tmp'"
    )
    assert (
        main_text(await run(harness, 30, "import os; os.environ['M4_ENV']", context_id="ctx-a"))
        == "'1'"
    )
    await harness.send(31, "destroy_context", context_id="ctx-a")
    assert (await harness.reply(31, timeout=30.0))["ok"]
    context = harness.server.contexts["default"]
    assert isinstance(context, KernelContext)
    assert not (context.socket_dir.parent / "ctx-a").exists()


BASH_KERNEL_INSTALLED = importlib.util.find_spec("bash_kernel") is not None
BASH_KERNEL_REQUIRED = os.environ.get("RAYITO_REQUIRE_BASH_KERNEL") == "1"

bash_kernel_installed = pytest.mark.skipif(
    not BASH_KERNEL_INSTALLED and not BASH_KERNEL_REQUIRED,
    reason="bash_kernel is not installed in the test venv (requirements-poly.txt)",
)


def fail_when_the_required_bash_kernel_is_missing() -> None:
    """CI sets ``RAYITO_REQUIRE_BASH_KERNEL=1`` so a venv without the poly
    pins fails loudly instead of silently skipping the only real bash case."""
    if BASH_KERNEL_REQUIRED and not BASH_KERNEL_INSTALLED:
        pytest.fail(
            "RAYITO_REQUIRE_BASH_KERNEL=1 but bash_kernel is not importable: "
            "run pytest with --with-requirements requirements-poly.txt"
        )


@bash_kernel_installed
async def test_bash_context_starts_without_the_ipython_config(harness: Harness) -> None:
    fail_when_the_required_bash_kernel_is_missing()
    assert "bash" in harness.ready["languages"]  # type: ignore[attr-defined]
    spec_path = harness.paths.kernelspec_dir_for("rayito-bash") / "kernel.json"  # type: ignore[attr-defined]
    argv = json.loads(spec_path.read_text(encoding="utf-8"))["argv"]
    assert "bash_kernel" in argv
    assert not any(arg.startswith("--config") for arg in argv)
    started = time.monotonic()
    await harness.send(
        50,
        "create_context",
        context_id="default-bash",
        language="bash",
        cwd=harness.paths.socket_root.as_posix(),  # type: ignore[attr-defined]
    )
    reply = await harness.reply(50, timeout=120.0)
    assert reply["ok"], reply
    print(f"\n[m7] bash kernel start: {time.monotonic() - started:.2f} s")
    events = await run(harness, 51, "echo 1", context_id="default-bash")
    stdout = "".join(e["text"] for e in events if e["event"] == "stdout")
    assert "1" in stdout
    assert error_of(events) is None
    assert events[-1]["event"] == "end"
    await harness.send(52, "list_contexts")
    contexts = (await harness.reply(52))["payload"]["contexts"]
    assert ("default-bash", "bash") in [(c["context_id"], c["language"]) for c in contexts]
    await harness.send(53, "reseed")
    assert (await harness.reply(53))["payload"]["skipped"] == ["default-bash"]
    await harness.send(54, "destroy_context", context_id="default-bash")
    assert (await harness.reply(54))["ok"]
