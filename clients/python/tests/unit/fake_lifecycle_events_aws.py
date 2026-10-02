"""Fakes en memoria para probar `LifecycleEvents`/`AsyncLifecycleEvents` sin
AWS: una tabla DynamoDB mínima (put/delete/query) y un lector de Secrets
Manager. Nuevo para `m15-events-webhooks`; no toca `fake_stacks.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeTable:
    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def put_item(self, *, Item: dict[str, Any]) -> None:
        self.calls.append("put_item")
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, *, Key: dict[str, Any]) -> None:
        self.calls.append("delete_item")
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Enough of real `Query` to exercise `_dynamodb.query_events`'s
        pagination: `Limit` caps the *raw* page before `FilterExpression`
        runs (the real DynamoDB behaviour `query_events` works around), and
        `ExclusiveStartKey`/`LastEvaluatedKey` carry an opaque cursor
        across calls. Only ever parses the one `FilterExpression` shape
        `query_events` builds (`"kind IN (:k0, :k1, ...)"`) — this is a
        fake for this one caller, not a DynamoDB expression evaluator."""
        self.calls.append("query")
        values = kwargs["ExpressionAttributeValues"]
        wanted_pk = values[":pk"]
        index_name = kwargs.get("IndexName")
        ascending = kwargs.get("ScanIndexForward", True)
        if index_name == "gsi1":
            items = [item for item in self.items.values() if item.get("gsi1pk") == wanted_pk]
            sort_key = "gsi1sk"
        else:
            items = [item for (pk, _sk), item in self.items.items() if pk == wanted_pk]
            sort_key = "sk"
        items.sort(key=lambda item: item[sort_key], reverse=not ascending)

        start_after = kwargs.get("ExclusiveStartKey")
        if start_after is not None:
            start_index = next(
                i for i, item in enumerate(items) if item[sort_key] == start_after[sort_key]
            )
            items = items[start_index + 1 :]

        limit = kwargs.get("Limit")
        page = items[:limit] if limit else items
        last_evaluated_key = (
            {sort_key: page[-1][sort_key]} if limit and len(items) > len(page) else None
        )

        filter_expression = kwargs.get("FilterExpression")
        if filter_expression is not None:
            wanted_kinds = {
                values[token.strip()]
                for token in filter_expression.removeprefix("kind IN (").removesuffix(")").split(
                    ","
                )
            }
            page = [item for item in page if item["kind"] in wanted_kinds]

        result: dict[str, Any] = {"Items": page}
        if last_evaluated_key is not None:
            result["LastEvaluatedKey"] = last_evaluated_key
        return result


@dataclass
class FakeDynamoResource:
    table: FakeTable

    def Table(self, name: str) -> FakeTable:
        return self.table


@dataclass
class FakeSecretsClient:
    """`binary=True` answers with `SecretBinary` instead of `SecretString`,
    like a secret created with `--secret-binary`."""

    secrets: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    binary: bool = False

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if self.binary:
            return {"SecretBinary": self.secrets[SecretId]}
        return {"SecretString": self.secrets[SecretId].decode("utf-8")}


@dataclass
class FakeAwsSession:
    table: FakeTable
    secrets_client: FakeSecretsClient

    def resource(self, service_name: str, **_kwargs: Any) -> FakeDynamoResource:
        assert service_name == "dynamodb"
        return FakeDynamoResource(self.table)

    def client(self, service_name: str, **_kwargs: Any) -> FakeSecretsClient:
        assert service_name == "secretsmanager"
        return self.secrets_client
