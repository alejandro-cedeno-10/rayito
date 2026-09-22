"""Reglas puras del pool (`_pool_base`): validación de `PoolConfig`,
selección y reciclado de plazas, reconciliación, backoff, estadísticas,
registro ↔ JSON y el calendario `TakePoll`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rayito import IdlePolicy, PoolConfig, PoolStats
from rayito._pool_base import (
    POOL_REJECTED_KWARGS,
    FillBackoff,
    PoolCounters,
    SlotRecord,
    TakePoll,
    launch_kwargs,
    pick_ready_slot,
    reconcile_action,
    record_from_dict,
    record_from_info,
    record_to_dict,
    reject_launch_kwargs_with_pool,
    resolved_pool_idle,
    sandbox_info_from_record,
    slot_is_stale,
    stats_from_records,
)
from rayito._sandbox_base import ReadinessPoll
from rayito.exceptions import InvalidArgumentException, SandboxLifetimeException

from .conftest import ACCESS_TOKEN, IMAGE_ARN, REGION, FakeClock

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
TOKEN_B = "b" * 43


def record(
    sandbox_id: str,
    *,
    started_at: datetime = NOW,
    duration: int = 7200,
    state: str = "ready",
    access_token: str = ACCESS_TOKEN,
) -> SlotRecord:
    return SlotRecord(
        sandbox_id=sandbox_id,
        access_token=access_token,
        endpoint="host:1234",
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=started_at,
        maximum_duration_seconds=duration,
        region=REGION,
        state="warming" if state == "warming" else "ready",
        idle=IdlePolicy(max_idle_seconds=300, suspended_duration_seconds=6900),
        parked_at=None if state == "warming" else started_at + timedelta(seconds=8),
    )


# ------------------------------------------------------------------ PoolConfig


def test_defaults_park_for_the_whole_wall() -> None:
    config = PoolConfig(size=1)

    assert config.timeout == 28800
    assert config.min_remaining_seconds == 3600
    assert config.idle == IdlePolicy()
    assert config.fill_concurrency == 4
    assert config.sweep_interval_seconds == 30.0
    assert config.ready_timeout == 90.0
    assert resolved_pool_idle(config).suspended_duration_seconds == 28800 - 300
    assert not hasattr(config, "access_token")
    assert not hasattr(config, "allowed_ports")


def test_launch_kwargs_map_one_to_one_onto_create() -> None:
    config = PoolConfig(
        size=2,
        template=IMAGE_ARN,
        template_version="3.0",
        timeout=7200,
        envs={"A": "1"},
        metadata={"team": "x"},
        cpu_time_limit=60,
        execution_role_arn="arn:aws:iam::1:role/r",
        ingress=["ALL_INGRESS"],
        egress=["INTERNET_EGRESS"],
        logging="cloudwatch",
        ready_timeout=30,
    )

    assert launch_kwargs(config) == {
        "template": IMAGE_ARN,
        "template_version": "3.0",
        "timeout": 7200,
        "idle": IdlePolicy(),
        "envs": {"A": "1"},
        "metadata": {"team": "x"},
        "cpu_time_limit": 60,
        "execution_role_arn": "arn:aws:iam::1:role/r",
        "ingress": ["ALL_INGRESS"],
        "egress": ["INTERNET_EGRESS"],
        "logging": "cloudwatch",
        "ready_timeout": 30,
    }


@pytest.mark.parametrize("size", [0, 65, -1, True, 2.5])
def test_size_cap(size: object) -> None:
    with pytest.raises(InvalidArgumentException, match=r"size.*1\.\.=64"):
        PoolConfig(size=size)  # type: ignore[arg-type]


def test_idle_without_auto_resume_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match=r"idle.*auto_resume"):
        PoolConfig(size=2, idle=IdlePolicy(auto_resume=False))


def test_idle_none_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="idle"):
        PoolConfig(size=2, idle=None)  # type: ignore[arg-type]


def test_idle_longer_than_timeout_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match=r"idle.max_idle_seconds"):
        PoolConfig(size=1, timeout=600, idle=IdlePolicy(max_idle_seconds=600))


def test_timeout_goes_through_validate_timeout() -> None:
    with pytest.raises(SandboxLifetimeException, match="timeout"):
        PoolConfig(size=1, timeout=28801)
    with pytest.raises(InvalidArgumentException, match="timeout"):
        PoolConfig(size=1, timeout=0)


@pytest.mark.parametrize("value", [59, 28741, 0, 1.5])
def test_min_remaining_bounds(value: object) -> None:
    with pytest.raises(InvalidArgumentException, match="min_remaining_seconds"):
        PoolConfig(size=1, min_remaining_seconds=value)  # type: ignore[arg-type]


def test_min_remaining_relative_to_timeout() -> None:
    assert PoolConfig(size=1, timeout=720, min_remaining_seconds=600).min_remaining_seconds == 600
    with pytest.raises(InvalidArgumentException, match=r"min_remaining_seconds.*660"):
        PoolConfig(size=1, timeout=720, min_remaining_seconds=661)


@pytest.mark.parametrize("value", [0, 9, 2.0])
def test_fill_concurrency_bounds(value: object) -> None:
    with pytest.raises(InvalidArgumentException, match=r"fill_concurrency.*1\.\.=8"):
        PoolConfig(size=1, fill_concurrency=value)  # type: ignore[arg-type]


def test_sweep_interval_floor() -> None:
    with pytest.raises(InvalidArgumentException, match="sweep_interval_seconds"):
        PoolConfig(size=1, sweep_interval_seconds=4.9)
    assert PoolConfig(size=1, sweep_interval_seconds=5).sweep_interval_seconds == 5


def test_ready_timeout_positive() -> None:
    with pytest.raises(InvalidArgumentException, match="ready_timeout"):
        PoolConfig(size=1, ready_timeout=0)


def test_payload_validators_apply() -> None:
    with pytest.raises(InvalidArgumentException, match="envs"):
        PoolConfig(size=1, envs={"A": 1})  # type: ignore[dict-item]
    with pytest.raises(InvalidArgumentException, match="metadata"):
        PoolConfig(size=1, metadata={"": "x"})
    with pytest.raises(InvalidArgumentException, match="cpu_time_limit"):
        PoolConfig(size=1, cpu_time_limit=0)


# ------------------------------------------------------------- pick / stale


def test_pick_ready_slot_takes_the_earliest_expiry_with_enough_life() -> None:
    older = record("microvm-old", started_at=NOW - timedelta(minutes=1))
    newer = record("microvm-new", started_at=NOW)
    warming = record("microvm-warm", started_at=NOW - timedelta(hours=1), state="warming")
    stale = record("microvm-stale", started_at=NOW - timedelta(hours=1, minutes=1))

    picked = pick_ready_slot([newer, warming, stale, older], NOW, 3600)

    assert picked is older
    assert pick_ready_slot([stale, warming], NOW, 3600) is None
    assert pick_ready_slot([], NOW, 3600) is None


def test_slot_is_stale_at_the_boundary() -> None:
    slot = record("microvm-x", started_at=NOW, duration=7200)

    assert slot_is_stale(slot, NOW + timedelta(seconds=3600), 3600) is False
    assert slot_is_stale(slot, NOW + timedelta(seconds=3601), 3600) is True
    assert slot.remaining_seconds(NOW + timedelta(hours=3)) == 0.0
    assert slot.expires_at == NOW + timedelta(seconds=7200)


# ------------------------------------------------------------- reconcile


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        ("SUSPENDED", "keep"),
        ("SUSPENDING", "keep"),
        ("PENDING", "keep"),
        ("RUNNING", "repark"),
        (None, "check"),
        ("TERMINATING", "drop"),
        ("TERMINATED", "drop"),
    ],
)
def test_reconcile_action_branches(listed: str | None, expected: str) -> None:
    assert reconcile_action(record("microvm-x"), listed) == expected


def test_reconcile_action_ignores_warming_records() -> None:
    assert reconcile_action(record("microvm-x", state="warming"), None) == "keep"


# --------------------------------------------------------------- backoff


def test_fill_backoff_doubles_to_sixty_and_resets() -> None:
    backoff = FillBackoff(random=lambda: 0.5)

    delays = [backoff.next_delay() for _ in range(8)]

    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    backoff.reset()
    assert backoff.next_delay() == 1


def test_fill_backoff_jitter_is_plus_minus_25_percent() -> None:
    low = FillBackoff(random=lambda: 0.0)
    high = FillBackoff(random=lambda: 1.0)

    assert low.next_delay() == pytest.approx(0.75)
    assert high.next_delay() == pytest.approx(1.25)


# ----------------------------------------------------------------- stats


def test_stats_arithmetic_and_no_token() -> None:
    counters = PoolCounters(takes=3, hits=2, misses=1, launched=5, recycled=1, lost=1, failed=0)
    ready = record("microvm-b", started_at=NOW)
    warming = record("microvm-a", started_at=NOW - timedelta(minutes=5), state="warming")

    stats = stats_from_records([ready, warming], size=2, warming=1, counters=counters)

    assert stats == PoolStats(
        size=2,
        ready=1,
        warming=1,
        takes=3,
        hits=2,
        misses=1,
        launched=5,
        recycled=1,
        lost=1,
        failed=0,
        slots=stats.slots,
    )
    assert stats.takes == stats.hits + stats.misses
    assert [slot.sandbox_id for slot in stats.slots] == ["microvm-a", "microvm-b"]
    assert stats.slots[0].state == "warming"
    assert stats.slots[1].parked_at == NOW + timedelta(seconds=8)
    assert stats.slots[1].expires_at == NOW + timedelta(seconds=7200)
    assert not any(hasattr(slot, "access_token") for slot in stats.slots)
    assert ACCESS_TOKEN not in repr(stats)


# --------------------------------------------------------------- record


def test_record_round_trip_and_redaction() -> None:
    original = record("microvm-x", started_at=NOW)

    data = record_to_dict(original)
    restored = record_from_dict(data)

    assert restored == original
    assert data["started_at"] == "2026-09-16T12:00:00Z"
    assert data["parked_at"] == "2026-09-16T12:00:08Z"
    assert data["idle"] == {
        "max_idle_seconds": 300,
        "suspended_duration_seconds": 6900,
        "auto_resume": True,
    }
    assert data["access_token"] == ACCESS_TOKEN
    assert "<redacted>" in repr(original)
    assert ACCESS_TOKEN not in repr(original)
    assert ACCESS_TOKEN not in str(original)


def test_record_from_dict_rejects_garbage() -> None:
    with pytest.raises(InvalidArgumentException, match="registro de plaza inválido"):
        record_from_dict({"sandbox_id": "x"})
    with pytest.raises(InvalidArgumentException, match="estado de plaza"):
        record_from_dict({**record_to_dict(record("microvm-x")), "state": "taken"})


def test_record_from_info_and_back() -> None:
    original = record("microvm-x", started_at=NOW)

    info = sandbox_info_from_record(original)
    rebuilt = record_from_info(
        info, access_token=TOKEN_B, region=REGION, state="ready", parked_at=original.parked_at
    )

    assert info.state == "SUSPENDED"
    assert info.sandbox_id == "microvm-x"
    assert info.endpoint == "host:1234"
    assert info.expires_at == original.expires_at
    assert rebuilt.access_token == TOKEN_B
    assert rebuilt.idle == original.idle
    assert sandbox_info_from_record(original, "RUNNING").state == "RUNNING"


# --------------------------------------------------------------- TakePoll


def test_take_poll_schedule_is_fast_and_bounded() -> None:
    clock = FakeClock()
    poll = TakePoll(timeout=60, monotonic=clock)

    delays = [poll.next_delay() for _ in range(5)]

    assert delays == [0.1, 0.2, 0.4, 0.5, 0.5]
    assert TakePoll.JITTER == 0.0
    default = ReadinessPoll(timeout=60, monotonic=clock)
    assert [default.next_delay() for _ in range(5)] == [0.25, 0.5, 1.0, 2.0, 2.0]


# ----------------------------------------------------------- create(pool=)


def test_reject_launch_kwargs_names_the_first_offender_in_signature_order() -> None:
    reject_launch_kwargs_with_pool({"template": None, "timeout": 3600, "logging": "disabled"})
    with pytest.raises(InvalidArgumentException, match="`envs`"):
        reject_launch_kwargs_with_pool({"envs": {"A": "1"}, "metadata": {"b": "2"}})
    with pytest.raises(InvalidArgumentException, match="`idle`"):
        reject_launch_kwargs_with_pool({"idle": None, "envs": {"A": "1"}})
    with pytest.raises(InvalidArgumentException, match="`keep_on_failure`"):
        reject_launch_kwargs_with_pool({"keep_on_failure": True})
    assert POOL_REJECTED_KWARGS[0] == "template"
    assert POOL_REJECTED_KWARGS[-1] == "transport"
    assert "allowed_ports" in POOL_REJECTED_KWARGS
