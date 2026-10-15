"""Fakes de los clientes de AWS de la pila `dashboard`: cada uno anota sus
llamadas para comprobar qué se llamó y, sobre todo, qué no."""

from __future__ import annotations

from typing import Any


class FakeDynamo:
    """Cliente DynamoDB de bajo nivel en memoria con la semántica de
    `attribute_not_exists(pk)`: repetir una clave falla como en AWS."""

    def __init__(self, *, fail: bool = False) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail = fail

    def _put(self, item: dict[str, Any], condition: str | None) -> None:
        key = (item["pk"]["S"], item.get("sk", {}).get("S", ""))
        if self.fail:
            raise RuntimeError("dynamodb caído")
        if condition is not None and key in self.items:
            raise RuntimeError("ConditionalCheckFailed")
        self.items[key] = item

    def put_item(self, *, TableName: str, Item: dict[str, Any], **kwargs: Any) -> None:
        self.calls.append("put_item")
        self._put(Item, kwargs.get("ConditionExpression"))

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> None:
        self.calls.append("transact_write_items")
        if self.fail:
            raise RuntimeError("dynamodb caído")
        staged = dict(self.items)
        try:
            for entry in TransactItems:
                put = entry["Put"]
                self._put(put["Item"], put.get("ConditionExpression"))
        except RuntimeError:
            self.items = staged
            raise

    def update_item(self, *, TableName: str, Key: dict[str, Any], **kwargs: Any) -> None:
        """El contador `ADD used :one` con la condición `used < :limit`."""
        from botocore.exceptions import ClientError

        self.calls.append("update_item")
        if self.fail:
            raise RuntimeError("dynamodb caído")
        values = kwargs["ExpressionAttributeValues"]
        row = self.items.setdefault((Key["pk"]["S"], ""), {"pk": Key["pk"]})
        used = int(row.get("used", {"N": "0"})["N"])
        if used >= int(values[":limit"]["N"]):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        row["used"] = {"N": str(used + 1)}
        row["expires_at"] = values[":expires"]

    def get_item(self, *, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("get_item")
        found = self.items.get((Key["pk"]["S"], ""))
        return {"Item": found} if found is not None else {}


class FakeSsm:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str) -> dict[str, Any]:
        self.calls.append(Name)
        if Name not in self.values:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.values[Name]}}


class FakeControlPlane:
    """`ManagementPlane` mínimo: un inventario fijo y las acciones anotadas."""

    def __init__(self, ids: tuple[str, ...] = (), region: str = "us-east-1") -> None:
        from datetime import UTC, datetime

        from rayito._models import SandboxListItem

        started = datetime(2026, 10, 13, tzinfo=UTC)
        arn = f"arn:aws:lambda:{region}:123456789012:microvm-image:rayito-base"
        self.items = [
            SandboxListItem(
                sandbox_id=sandbox_id,
                state="RUNNING",
                template=arn,
                template_version="1.0",
                started_at=started,
            )
            for sandbox_id in ids
        ]
        self.acted: list[tuple[str, str]] = []
        self.listed = 0
        self._region = region

    @property
    def region(self) -> str:
        return self._region

    def resolve_template_arn(self, template: str) -> str:
        return template

    def list_microvms(self, **_: Any) -> Any:
        self.listed += 1
        return iter(self.items)

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> Any:
        from rayito._models import ImageVersionInfo

        return ImageVersionInfo(minimum_memory_mib=2048)

    def terminate_microvm(self, sandbox_id: str) -> bool:
        self.acted.append(("terminate_microvm", sandbox_id))
        return True

    def suspend_microvm(self, sandbox_id: str) -> bool:
        self.acted.append(("suspend_microvm", sandbox_id))
        return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        self.acted.append(("resume_microvm", sandbox_id))
        return True
