"""Ejecuta un caso de `testdata/cli/command-cases.json` con la CLI de Python.

Un caso describe un mundo en JSON (sandboxes, imágenes, logs, pilas), los
argumentos y lo que la CLI tiene que producir. La CLI de TypeScript ejecuta
los mismos casos (`tests/unit/cli/command-cases.test.ts`), así que cada
comando se prueba contra lo que Python imprime y devuelve (ADR-027).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from rayito._cloudwatch_logs import parse_since
from rayito._custom_domain._service import CustomDomain
from rayito._lifecycle_events._service import LifecycleEvents
from rayito._stacks._model import StackStatus
from rayito._stacks._service import OptionalStacks
from rayito.cli import domain as domain_cli
from rayito.cli import events as events_cli
from rayito.cli import sandbox as sandbox_cli
from rayito.cli import stack as stack_cli
from rayito.cli._console import age
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..fake_stacks import FakeStackProvisioner
from .conftest import ACCOUNT_ID, REGION, FakeControlPlane, list_item, sandbox_info

TOKEN_PAGE_END = "end"


def instant(text: str) -> datetime:
    """El instante con su desfase tal cual: boto3 entrega la zona local, y la CLI
    tiene que escribir siempre UTC aunque el mundo traiga otro desfase."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def image_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{name}"


@dataclass
class FakeMicrovmsClient:
    """`list-microvm-images`, `list-microvm-image-versions` y `get-microvm-image-version`."""

    images: list[dict[str, Any]]
    versions: dict[str, list[dict[str, Any]]]
    artifacts: dict[str, str]

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)

    def get_microvm_image_version(self, *, imageIdentifier: str, imageVersion: str) -> Any:
        uri = self.artifacts.get(f"{imageIdentifier.rsplit(':', 1)[-1]}@{imageVersion}")
        return {} if uri is None else {"codeArtifact": {"uri": f"s3://bucket/key/{uri}"}}


@dataclass
class _Paginator:
    client: FakeMicrovmsClient
    operation: str

    def paginate(self, **params: Any) -> Iterator[dict[str, Any]]:
        if self.operation == "list_microvm_images":
            prefix = params.get("nameFilter", "")
            yield {
                "items": [
                    {
                        "name": image["name"],
                        "imageArn": image_arn(image["name"]),
                        "state": image["state"],
                        "createdAt": instant(image["createdAt"]),
                        **(
                            {"latestActiveImageVersion": image["active"]}
                            if image.get("active")
                            else {}
                        ),
                        **(
                            {"latestFailedImageVersion": image["failed"]}
                            if image.get("failed")
                            else {}
                        ),
                    }
                    for image in self.client.images
                    if image["name"].startswith(prefix)
                ]
            }
            return
        name = params["imageIdentifier"].rsplit(":", 1)[-1]
        yield {
            "items": [
                {
                    "imageVersion": version["version"],
                    "state": version["state"],
                    "status": version["status"],
                    "createdAt": instant(version["createdAt"]),
                    **(
                        {"baseImageVersion": version["baseVersion"]}
                        if version.get("baseVersion")
                        else {}
                    ),
                    **(
                        {"codeArtifact": {"uri": f"s3://bucket/key/{version['artifact']}"}}
                        if version.get("artifact")
                        else {}
                    ),
                }
                for version in self.client.versions.get(name, [])
            ]
        }


@dataclass
class FakeLogsClient:
    streams: list[str]
    events: dict[str, list[dict[str, Any]]]

    def describe_log_streams(self, **params: Any) -> dict[str, Any]:
        if params["logGroupName"] not in {"/rayito/rayito-base", "/rayito/other"}:
            raise _not_found()
        prefix = params.get("logStreamNamePrefix", "")
        return {
            "logStreams": [
                {"logStreamName": name} for name in self.streams if name.startswith(prefix)
            ]
        }

    def get_log_events(self, **params: Any) -> dict[str, Any]:
        if params.get("nextToken") == TOKEN_PAGE_END:
            return {"events": [], "nextForwardToken": TOKEN_PAGE_END}
        start = params.get("startTime", 0)
        events = [
            {"timestamp": event["timestamp"], "message": event["message"]}
            for event in self.events.get(params["logStreamName"], [])
            if event["timestamp"] >= start
        ]
        return {"events": events, "nextForwardToken": TOKEN_PAGE_END}


