"""`rayito sandbox list --metadata k=v --state suspended --index-table NAME`
(M14): el flag construye un `DynamoDbIndex` sobre la sesión de la CLI y el
listado une `list-microvms` con sus filas sin sondear ningún endpoint; sin
el flag la CLI no construye ningún cliente `dynamodb`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito import DynamoDbIndex
from rayito._index import record_for
from rayito.cli import sandbox as sandbox_cli
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_dynamodb import TABLE, fake_index
from .conftest import STARTED_AT, FakeControlPlane, list_item, sandbox_info


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> DynamoDbIndex:
    built, _, _ = fake_index(now=STARTED_AT.timestamp() + 60)
    for sandbox_id, user in (("microvm-a", "42"), ("microvm-b", "42"), ("microvm-c", "7")):
        built.put(record_for(sandbox_info(sandbox_id), {"user": user}))
    tables: list[str] = []

    def factory(table_name: str, **kwargs: Any) -> DynamoDbIndex:
        tables.append(table_name)
        assert set(kwargs) == {"session", "region"}
        return built

    monkeypatch.setattr(sandbox_cli, "DynamoDbIndex", factory)
    built.tables = tables  # type: ignore[attr-defined]
    return built


def test_index_table_lists_suspended_sandboxes_by_metadata(
    runner: CliRunner, clients: Clients, fake_plane: FakeControlPlane, index: DynamoDbIndex
) -> None:
    fake_plane.items = [
        list_item("microvm-a", "SUSPENDED"),
        list_item("microvm-b", "RUNNING"),
        list_item("microvm-c", "SUSPENDED"),
        list_item("microvm-d", "SUSPENDED"),
    ]
    result = runner.invoke(
        app,
        [
            "--json",
            "sandbox",
            "list",
            "--metadata",
            "user=42",
            "--state",
            "suspended",
            "--index-table",
            TABLE,
        ],
        obj=clients,
    )
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [(row["sandbox_id"], row["state"]) for row in rows] == [("microvm-a", "SUSPENDED")]
    assert rows[0]["metadata"] == {"user": "42"}
    assert index.tables == [TABLE]  # type: ignore[attr-defined]
    assert fake_plane.tokens == []


def test_without_the_flag_the_list_is_unchanged_and_builds_no_index(
    runner: CliRunner, clients: Clients, fake_plane: FakeControlPlane, index: DynamoDbIndex
) -> None:
    fake_plane.items = [list_item("microvm-a", "SUSPENDED")]
    result = runner.invoke(app, ["--json", "sandbox", "list"], obj=clients)
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [row["sandbox_id"] for row in rows] == ["microvm-a"]
    assert "metadata" not in rows[0]
    assert index.tables == []  # type: ignore[attr-defined]


def test_unknown_state_is_a_usage_error(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(app, ["sandbox", "list", "--state", "sleeping"], obj=clients)
    assert result.exit_code == 2


def test_all_states_does_not_combine_with_the_index(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(
        app, ["sandbox", "list", "--all-states", "--index-table", TABLE], obj=clients
    )
    assert result.exit_code == 2
