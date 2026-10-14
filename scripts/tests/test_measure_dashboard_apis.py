"""Pruebas sin red de `scripts/measure/dashboard_apis.py` (tareas 1.1 y 1.2 del
cambio `dashboard`): el escalado de TPS, la lectura de cuotas, el retraso de Cost
Explorer, la redacción del informe y que el coste facturado sólo se lea con
`--billed`. El adaptador real de boto3 lo ejercita quien corre el script."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "measure" / "dashboard_apis.py"
)
TODAY = date(2026, 10, 14)


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("dashboard_apis_measure", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


measure = load_module()


class FakePort:
    """Throttles an operation once more than `limit` calls land in one burst
    (a burst is one `_fire`, the calls of a second)."""

    def __init__(
        self,
        *,
        microvm_limit: int | None = None,
        quotas: list[dict[str, Any]] | None = None,
        cost_pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.limit = microvm_limit
        self.lock = threading.Lock()
        self.calls = {"ListMicrovms": 0, "ListMicrovmImages": 0}
        self.in_burst = 0
        self.quota_rows = quotas or []
        self.pages = list(cost_pages or [])
        self.cost_calls = 0

    def _list(self, name: str) -> None:
        with self.lock:
            self.calls[name] += 1
            count = self.calls[name]
        if (
            self.limit is not None
            and name == "ListMicrovms"
            and count % 100 > self.limit
        ):
            raise measure.Throttled

    def list_microvms(self) -> None:
        self._list("ListMicrovms")

    def list_microvm_images(self) -> None:
        self._list("ListMicrovmImages")

    def service_quotas(self) -> list[dict[str, Any]]:
        return self.quota_rows

    def daily_cost(self, start: date, end: date, token: str | None) -> dict[str, Any]:
        self.cost_calls += 1
        return self.pages.pop(0) if self.pages else {"ResultsByTime": []}


def no_sleep(_: float) -> None:
    return None


class FakeClock:
    """A clock that only `sleep` moves, and a `sleep` that waits until the call
    launched just before it has really started, so the timestamps the calls
    record are deterministic despite the worker threads."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.started = 0
        self.stamps: list[float] = []
        self.lock = threading.Lock()

    def clock(self) -> float:
        return self.now

    def record_call(self) -> list[float]:
        with self.lock:
            self.stamps.append(self.now)
            self.started += 1
            return list(self.stamps)

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        while self.started < self.sleeps:
            threading.Event().wait(0.0005)
        self.now += seconds


def throttling_above(limit: int, fake: FakeClock) -> Any:
    """Throttles a call when more than `limit` calls started within the last second."""

    def call() -> None:
        stamps = fake.record_call()
        recent = [stamp for stamp in stamps if stamp > fake.now - 1 + 1e-9]
        if len(recent) > limit:
            raise measure.Throttled

    return call


def test_tps_climbs_until_the_first_throttled_step_and_stops_there() -> None:
    fake = FakeClock()
    result = measure.measure_tps(
        "ListMicrovms",
        throttling_above(4, fake),
        rates=(1, 2, 5, 10),
        step_seconds=2,
        sleep=fake.sleep,
        clock=fake.clock,
    )

    assert result.highest_clean_requested_rate == 2
    assert result.first_throttled_requested_rate == 5
    assert result.throttled > 0
    assert result.calls == (1 + 2 + 5) * 2


def test_a_stray_throttled_call_does_not_end_the_climb() -> None:
    calls = [0]

    def one_stray_throttle() -> None:
        calls[0] += 1
        if calls[0] == 3:
            raise measure.Throttled

    result = measure.measure_tps(
        "ListMicrovms",
        one_stray_throttle,
        rates=(10, 20),
        step_seconds=2,
        sleep=no_sleep,
    )

    assert result.highest_clean_requested_rate == 20
    assert result.first_throttled_requested_rate is None
    assert result.throttled == 1


