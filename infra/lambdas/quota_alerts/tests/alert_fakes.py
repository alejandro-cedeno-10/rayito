"""Dobles de los puertos y de los clientes de `boto3` de la comprobación."""

from __future__ import annotations

from typing import Any

from quota_alert_check.decision import Level

from rayito._dashboard._domain import AppliedQuota, QuotaReport, build_quota_report

MEMORY_QUOTA_CODE = "L-CD1C0CC4"
MIB_PER_GB = 1024


def report_for(allocated_gb: float, quota_gb: float, *, unresolved: int = 0) -> QuotaReport:
    applied = [AppliedQuota(MEMORY_QUOTA_CODE, "memoria", quota_gb, True)]
    memory_mib: list[int | None] = [int(allocated_gb * MIB_PER_GB)]
    memory_mib.extend([None] * unresolved)
    return build_quota_report("us-east-1", applied, memory_mib)


def report_without_quota() -> QuotaReport:
    return build_quota_report("us-east-1", [], [MIB_PER_GB])


class FakeQuotas:
    def __init__(self, report: QuotaReport) -> None:
        self._report = report

    def report(self) -> QuotaReport:
        return self._report


class FakeState:
    def __init__(self, level: Level = "ok") -> None:
        self.level: Level = level
        self.saved: list[Level] = []

    def last_level(self) -> Level:
        return self.level

    def save_level(self, level: Level) -> None:
        self.level = level
        self.saved.append(level)


class FakeNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail = fail

    def publish(self, subject: str, message: str) -> None:
        if self._fail:
            raise RuntimeError("sns caído")
        self.sent.append((subject, message))


class FakeMetrics:
    def __init__(self) -> None:
        self.points: list[tuple[str, float]] = []

    def put_fraction(self, region: str, fraction: float) -> None:
        self.points.append((region, fraction))


class FakeClient:
    """Un cliente de `boto3` que sólo graba las llamadas."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return self._responses.get(name, {})

        return call
