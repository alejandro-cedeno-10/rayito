"""`PoolConfig.warmup`, `allow_internet_access` y `network`
(`ai-agent-fast-start`): orden asentar -> calentar -> aparcar, un paso
fallido como calentamiento fallido, los pasos en segundo plano se sueltan y
el esquema del backend no cambia. Lo que necesita el pool falso vive en
`test_pool_sync.py`/`test_pool_async.py`; aquí, la validación y los pasos
de `agent_pool_warmup`."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rayito import (
    PoolConfig,
    WarmupStep,
    agent_pool_warmup,
)
from rayito._pool_base import POOL_SCHEMA, SlotRecord, launch_kwargs
from rayito.exceptions import InvalidArgumentException


def test_slot_record_schema_is_unchanged() -> None:
    fields = [f.name for f in dataclasses.fields(SlotRecord)]
    assert "warmup" not in fields and "serve_secret" not in fields
    assert POOL_SCHEMA == "rayito.pool/1"


def test_launch_kwargs_only_carry_network_when_set() -> None:
    assert "network" not in launch_kwargs(PoolConfig(size=1))
    assert "allow_internet_access" not in launch_kwargs(PoolConfig(size=1))
    closed = launch_kwargs(PoolConfig(size=1, allow_internet_access=False))
    assert closed["allow_internet_access"] is False
    custom = launch_kwargs(PoolConfig(size=1, network={"deny_out": ["0.0.0.0/0"]}))
    assert custom["network"] == {"deny_out": ["0.0.0.0/0"]}


@pytest.mark.parametrize(
    "warmup",
    ["opencode --version", [object()], [WarmupStep(cmd="  ")], [WarmupStep("x", False, 0)]],
)
def test_invalid_warmup_is_rejected(warmup: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        PoolConfig(size=1, warmup=warmup)


def test_invalid_network_is_rejected_at_config_time() -> None:
    with pytest.raises(InvalidArgumentException):
        PoolConfig(size=1, network={"deny_out": ["no-es-un-cidr"]})


def test_agent_pool_warmup_without_serve() -> None:
    steps = agent_pool_warmup()
    assert [s.background for s in steps] == [False]
    assert steps[0].cmd.startswith("opencode --version")


def test_agent_pool_warmup_with_serve_orders_config_server_and_ready() -> None:
    steps = agent_pool_warmup("opencode", serve=True)
    tags = [s.tag for s in steps]
    assert tags == [
        "rayito-agent-serve-config",
        None,
        "rayito-agent-serve",
        "rayito-agent-serve-ready",
    ]
    serve = steps[2]
    assert serve.background
    assert "/dev/urandom" in serve.cmd and "--hostname 127.0.0.1" in serve.cmd
    ready = steps[3]
    assert "/global/health" in ready.cmd
    assert "/config?directory=%2Fhome%2Fuser%2F.rayito%2Fagent%2Fwarm" in ready.cmd
    assert "OPENCODE_SERVER_PASSWORD=" not in ready.cmd


def test_agent_pool_warmup_rejects_serve_for_other_runtimes() -> None:
    class Other:
        name = "otro"

        def __getattr__(self, attr: str) -> Callable[..., Any]:
            return lambda *a, **k: ()

    with pytest.raises(InvalidArgumentException):
        agent_pool_warmup(Other(), serve=True)


def test_agent_pool_warmup_matches_shared_vectors() -> None:
    vectors = json.loads(
        (Path(__file__).parents[4] / "testdata" / "agent" / "pool-warmup.json").read_text(
            encoding="utf-8"
        )
    )
    for case in vectors["cases"]:
        steps = agent_pool_warmup(serve=case["serve"])
        assert [{"cmd": s.cmd, "background": s.background, "tag": s.tag} for s in steps] == case[
            "steps"
        ]
