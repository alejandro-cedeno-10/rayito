"""Backends del pool: el dict en memoria y el fichero JSON 0600 atómico."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rayito import IdlePolicy, InMemoryPoolBackend, JsonFilePoolBackend
from rayito._pool_backends import PoolBackend
from rayito._pool_base import POOL_SCHEMA, SlotRecord
from rayito.exceptions import InvalidArgumentException

from .conftest import ACCESS_TOKEN, IMAGE_ARN, REGION

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
TOKEN_B = "b" * 43


def record(sandbox_id: str, access_token: str = ACCESS_TOKEN, state: str = "ready") -> SlotRecord:
    return SlotRecord(
        sandbox_id=sandbox_id,
        access_token=access_token,
        endpoint="host:1234",
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=NOW,
        maximum_duration_seconds=7200,
        region=REGION,
        state="warming" if state == "warming" else "ready",
        idle=IdlePolicy(max_idle_seconds=300, suspended_duration_seconds=6900),
        execution_role_arn="arn:aws:iam::1:role/r",
        ingress=("arn:ingress",),
        parked_at=None if state == "warming" else NOW + timedelta(seconds=8),
    )


def test_in_memory_upsert_delete_load() -> None:
    backend: PoolBackend = InMemoryPoolBackend()

    assert backend.persistent is False
    assert backend.load() == ()
    backend.save(record("microvm-a"))
    backend.save(record("microvm-b", TOKEN_B))
    backend.save(record("microvm-a", state="warming"))
    assert {r.sandbox_id: r.state for r in backend.load()} == {
        "microvm-a": "warming",
        "microvm-b": "ready",
    }
    backend.delete("microvm-a")
    backend.delete("microvm-unknown")
    assert [r.sandbox_id for r in backend.load()] == ["microvm-b"]


def test_json_round_trip_atomic_write_and_contents(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    backend = JsonFilePoolBackend(path)
    first = record("microvm-a")
    second = record("microvm-b", TOKEN_B, state="warming")

    assert backend.persistent is True
    backend.save(first)
    backend.save(second)
    reloaded = JsonFilePoolBackend(path).load()

    assert set(reloaded) == {first, second}
    assert not path.with_name("pool.json.tmp").exists()
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["schema"] == POOL_SCHEMA
    assert set(document) == {"schema", "slots"}
    assert ACCESS_TOKEN in text
    assert TOKEN_B in text
    assert set(document["slots"][0]) == {
        "sandbox_id",
        "access_token",
        "endpoint",
        "template",
        "template_version",
        "started_at",
        "maximum_duration_seconds",
        "region",
        "state",
        "idle",
        "execution_role_arn",
        "ingress",
        "egress",
        "parked_at",
    }


@pytest.mark.skipif(os.name == "nt", reason="el modo del fichero no aplica en Windows")
def test_json_file_mode_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    JsonFilePoolBackend(path).save(record("microvm-a"))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_json_missing_file_loads_empty(tmp_path: Path) -> None:
    backend = JsonFilePoolBackend(tmp_path / "missing.json")

    assert backend.load() == ()
    backend.delete("microvm-unknown")
    assert not (tmp_path / "missing.json").exists()


def test_json_foreign_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({"schema": "other/1", "slots": []}), encoding="utf-8")

    with pytest.raises(InvalidArgumentException, match="other/1"):
        JsonFilePoolBackend(path).load()


def test_json_garbage_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(InvalidArgumentException, match="JSON"):
        JsonFilePoolBackend(path).load()


def test_json_delete_rewrites_and_ignores_unknown(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    backend = JsonFilePoolBackend(path)
    backend.save(record("microvm-a"))
    backend.save(record("microvm-b", TOKEN_B))

    backend.delete("microvm-a")
    backend.delete("microvm-zzz")

    assert [r.sandbox_id for r in backend.load()] == ["microvm-b"]
    assert ACCESS_TOKEN not in path.read_text(encoding="utf-8")


POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="modos y enlaces POSIX")


def test_json_write_ignores_a_pre_created_temp(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    planted = path.with_name("pool.json.tmp")
    planted.write_text("del atacante", encoding="utf-8")
    if os.name == "posix":
        os.chmod(planted, 0o666)

    JsonFilePoolBackend(path).save(record("microvm-a"))

    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == POOL_SCHEMA
    assert ACCESS_TOKEN in path.read_text(encoding="utf-8")
    assert planted.read_text(encoding="utf-8") == "del atacante"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX_ONLY
def test_json_write_never_follows_a_symlink(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("intacto", encoding="utf-8")
    path.with_name("pool.json.tmp").symlink_to(victim)

    JsonFilePoolBackend(path).save(record("microvm-a"))

    assert victim.read_text(encoding="utf-8") == "intacto"
    assert ACCESS_TOKEN in path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_json_read_refuses_a_symlinked_state_file(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    JsonFilePoolBackend(real).save(record("microvm-a"))
    path = tmp_path / "pool.json"
    path.symlink_to(real)

    with pytest.raises(InvalidArgumentException, match="fichero regular"):
        JsonFilePoolBackend(path).load()


def test_json_write_leaves_no_temporary(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    backend = JsonFilePoolBackend(path)

    backend.save(record("microvm-a"))
    backend.save(record("microvm-b", TOKEN_B))
    backend.delete("microvm-b")

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["pool.json"]
