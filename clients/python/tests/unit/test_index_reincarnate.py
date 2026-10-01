"""`reincarnate()` conserva `index=` (M14): el sucesor de un sandbox creado
con `index=DynamoDbIndex(...)` escribe su propia fila condicional y el
listado con índice lo encuentra; sin `index=` el sucesor no toca DynamoDB.
Sync y async contra el `rayd` falso y el plano de control con Stubber."""

from __future__ import annotations

from typing import Any

from rayito import AsyncSandbox, S3Prefix, Sandbox

from .conftest import SANDBOX_ID, STARTED_AT, RaydEndpoint, StubbedControlPlane, list_item
from .fake_dynamodb import TABLE, fake_index
from .test_persistence_async import create as create_async
from .test_persistence_sync import (
    BUCKET,
    SUCCESSOR_ID,
    create,
    stub_launch,
    stub_terminate,
)

METADATA = {"user": "42"}
# El reloj del índice en el arranque del VM falso: la fila no ha caducado.
NOW = STARTED_AT.timestamp()


def stub_listing(control_plane: StubbedControlPlane, *sandbox_ids: str) -> None:
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item(sid, "RUNNING") for sid in sandbox_ids]}
    )


def put_ids(api: Any) -> list[str]:
    return [put["Item"]["pk"]["S"] for put in api.calls("put_item")]


def test_reincarnate_writes_the_successor_row_and_listing_finds_it(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    index, api, _ = fake_index(now=NOW)
    original = create(
        control_plane, fake_rayd, persist=S3Prefix(BUCKET), metadata=METADATA, index=index
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate()
    try:
        assert successor._launch_options is not None
        assert successor._launch_options.index is index
        assert put_ids(api) == [SANDBOX_ID, SUCCESSOR_ID]
        last = api.calls("put_item")[-1]
        assert last["ConditionExpression"] == "attribute_not_exists(pk)"
        assert last["Item"]["metadata"] == {"M": {"user": {"S": "42"}}}

        stub_listing(control_plane, SUCCESSOR_ID)
        found = list(
            Sandbox.list(metadata=METADATA, index=index, control_plane=control_plane.plane)
        )
        assert [item.sandbox_id for item in found] == [SUCCESSOR_ID]
        (batch,) = api.calls("batch_get_item")
        assert batch["RequestItems"][TABLE]["Keys"] == [{"pk": {"S": SUCCESSOR_ID}}]
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


def test_reincarnate_without_index_never_touches_dynamodb(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    _, api, session = fake_index()
    original = create(control_plane, fake_rayd, persist=S3Prefix(BUCKET), metadata=METADATA)
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = original.reincarnate()
    try:
        assert successor._launch_options is not None
        assert successor._launch_options.index is None
        assert api.requests == []
        assert session.built == []
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        successor.kill()


async def test_async_reincarnate_writes_the_successor_row_and_listing_finds_it(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    index, api, _ = fake_index(now=NOW)
    original = await create_async(
        control_plane, fake_rayd, persist=S3Prefix(BUCKET), metadata=METADATA, index=index
    )
    stub_launch(control_plane, fake_rayd, sandbox_id=SUCCESSOR_ID)
    stub_terminate(control_plane, SANDBOX_ID)
    successor = await original.reincarnate()
    try:
        assert successor._launch_options is not None
        assert successor._launch_options.index is index
        assert put_ids(api) == [SANDBOX_ID, SUCCESSOR_ID]

        stub_listing(control_plane, SUCCESSOR_ID)
        found = await AsyncSandbox.list(
            metadata=METADATA, index=index, control_plane=control_plane.plane
        )
        assert [item.sandbox_id for item in found] == [SUCCESSOR_ID]
    finally:
        stub_terminate(control_plane, SUCCESSOR_ID)
        await successor.kill()