def test_the_calls_of_a_step_are_spread_over_the_second_not_fired_at_once() -> None:
    fake = FakeClock()
    measure.measure_tps(
        "ListMicrovms",
        throttling_above(10, fake),
        rates=(4,),
        step_seconds=1,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert fake.stamps == [0.0, 0.25, 0.5, 0.75]


def test_the_achieved_rate_is_what_was_sent_over_the_elapsed_time() -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds * 2

    result = measure.measure_tps(
        "ListMicrovms",
        lambda: None,
        rates=(4,),
        step_seconds=1,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert result.highest_clean_requested_rate == 4
    assert result.achieved_rate_at_highest_clean == 2.0


def test_tps_without_throttling_reports_the_top_rate_and_no_throttled_rate() -> None:
    result = measure.measure_tps(
        "ListMicrovmImages", lambda: None, rates=(1, 3), step_seconds=1, sleep=no_sleep
    )
    assert (
        result.highest_clean_requested_rate,
        result.first_throttled_requested_rate,
        result.throttled,
    ) == (
        3,
        None,
        0,
    )


def test_a_non_throttling_error_aborts_the_measurement() -> None:
    def broken() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        measure.measure_tps(
            "ListMicrovms", broken, rates=(1,), step_seconds=1, sleep=no_sleep
        )


def test_only_microvm_quotas_are_reported_with_what_they_publish() -> None:
    port = FakePort(
        quotas=[
            {"QuotaName": "Concurrent executions", "Value": 1000.0},
            {
                "QuotaName": "MicroVM memory (GB)",
                "Value": 1024.0,
                "Adjustable": True,
                "UsageMetric": {
                    "MetricNamespace": "AWS/Usage",
                    "MetricName": "ResourceCount",
                    "MetricStatisticRecommendation": "Maximum",
                },
            },
            {"QuotaName": "MicroVM TPS", "Value": 5.0},
        ]
    )
    quotas = measure.microvm_quotas(port)
    assert [quota["name"] for quota in quotas] == ["MicroVM memory (GB)", "MicroVM TPS"]
    assert quotas[0]["publishesUsageMetric"] is True
    assert quotas[0]["usageMetric"] == {
        "namespace": "AWS/Usage",
        "name": "ResourceCount",
        "statistic": "Maximum",
    }
    assert (
        quotas[1]["publishesUsageMetric"] is False and quotas[1]["usageMetric"] is None
    )


def day(
    start: str, usage_type: str, amount: str, *, estimated: bool = False
) -> dict[str, Any]:
    return {
        "TimePeriod": {"Start": start},
        "Estimated": estimated,
        "Groups": [
            {
                "Keys": [usage_type],
                "Metrics": {
                    "UnblendedCost": {"Amount": amount},
                    "UsageQuantity": {"Amount": amount},
                },
            }
        ],
    }


def test_the_lag_is_the_age_of_the_newest_day_with_microvm_usage() -> None:
    page = {
        "ResultsByTime": [
            day("2026-10-10", "EUW1-Lambda-MicroVM-Memory-GB-Second-ARM", "3.0"),
            day("2026-10-12", "Lambda-MicroVM-vCPU-Second-ARM", "1.5", estimated=True),
            day("2026-10-13", "Lambda-MicroVM-vCPU-Second-ARM", "0"),
            day("2026-10-13", "Request", "9"),
        ]
    }
    port = FakePort(cost_pages=[page])
    cost = measure.cost_lag(port, TODAY)
    assert cost["newestDayWithUsageLagDays"] == 2
    assert cost["newestDayWasEstimated"] is True
    assert cost["usageTypes"] == [
        "EUW1-Lambda-MicroVM-Memory-GB-Second-ARM",
        "Lambda-MicroVM-vCPU-Second-ARM",
    ]
    assert cost["billedRequests"] == 1 and cost["billedUsd"] == 0.01


def test_no_microvm_usage_gives_no_lag() -> None:
    cost = measure.cost_lag(FakePort(cost_pages=[{"ResultsByTime": []}]), TODAY)
    assert cost["newestDayWithUsageLagDays"] is None and cost["usageTypes"] == []


def test_the_billed_requests_are_capped_even_if_the_token_never_ends() -> None:
    page = {"ResultsByTime": [], "NextPageToken": "again"}
    port = FakePort(cost_pages=[page] * 10)
    cost = measure.cost_lag(port, TODAY)
    assert port.cost_calls == measure.MAX_BILLED_REQUESTS == cost["billedRequests"]


def test_without_billed_cost_explorer_is_never_called() -> None:
    port = FakePort()
    report = measure.run_measurement(
        port, "us-east-1", billed=False, today=TODAY, sleep=no_sleep
    )
    assert port.cost_calls == 0
    assert "--billed" in report.cost["skipped"]
    assert [result.operation for result in report.tps] == [
        "ListMicrovms",
        "ListMicrovmImages",
    ]


def test_with_billed_the_cost_is_measured() -> None:
    port = FakePort(cost_pages=[{"ResultsByTime": []}])
    report = measure.run_measurement(
        port, "us-east-1", billed=True, today=TODAY, sleep=no_sleep
    )
    assert port.cost_calls == 1 and report.cost["billedRequests"] == 1


def test_the_report_redacts_account_ids_and_arns() -> None:
    text = "arn:aws:iam::123456789012:role/x and 123456789012 and fine 12345"
    redacted = measure.redact(text)
    assert "123456789012" not in redacted and "arn:aws" not in redacted
    assert "fine 12345" in redacted


def test_plan_prints_the_costs_and_makes_no_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(_: str) -> Any:
        raise AssertionError("plan must not build an AWS client")

    assert measure.main(["plan"], port_factory=forbidden) == 0
    out = capsys.readouterr().out
    assert "--billed" in out and "$0.01" in out and "ListMicrovms" in out
    assert "account-wide" in out and "test account" in out


def test_run_needs_a_region_and_reads_it_from_the_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert measure.main(["run"], port_factory=lambda _: FakePort(), environ={}) == 2
    regions: list[str] = []

    def factory(region: str) -> FakePort:
        regions.append(region)
        return FakePort()

    code = measure.main(
        ["run"],
        port_factory=factory,
        environ={"AWS_REGION": "eu-west-1"},
        today=lambda: TODAY,
        sleep=no_sleep,
    )
    assert code == 0 and regions == ["eu-west-1"]
    assert json.loads(capsys.readouterr().out)["region"] == "eu-west-1"


def test_the_script_names_no_account_id() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert not measure.ACCOUNT_ID_PATTERN.search(source)


def test_the_real_adapter_never_retries_so_throttling_is_visible() -> None:
    pytest.importorskip("boto3")
    port = measure.BotoPort("us-east-1")

    retries = port._microvms.meta.config.retries
    assert retries["total_max_attempts"] == measure.SINGLE_ATTEMPT
