"""Dominio puro del panel (`rayito._dashboard._domain`): los vectores
compartidos con TypeScript (`testdata/dashboard/`), la caché con caducidad y
la frontera hexagonal (el dominio y los puertos no importan infraestructura)."""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from rayito._dashboard import _domain, _ports
from rayito._dashboard._domain import (
    AppliedQuota,
    CostGroup,
    CostPeriod,
    CostQueryResult,
    TtlCache,
    build_quota_report,
    estimate_compute_usd,
    estimate_sandbox_cost,
    fold_billed_cost,
    microvm_component,
    plan_bulk,
    quota_default,
)
from rayito._limits import QUOTA_DEFAULTS, QUOTA_NAMES

from .conftest import FakeClock

VECTORS = Path(__file__).resolve().parents[4] / "testdata" / "dashboard"
FETCHED_AT = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_IMPORTS = ("boto3", "botocore", "grpc", "rayito.v1", "urllib3", "httpx", "requests")


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((VECTORS / name).read_text(encoding="utf-8"))
    return loaded


def applied_for(case: dict[str, Any]) -> list[AppliedQuota]:
    region = case["region"]
    applied = [
        AppliedQuota(
            code=code,
            name=QUOTA_NAMES[code],
            value=float(case["override"].get(code, quota_default(code, region))),
            adjustable=True,
        )
        for code in QUOTA_NAMES
        if code not in case["drop"]
    ]
    assert applied
    return applied


def memory_samples(fleet: list[dict[str, Any]]) -> list[int | None]:
    return [group["memoryMiB"] for group in fleet for _ in range(group["count"])]


@pytest.mark.parametrize(
    "case", load("quota-report-vectors.json")["cases"], ids=lambda c: c["name"]
)
def test_quota_report_vectors(case: dict[str, Any]) -> None:
    report = build_quota_report(case["region"], applied_for(case), memory_samples(case["fleet"]))
    expected = case["expected"]
    memory = report.memory
    assert memory.allocated_gb == pytest.approx(expected["memory"]["allocatedGb"])
    assert memory.quota_gb == expected["memory"]["quotaGb"]
    if expected["memory"]["fraction"] is None:
        assert memory.fraction is None
    else:
        assert memory.fraction == pytest.approx(expected["memory"]["fraction"])
    assert memory.level == expected["memory"]["level"]
    assert memory.sandboxes == expected["memory"]["sandboxes"]
    assert memory.unresolved == expected["memory"]["unresolved"]
    assert sorted((w.kind, w.code) for w in report.warnings) == sorted(
        (w["kind"], w["code"]) for w in expected["warnings"]
    )
    assert report.applied_rate("SuspendMicrovm") == expected["suspendRate"]


def test_a_warning_carries_the_increase_link_and_no_account_data() -> None:
    case = load("quota-report-vectors.json")["cases"][1]
    report = build_quota_report(case["region"], applied_for(case), memory_samples(case["fleet"]))
    warning = next(w for w in report.warnings if w.kind == "memory_high")
    assert warning.increase_url.startswith(
        "https://eu-west-1.console.aws.amazon.com/servicequotas/"
    )
    assert warning.increase_url.endswith("/services/lambda/quotas/L-CD1C0CC4")
    assert "80 %" in warning.message


def test_the_catalog_is_complete_and_tps_pairs_rate_with_burst() -> None:
    assert set(QUOTA_NAMES) == set(QUOTA_DEFAULTS)
    case = load("quota-report-vectors.json")["cases"][0]
    report = build_quota_report("us-east-1", applied_for(case), [])
    suspend = next(limit for limit in report.tps if limit.operation == "SuspendMicrovm")
    assert (suspend.rate, suspend.burst) == (2, 2)
    assert {limit.operation for limit in report.tps} == {
        "RunMicrovm",
        "ResumeMicrovm",
        "SuspendMicrovm",
        "TerminateMicrovm",
        "GetMicrovm",
        "CreateMicrovmAuthToken",
        "CreateMicrovmShellAuthToken",
    }


