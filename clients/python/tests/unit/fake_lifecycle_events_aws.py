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
        self.calls.append("query")
        values = kwargs["ExpressionAttributeValues"]
        pk_key = ":pk"
        wanted_pk = values[pk_key]
        index_name = kwargs.get("IndexName")
        ascending = kwargs.get("ScanIndexForward", True)
        if index_name == "gsi1":
            items = [item for item in self.items.values() if item.get("gsi1pk") == wanted_pk]
            items.sort(key=lambda item: item["gsi1sk"], reverse=not ascending)
        else:
            items = [item for (pk, _sk), item in self.items.items() if pk == wanted_pk]
            items.sort(key=lambda item: item["sk"], reverse=not ascending)
        limit = kwargs.get("Limit")
        return {"Items": items[:limit] if limit else items}


@dataclass
class FakeDynamoResource:
    table: FakeTable

    def Table(self, name: str) -> FakeTable:
        return self.table


@dataclass
class FakeSecretsClient:
    secrets: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
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
