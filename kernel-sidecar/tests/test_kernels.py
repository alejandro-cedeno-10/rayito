"""The environment ``KernelContext`` launches the kernel with, on any host:
a fake ``AsyncKernelManager`` records the ``env`` the kernel process would
get and a fake client answers the probe cell, so the ``create_context`` and
``restart_context`` ops run the sidecar's real path (``m9-egress-policy``
design D7/D18)."""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_server import Harness, MemoryTransport

from rayito_kernel_sidecar.kernels import ContextBase, KernelContext, KernelPaths
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.server import ServerOptions, SidecarServer

SIDECAR_ROOT = Path(__file__).resolve().parents[1]
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
NO_PROXY_VALUE = "localhost,127.0.0.1,::1"


def egress_proxy_envs(port: int) -> dict[str, str]:
    """The eight keys of ``rayd-core``'s ``egress_proxy_env``
    (``network/proxy_env.rs``): the four variables in upper and lower case,
    ``ALL_PROXY`` with ``socks5h``."""
    http = f"http://127.0.0.1:{port}"
    values = {
        "HTTP_PROXY": http,
        "HTTPS_PROXY": http,
        "ALL_PROXY": f"socks5h://127.0.0.1:{port}",
        "NO_PROXY": NO_PROXY_VALUE,
    }
    return {name: value for key, value in values.items() for name in (key, key.lower())}


class FakeChannel:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def get_msg(self) -> dict[str, Any]:
        return await self.messages.get()


class FakeKernelClient:
    """Answers each ``execute`` with its ``execute_reply`` and an ``idle``
    ``status``, all that ``run_silent`` waits for."""

    def __init__(self) -> None:
        self.iopub_channel = FakeChannel()
        self.shell_channel = FakeChannel()
        self._ids = itertools.count(1)

    def start_channels(self, **channels: bool) -> None:
        pass

    def stop_channels(self) -> None:
        pass

    async def wait_for_ready(self, timeout: float) -> None:
        pass

    def execute(self, code: str, **options: Any) -> str:
        msg_id = f"msg-{next(self._ids)}"
        parent = {"msg_id": msg_id}
        self.shell_channel.messages.put_nowait(
            {"parent_header": parent, "msg_type": "execute_reply", "content": {"status": "ok"}}
        )
        self.iopub_channel.messages.put_nowait(
            {"parent_header": parent, "msg_type": "status", "content": {"execution_state": "idle"}}
        )
        return msg_id


class FakeProvisioner:
    pids = itertools.count(4000)

    def __init__(self) -> None:
        self.pid = next(FakeProvisioner.pids)
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        return 0


class FakeKernelManager:
    """Records, per context, the ``env`` that ``start_kernel`` would pass to
    ``Popen``: the kernel process environment."""

    launches: list[tuple[str, dict[str, str]]] = []

    def __init__(self, *, connection_file: str, **options: Any) -> None:
        self._context_id = Path(connection_file).parent.name
        self.provisioner: FakeProvisioner | None = None

    async def start_kernel(self, *, env: Mapping[str, str], **options: Any) -> None:
        FakeKernelManager.launches.append((self._context_id, dict(env)))
        self.provisioner = FakeProvisioner()

    def client(self) -> FakeKernelClient:
        return FakeKernelClient()

    async def shutdown_kernel(self, now: bool = False) -> None:
        pass


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    for key in PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setattr("jupyter_client.manager.AsyncKernelManager", FakeKernelManager)
    monkeypatch.setattr(FakeKernelManager, "launches", [])
    paths = KernelPaths(socket_root=tmp_path / "k", sidecar_root=SIDECAR_ROOT)
    transport = MemoryTransport()
    logger = SidecarLogger()
    server: SidecarServer | None = None

    def factory(context_id: str, language: str, cwd: str, envs: Mapping[str, str]) -> ContextBase:
        assert server is not None
        return KernelContext(
            context_id, language, cwd, envs, logger, paths, on_kernel_died=server.on_kernel_died
        )

    server = SidecarServer(
        transport,
        factory,
        logger,
        ServerOptions(default_cwd=str(tmp_path), context_ready_timeout=5.0, languages=()),
    )
    harness = Harness(transport, server)
    harness.task = asyncio.create_task(server.run())
    assert (await transport.next_event())["event"] == "ready"
    yield harness
    await harness.stop()


async def test_op_envs_proxy_variables_reach_the_kernel_environment(harness: Harness) -> None:
    """``rayd`` puts the local proxy variables in the ``envs`` of
    ``create_context`` and ``restart_context`` and the sidecar never has them
    in its own environment: the eight keys reach the kernel environment
    unchanged in both ops, with no sidecar change (design D7)."""
    assert [context for context, _ in FakeKernelManager.launches] == ["default"]
    assert not set(egress_proxy_envs(1)) & set(FakeKernelManager.launches[0][1])
    created = egress_proxy_envs(40123)
    await harness.send(1, "create_context", context_id="ctx-egress", envs=created)
    assert (await harness.reply(1))["ok"]
    restarted = egress_proxy_envs(40999)
    await harness.send(2, "restart_context", context_id="ctx-egress", envs=restarted)
    assert (await harness.reply(2))["ok"]
    await harness.send(3, "restart_context", context_id="default", envs=restarted)
    assert (await harness.reply(3))["ok"]
    launches = FakeKernelManager.launches[1:]
    assert [context for context, _ in launches] == ["ctx-egress", "ctx-egress", "default"]
    for (_, env), expected in zip(launches, (created, restarted, restarted), strict=True):
        assert len(expected) == 8
        assert {key: env.get(key) for key in expected} == expected
        assert env["OMP_NUM_THREADS"] == "1"
        assert env["PATH"] == os.environ["PATH"]
