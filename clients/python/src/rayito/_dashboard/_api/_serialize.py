"""Del dominio a los documentos JSON de `/api/v1` (claves en `camelCase`).
Funciones puras: ningún modelo del dominio sale por la API sin pasar por
aquí, así que un campo nuevo del dominio (un `envs`, un token) no se filtra
sin querer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rayito._dashboard._api._contract import iso_utc
from rayito._dashboard._domain import (
    BilledCost,
    BulkPlan,
    CostEstimate,
    ImageSummary,
    ImageVersionSummary,
    QuotaReport,
    SandboxView,
)
from rayito._lifecycle_events._domain import EventRecord
from rayito._limits import QUOTA_CRITICAL_FRACTION, QUOTA_WARNING_FRACTION

MILLIS_PER_SECOND = 1000


def sandbox_json(view: SandboxView) -> dict[str, Any]:
    return {
        "sandboxId": view.sandbox_id,
        "state": view.state,
        "image": view.template_name,
        "imageArn": view.template,
        "imageVersion": view.template_version,
        "startedAt": iso_utc(view.started_at),
        "memoryMib": view.memory_mib,
    }


def image_json(image: ImageSummary) -> dict[str, Any]:
    return {
        "name": image.name,
        "imageArn": image.image_arn,
        "state": image.state,
        "latestActiveVersion": image.latest_active_version,
        "latestFailedVersion": image.latest_failed_version,
        "createdAt": iso_utc(image.created_at),
    }


def image_version_json(version: ImageVersionSummary) -> dict[str, Any]:
    return {
        "imageVersion": version.image_version,
        "state": version.state,
        "status": version.status,
        "baseImageVersion": version.base_image_version,
        "artifact": version.artifact,
        "memoryMib": version.memory_mib,
        "createdAt": iso_utc(version.created_at),
    }


def quotas_json(report: QuotaReport) -> dict[str, Any]:
    memory = report.memory
    return {
        "region": report.region,
        "entries": [
            {
                "code": entry.code,
                "name": entry.name,
                "kind": entry.kind,
                "operation": entry.operation,
                "default": entry.default,
                "value": entry.value,
                "adjustable": entry.adjustable,
                "belowDefault": entry.below_default,
                "increaseUrl": entry.increase_url,
            }
            for entry in report.entries
        ],
        "memory": {
            "allocatedGb": memory.allocated_gb,
            "quotaGb": memory.quota_gb,
            "fraction": memory.fraction,
            "level": memory.level,
            "sandboxes": memory.sandboxes,
            "unresolved": memory.unresolved,
        },
        "tps": [
            {"operation": limit.operation, "rate": limit.rate, "burst": limit.burst}
            for limit in report.tps
        ],
        "warnings": [
            {
                "kind": warning.kind,
                "code": warning.code,
                "message": warning.message,
                "increaseUrl": warning.increase_url,
            }
            for warning in report.warnings
        ],
        "thresholds": {
            "warning": QUOTA_WARNING_FRACTION,
            "critical": QUOTA_CRITICAL_FRACTION,
        },
    }


def estimate_json(estimate: CostEstimate) -> dict[str, Any]:
    return {
        "label": estimate.label,
        "pricingRegion": estimate.pricing_region,
        "basis": estimate.basis,
        "totalUsd": estimate.total_usd,
        "items": [
            {
                "sandboxId": item.sandbox_id,
                "state": item.state,
                "memoryMib": item.memory_mib,
                "activeSeconds": item.active_seconds,
                "costUsd": item.cost_usd,
            }
            for item in estimate.items
        ],
    }


def billed_json(billed: BilledCost) -> dict[str, Any]:
    return {
        "label": billed.label,
        "start": billed.start.isoformat(),
        "end": billed.end.isoformat(),
        "region": billed.region,
        "totalUsd": billed.total_usd,
        "partial": billed.partial,
        "requests": billed.requests,
        "fetchedAt": iso_utc(billed.fetched_at),
        "lines": [
            {
                "usageType": line.usage_type,
                "component": line.component,
                "quantity": line.quantity,
                "unit": line.unit,
                "costUsd": line.cost_usd,
            }
            for line in billed.lines
        ],
    }


def event_json(event: EventRecord) -> dict[str, Any]:
    occurred_at = datetime.fromtimestamp(event.occurred_at_ms / MILLIS_PER_SECOND, UTC)
    return {
        "eventId": event.event_id,
        "sandboxId": event.sandbox_id,
        "type": event.type,
        "killReason": event.kill_reason,
        "generation": event.generation,
        "occurredAt": iso_utc(occurred_at),
        "imageArn": event.image_arn,
        "imageVersion": event.image_version,
    }


def plan_json(plan: BulkPlan) -> dict[str, Any]:
    return {
        "action": plan.action,
        "operation": plan.operation,
        "count": plan.count,
        "tps": plan.tps,
        "estimatedSeconds": plan.estimated_seconds,
    }