def _not_found() -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Op")


def build_plane(world: dict[str, Any]) -> FakeControlPlane:
    plane = FakeControlPlane()
    for item in world.get("sandboxes", []):
        plane.items.append(
            list_item(
                item["id"],
                item["state"],
                template=image_arn(item["image"]),
                version=item["version"],
                started_at=instant(item["startedAt"]),
            )
        )
    for info in world.get("infos", []):
        plane.infos[info["id"]] = sandbox_info(
            info["id"],
            info["state"],
            template=image_arn(info["image"]),
            version=info["version"],
            started_at=instant(info["startedAt"]),
            metadata=info.get("metadata"),
        )
    plane.missing.update(world.get("missing", []))
    return plane


def build_clients(world: dict[str, Any], plane: FakeControlPlane) -> Clients:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    microvms = FakeMicrovmsClient(
        images=world.get("images", []),
        versions=world.get("imageVersions", {}),
        artifacts=world.get("artifacts", {}),
    )
    logs = FakeLogsClient(
        streams=world.get("logs", {}).get("streams", []),
        events=world.get("logs", {}).get("events", {}),
    )
    return Clients(
        session=session,
        region=REGION,
        clients={"microvms": microvms, "logs": logs},
        control_plane_override=plane,
        account_id_override=ACCOUNT_ID,
    )


def build_provisioner(world: dict[str, Any]) -> FakeStackProvisioner:
    provisioner = FakeStackProvisioner()
    for name, stack in world.get("stacks", {}).items():
        provisioner.stacks[name] = StackStatus(
            name=name,
            state=stack["state"],
            outputs=dict(stack.get("outputs", {})),
            parameters=dict(stack.get("parameters", {})),
        )
    return provisioner


class _UnusedEventsGateway:
    """Estos casos sólo ejecutan lo que va por la pila: tocar DynamoDB es un error del caso."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"estos casos no usan el gateway de eventos ({name})")


def run_case(case: dict[str, Any], monkeypatch: Any) -> dict[str, Any]:
    """Lo que Python produce para `case`: código, stdout, stderr y efectos."""
    world = case.get("world", {})
    plane = build_plane(world)
    provisioner = build_provisioner(world)
    now = instant(case["now"]) if "now" in case else datetime.now(UTC)
    monkeypatch.setattr(sandbox_cli, "age", lambda started_at: age(started_at, now))
    monkeypatch.setattr(sandbox_cli, "parse_since", lambda text: parse_since(text, now))
    monkeypatch.setattr(
        stack_cli, "OptionalStacks", lambda **_: OptionalStacks(provisioner=provisioner)
    )
    monkeypatch.setattr(
        events_cli,
        "_events",
        lambda ctx, stack_name: LifecycleEvents(
            stack_name=stack_name,
            stacks=OptionalStacks(provisioner=provisioner),
            gateway=_UnusedEventsGateway(),
        ),
    )
    monkeypatch.setattr(domain_cli, "_stacks", lambda ctx: OptionalStacks(provisioner=provisioner))
    monkeypatch.setattr(
        domain_cli,
        "_domain",
        lambda ctx, public_domain, stack_name: CustomDomain(
            public_domain=public_domain, stack_name=stack_name, provisioner=provisioner
        ),
    )
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", False)
    result = CliRunner().invoke(
        app,
        case["args"],
        obj=build_clients(world, plane),
        input=case.get("stdin", ""),
    )
    return {
        "exitCode": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "effects": {
            "terminated": plane.terminated,
            "stackCalls": [call[0] for call in provisioner.calls],
            "stackParameters": provisioner.sent_parameters,
        },
    }


def dump(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
