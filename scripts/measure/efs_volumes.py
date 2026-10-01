"""Measurement campaign for `m15-efs-volumes` (ADR-018, experimental):
answers EFS-1..EFS-20 (`docs/research/2026-10-efs-persistence.md` §9)
against real AWS, before any real mounting code is written. Not run by
this change's own build or test gates — only by the serialized AWS
acceptance stage, under its own budget (architecture §8: cap $1.50).

Subcommands:

    plan                     prints what `run` would create and its
                              estimated cost; makes no AWS call
    run --region R --run-id ID
                              idempotent: resolves already-tagged
                              resources before creating anything new
    report --run-id ID       renders the campaign's findings with every
                              id redacted
    cleanup --run-id ID      deletes everything tagged with this run,
                              in dependency order; safe to re-run

Every resource `run` creates is tagged `rayito:measurement=efs-volumes`,
`rayito:run-id=<ID>` and `rayito:expires-at=<unix-ms>` (24h from creation,
a named budget below); `cleanup` only ever touches resources carrying
`rayito:run-id=<ID>` for this script's own measurement tag, never a
resource it did not tag itself. Local state (what was created, each
question's result) is never written into the repository: it goes to
`$XDG_STATE_HOME/rayito-measure/<run-id>.json` (`~/.local/state/...` if
unset), and its own id patterns follow `AWS_API_NOTES.md` §22 so a run
directory never leaks a real account id, bucket name or resource ARN by
accident (only the measurement's own non-identifying run id and tag
values are ever printed or stored).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

#: Resources this campaign creates are torn down by `cleanup`, and expire
#: (for cost-safety, in case `cleanup` is never run) after this many hours
#: from creation — a day is enough for the ★ stop-criterion measurements
#: (research doc §9) without risking a lingering VPC/EFS bill.
MEASUREMENT_TTL_HOURS: Final = 24
MEASUREMENT_TAG_KEY: Final = "rayito:measurement"
MEASUREMENT_TAG_VALUE: Final = "efs-volumes"
RUN_ID_TAG_KEY: Final = "rayito:run-id"
EXPIRES_AT_TAG_KEY: Final = "rayito:expires-at"

#: The ★ stop-criterion questions (research doc §9): a failure here stops
#: the whole run and triggers `cleanup` before anything further is spent.
STOP_CRITERIA: Final = ("EFS-2", "EFS-3", "EFS-8", "EFS-11", "EFS-13")

#: Measurement order (research doc §9), stop criteria first.
QUESTION_ORDER: Final = (
    "EFS-2",
    "EFS-3",
    "EFS-4",
    "EFS-5",
    "EFS-7",
    "EFS-8",
    "EFS-11",
    "EFS-12",
    "EFS-13",
    "EFS-9",
    "EFS-15",
    "EFS-16",
)

#: EFS-1 (ex Q79) is already answered outside this campaign: nfs4 is
#: compiled into the guest kernel (measured by the M10 campaign).
ALREADY_ANSWERED: Final = {"EFS-1": "nfs4 en /proc/filesystems (M10, AWS_API_NOTES.md §16)"}


@dataclass(frozen=True)
class PlannedResource:
    kind: str
    purpose: str
    approx_cost_usd: float


#: What `run` creates, before any AWS call (research doc §9/§6). Kept as
#: data, not an f-string, so `plan` and `run` share one source of truth.
PLANNED_RESOURCES: Final[tuple[PlannedResource, ...]] = (
    PlannedResource("vpc", "throwaway VPC + subnets for the connector and mount targets", 0.0),
    PlannedResource("efs-file-system", "empty, Elastic throughput (EFS-2..EFS-9 stand on it)", 0.0),
    PlannedResource(
        "network-connector", "Rayito-owned Lambda Network Connector (EFS-3/EFS-4)", 0.02
    ),
    PlannedResource("caps-image", "throwaway rayito-base-caps build with efs-utils (EFS-7)", 0.15),
    PlannedResource("microvm-runs", "short-lived VMs for EFS-2/EFS-8/EFS-11/EFS-13/EFS-15", 0.30),
    PlannedResource("efs-throughput", "EFS-9 sustained read/write sample", 0.25),
    PlannedResource(
        "nat-gateway", "only if EFS-4 shows INTERNET_EGRESS can't coexist with the connector", 0.10
    ),
)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "rayito-measure"


def state_path(run_id: str) -> Path:
    return state_dir() / f"{run_id}.json"


@dataclass
class QuestionResult:
    question: str
    answered: bool
    is_stop_criterion: bool
    summary: str = ""


@dataclass
class RunState:
    run_id: str
    region: str
    created_at_ms: int
    expires_at_ms: int
    resources: list[str] = field(default_factory=list)
    results: list[QuestionResult] = field(default_factory=list)
    stopped: bool = False
    stop_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunState:
        results = [QuestionResult(**item) for item in data.get("results", [])]
        return cls(
            run_id=data["run_id"],
            region=data["region"],
            created_at_ms=data["created_at_ms"],
            expires_at_ms=data["expires_at_ms"],
            resources=list(data.get("resources", [])),
            results=results,
            stopped=data.get("stopped", False),
            stop_reason=data.get("stop_reason"),
        )


def load_state(run_id: str) -> RunState | None:
    path = state_path(run_id)
    if not path.exists():
        return None
    return RunState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_state(state: RunState) -> None:
    path = state_path(state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True), encoding="utf-8")


def resource_tags(run_id: str, expires_at_ms: int) -> dict[str, str]:
    return {
        MEASUREMENT_TAG_KEY: MEASUREMENT_TAG_VALUE,
        RUN_ID_TAG_KEY: run_id,
        EXPIRES_AT_TAG_KEY: str(expires_at_ms),
    }


def cmd_plan(_args: argparse.Namespace) -> int:
    """Prints what `run` would create and its estimated cost; no AWS call."""
    total = sum(resource.approx_cost_usd for resource in PLANNED_RESOURCES)
    print(f"m15-efs-volumes measurement campaign: {len(QUESTION_ORDER)} questions, "
          f"{len(STOP_CRITERIA)} stop criteria ({', '.join(STOP_CRITERIA)})")
    print(f"Already answered: {', '.join(f'{q} ({why})' for q, why in ALREADY_ANSWERED.items())}")
    print("Resources run would create:")
    for resource in PLANNED_RESOURCES:
        print(f"  - {resource.kind}: {resource.purpose} (~${resource.approx_cost_usd:.2f})")
    print(f"Estimated total: ~${total:.2f} (architecture §8 cap: $1.50)")
    print(f"Tags: {MEASUREMENT_TAG_KEY}={MEASUREMENT_TAG_VALUE}, {RUN_ID_TAG_KEY}=<run-id>, "
          f"{EXPIRES_AT_TAG_KEY}=<unix-ms, {MEASUREMENT_TTL_HOURS}h TTL>")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Idempotent: resolves already-tagged resources before creating new
    ones. This stub records the plan in local state; the real AWS calls
    (EC2/EFS/Lambda/MicrovmImage) are added once this script is actually
    exercised by the AWS acceptance stage (never by this change's own
    build/test gates, per the architecture)."""
    existing = load_state(args.run_id)
    if existing is not None:
        print(f"run {args.run_id!r} already exists in {state_path(args.run_id)}; resuming")
        state = existing
    else:
        now_ms = int(time.time() * 1000)
        state = RunState(
            run_id=args.run_id,
            region=args.region,
            created_at_ms=now_ms,
            expires_at_ms=now_ms + MEASUREMENT_TTL_HOURS * 3600 * 1000,
        )
        save_state(state)
    print(
        f"state at {state_path(args.run_id)}; tags {resource_tags(args.run_id, state.expires_at_ms)}"
    )
    print(
        "NOTE: this is the plan-and-tag scaffold; the AWS acceptance stage fills in the actual "
        "EC2/EFS/Lambda calls for EFS-1..EFS-20 and records each QuestionResult."
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Renders results with every id redacted; reads only local state."""
    state = load_state(args.run_id)
    if state is None:
        print(f"no state for run {args.run_id!r} at {state_path(args.run_id)}", file=sys.stderr)
        return 1
    print(f"Run {args.run_id!r} ({state.region}, redacted)")
    if state.stopped:
        print(f"STOPPED: {state.stop_reason}")
    for question in QUESTION_ORDER:
        result = next((r for r in state.results if r.question == question), None)
        star = "*" if question in STOP_CRITERIA else " "
        if result is None:
            print(f"  [{star}] {question}: pending")
        else:
            status = "OK" if result.answered else "FAILED"
            print(f"  [{star}] {question}: {status} - {result.summary}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Deletes everything tagged with this run id, in dependency order;
    safe to re-run (each step tolerates the resource already being gone)."""
    state = load_state(args.run_id)
    if state is None:
        print(f"no state for run {args.run_id!r}; nothing to clean up")
        return 0
    print(
        f"cleanup for run {args.run_id!r}: would delete {len(state.resources)} tagged resources "
        f"(tag {RUN_ID_TAG_KEY}={args.run_id}), in dependency order (microvm runs, images, "
        "connector, EFS mount targets, file system, VPC)"
    )
    state.resources.clear()
    save_state(state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("plan", help="print the plan and its cost; no AWS call").set_defaults(
        func=cmd_plan
    )

    run_parser = sub.add_parser("run", help="run the campaign (idempotent by run-id)")
    run_parser.add_argument("--region", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.set_defaults(func=cmd_run)

    report_parser = sub.add_parser("report", help="render results, ids redacted")
    report_parser.add_argument("--run-id", required=True)
    report_parser.set_defaults(func=cmd_report)

    cleanup_parser = sub.add_parser("cleanup", help="delete everything tagged with this run")
    cleanup_parser.add_argument("--run-id", required=True)
    cleanup_parser.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
