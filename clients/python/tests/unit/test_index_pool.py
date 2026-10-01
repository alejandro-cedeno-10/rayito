"""`PoolConfig(index=...)` (M14): el pool escribe la fila del índice de cada
plaza al rellenar, con la `metadata` del pool, así que las plazas aparcadas
(`SUSPENDED`) aparecen en `Sandbox.list(metadata=, index=)` sin despertarlas;
una escritura fallida descarta la plaza como un calentamiento fallido."""

# Los fixtures del pool se importan de test_pool_sync: sus nombres vuelven
# como parámetros de los tests (así los resuelve pytest).
# ruff: noqa: F811

from __future__ import annotations

import pytest

from rayito import PoolConfig, Sandbox
from rayito._pool_base import launch_kwargs
from rayito.exceptions import InvalidArgumentException

from .fake_control_plane import FakeControlPlane
from .fake_dynamodb import fake_index
from .test_pool_sync import (  # noqa: F401
    POOL_TIMEOUT,
    PoolFactory,
    TickingNow,
    clock,
    make_pool,
    now,
    plane,
    sleeps,
    transport,
    wait_idle,
    wait_until,
)


def test_pool_config_rejects_anything_but_a_dynamodb_index() -> None:
    with pytest.raises(InvalidArgumentException, match="DynamoDbIndex"):
        PoolConfig(size=1, index="rayito-sandboxes")  # type: ignore[arg-type]


def test_launch_kwargs_only_carry_the_index_when_configured() -> None:
    index, _, _ = fake_index()
    assert "index" not in launch_kwargs(PoolConfig(size=1))
    assert launch_kwargs(PoolConfig(size=1, index=index))["index"] is index


def test_refill_writes_one_row_per_slot_and_parked_slots_are_listable(
    make_pool: PoolFactory, plane: FakeControlPlane, now: TickingNow
) -> None:
    index, api, _ = fake_index(now=now.current.timestamp() + 60)
    pool = make_pool(size=2, metadata={"pool": "a"}, index=index).start()
    wait_idle(pool, 2)
    puts = api.calls("put_item")
    assert len(puts) == 2
    assert all(put["Item"]["metadata"] == {"M": {"pool": {"S": "a"}}} for put in puts)
    parked = sorted(put["Item"]["pk"]["S"] for put in puts)
    since = len(plane.calls)

    found = Sandbox.list(
        metadata={"pool": "a"}, states=["SUSPENDED"], index=index, control_plane=plane
    )

    assert sorted(item.sandbox_id for item in found) == parked
    probes = [c.operation for c in plane.calls[since:] if c.operation != "ListMicrovms"]
    assert probes == []


def test_a_failed_row_write_discards_the_slot(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    index, api, _ = fake_index()
    api.put_error = "AccessDeniedException"
    pool = make_pool(size=1, index=index).start()
    wait_until(lambda: pool.stats().failed >= 1, "el calentamiento no falló")
    assert plane.calls_to("TerminateMicrovm")
    assert pool.stats().ready == 0


def test_timeout_bounds_the_row_ttl(make_pool: PoolFactory, now: TickingNow) -> None:
    index, api, _ = fake_index(now=now.current.timestamp() + 60)
    pool = make_pool(size=1, index=index).start()
    wait_idle(pool, 1)
    (put,) = api.calls("put_item")
    started = int(put["Item"]["started_at_ms"]["N"]) // 1000
    assert int(put["Item"]["expires_at"]["N"]) == started + POOL_TIMEOUT + 3600
