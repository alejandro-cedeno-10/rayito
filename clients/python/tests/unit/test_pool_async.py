"""`AsyncSandboxPool`: los casos de `test_pool_sync.py` uno a uno sobre el
plano de control falso y un `rayd` falso por plaza (`grpc.aio`)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest

from rayito import (
    AsyncSandbox,
    AsyncSandboxPool,
    IdlePolicy,
    JsonFilePoolBackend,
    PoolClosedException,
    PoolConfig,
    S3Staging,
)
from rayito._payload import access_token_sha256
from rayito._s3 import S3Gateway
from rayito._transport import ACCESS_TOKEN_KEY
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    QuotaExceededException,
)

from .conftest import DEADLINE_KEY, IMAGE_ARN, JWE, FakeClock
from .fake_control_plane import FakeControlPlane, PoolTransport, max_calls_in_window
from .fake_process import presented_token_sha256
from .test_pool_sync import (
    POLL_SECONDS,
    POOL_MIN_REMAINING,
    POOL_TIMEOUT,
    TOKEN_LENGTH,
    WAIT_BUDGET_SECONDS,
    BrokenWarmingSaveBackend,
    SpyBackend,
    TickingNow,
    ops_for,
)


@pytest.fixture
def now() -> TickingNow:
    return TickingNow()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def plane(clock: FakeClock, now: TickingNow) -> Iterator[FakeControlPlane]:
    fake = FakeControlPlane(clock=clock, now=now)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def transport() -> PoolTransport:
    return PoolTransport.for_pool()


@pytest.fixture
def sleeps() -> list[float]:
    return []


PoolFactory = Callable[..., AsyncSandboxPool]


@pytest.fixture
async def make_pool(
    plane: FakeControlPlane,
    transport: PoolTransport,
    clock: FakeClock,
    now: TickingNow,
    sleeps: list[float],
) -> Any:
    pools: list[AsyncSandboxPool] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def factory(size: int = 2, **overrides: Any) -> AsyncSandboxPool:
        backend = overrides.pop("backend", None)
        session = overrides.pop("session", None)
        config = PoolConfig(
            size=size,
            template=IMAGE_ARN,
            timeout=POOL_TIMEOUT,
            min_remaining_seconds=POOL_MIN_REMAINING,
            ready_timeout=10.0,
            **overrides,
        )
        pool = AsyncSandboxPool(
            config,
            backend=backend,
            session=session,
            control_plane=plane,
            transport=transport,
            monotonic=clock,
            now=now,
            sleep=fake_sleep,
            random=lambda: 0.5,
        )
        pools.append(pool)
        return pool

    try:
        yield factory
    finally:
        plane.release_all()
        for pool in pools:
            await pool.close()


async def wait_until(
    predicate: Callable[[], bool], what: str, budget: float = WAIT_BUDGET_SECONDS
) -> None:
    deadline = time.monotonic() + budget
    while not predicate():
        assert time.monotonic() < deadline, f"{what} no ocurrió en {budget:g} s"
        await asyncio.sleep(POLL_SECONDS)


async def wait_idle(pool: AsyncSandboxPool, ready: int, what: str = "el pool no se llenó") -> None:
    await wait_until(lambda: pool.stats().ready == ready and pool.stats().warming == 0, what)


def ready_ids(pool: AsyncSandboxPool) -> list[str]:
    return [slot.sandbox_id for slot in pool.stats().slots if slot.state == "ready"]


# ------------------------------------------------------------------ warm-up


async def test_warm_up_sequence_and_record_contents(
    make_pool: PoolFactory, plane: FakeControlPlane, transport: PoolTransport
) -> None:
    pool = await make_pool(size=2).start()
    await wait_idle(pool, 2)

    records = pool.backend.load()
    assert len(records) == 2
    assert {record.state for record in records} == {"ready"}
    assert all(record.parked_at is not None for record in records)
    assert len({record.access_token for record in records}) == 2
    for record in records:
        vm = plane.microvms[record.sandbox_id]
        assert vm.token_sha256 == access_token_sha256(record.access_token)
        assert vm.state == "SUSPENDED"
        assert ops_for(plane, record.sandbox_id) == [
            "CreateMicrovmAuthToken",
            "GetMicrovm",
            "SuspendMicrovm",
            "GetMicrovm",
        ]
        health = vm.rayd.servicer.health_calls
        assert health and health[0][ACCESS_TOKEN_KEY] == record.access_token
        assert record.idle == IdlePolicy(
            max_idle_seconds=300, suspended_duration_seconds=POOL_TIMEOUT - 300
        )
    assert len(plane.calls_to("RunMicrovm")) == 2
    assert transport.currently_open == 0
    assert pool.stats().launched == 2


async def test_record_saved_before_the_park(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    backend = SpyBackend()
    plane.fail("SuspendMicrovm", 1, RuntimeError("suspend exploded"))
    pool = await make_pool(size=1, backend=backend).start()

    await wait_until(lambda: pool.stats().failed == 1, "el calentamiento no falló")
    await wait_idle(pool, 1)

    failed_id = next(vm.sandbox_id for vm in plane.microvms.values() if vm.state == "TERMINATED")
    assert backend.saved[failed_id] == ["warming"]
    assert failed_id not in {record.sandbox_id for record in backend.load()}
    assert "TerminateMicrovm" in ops_for(plane, failed_id)
    assert pool.stats().launched == 2


async def test_fill_respects_concurrency_and_bucket_pacing(
    make_pool: PoolFactory, plane: FakeControlPlane, transport: PoolTransport
) -> None:
    pool = await make_pool(size=6, fill_concurrency=2).start()
    await wait_idle(pool, 6)

    runs = plane.timestamps("RunMicrovm")
    suspends = plane.timestamps("SuspendMicrovm")
    assert len(runs) == 6 and len(suspends) == 6
    assert max_calls_in_window(runs, 1.0) <= 10
    assert max_calls_in_window(suspends, 1.0) <= 4
    assert max(suspends) - min(suspends) >= (6 - 2) / 2
    assert transport.max_open <= 2


# --------------------------------------------------------------------- take


async def test_take_pops_the_oldest_and_resumes_explicitly(
    make_pool: PoolFactory, plane: FakeControlPlane, now: TickingNow
) -> None:
    now.step = 60.0
    pool = await make_pool(size=2).start()
    await wait_idle(pool, 2)
    records = {record.sandbox_id: record for record in pool.backend.load()}
    oldest = min(records.values(), key=lambda record: record.expires_at)
    before = len(plane.calls)

    sandbox = await pool.take()
    try:
        assert sandbox.sandbox_id == oldest.sandbox_id
        assert sandbox.access_token == oldest.access_token
        assert ops_for(plane, oldest.sandbox_id, since=before) == [
            "ResumeMicrovm",
            "CreateMicrovmAuthToken",
        ]
        assert oldest.sandbox_id not in {record.sandbox_id for record in pool.backend.load()}
        assert (await sandbox.run_code("1+1")).text == "2"
        assert (await sandbox.get_health()).sandbox_id == oldest.sandbox_id
        await sandbox.get_metrics()
        presented = plane.microvms[oldest.sandbox_id].rayd.servicer.metrics_calls[-1]
        assert presented_token_sha256(presented) == access_token_sha256(oldest.access_token)
        stats = pool.stats()
        assert stats.hits == 1 and stats.takes == 1 and stats.misses == 0
    finally:
        await sandbox.kill()
    await wait_idle(pool, 2, "el pool no rellenó tras la toma")
    assert pool.stats().launched == 3


async def test_take_poll_schedule_is_the_fast_one(
    make_pool: PoolFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = AsyncSandbox._wait_until_ready

    async def spy(self: AsyncSandbox, **kwargs: Any) -> Any:
        seen.append(kwargs["readiness"].__name__)
        return await original(self, **kwargs)

    monkeypatch.setattr(AsyncSandbox, "_wait_until_ready", spy)
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)

    await (await pool.take()).kill()

    assert seen[0] == "ReadinessPoll"
    assert "TakePoll" in seen


async def test_empty_pool_falls_back_to_create(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    pool = await make_pool(size=2).start()
    await wait_idle(pool, 2)
    parked = set(ready_ids(pool))
    plane.hold("RunMicrovm", 2)

    first = await pool.take()
    second = await pool.take()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 4, "los rellenos no llegaron")
    third = await pool.take()
    try:
        assert {first.sandbox_id, second.sandbox_id} == parked
        assert third.sandbox_id not in parked
        assert (await third.run_code("1+1")).text == "2"
        stats = pool.stats()
        assert stats.hits == 2 and stats.misses == 1 and stats.takes == 3
        assert len(plane.calls_to("RunMicrovm")) == 5
    finally:
        plane.release("RunMicrovm")
        for sandbox in (first, second, third):
            await sandbox.kill()
    await wait_idle(pool, 2)
    assert pool.stats().launched == 5


async def test_take_wait_is_served_by_a_refill(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    plane.hold("RunMicrovm", 1)
    pool = await make_pool(size=1).start()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 1, "el calentamiento no arrancó")
    threading.Timer(0.3, plane.release, args=("RunMicrovm",)).start()

    started = time.monotonic()
    sandbox = await pool.take(wait=5)
    try:
        assert time.monotonic() - started < 5
        stats = pool.stats()
        assert stats.hits == 1 and stats.misses == 0
    finally:
        await sandbox.kill()


async def test_take_without_wait_on_empty_pool_does_not_block(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    plane.hold("RunMicrovm", 1)
    pool = await make_pool(size=1).start()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 1, "el calentamiento no arrancó")

    sandbox = await pool.take()
    try:
        assert pool.stats().misses == 1
    finally:
        plane.release("RunMicrovm")
        await sandbox.kill()


async def test_slot_lost_at_take_falls_back(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    lost_id = ready_ids(pool)[0]
    plane.fail("ResumeMicrovm", 1, False)

    sandbox = await pool.take()
    try:
        assert sandbox.sandbox_id != lost_id
        assert (await sandbox.run_code("1+1")).text == "2"
        assert "TerminateMicrovm" in ops_for(plane, lost_id)
        stats = pool.stats()
        assert (stats.lost, stats.misses, stats.hits, stats.takes) == (1, 1, 0, 1)
    finally:
        await sandbox.kill()
    await wait_idle(pool, 1)


async def test_open_failure_at_take_falls_back(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    failing_id = ready_ids(pool)[0]
    plane.fail("CreateMicrovmAuthToken", 2, AuthenticationException("denied"))

    sandbox = await pool.take()
    try:
        assert sandbox.sandbox_id != failing_id
        assert "TerminateMicrovm" in ops_for(plane, failing_id)
        stats = pool.stats()
        assert (stats.failed, stats.misses, stats.hits, stats.lost) == (1, 1, 0, 0)
    finally:
        await sandbox.kill()


# ------------------------------------------------------------------- sweep


async def test_recycle_with_a_fake_clock(
    make_pool: PoolFactory, plane: FakeControlPlane, now: TickingNow
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    old_id = ready_ids(pool)[0]

    now.advance(3700)
    await pool._request_sweep()

    await wait_until(lambda: pool.stats().recycled == 1, "no se recicló")
    await wait_idle(pool, 1)
    assert ready_ids(pool) != [old_id]
    assert plane.microvms[old_id].state == "TERMINATED"
    assert pool.stats().launched == 2


async def test_reconcile_terminated_out_of_band(
    make_pool: PoolFactory, plane: FakeControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    dead_id = ready_ids(pool)[0]
    plane.set_state(dead_id, "TERMINATED", reason="Success.")
    gets_before = len(ops_for(plane, dead_id))

    with caplog.at_level(logging.WARNING, logger="rayito.pool"):
        await pool._request_sweep()
        await wait_until(lambda: pool.stats().lost == 1, "no se detectó la pérdida")
        await wait_idle(pool, 1)

    assert ops_for(plane, dead_id)[gets_before:] == ["GetMicrovm"]
    assert ready_ids(pool) != [dead_id]
    assert any(
        dead_id in record.getMessage() and "Success." in record.getMessage()
        for record in caplog.records
    )


async def test_reconcile_absent_and_not_found(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    gone_id = ready_ids(pool)[0]
    del plane.microvms[gone_id]

    await pool._request_sweep()

    await wait_until(lambda: pool.stats().lost == 1, "no se detectó la pérdida")
    await wait_idle(pool, 1)
    assert gone_id not in ready_ids(pool)


async def test_reconcile_resumed_out_of_band_is_reparked(
    make_pool: PoolFactory, plane: FakeControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    slot_id = ready_ids(pool)[0]
    plane.set_state(slot_id, "RUNNING")
    suspends_before = len(plane.calls_to("SuspendMicrovm"))

    with caplog.at_level(logging.WARNING, logger="rayito.pool"):
        await pool._request_sweep()
        await wait_until(
            lambda: len(plane.calls_to("SuspendMicrovm")) == suspends_before + 1,
            "no se volvió a aparcar",
        )

    assert plane.microvms[slot_id].state == "SUSPENDED"
    assert ready_ids(pool) == [slot_id]
    reparked = [r for r in caplog.records if "vuelta a aparcar" in r.getMessage()]
    assert len(reparked) == 1 and slot_id in reparked[0].getMessage()


async def test_listing_failure_aborts_the_sweep(
    make_pool: PoolFactory, plane: FakeControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)
    slot_id = ready_ids(pool)[0]
    plane.set_state(slot_id, "TERMINATED", reason="Success.")
    listings = len(plane.calls_to("ListMicrovms"))
    plane.fail("ListMicrovms", listings + 1, RuntimeError("throttled"))

    with caplog.at_level(logging.WARNING, logger="rayito.pool"):
        await pool._request_sweep()
        await wait_until(lambda: len(plane.calls_to("ListMicrovms")) == listings + 1, "sin barrido")
        await asyncio.sleep(0.1)

    assert ready_ids(pool) == [slot_id]
    assert any("list-microvms" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------- backoff


async def test_backoff_after_failures(
    make_pool: PoolFactory,
    plane: FakeControlPlane,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    plane.fail("RunMicrovm", 1, QuotaExceededException("memory quota"))
    plane.fail("RunMicrovm", 2, QuotaExceededException("memory quota"))

    with caplog.at_level(logging.WARNING, logger="rayito.pool"):
        pool = await make_pool(size=1).start()
        await wait_idle(pool, 1)

    assert sleeps == [1.0, 2.0]
    assert len(plane.calls_to("RunMicrovm")) == 3
    assert pool.stats().failed == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("QuotaExceededException" in message for message in warnings)
    token = pool.backend.load()[0].access_token
    assert not any(token in message for message in warnings)


async def test_backend_failure_at_launch_terminates_the_vm(
    make_pool: PoolFactory, plane: FakeControlPlane, sleeps: list[float]
) -> None:
    backend = BrokenWarmingSaveBackend()
    pool = await make_pool(size=1, backend=backend).start()
    await wait_idle(pool, 1)

    launched = list(plane.microvms)
    assert len(launched) == 2
    assert plane.microvms[launched[0]].state == "TERMINATED"
    assert ops_for(plane, launched[0]) == ["TerminateMicrovm"]
    assert backend.rejected == 1
    assert pool.stats().failed == 1
    assert pool.stats().launched == 1
    assert sleeps == [1.0]
    assert plane.live_ids == ready_ids(pool) == [launched[1]]
    assert [record.sandbox_id for record in backend.load()] == [launched[1]]


async def test_backoff_resets_after_success(
    make_pool: PoolFactory, plane: FakeControlPlane, sleeps: list[float]
) -> None:
    plane.fail("RunMicrovm", 1, QuotaExceededException("q"))
    plane.fail("RunMicrovm", 3, QuotaExceededException("q"))
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)

    await (await pool.take()).kill()
    await wait_idle(pool, 1)

    assert sleeps == [1.0, 1.0]


# ------------------------------------------------------------------- close


async def test_close_drains_and_joins(make_pool: PoolFactory, plane: FakeControlPlane) -> None:
    async with make_pool(size=3) as pool:
        await wait_idle(pool, 3)
        ids = ready_ids(pool)
        filler = pool._filler

    assert filler is not None and filler.done()
    for sandbox_id in ids:
        assert plane.microvms[sandbox_id].state == "TERMINATED"
    assert pool.backend.load() == ()
    assert plane.live_ids == []
    with pytest.raises(PoolClosedException):
        await pool.take()
    await pool.close()


async def test_close_with_warm_up_in_flight_terminates_instead_of_parking(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    plane.hold("RunMicrovm", 1)
    pool = await make_pool(size=1).start()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 1, "el calentamiento no arrancó")

    closer = asyncio.create_task(pool.close())
    await asyncio.sleep(0.1)
    plane.release("RunMicrovm")
    await asyncio.wait_for(closer, WAIT_BUDGET_SECONDS)

    assert plane.live_ids == []
    assert pool.backend.load() == ()


async def test_no_drain_needs_persistence(
    make_pool: PoolFactory, plane: FakeControlPlane, tmp_path: Path
) -> None:
    memory = await make_pool(size=1).start()
    await wait_idle(memory, 1)
    with pytest.raises(InvalidArgumentException, match=r"drain.*persistent"):
        await memory.close(drain=False)
    await memory.close()

    backend = JsonFilePoolBackend(tmp_path / "pool.json")
    persistent = await make_pool(size=1, backend=backend).start()
    await wait_idle(persistent, 1)
    slot_id = ready_ids(persistent)[0]
    terminates = len(plane.calls_to("TerminateMicrovm"))

    await persistent.close(drain=False)

    assert [record.sandbox_id for record in backend.load()] == [slot_id]
    assert len(plane.calls_to("TerminateMicrovm")) == terminates
    assert plane.microvms[slot_id].state == "SUSPENDED"


async def test_recovery_after_a_simulated_crash(
    make_pool: PoolFactory, plane: FakeControlPlane, tmp_path: Path
) -> None:
    path = tmp_path / "pool.json"
    first = await make_pool(size=2, backend=JsonFilePoolBackend(path)).start()
    await wait_idle(first, 2)
    await first.close(drain=False)
    orphan_id, ready_id = sorted(ready_ids(first))
    document = json.loads(path.read_text(encoding="utf-8"))
    for slot in document["slots"]:
        if slot["sandbox_id"] == orphan_id:
            slot["state"] = "warming"
            slot["parked_at"] = None
    path.write_text(json.dumps(document), encoding="utf-8")
    terminates = len(plane.calls_to("TerminateMicrovm"))

    second = await make_pool(size=1, backend=JsonFilePoolBackend(path)).start()

    assert plane.calls_to("TerminateMicrovm")[terminates:][0].sandbox_id == orphan_id
    assert len(plane.calls_to("TerminateMicrovm")) == terminates + 1
    stats = second.stats()
    assert (stats.ready, stats.lost, stats.launched) == (1, 1, 0)
    sandbox = await second.take()
    try:
        assert sandbox.sandbox_id == ready_id
        assert (await sandbox.run_code("1+1")).text == "2"
    finally:
        await sandbox.kill()


# ------------------------------------------------------------ create(pool=)


async def test_create_with_pool_is_sugar_for_take(make_pool: PoolFactory) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)

    sandbox = await AsyncSandbox.create(pool=pool)
    try:
        assert pool.stats().hits == 1
        assert (await sandbox.run_code("1+1")).text == "2"
    finally:
        await sandbox.kill()
    with pytest.raises(InvalidArgumentException, match="`envs`"):
        await AsyncSandbox.create(pool=pool, envs={"A": "1"})


async def test_create_with_pool_signs_transfers_with_the_pool_session(
    make_pool: PoolFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Las URLs y las transferencias enrutadas de una plaza se firman con la
    sesión boto3 del pool, no con la cadena por defecto."""
    session = boto3.session.Session(region_name="us-east-1")
    built: list[object] = []

    def from_session(cls: type[S3Gateway], given: object, region: str) -> S3Gateway:
        built.append(given)
        return cls(None)

    monkeypatch.setattr("rayito._s3.S3Gateway.from_session", classmethod(from_session))
    pool = await make_pool(size=1, session=session).start()
    await wait_idle(pool, 1)
    staging = S3Staging(bucket="amzn-s3-demo-bucket")

    sandbox = await AsyncSandbox.create(pool=pool, transfer=staging)
    try:
        await sandbox._transfers._gateway_for(staging)
        assert built == [session]
    finally:
        await sandbox.kill()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("template", IMAGE_ARN),
        ("template_version", "2.0"),
        ("timeout", 900),
        ("idle", None),
        ("envs", {"A": "1"}),
        ("metadata", {"a": "b"}),
        ("cpu_time_limit", 10),
        ("execution_role_arn", "arn:aws:iam::1:role/r"),
        ("allowed_ports", [3000]),
        ("ingress", ["ALL_INGRESS"]),
        ("egress", ["INTERNET_EGRESS"]),
        ("logging", "cloudwatch"),
        ("region", "us-east-1"),
        ("session", object()),
        ("access_token", "a" * TOKEN_LENGTH),
        ("keep_on_failure", True),
        ("control_plane", object()),
        ("transport", object()),
    ],
)
async def test_create_with_pool_rejects_each_launch_kwarg(
    make_pool: PoolFactory, name: str, value: object
) -> None:
    pool = make_pool(size=1)
    kwargs: Any = {name: value}

    with pytest.raises(InvalidArgumentException, match=f"`{name}`"):
        await AsyncSandbox.create(pool=pool, **kwargs)


