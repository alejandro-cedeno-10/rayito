"""Carga de los vectores de `testdata/dashboard/` y construcción del mundo
falso que describen, para los tests que corren los mismos ficheros que
TypeScript."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rayito._dashboard._api._contract import LogLine
from rayito._lifecycle_events._domain import EventRecord
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
)

from .conftest import IMAGE_ARN
from .dashboard_support import FakeLogs, Rig
from .fake_dashboard import (
    FakeEvents,
    FakePlane,
    FakeQuotaSource,
    RaisingError,
    applied_defaults,
    list_item,
    sandbox_id,
)

VECTORS_DIR = Path(__file__).resolve().parents[4] / "testdata" / "dashboard"
EVENT_OCCURRED_AT_MS = 1_760_000_000_000
DEFAULT_QUOTA_MEMORY_GB = 100


def load_vectors(name: str) -> Any:
    return json.loads((VECTORS_DIR / name).read_text(encoding="utf-8"))


def expand(value: Any) -> Any:
    """Resuelve `{"$repeat": {...}}` y `{"$sandboxIds": N}` de los vectores."""
    if isinstance(value, list):
        return [expand(item) for item in value]
    if isinstance(value, dict):
        if "$repeat" in value:
            return value["$repeat"]["text"] * value["$repeat"]["times"]
        if "$sandboxIds" in value:
            return [sandbox_id(number) for number in range(value["$sandboxIds"])]
        return {key: expand(item) for key, item in value.items()}
    return value


def failure_for(spec: Mapping[str, Any]) -> Exception:
    kind = spec["kind"]
    message = spec.get("message", "")
    code = spec.get("code")
    if kind == "aws":
        return (
            RaisingError(code or "")
            if message == "mensaje que no debe salir"
            else _aws(message, code)
        )
    if kind == "authentication":
        return AuthenticationException(message, aws_code=code)
    if kind == "invalid_argument":
        return InvalidArgumentException(message, aws_code=code)
    if kind == "rate_limit":
        return RateLimitException(message, aws_code=code)
    if kind == "not_found":
        return NotFoundException(message)
    return RuntimeError(message)


def _aws(message: str, code: str | None) -> SandboxException:
    return SandboxException(message, aws_code=code)


def _event(number: int) -> EventRecord:
    return EventRecord(
        f"evt-{number}",
        sandbox_id(number),
        "killed",
        "timeout",
        1,
        EVENT_OCCURRED_AT_MS,
        IMAGE_ARN,
        "1.0",
    )


def _plane(world: Mapping[str, Any]) -> FakePlane:
    items = [
        list_item(entry["n"], state=entry.get("state", "RUNNING"))
        for entry in world.get("sandboxes", [])
    ]
    plane = FakePlane(items)
    unchanged = {tuple(text.split(":")) for text in world.get("unchanged", [])}
    plane.unchanged = {(_snake(name), sandbox_id(int(number))) for name, number in unchanged}
    for key, spec in world.get("failures", {}).items():
        name, _, number = key.partition(":")
        failure = failure_for(spec)
        if number:
            plane.fail[(_snake(name), sandbox_id(int(number)))] = failure
        else:
            plane.list_microvms = _raising(failure)  # type: ignore[method-assign]
    return plane


def _raising(failure: Exception) -> Any:
    def raise_it(**_: Any) -> Any:
        raise failure

    return raise_it


def _snake(name: str) -> str:
    return {
        "terminateMicrovm": "terminate_microvm",
        "suspendMicrovm": "suspend_microvm",
        "resumeMicrovm": "resume_microvm",
        "listMicrovms": "list_microvms",
    }[name]


def build_rig(world: Mapping[str, Any]) -> tuple[Rig, FakeLogs | None, FakeEvents | None]:
    """El `Rig` de un mundo de los vectores, con sus fakes de logs y eventos."""
    logs = None
    if world.get("logs") is not None:
        spec = world["logs"]
        logs = FakeLogs(
            [LogLine(**line) for line in spec["lines"]],
            truncated=spec.get("truncated", False),
            fallback=spec.get("fallback", False),
        )
    events = None
    if world.get("events") is not None:
        events = FakeEvents([_event(entry["n"]) for entry in world["events"]])
    quota_gb = world.get("quotaMemoryGb", DEFAULT_QUOTA_MEMORY_GB)
    rig = Rig(
        _plane(world),
        allow_actions=world.get("allowActions", False),
        cost_explorer=world.get("costExplorer", False),
        events=events,
        logs=logs,
        quota_source=FakeQuotaSource(applied_defaults(memory_gb=quota_gb)),
    )
    return rig, logs, events