def period(raw: dict[str, Any]) -> CostPeriod:
    return CostPeriod(
        start=date.fromisoformat(raw["start"]),
        end=date.fromisoformat(raw["end"]),
        estimated=raw["estimated"],
        groups=tuple(
            CostGroup(g["usageType"], g["costUsd"], g["quantity"], g["unit"]) for g in raw["groups"]
        ),
    )


@pytest.mark.parametrize("case", load("cost-vectors.json")["billed"], ids=lambda c: c["name"])
def test_billed_cost_vectors(case: dict[str, Any]) -> None:
    result = CostQueryResult(tuple(period(p) for p in case["periods"]), requests=1)
    cost = fold_billed_cost(
        result, start=date(2026, 10, 1), end=date(2026, 11, 2), region=None, fetched_at=FETCHED_AT
    )
    expected = case["expected"]
    assert [
        (line.usage_type, line.component, line.quantity, line.unit, line.cost_usd)
        for line in cost.lines
    ] == [
        (line["usageType"], line["component"], line["quantity"], line["unit"], line["costUsd"])
        for line in expected["lines"]
    ]
    assert cost.total_usd == expected["totalUsd"]
    assert cost.partial is expected["partial"]
    assert cost.label == "billed"


@pytest.mark.parametrize("case", load("cost-vectors.json")["estimates"])
def test_estimated_compute_vectors(case: dict[str, Any]) -> None:
    assert estimate_compute_usd(case["memoryMiB"], case["seconds"]) == pytest.approx(
        case["usd"], abs=1e-6
    )


def test_microvm_component_ignores_other_lambda_usage_types() -> None:
    assert microvm_component("Lambda-MicroVM-vCPU-Second-ARM") == "vcpu"
    assert microvm_component("USE2-Lambda-MicroVM-Snapshot-Write-GB") == "snapshot-write"
    assert microvm_component("Request") is None
    assert microvm_component("Lambda-GB-Second-ARM") is None


def test_only_running_sandboxes_with_known_memory_get_an_estimate() -> None:
    started = datetime(2026, 10, 13, 11, 0, 0, tzinfo=UTC)
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
    running = estimate_sandbox_cost("a", "RUNNING", started, 2048, now)
    assert running.active_seconds == 3600
    assert running.cost_usd == pytest.approx(0.1261, abs=1e-6)
    assert running.label == "estimated"
    for state, memory in (("SUSPENDED", 2048), ("RUNNING", None)):
        skipped = estimate_sandbox_cost("b", state, started, memory, now)
        assert (skipped.active_seconds, skipped.cost_usd) == (None, None)
        assert skipped.label == "estimated"


def test_a_clock_before_the_start_never_gives_a_negative_estimate() -> None:
    started = datetime(2026, 10, 13, 13, 0, 0, tzinfo=UTC)
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
    assert estimate_sandbox_cost("a", "RUNNING", started, 1024, now).cost_usd == 0


@pytest.mark.parametrize("case", load("bulk-plan-vectors.json")["cases"])
def test_bulk_plan_vectors(case: dict[str, Any]) -> None:
    plan = plan_bulk(case["action"], case["count"], case["appliedRate"])
    assert plan.tps == case["tps"]
    assert plan.estimated_seconds == pytest.approx(case["estimatedSeconds"])


def test_ttl_cache_expires_entries_after_the_ttl() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(10, clock)
    assert cache.get("a") is None
    cache.put("a", 1)
    clock.advance(9.9)
    assert cache.get("a") == 1
    clock.advance(0.1)
    assert cache.get("a") is None


def imported_modules(module: Any) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", [_domain, _ports])
def test_domain_and_ports_import_no_infrastructure(module: Any) -> None:
    for name in imported_modules(module):
        assert not name.startswith(FORBIDDEN_IMPORTS), f"{module.__name__} importa {name}"
