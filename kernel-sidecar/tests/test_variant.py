"""The ``warmup_variant`` build flag. The real-kernel case (Linux only:
``ipc`` sockets) copies the sidecar root to a temporary directory with the
marker set to ``slim`` and expects a kernel with no scientific module loaded
that still produces ``e2b/chart`` on the first displayed figure."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_kernel import main_text, run
from test_server import Harness, MemoryTransport

from rayito_kernel_sidecar.kernels import (
    ContextBase,
    KernelContext,
    KernelPaths,
    install_kernelspecs,
)
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.server import ServerOptions, SidecarServer

SIDECAR_ROOT = Path(__file__).resolve().parents[1]
MARKER = Path("ipython") / "startup" / "warmup_variant"
COPIED_TREES = ("ipython", "jupyter", "src")
SCIENTIFIC_MODULES = ("numpy", "pandas", "matplotlib")

real_kernel = pytest.mark.skipif(sys.platform == "win32", reason="ipc transport needs a Unix host")


def slim_sidecar_root(destination: Path) -> Path:
    """A copy of the checkout's sidecar root plus the ``slim`` marker."""
    for tree in COPIED_TREES:
        shutil.copytree(
            SIDECAR_ROOT / tree,
            destination / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    (destination / MARKER).write_text("slim\n", encoding="utf-8")
    return destination


async def start_harness(sidecar_root: Path, socket_root: Path) -> Harness:
    paths = KernelPaths(socket_root=socket_root, sidecar_root=sidecar_root)
    languages = tuple(install_kernelspecs(paths))
    transport = MemoryTransport()
    server: SidecarServer | None = None
    logger = SidecarLogger()

    def factory(context_id: str, language: str, cwd: str, envs: Mapping[str, str]) -> ContextBase:
        assert server is not None
        return KernelContext(
            context_id, language, cwd, envs, logger, paths, on_kernel_died=server.on_kernel_died
        )

    server = SidecarServer(
        transport,
        factory,
        logger,
        ServerOptions(
            default_cwd=str(socket_root), context_ready_timeout=60.0, languages=languages
        ),
    )
    harness = Harness(transport, server)
    harness.task = asyncio.create_task(server.run())
    ready = await transport.next_event(timeout=180.0)
    assert ready["event"] == "ready"
    assert ready["warmup_ms"] >= 0
    return harness


@pytest.fixture
async def slim_harness() -> AsyncIterator[Any]:
    with (
        tempfile.TemporaryDirectory(prefix="rayito-slim-root-") as root,
        tempfile.TemporaryDirectory(prefix="rayito-slim-k-") as socket_root,
    ):
        harness = await start_harness(slim_sidecar_root(Path(root)), Path(socket_root))
        try:
            yield harness
        finally:
            await harness.stop()


@pytest.mark.kernel
@real_kernel
async def test_slim_variant_starts_without_the_stack(slim_harness: Harness) -> None:
    loaded = await run(
        slim_harness,
        1,
        f"import sys; sorted(m for m in {SCIENTIFIC_MODULES!r} if m in sys.modules)",
    )
    assert main_text(loaded) == "[]"
    plot = await run(
        slim_harness, 2, "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()"
    )
    results = [e for e in plot if e["event"] == "result"]
    assert results, plot
    mime = results[0]["mime"]
    assert "image/png" in mime
    assert "e2b/chart" in mime
    chart = json.loads(mime["e2b/chart"])
    assert chart["type"] == "line"
    assert len(chart["elements"][0]["points"]) == 3


def test_repository_has_no_marker() -> None:
    assert not (SIDECAR_ROOT / MARKER).exists()
