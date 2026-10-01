"""`DynamoDbIndex`, el adaptador boto3 del índice (M14): cliente perezoso,
`PutItem` condicional, `BatchGetItem` en trozos de 100 con reintento de
`UnprocessedKeys`, errores sin el mensaje de AWS y parámetros validados
contra el modelo del servicio con `Stubber`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from rayito import DynamoDbIndex, IndexWriteException, SandboxIndexException
from rayito._index import record_for
from rayito._models import SandboxInfo
from rayito.exceptions import InvalidArgumentException

from .fake_dynamodb import TABLE, DynamoSpySession, FakeDynamoDb, fake_index

STARTED = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


NOW = STARTED.timestamp() + 60


def info(sandbox_id: str) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=sandbox_id,
        state="RUNNING",
        endpoint="example.invalid",
        template="arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        template_version="1",
        started_at=STARTED,
        maximum_duration_seconds=28_800,
    )


def test_constructing_the_index_builds_no_client() -> None:
    index, api, session = fake_index(now=NOW)
    assert session.built == []
    assert api.requests == []
    assert (
        repr(index) == "DynamoDbIndex(table_name='rayito-sandboxes', on_write_failure='terminate')"
    )


def test_the_client_is_built_once_on_first_use() -> None:
    index, _, session = fake_index(now=NOW)
    index.put(record_for(info("a"), {"k": "v"}))
    index.batch_get(["a"])
    assert session.built == ["dynamodb"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"table_name": "x"}, "table_name"),
        ({"table_name": "bad name!"}, "table_name"),
        ({"table_name": TABLE, "on_write_failure": "ignore"}, "on_write_failure"),
        ({"table_name": TABLE, "ttl_margin_seconds": -1}, "ttl_margin_seconds"),
        ({"table_name": TABLE, "ttl_margin_seconds": True}, "ttl_margin_seconds"),
    ],
)
def test_invalid_configuration_is_rejected_without_aws(kwargs: dict[str, Any], match: str) -> None:
    table_name = kwargs.pop("table_name")
    with pytest.raises(InvalidArgumentException, match=match):
        DynamoDbIndex(table_name, **kwargs)


def test_put_is_conditional_and_never_overwrites() -> None:
    index, api, _ = fake_index(now=NOW)
    record = record_for(info("a"), {"k": "v"})
    index.put(record)
    (params,) = api.calls("put_item")
    assert params["ConditionExpression"] == "attribute_not_exists(pk)"
    with pytest.raises(IndexWriteException) as excinfo:
        index.put(record)
    assert excinfo.value.aws_code == "ConditionalCheckFailedException"


def test_batch_get_splits_into_chunks_of_one_hundred() -> None:
    index, api, _ = fake_index(now=NOW)
    ids = [f"microvm-{n}" for n in range(230)]
    for sandbox_id in ids[:5]:
        index.put(record_for(info(sandbox_id), {"n": sandbox_id}))
    found = index.batch_get(ids)
    sizes = [len(call["RequestItems"][TABLE]["Keys"]) for call in api.calls("batch_get_item")]
    assert sizes == [100, 100, 30]
    assert sorted(found) == sorted(ids[:5])


def test_batch_get_with_no_ids_makes_no_call() -> None:
    index, api, session = fake_index(now=NOW)
    assert index.batch_get([]) == {}
    assert api.requests == []
    assert session.built == []


def test_unprocessed_keys_are_retried_with_bounded_backoff() -> None:
    api = FakeDynamoDb(unprocessed_rounds=2)
    index, _, _ = fake_index(api, now=NOW)
    sleeps: list[float] = []
    index._sleep = sleeps.append
    ids = [f"microvm-{n}" for n in range(8)]
    for sandbox_id in ids:
        index.put(record_for(info(sandbox_id), {}))
    assert sorted(index.batch_get(ids)) == sorted(ids)
    assert len(api.calls("batch_get_item")) == 3
    assert sleeps == [0.05, 0.1]


def test_unprocessed_keys_that_never_drain_raise_instead_of_a_partial_list() -> None:
    api = FakeDynamoDb(unprocessed_rounds=100)
    index, _, _ = fake_index(api, now=NOW)
    ids = [f"microvm-{n}" for n in range(64)]
    with pytest.raises(SandboxIndexException, match="sin procesar"):
        index.batch_get(ids)
    assert len(api.calls("batch_get_item")) == 6


def test_expired_rows_are_dropped_on_read() -> None:
    index, _, _ = fake_index(now=NOW)
    index.put(record_for(info("a"), {}, 0))
    index._clock = lambda: STARTED.timestamp() + 28_800 + 1
    assert index.batch_get(["a"]) == {}


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("ResourceNotFoundException", "no existe"),
        ("AccessDeniedException", "dynamodb:BatchGetItem"),
        ("ThrottlingException", "limitó"),
        ("InternalServerError", "InternalServerError"),
    ],
)
def test_read_errors_never_repeat_the_aws_message(code: str, fragment: str) -> None:
    index, api, _ = fake_index(now=NOW)
    api.batch_error = code
    with pytest.raises(SandboxIndexException) as excinfo:
        index.batch_get(["a"])
    assert fragment in str(excinfo.value)
    assert "secret-ish" not in str(excinfo.value)
    assert excinfo.value.aws_code == code
    assert not isinstance(excinfo.value, IndexWriteException)


def test_write_errors_are_index_write_exceptions() -> None:
    index, api, _ = fake_index(now=NOW)
    api.put_error = "AccessDeniedException"
    with pytest.raises(IndexWriteException, match="dynamodb:PutItem"):
        index.put(record_for(info("a"), {}))


def stubbed() -> tuple[DynamoDbIndex, Stubber]:
    session = boto3.session.Session(
        region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    client = session.client(
        "dynamodb", config=Config(retries={"total_max_attempts": 1, "mode": "standard"})
    )
    stubber = Stubber(client)
    index = DynamoDbIndex(TABLE, session=cast(Any, DynamoSpySession(api=client)))
    index._clock = lambda: NOW
    return index, stubber


def test_parameters_match_the_botocore_service_model() -> None:
    index, stubber = stubbed()
    record = record_for(info("a"), {"user": "42"})
    stubber.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": record.to_item(),
            "ConditionExpression": "attribute_not_exists(pk)",
        },
    )
    stubber.add_response(
        "batch_get_item",
        {"Responses": {TABLE: [record.to_item()]}, "UnprocessedKeys": {}},
        {"RequestItems": {TABLE: {"Keys": [{"pk": {"S": "a"}}], "ConsistentRead": False}}},
    )
    with stubber:
        index.put(record)
        assert index.batch_get(["a"]) == {"a": record}
    stubber.assert_no_pending_responses()