async def test_create_with_pool_passes_the_knobs_through(
    make_pool: PoolFactory, plane: FakeControlPlane
) -> None:
    pool = await make_pool(size=1).start()
    await wait_idle(pool, 1)

    sandbox = await AsyncSandbox.create(
        pool=pool, request_timeout=7.5, ready_timeout=8.0, reconnect_timeout=9.0
    )
    try:
        await sandbox.get_metrics()
        seen = plane.microvms[sandbox.sandbox_id].rayd.servicer.metrics_calls[-1][DEADLINE_KEY]
        assert 5.0 < float(seen) < 8.0
        assert sandbox._ready_timeout == 8.0
        assert sandbox._reconnect_timeout == 9.0
    finally:
        await sandbox.kill()


async def test_create_with_unstarted_pool_raises_pool_closed(make_pool: PoolFactory) -> None:
    pool = make_pool(size=1)

    with pytest.raises(PoolClosedException, match="not started"):
        await AsyncSandbox.create(pool=pool)
    with pytest.raises(PoolClosedException):
        await pool.take()


# ----------------------------------------------------------------- secrets


async def test_caplog_holds_no_secret(
    make_pool: PoolFactory, plane: FakeControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        pool = await make_pool(size=2).start()
        await wait_idle(pool, 2)
        tokens = [record.access_token for record in pool.backend.load()]
        sandbox = await pool.take()
        tokens.append(sandbox.access_token)
        await sandbox.kill()
        await wait_idle(pool, 2)
        lost_id = ready_ids(pool)[0]
        plane.set_state(lost_id, "TERMINATED", reason="Success.")
        await pool._request_sweep()
        await wait_until(lambda: pool.stats().lost == 1, "sin pérdida")
        await wait_idle(pool, 2)
        tokens.extend(record.access_token for record in pool.backend.load())
        await pool.close()

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert caplog.records
    assert JWE not in text
    assert "token_sha256" not in text
    assert all(token not in text for token in tokens)


# ------------------------------------------------------------------- stats


async def test_stats_after_a_scripted_sequence(
    make_pool: PoolFactory, plane: FakeControlPlane, now: TickingNow
) -> None:
    now.step = 60.0
    pool = await make_pool(size=2).start()
    await wait_idle(pool, 2)
    now.step = 0.0
    oldest, newest = sorted(pool.backend.load(), key=lambda record: record.expires_at)
    assert oldest.expires_at < newest.expires_at
    now.current = newest.expires_at - timedelta(seconds=POOL_MIN_REMAINING)
    await pool._request_sweep()
    await wait_until(lambda: pool.stats().recycled == 1, "sin reciclado")
    await wait_idle(pool, 2)
    plane.hold("RunMicrovm", 2)

    hit = await pool.take()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 4, "el relleno no llegó al run")
    remaining_id = ready_ids(pool)[0]
    plane.set_state(remaining_id, "TERMINATED", reason="Success.")
    await pool._request_sweep()
    await wait_until(lambda: len(plane.calls_to("RunMicrovm")) == 5, "el segundo relleno no llegó")
    miss = await pool.take()
    plane.release("RunMicrovm")
    await wait_idle(pool, 2)
    await hit.kill()
    await miss.kill()

    stats = pool.stats()
    assert (stats.size, stats.ready, stats.warming) == (2, 2, 0)
    assert (stats.takes, stats.hits, stats.misses) == (2, 1, 1)
    assert (stats.launched, stats.recycled, stats.lost, stats.failed) == (6, 1, 1, 0)
    assert len(stats.slots) == 2
    assert all(slot.state == "ready" for slot in stats.slots)
