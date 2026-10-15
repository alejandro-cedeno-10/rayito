"""Measurement script for the management dashboard (`dashboard` change, tasks
1.1 and 1.2): the API facts the panel's constants assume and `AWS_API_NOTES.md`
§30 still lists as "pending to measure". It is **not** run by any gate: a person
runs it once, with their own credentials, and pastes the redacted report into
the notes.

What it measures, read-only:

1. **TPS of `ListMicrovms` and `ListMicrovmImages`** (not in the quota table of
   §11): each operation is called at growing requested rates for a few seconds
   per step, with the calls spread evenly over each second, and the report says
   the highest requested rate without throttling, the rate actually achieved in
   that step (the call latency can pull it below the requested one) and the
   first requested rate that was throttled (`ThrottlingException`). Both
   operations are free, but **the limits are account-wide**: climbing until
   throttled can also throttle other callers of the same account (other SDK
   users, a deployed dashboard), so run it outside working hours or in a test
   account.
2. **Service Quotas utilization for MicroVMs**: the `lambda` quotas whose name
   mentions MicroVMs, with their value, whether they are adjustable and whether
   Service Quotas publishes a usage metric for them (`UsageMetric`: namespace and
   metric name only). Free.
3. **Cost Explorer lag and usage types** (only with `--billed`): the last days of
   `AWS Lambda` cost grouped by usage type, reporting the newest day that already
   has MicroVM usage (the lag, in days) and the `Lambda-MicroVM-*` usage types
   seen, region prefix included. **Each Cost Explorer request is billed**
   (`COST_EXPLORER_REQUEST_PRICE_USD`); the script stops at
   `MAX_BILLED_REQUESTS`.

Everything environment-specific comes from the environment: the region from
`--region` or `AWS_REGION`/`AWS_DEFAULT_REGION`, the credentials and any profile
from boto3's default chain (`AWS_PROFILE`, SSO, role...). Nothing is written to
disk, and the printed report carries no account id, ARN or resource id
(`redact` scrubs it): only operation names, rates, counts, quota names and AWS's
own usage-type names.

Subcommands:

    plan                       prints what `run` would call and what it costs;
                               makes no AWS call
    run [--region R] [--billed]
                               measures and prints the redacted JSON report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_SOURCE = REPO_ROOT / "clients" / "python" / "src"
if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from rayito._limits import (
    COST_EXPLORER_REGION,
    COST_EXPLORER_REQUEST_PRICE_USD,
    COST_EXPLORER_SERVICE_NAME,
    QUOTA_SERVICE_CODE,
)

#: Requests per second tried, in order, on each list operation, and how long
#: each step lasts. A step with a throttled call ends the climb: the script
#: never keeps hammering an API that already said stop.
RATE_STEPS: Final = (1, 2, 5, 10, 20, 50)
STEP_SECONDS: Final = 5
#: Parte de las llamadas de un paso que debe salir throttleada para dar el paso
#: por saturado. Medido en AWS real (AWS_API_NOTES.md §30): a ritmos bajos
#: aparece algún throttling suelto (cuenta compartida, cubo de fichas) que no
#: es el límite sostenido; el límite se ve cuando se rechaza una fracción
#: apreciable de las llamadas.
SATURATED_SHARE: Final = 0.1
THROTTLING_CODES: Final = frozenset({"ThrottlingException", "TooManyRequestsException"})
LIST_OPERATIONS: Final = ("ListMicrovms", "ListMicrovmImages")
#: A page of one item is enough: the measurement is about the request rate.
PAGE_SIZE: Final = 1

MICROVM_QUOTA_MARKER: Final = "microvm"
MICROVM_USAGE_MARKER: Final = "Lambda-MicroVM-"
#: Days of daily cost read to find the lag, and the most billed requests the
#: script may make (one page is expected; the cap bounds a runaway token).
LAG_WINDOW_DAYS: Final = 7
MAX_BILLED_REQUESTS: Final = 3
COST_METRICS: Final = ("UnblendedCost", "UsageQuantity")
REGION_ENV_NAMES: Final = ("AWS_REGION", "AWS_DEFAULT_REGION", "RAYITO_E2E_REGION")
REDACTION: Final = "<redacted>"
ACCOUNT_ID_PATTERN: Final = re.compile(r"\b\d{12}\b")
ARN_PATTERN: Final = re.compile(r"arn:aws[a-z-]*:[^\s\"']+")
EXIT_USAGE: Final = 2
#: Un solo intento por llamada: con los reintentos por defecto de botocore un
#: throttling se reintenta con espera y nunca llega a verse, y la tasa lograda
#: sale falseada a la baja.
SINGLE_ATTEMPT: Final = 1


class Throttled(Exception):
    """AWS answered with a throttling code."""


class MeasurementPort(Protocol):
    """What the measurement needs from AWS; the real adapter wraps boto3 and
    the tests use a fake, so the logic below never touches the network."""

    def list_microvms(self) -> None: ...

    def list_microvm_images(self) -> None: ...

    def service_quotas(self) -> list[Mapping[str, Any]]: ...

    def daily_cost(
        self, start: date, end: date, token: str | None
    ) -> Mapping[str, Any]: ...


@dataclass
class TpsResult:
    """`*_requested_rate` is what the script asked for; `achieved_rate_at_highest_clean`
    is what it really sent in that step (calls over elapsed seconds)."""

    operation: str
    highest_clean_requested_rate: int | None
    achieved_rate_at_highest_clean: float | None
    first_throttled_requested_rate: int | None
    calls: int
    throttled: int


@dataclass
class Report:
    region: str
    tps: list[TpsResult] = field(default_factory=list)
    quotas: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] = field(default_factory=dict)


def _spread(call: Callable[[], None], count: int, sleep: Callable[[float], None]) -> int:
    """`count` calls started at even intervals across one second; how many were
    throttled. Any other error propagates: a measurement that cannot tell
    throttling from a failure is worthless."""

    def attempt() -> bool:
        try:
            call()
        except Throttled:
            return True
        return False

    interval = 1 / count
    with ThreadPoolExecutor(max_workers=count) as pool:
        pending = []
        for _ in range(count):
            pending.append(pool.submit(attempt))
            sleep(interval)
        return sum(future.result() for future in pending)


def measure_tps(
    operation: str,
    call: Callable[[], None],
    *,
    rates: Sequence[int] = RATE_STEPS,
    step_seconds: int = STEP_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TpsResult:
    """Climbs `rates` (calls per second, `step_seconds` seconds each) until a
    call is throttled, and reports the rate each clean step really achieved."""
    clean: int | None = None
    achieved: float | None = None
    calls = throttled = 0
    for rate in rates:
        step_throttled = 0
        started = clock()
        for _ in range(step_seconds):
            step_throttled += _spread(call, rate, sleep)
            calls += rate
        elapsed = clock() - started
        throttled += step_throttled
        if step_throttled >= SATURATED_SHARE * rate * step_seconds:
            return TpsResult(operation, clean, achieved, rate, calls, throttled)
        clean = rate
        achieved = round(rate * step_seconds / elapsed, 1) if elapsed > 0 else None
    return TpsResult(operation, clean, achieved, None, calls, throttled)


def microvm_quotas(port: MeasurementPort) -> list[dict[str, Any]]:
    """The MicroVM quotas of Service Quotas and what each one publishes."""
    found: list[dict[str, Any]] = []
    for quota in port.service_quotas():
        name = str(quota.get("QuotaName", ""))
        if MICROVM_QUOTA_MARKER not in name.lower():
            continue
        usage = quota.get("UsageMetric") or {}
        found.append(
            {
                "name": name,
                "value": quota.get("Value"),
                "adjustable": bool(quota.get("Adjustable")),
                "publishesUsageMetric": bool(usage),
                "usageMetric": {
                    "namespace": usage.get("MetricNamespace"),
                    "name": usage.get("MetricName"),
                    "statistic": usage.get("MetricStatisticRecommendation"),
                }
                if usage
                else None,
            }
        )
    return found


def _amount(group: Mapping[str, Any], metric: str) -> float:
    return float(group.get("Metrics", {}).get(metric, {}).get("Amount", 0) or 0)


def cost_lag(port: MeasurementPort, today: date) -> dict[str, Any]:
    """Newest day with MicroVM usage in the last `LAG_WINDOW_DAYS` days, as a
    lag in days from `today`, and the usage types seen. Counts its requests."""
    start = today - timedelta(days=LAG_WINDOW_DAYS)
    end = today + timedelta(days=1)
    usage_types: set[str] = set()
    newest: date | None = None
    estimated_days: list[str] = []
    requests = 0
    token: str | None = None
    while requests < MAX_BILLED_REQUESTS:
        page = port.daily_cost(start, end, token)
        requests += 1
        for result in page.get("ResultsByTime", []):
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            has_usage = False
            for group in result.get("Groups", []):
                usage_type = str(group["Keys"][0])
                if MICROVM_USAGE_MARKER not in usage_type:
                    continue
                usage_types.add(usage_type)
                if any(_amount(group, metric) > 0 for metric in COST_METRICS):
                    has_usage = True
            if has_usage:
                newest = day if newest is None else max(newest, day)
                if result.get("Estimated"):
                    estimated_days.append(day.isoformat())
        token = page.get("NextPageToken")
        if not token:
            break
    return {
        "billedRequests": requests,
        "billedUsd": round(requests * COST_EXPLORER_REQUEST_PRICE_USD, 2),
        "windowDays": LAG_WINDOW_DAYS,
        "newestDayWithUsageLagDays": None if newest is None else (today - newest).days,
        "newestDayWasEstimated": newest is not None
        and newest.isoformat() in estimated_days,
        "usageTypes": sorted(usage_types),
    }


def redact(text: str) -> str:
    """No account id or ARN ever leaves this script."""
    return ACCOUNT_ID_PATTERN.sub(REDACTION, ARN_PATTERN.sub(REDACTION, text))


def run_measurement(
    port: MeasurementPort,
    region: str,
    *,
    billed: bool,
    today: date,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    report = Report(region=region)
    calls = {
        "ListMicrovms": port.list_microvms,
        "ListMicrovmImages": port.list_microvm_images,
    }
    report.tps = [
        measure_tps(name, calls[name], sleep=sleep, clock=clock) for name in LIST_OPERATIONS
    ]
    report.quotas = microvm_quotas(port)
    report.cost = (
        cost_lag(port, today)
        if billed
        else {
            "skipped": f"pass --billed to measure it ({_price()} per request, at most "
            f"{MAX_BILLED_REQUESTS} requests)"
        }
    )
    return report


def _price() -> str:
    return f"${COST_EXPLORER_REQUEST_PRICE_USD:.2f}"


def plan_text() -> str:
    calls = sum(rate * STEP_SECONDS for rate in RATE_STEPS) * len(LIST_OPERATIONS)
    rates = ", ".join(map(str, RATE_STEPS))
    billed_cap = MAX_BILLED_REQUESTS * COST_EXPLORER_REQUEST_PRICE_USD
    lines = (
        "dashboard_apis.py would, read-only and with your default AWS credentials:",
        (
            f"- call {' and '.join(LIST_OPERATIONS)} at {rates} calls/s requested, spread "
            f"over each second, {STEP_SECONDS} s per step, stopping each at the first throttled "
            f"step (at most {calls} free calls)"
        ),
        (
            "- WARNING: those limits are account-wide; throttling can also hit other callers "
            "of the same account (other SDK users, a deployed dashboard). Run it outside "
            "working hours or in a test account"
        ),
        f"- list the '{QUOTA_SERVICE_CODE}' Service Quotas that mention MicroVMs (free)",
        (
            f"- with --billed only: read {LAG_WINDOW_DAYS} days of "
            f"'{COST_EXPLORER_SERVICE_NAME}' cost from Cost Explorer ({COST_EXPLORER_REGION}), "
            f"{_price()} per request, at most {MAX_BILLED_REQUESTS} requests (${billed_cap:.2f})"
        ),
        "Nothing is created or written to disk; the report carries no account id or ARN.",
    )
    return "\n".join(lines)


def report_json(report: Report) -> str:
    document = {
        "region": report.region,
        "tps": [vars(result) for result in report.tps],
        "quotas": report.quotas,
        "cost": report.cost,
    }
    return redact(json.dumps(document, indent=2, sort_keys=True))


class BotoPort:
    """The real adapter over boto3's default credential chain."""

    def __init__(self, region: str) -> None:
        import boto3
        from botocore.config import Config

        no_retries = Config(retries={"total_max_attempts": SINGLE_ATTEMPT, "mode": "standard"})
        session = boto3.session.Session(region_name=region)
        self._microvms = session.client("lambda-microvms", region_name=region, config=no_retries)
        self._quotas = session.client("service-quotas", region_name=region)
        self._ce = session.client("ce", region_name=COST_EXPLORER_REGION)

    @staticmethod
    def _guard(invoke: Callable[[], Any]) -> Any:
        from botocore.exceptions import ClientError

        try:
            return invoke()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in THROTTLING_CODES:
                raise Throttled from None
            raise

    def list_microvms(self) -> None:
        self._guard(lambda: self._microvms.list_microvms(maxResults=PAGE_SIZE))

    def list_microvm_images(self) -> None:
        self._guard(lambda: self._microvms.list_microvm_images(maxResults=PAGE_SIZE))

    def service_quotas(self) -> list[Mapping[str, Any]]:
        paginator = self._quotas.get_paginator("list_service_quotas")
        return [
            quota
            for page in paginator.paginate(ServiceCode=QUOTA_SERVICE_CODE)
            for quota in page.get("Quotas", [])
        ]

    def daily_cost(
        self, start: date, end: date, token: str | None
    ) -> Mapping[str, Any]:
        arguments: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": list(COST_METRICS),
            "Filter": {
                "Dimensions": {"Key": "SERVICE", "Values": [COST_EXPLORER_SERVICE_NAME]}
            },
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        }
        if token:
            arguments["NextPageToken"] = token
        result: Mapping[str, Any] = self._guard(
            lambda: self._ce.get_cost_and_usage(**arguments)
        )
        return result


def resolve_region(argument: str | None, environ: Mapping[str, str]) -> str | None:
    if argument:
        return argument
    return next((environ[name] for name in REGION_ENV_NAMES if environ.get(name)), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="print what `run` would do; no AWS call")
    run = commands.add_parser("run", help="measure and print the redacted report")
    run.add_argument("--region", help=f"default: {' or '.join(REGION_ENV_NAMES)}")
    run.add_argument(
        "--billed",
        action="store_true",
        help=f"also read Cost Explorer ({_price()} per request, at most {MAX_BILLED_REQUESTS})",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    port_factory: Callable[[str], MeasurementPort] = BotoPort,
    environ: Mapping[str, str] = os.environ,
    today: Callable[[], date] = date.today,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "plan":
        print(plan_text())
        return 0
    region = resolve_region(arguments.region, environ)
    if region is None:
        print(f"no region: pass --region or set {REGION_ENV_NAMES[0]}", file=sys.stderr)
        return EXIT_USAGE
    report = run_measurement(
        port_factory(region),
        region,
        billed=arguments.billed,
        today=today(),
        sleep=sleep,
    )
    print(report_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
