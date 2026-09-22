"""Prune old versions of a Lambda MicroVM image, one at a time
(`rayito image prune`; `scripts/image_prune.py` is a shim over it).

Every ``image-publish`` since M4 leaves a version behind and each one costs
a week of snapshot storage; deleting them naively fails with
``ConflictException`` while the image is ``UPDATING`` or ``DELETING`` (one
delete at a time moves the image through those states, ``AWS_API_NOTES.md``
§4). The run:

1. lists every version (paginated) and sorts them newest first;
2. keeps the ``--keep`` newest launchable versions (``SUCCESSFUL`` and
   ``ACTIVE``), every version a live MicroVM runs (``list-microvms`` on the
   image, states other than ``TERMINATED``/``TERMINATING``, read right
   before deleting) and every version in ``PENDING``, ``IN_PROGRESS``,
   ``DELETING`` or ``DELETED`` (a build or a delete in flight is never
   touched);
3. deletes the rest oldest first, serially: after each
   ``delete-microvm-image-version`` it waits until
   ``get-microvm-image-version`` says ``DELETED`` (or the version is gone)
   **and** ``get-microvm-image`` has left ``UPDATING``/``DELETING``; a
   ``ConflictException`` waits for the image to settle and retries with
   5/10/20/40/80 s backoff, at most five attempts. If the service refuses
   to delete an ``ACTIVE`` version outright, it is deactivated first
   (``update-microvm-image-version --status INACTIVE``) and the delete
   retried after the image settles.
4. prints a table (version, state, status, createdAt, action) and a JSON
   summary; ``--dry-run`` prints the plan without any mutating call.

Exit code 1 when a candidate is still present after its attempts. Only
versions are ever deleted, never the image. Every parameter name appears
literally in ``docs/aws-api/model_summary.md``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from botocore.exceptions import ClientError

from rayito.cli._console import client_error_code, client_error_message, echo, emit_json

DEFAULT_IMAGE_NAME = "rayito-base"
DEFAULT_KEEP = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 5.0
RETRY_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 80.0)
MAX_DELETE_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)
LAUNCHABLE_STATE = "SUCCESSFUL"
ACTIVE_STATUS = "ACTIVE"
IN_FLIGHT_STATES = frozenset({"PENDING", "IN_PROGRESS", "DELETING", "DELETED"})
BUSY_IMAGE_STATES = frozenset({"UPDATING", "DELETING"})
LIVE_MICROVM_EXCLUDED_STATES = frozenset({"TERMINATED", "TERMINATING"})
GONE_VERSION_STATE = "DELETED"
CONFLICT = "ConflictException"
NOT_FOUND = "ResourceNotFoundException"
THROTTLED = "ThrottlingException"
REFUSED_ACTIVE_CODES = frozenset({"ValidationException", CONFLICT})
TABLE_HEADER = ("version", "state", "status", "createdAt", "action")

Sleeper = Callable[[float], None]
Clock = Callable[[], float]
Emitter = Callable[[str], None]


class PruneClients(Protocol):
    """What the pruner needs from `rayito.cli._session.Clients`."""

    @property
    def region(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def microvms(self) -> Any: ...


@dataclass(frozen=True)
class Version:
    image_version: str
    state: str
    status: str
    created_at: datetime

    @property
    def launchable(self) -> bool:
        return self.state == LAUNCHABLE_STATE and self.status == ACTIVE_STATUS

    @property
    def in_flight(self) -> bool:
        return self.state in IN_FLIGHT_STATES


@dataclass(frozen=True)
class Plan:
    """What the run will do to every version, newest first."""

    keep: list[tuple[Version, str]]
    delete: list[Version]

    def rows(self) -> list[tuple[str, str, str, str, str]]:
        rows = [
            (v.image_version, v.state, v.status, v.created_at.isoformat(), f"keep ({why})")
            for v, why in self.keep
        ]
        rows.extend(
            (v.image_version, v.state, v.status, v.created_at.isoformat(), "delete")
            for v in self.delete
        )
        rows.sort(key=lambda row: row[3], reverse=True)
        return rows


@dataclass
class DeleteReport:
    image_version: str
    outcome: str = "pending"
    attempts: int = 0
    conflicts: int = 0
    deactivated: bool = False
    waited_seconds: float = 0.0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "imageVersion": self.image_version,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "conflicts": self.conflicts,
            "deactivated": self.deactivated,
            "waitedSeconds": round(self.waited_seconds, 1),
            "lastError": self.last_error,
        }


@dataclass(frozen=True)
class PruneSettings:
    image_name: str = DEFAULT_IMAGE_NAME
    keep: int = DEFAULT_KEEP
    dry_run: bool = False
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.keep < 1:
            raise ValueError("--keep must be at least 1")


def image_arn(region: str, account_id: str, image_name: str) -> str:
    return f"arn:aws:lambda:{region}:{account_id}:microvm-image:{image_name}"


def list_versions(microvms: Any, arn: str) -> list[Version]:
    paginator = microvms.get_paginator("list_microvm_image_versions")
    versions = [
        Version(
            image_version=str(item["imageVersion"]),
            state=str(item["state"]),
            status=str(item["status"]),
            created_at=item["createdAt"],
        )
        for page in paginator.paginate(imageIdentifier=arn)
        for item in page["items"]
    ]
    versions.sort(key=lambda version: version.created_at, reverse=True)
    return versions


def live_versions(microvms: Any, arn: str) -> set[str]:
    """Versions a MicroVM that is not terminated (or terminating) runs."""
    paginator = microvms.get_paginator("list_microvms")
    return {
        str(item["imageVersion"])
        for page in paginator.paginate(imageIdentifier=arn)
        for item in page["items"]
        if item["state"] not in LIVE_MICROVM_EXCLUDED_STATES
    }


def build_plan(versions: list[Version], live: set[str], keep: int) -> Plan:
    """Newest first: the first ``keep`` launchable versions stay, so does
    anything live or in flight; everything else goes oldest first."""
    kept: list[tuple[Version, str]] = []
    candidates: list[Version] = []
    launchable_kept = 0
    for version in versions:
        if version.launchable and launchable_kept < keep:
            launchable_kept += 1
            kept.append((version, "newest"))
        elif version.image_version in live:
            kept.append((version, "live microvm"))
        elif version.in_flight:
            kept.append((version, version.state.lower()))
        else:
            candidates.append(version)
    candidates.sort(key=lambda version: version.created_at)
    return Plan(keep=kept, delete=candidates)


def image_state(microvms: Any, arn: str) -> str:
    return str(microvms.get_microvm_image(imageIdentifier=arn)["state"])


def version_state(microvms: Any, arn: str, image_version: str) -> str | None:
    """``None`` once the version no longer exists."""
    try:
        detail = microvms.get_microvm_image_version(imageIdentifier=arn, imageVersion=image_version)
    except ClientError as exc:
        if client_error_code(exc) == NOT_FOUND:
            return None
        raise
    return str(detail["state"])


def refusal(exc: ClientError) -> str:
    return f"{client_error_code(exc)}: {client_error_message(exc)}"


class Pruner:
    def __init__(
        self,
        microvms: Any,
        arn: str,
        *,
        wait_timeout: float,
        sleep: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
        emit: Emitter = echo,
    ) -> None:
        self.microvms = microvms
        self.arn = arn
        self.wait_timeout = wait_timeout
        self.sleep = sleep
        self.clock = clock
        self.emit = emit

    def wait_until_image_settled(self, report: DeleteReport) -> bool:
        """Polls ``get-microvm-image`` until it leaves ``UPDATING``/``DELETING``."""
        started = self.clock()
        while True:
            state = image_state(self.microvms, self.arn)
            if state not in BUSY_IMAGE_STATES:
                report.waited_seconds += self.clock() - started
                return True
            if self.clock() - started >= self.wait_timeout:
                report.waited_seconds += self.clock() - started
                report.last_error = f"image still {state} after {self.wait_timeout:.0f}s"
                return False
            self.sleep(POLL_INTERVAL_SECONDS)

    def wait_until_version_gone(self, report: DeleteReport) -> bool:
        """``DELETED`` or ``ResourceNotFoundException``, then the image settled."""
        started = self.clock()
        while True:
            state = version_state(self.microvms, self.arn, report.image_version)
            if state is None or state == GONE_VERSION_STATE:
                report.waited_seconds += self.clock() - started
                return self.wait_until_image_settled(report)
            if self.clock() - started >= self.wait_timeout:
                report.waited_seconds += self.clock() - started
                report.last_error = f"version still {state} after {self.wait_timeout:.0f}s"
                return False
            self.sleep(POLL_INTERVAL_SECONDS)

    def delete_once(self, report: DeleteReport) -> str | None:
        """One ``delete-microvm-image-version``; the error code when refused."""
        try:
            self.microvms.delete_microvm_image_version(
                imageIdentifier=self.arn, imageVersion=report.image_version
            )
        except ClientError as exc:
            code = client_error_code(exc)
            report.last_error = refusal(exc)
            if code == NOT_FOUND:
                return None
            return code
        return None

    def deactivate(self, report: DeleteReport) -> None:
        """Only when the service refused to delete an ``ACTIVE`` version."""
        try:
            self.microvms.update_microvm_image_version(
                imageIdentifier=self.arn,
                imageVersion=report.image_version,
                status="INACTIVE",
            )
        except ClientError as exc:
            report.last_error = refusal(exc)
            return
        report.deactivated = True
        self.wait_until_image_settled(report)

    def prune(self, version: Version) -> DeleteReport:
        report = DeleteReport(image_version=version.image_version)
        for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
            report.attempts = attempt
            refused = self.delete_once(report)
            if refused is None:
                if self.wait_until_version_gone(report):
                    report.outcome = "deleted"
                    return report
                report.outcome = "timeout"
                return report
            if refused == CONFLICT:
                report.conflicts += 1
                self.wait_until_image_settled(report)
            elif refused in REFUSED_ACTIVE_CODES and version.status == ACTIVE_STATUS:
                self.deactivate(report)
            elif refused != THROTTLED:
                report.outcome = "refused"
                return report
            self.emit(
                f"  {version.image_version}: {refused} on attempt {attempt}; "
                f"retrying in {backoff:.0f}s"
            )
            self.sleep(backoff)
        report.outcome = "failed"
        return report


def format_table(plan: Plan) -> list[str]:
    rows = [TABLE_HEADER, *plan.rows()]
    widths = [max(len(row[i]) for row in rows) for i in range(len(TABLE_HEADER))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]


def prune_summary(
    arn: str, plan: Plan, reports: list[DeleteReport], dry_run: bool
) -> dict[str, Any]:
    return {
        "imageArn": arn,
        "dryRun": dry_run,
        "kept": [v.image_version for v, _ in plan.keep],
        "planned": [v.image_version for v in plan.delete],
        "deleted": [r.image_version for r in reports if r.outcome == "deleted"],
        "reports": [r.as_dict() for r in reports],
        "finishedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def run(
    clients: PruneClients,
    settings: PruneSettings,
    *,
    sleep: Sleeper = time.sleep,
    json_output: bool = False,
) -> int:
    """The plan, the serialised deletes and the summary; with `--json` the
    table and the progress go to stderr and stdout carries the summary."""

    def emit(message: str) -> None:
        echo(message, err=json_output)

    arn = image_arn(clients.region, clients.account_id, settings.image_name)
    versions = list_versions(clients.microvms, arn)
    live = live_versions(clients.microvms, arn)
    plan = build_plan(versions, live, settings.keep)
    emit(f"{arn}: {len(versions)} versions, keep {settings.keep}, live {sorted(live)}")
    for line in format_table(plan):
        emit(line)
    reports: list[DeleteReport] = []
    if settings.dry_run:
        emit_json(prune_summary(arn, plan, reports, True))
        return 0
    pruner = Pruner(
        clients.microvms, arn, wait_timeout=settings.wait_timeout, sleep=sleep, emit=emit
    )
    for version in plan.delete:
        if version.image_version in live_versions(clients.microvms, arn):
            emit(f"  {version.image_version}: a MicroVM started on it meanwhile; kept")
            continue
        started = time.monotonic()
        report = pruner.prune(version)
        reports.append(report)
        emit(
            f"  {version.image_version}: {report.outcome} after {report.attempts} attempt(s), "
            f"{report.conflicts} conflict(s), waited {report.waited_seconds:.0f}s "
            f"({time.monotonic() - started:.0f}s wall)"
        )
    emit_json(prune_summary(arn, plan, reports, False))
    return 0 if all(report.outcome == "deleted" for report in reports) else 1
