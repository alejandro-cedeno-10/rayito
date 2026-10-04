"""Puts `infra/lambdas/events_webhooks/` on `sys.path` so its modules
(`domain`, `ports`, `adapters`, `handlers`) import the same way they do
inside the Lambda runtime (where they are the zip's own root), without a
package install step. Also holds the in-memory `boto3` Table fake shared
by every test that exercises `adapters.dynamodb.DynamoDbStore` or a
handler built on top of it, so there is exactly one fake to keep in sync
with the real `boto3` resource API.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

LAMBDA_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = LAMBDA_ROOT.parents[1] / "events-webhooks.yaml"
if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))


class _ConditionalCheckFailedException(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = _ConditionalCheckFailedException


class _Client:
    exceptions = _Exceptions


class FakeTable:
    """Minimal in-memory stand-in for `boto3.resource("dynamodb").Table(...)`
    — just enough of `put_item`/`scan`/`query` for `DynamoDbStore`
    (single-table design: `pk`/`sk`, no real indexes). Conditional writes
    and the scan filter are evaluated for exactly the named expressions
    `adapters.dynamodb` declares; any other expression is a test bug."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.meta = type("Meta", (), {"client": _Client})()

    def put_item(
        self,
        *,
        Item: dict[str, Any],
        ConditionExpression: str | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> None:
        key = (Item["pk"], Item["sk"])
        existing = self.items.get(key)
        if ConditionExpression is not None and not _condition_holds(
            ConditionExpression, existing, ExpressionAttributeValues or {}
        ):
            raise _ConditionalCheckFailedException()
        self.items[key] = dict(Item)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        from adapters import dynamodb as adapter

        assert kwargs["FilterExpression"] == adapter.OPEN_STATE_FILTER
        values = kwargs["ExpressionAttributeValues"]
        items = [
            item
            for (pk, _sk), item in self.items.items()
            if pk.startswith(values[":prefix"]) and item.get("last_kind") != values[":killed"]
        ]
        return {"Items": items}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        wanted_pk = kwargs["ExpressionAttributeValues"][":pk"]
        items = [item for (pk, _sk), item in self.items.items() if pk == wanted_pk]
        return {"Items": items}


def _condition_holds(
    expression: str, existing: dict[str, Any] | None, values: dict[str, Any]
) -> bool:
    from adapters import dynamodb as adapter

    if existing is None:
        return True  # every condition starts with `attribute_not_exists(pk) OR`
    if expression == adapter.IF_ABSENT:
        return False
    if expression == adapter.IF_NOT_DELIVERED:
        return bool(existing.get("delivery_status") != values[":delivered"])
    if expression == adapter.IF_OPEN:
        return bool(existing.get("last_kind") != values[":killed"])
    if expression == adapter.IF_OPEN_AND_NOT_NEWER:
        return bool(
            existing.get("last_kind") != values[":killed"]
            and existing["last_seen_ms"] <= values[":seen"]
        )
    raise AssertionError(f"condition not modelled by FakeTable: {expression}")


class FakeDynamoResource:
    """Stands in for `boto3.resource("dynamodb")`: `.Table(name)` always
    hands back the one `FakeTable` it was built with, same as every table
    name in these tests resolving to the same in-memory store."""

    def __init__(self, table: FakeTable) -> None:
        self._table = table

    def Table(self, _name: str) -> FakeTable:
        return self._table


class FakeContext:
    """A Lambda `context` whose remaining time only moves when the test
    says so (`spend`), so time-budget decisions are deterministic."""

    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms

    def spend(self, seconds: float) -> None:
        self.remaining_ms -= int(seconds * 1000)


def template_environment(function_logical_id: str) -> dict[str, str]:
    """The `Environment.Variables` names `infra/events-webhooks.yaml`
    declares for one function, each with a placeholder value: handler tests
    run against exactly this environment, never a hand-picked one."""
    variables = template_resources()[function_logical_id]["Properties"]["Environment"]["Variables"]
    return {name: f"test-{name.lower()}" for name in variables}


def template_resources() -> dict[str, Any]:
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    def _short_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node, deep=True)}
        return {suffix: loader.construct_mapping(node, deep=True)}

    _Loader.add_multi_constructor("!", _short_tag)
    document = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)
    return dict(document["Resources"])
