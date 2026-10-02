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

`run` provisions its own throwaway VPC and one subnet (tagged
`rayito:run-id=<ID>`, discovered by that tag before ever creating a new
one), then deploys the already-reviewed `efs-volumes` `OptionalStack`
(`infra/efs-volumes.yaml`, `RetainData=false`) into them through
`rayito.OptionalStacks` — this script never re-implements the file
system, mount target, connector, security groups or IAM role that the
stack already creates and tears down correctly. `cleanup` reverses that
exact order (stack, then subnet, then VPC) and only drops a stage from
local state once its delete has actually succeeded (or the resource was
already gone): a failed `cleanup` leaves the rest of `state.resources` in
place for a retry, never clears blindly. The EFS-1..EFS-20 questions that
need a running MicroVM (EFS-2/3/8/9/11/12/13/15/16) still have to be
exercised by hand against the file system/connector this provisions — see
tasks.md for that part's own scaffold status.

Every resource `run` creates is tagged `rayito:measurement=efs-volumes`,
`rayito:run-id=<ID>` and `rayito:expires-at=<unix-ms>` (24h from creation,
a named budget below); `cleanup` only ever touches resources carrying
`rayito:run-id=<ID>` for this script's own measurement tag, never a
resource it did not tag itself, and resolves them by that tag rather than
by an id kept on disk — no AWS-assigned resource id (VPC, subnet, file
system, ARN...) is ever written to local state or printed by this script,
only the measurement's own non-identifying run id, tag values and
resource *kinds* (`AWS_API_NOTES.md` §22 "nunca... un id de recurso
real"). Local state is never written into the repository: it goes to
`$XDG_STATE_HOME/rayito-measure/<run-id>.json` (`~/.local/state/...` if
unset).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

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

#: EFS-1 is already answered outside this campaign: nfs4 is compiled into
#: the guest kernel (measured by the M10 campaign, before the EFS-1..EFS-20
#: renumbering of what the research doc used to call Q79).
ALREADY_ANSWERED: Final = {
    "EFS-1": "nfs4 en /proc/filesystems (M10, AWS_API_NOTES.md §16)"
}

#: `rayito stack deploy efs-volumes`'s own component name (`_stacks/_registry.py`).
EFS_STACK_COMPONENT: Final = "efs-volumes"
#: A /24 for the throwaway measurement VPC and a /28 carved from it for the
#: one subnet `run` needs (a single mount target and the connector's ENIs:
#: research doc §9 does not require multi-AZ).
MEASUREMENT_VPC_CIDR: Final = "10.90.0.0/24"
MEASUREMENT_SUBNET_CIDR: Final = "10.90.0.0/28"
#: `state.resources` records *kinds*, in creation order, never an
#: AWS-assigned id (see module docstring); `cleanup` walks this reversed.
RESOURCE_KINDS_IN_CREATION_ORDER: Final = ("vpc", "subnet", "stack")


@dataclass(frozen=True)
class PlannedResource:
    kind: str
    purpose: str
    approx_cost_usd: float


#: What `run` creates, before any AWS call (research doc §9/§6). Kept as
#: data, not an f-string, so `plan` and `run` share one source of truth.
PLANNED_RESOURCES: Final[tuple[PlannedResource, ...]] = (
    PlannedResource("vpc", "throwaway VPC + one subnet for the stack below", 0.0),
    PlannedResource(
        "efs-volumes-stack",
        "rayito stack deploy efs-volumes: file system, mount target, connector, SGs, IAM role",
        0.02,
    ),
    PlannedResource(
        "caps-image", "throwaway rayito-base-caps build with efs-utils (EFS-7)", 0.15
    ),
    PlannedResource(
        "microvm-runs",
        "short-lived VMs for EFS-2/EFS-8/EFS-11/EFS-13/EFS-15 (by hand, see tasks.md)",
        0.30,
    ),
    PlannedResource("efs-throughput", "EFS-9 sustained read/write sample", 0.25),
    PlannedResource(
        "nat-gateway",
        "only if EFS-4 shows INTERNET_EGRESS can't coexist with the connector",
        0.10,
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
    #: Resource *kinds* already created or confirmed present this run, in
    #: `RESOURCE_KINDS_IN_CREATION_ORDER` order; never an AWS id (module
    #: docstring). `cleanup` pops one only once its delete has succeeded.
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
    path.write_text(
        json.dumps(state.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )


def resource_tags(run_id: str, expires_at_ms: int) -> dict[str, str]:
    return {
        MEASUREMENT_TAG_KEY: MEASUREMENT_TAG_VALUE,
        RUN_ID_TAG_KEY: run_id,
        EXPIRES_AT_TAG_KEY: str(expires_at_ms),
    }


def measurement_stack_name(run_id: str) -> str:
    return f"rayito-efs-volumes-measure-{run_id}"


# ------------------------------------------------------------- AWS port


class MeasurementAwsPort(Protocol):
    """What `run`/`cleanup` need from AWS: a throwaway VPC and subnet of
    this script's own, found by the `rayito:run-id` tag before ever being
    created, plus the `efs-volumes` `OptionalStack` (everything else:
    file system, mount target, connector, security groups, IAM role).
    Real behind boto3/`rayito.OptionalStacks` (`real_port`), faked in
    `scripts/tests/test_measure_efs_volumes.py`.
    """

    def find_tagged_vpc(self, run_id: str) -> str | None: ...
    def create_vpc(self, *, tags: Mapping[str, str]) -> str: ...
    def delete_vpc(self, vpc_id: str) -> None: ...

    def find_tagged_subnet(self, run_id: str) -> str | None: ...
    def create_subnet(self, vpc_id: str, *, tags: Mapping[str, str]) -> str: ...
    def delete_subnet(self, subnet_id: str) -> None: ...

    def stack_status(self, stack_name: str) -> Mapping[str, str] | None:
        """The stack's `Outputs`, or `None` if it does not exist."""
        ...

    def deploy_stack(
        self, stack_name: str, *, vpc_id: str, subnet_id: str, tags: Mapping[str, str]
    ) -> Mapping[str, str]:
        """`efs-volumes` with `RetainData=false`; returns its `Outputs`."""
        ...

    def destroy_stack(self, stack_name: str) -> None:
        """Idempotent: destroying a stack that does not exist is a no-op."""
        ...


def _tag_list(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


@dataclass
class _RealPort:
    """boto3 `ec2` plus `rayito.OptionalStacks`, both lazily imported by
    `real_port` so `plan`/`report`/the test module never need boto3 or a
    region to load."""

    ec2: Any
    stacks: Any

    def find_tagged_vpc(self, run_id: str) -> str | None:
        response = self.ec2.describe_vpcs(
            Filters=[{"Name": f"tag:{RUN_ID_TAG_KEY}", "Values": [run_id]}]
        )
        vpcs = response.get("Vpcs", [])
        return vpcs[0]["VpcId"] if vpcs else None

    def create_vpc(self, *, tags: Mapping[str, str]) -> str:
        response = self.ec2.create_vpc(
            CidrBlock=MEASUREMENT_VPC_CIDR,
            TagSpecifications=[{"ResourceType": "vpc", "Tags": _tag_list(tags)}],
        )
        vpc_id: str = response["Vpc"]["VpcId"]
        return vpc_id

    def delete_vpc(self, vpc_id: str) -> None:
        self.ec2.delete_vpc(VpcId=vpc_id)

    def find_tagged_subnet(self, run_id: str) -> str | None:
        response = self.ec2.describe_subnets(
            Filters=[{"Name": f"tag:{RUN_ID_TAG_KEY}", "Values": [run_id]}]
        )
        subnets = response.get("Subnets", [])
        return subnets[0]["SubnetId"] if subnets else None

    def create_subnet(self, vpc_id: str, *, tags: Mapping[str, str]) -> str:
        response = self.ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=MEASUREMENT_SUBNET_CIDR,
            TagSpecifications=[{"ResourceType": "subnet", "Tags": _tag_list(tags)}],
        )
        subnet_id: str = response["Subnet"]["SubnetId"]
        return subnet_id

    def delete_subnet(self, subnet_id: str) -> None:
        self.ec2.delete_subnet(SubnetId=subnet_id)

    def stack_status(self, stack_name: str) -> Mapping[str, str] | None:
        status = self.stacks.status(EFS_STACK_COMPONENT, stack_name=stack_name)
        return None if status is None else status.outputs

    def deploy_stack(
        self, stack_name: str, *, vpc_id: str, subnet_id: str, tags: Mapping[str, str]
    ) -> Mapping[str, str]:
        status = self.stacks.deploy(
            EFS_STACK_COMPONENT,
            stack_name=stack_name,
            parameters={"VpcId": vpc_id, "SubnetId1": subnet_id, "RetainData": "false"},
            tags=dict(tags),
        )
        outputs: Mapping[str, str] = status.outputs
        return outputs

    def destroy_stack(self, stack_name: str) -> None:
        self.stacks.destroy(EFS_STACK_COMPONENT, stack_name=stack_name)


def real_port(*, region: str, session: Any | None = None) -> MeasurementAwsPort:
    """The real adapter: boto3 `ec2` plus `rayito.OptionalStacks`. Called
    only from inside `cmd_run`/`cmd_cleanup` when the caller did not inject
    a fake, never at import time."""
    import boto3
    from rayito import OptionalStacks

    ec2 = (session or boto3).client("ec2", region_name=region)
    stacks = OptionalStacks(region=region, session=session)
    return _RealPort(ec2=ec2, stacks=stacks)


def _ensure_recorded(state: RunState, kind: str) -> None:
    if kind not in state.resources:
        state.resources.append(kind)
        save_state(state)


def _delete_one(aws: MeasurementAwsPort, kind: str, run_id: str) -> None:
    """Deletes the one resource of `kind` tagged with `run_id`, if any.
    Raised exceptions (a real AWS failure, never "already gone") propagate
    so the caller leaves `kind` in `state.resources` for a retry."""
    if kind == "stack":
        aws.destroy_stack(measurement_stack_name(run_id))
    elif kind == "subnet":
        subnet_id = aws.find_tagged_subnet(run_id)
        if subnet_id is not None:
            aws.delete_subnet(subnet_id)
    elif kind == "vpc":
        vpc_id = aws.find_tagged_vpc(run_id)
        if vpc_id is not None:
            aws.delete_vpc(vpc_id)
    else:  # pragma: no cover - state.json is only ever written by this script
        raise AssertionError(f"unknown resource kind in state: {kind!r}")


# ------------------------------------------------------------- subcommands


def cmd_plan(
    _args: argparse.Namespace, *, port: MeasurementAwsPort | None = None
) -> int:
    """Prints what `run` would create and its estimated cost; no AWS call."""
    del port
    total = sum(resource.approx_cost_usd for resource in PLANNED_RESOURCES)
    print(
        f"m15-efs-volumes measurement campaign: {len(QUESTION_ORDER)} questions, "
        f"{len(STOP_CRITERIA)} stop criteria ({', '.join(STOP_CRITERIA)})"
    )
    print(
        f"Already answered: {', '.join(f'{q} ({why})' for q, why in ALREADY_ANSWERED.items())}"
    )
    print("Resources run would create:")
    for resource in PLANNED_RESOURCES:
        print(
            f"  - {resource.kind}: {resource.purpose} (~${resource.approx_cost_usd:.2f})"
        )
    print(f"Estimated total: ~${total:.2f} (architecture §8 cap: $1.50)")
    print(
        f"Tags: {MEASUREMENT_TAG_KEY}={MEASUREMENT_TAG_VALUE}, {RUN_ID_TAG_KEY}=<run-id>, "
        f"{EXPIRES_AT_TAG_KEY}=<unix-ms, {MEASUREMENT_TTL_HOURS}h TTL>"
    )
    return 0


def cmd_run(args: argparse.Namespace, *, port: MeasurementAwsPort | None = None) -> int:
    """Idempotent: a throwaway VPC/subnet and the `efs-volumes` stack,
    each resolved by the `rayito:run-id` tag (or the stack's deterministic
    name) before anything new is created. The EFS-2..EFS-16 measurements
    that need a running MicroVM against the resulting file system/connector
    are not run here yet (tasks.md): this only makes the infra they need
    safe to create and, later, to tear down."""
    existing = load_state(args.run_id)
    if existing is not None:
        print(
            f"run {args.run_id!r} already exists in {state_path(args.run_id)}; resuming"
        )
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

    aws = port or real_port(region=args.region)
    tags = resource_tags(args.run_id, state.expires_at_ms)

    vpc_id = aws.find_tagged_vpc(args.run_id)
    if vpc_id is None:
        vpc_id = aws.create_vpc(tags=tags)
    _ensure_recorded(state, "vpc")

    subnet_id = aws.find_tagged_subnet(args.run_id)
    if subnet_id is None:
        subnet_id = aws.create_subnet(vpc_id, tags=tags)
    _ensure_recorded(state, "subnet")

    stack_name = measurement_stack_name(args.run_id)
    if aws.stack_status(stack_name) is None:
        aws.deploy_stack(stack_name, vpc_id=vpc_id, subnet_id=subnet_id, tags=tags)
    _ensure_recorded(state, "stack")

    print(f"state at {state_path(args.run_id)}; tags {tags}")
    print(
        "infra ready: throwaway VPC/subnet and the efs-volumes stack (RetainData=false). "
        "No resource id is printed or stored here (AWS_API_NOTES.md §22) — use "
        "`rayito stack status efs-volumes --stack-name "
        f"{stack_name}` to read FileSystemId/ConnectorArn when you need them."
    )
    print(
        "NOTE: EFS-2/EFS-3/EFS-8/EFS-9/EFS-11/EFS-12/EFS-13/EFS-15/EFS-16 still need a "
        "MicroVM launched by hand against this file system/connector (scaffold, see "
        "tasks.md); this command does not launch one."
    )
    return 0


def cmd_report(
    args: argparse.Namespace, *, port: MeasurementAwsPort | None = None
) -> int:
    """Renders results with every id redacted; reads only local state."""
    del port
    state = load_state(args.run_id)
    if state is None:
        print(
            f"no state for run {args.run_id!r} at {state_path(args.run_id)}",
            file=sys.stderr,
        )
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


def cmd_cleanup(
    args: argparse.Namespace, *, port: MeasurementAwsPort | None = None
) -> int:
    """Deletes everything tagged with this run id, in reverse creation
    order (stack, then subnet, then VPC); safe to re-run. A `kind` leaves
    `state.resources` only once its delete has succeeded (or the resource
    was already gone) — a real failure stops here and keeps the rest of
    the list for the next `cleanup` to retry, it never clears blindly."""
    state = load_state(args.run_id)
    if state is None:
        print(f"no state for run {args.run_id!r}; nothing to clean up")
        return 0
    aws = port or real_port(region=state.region)
    for kind in list(reversed(state.resources)):
        try:
            _delete_one(aws, kind, args.run_id)
        except Exception as exc:  # noqa: BLE001 - any AWS failure stops cleanup here
            print(
                f"cleanup for run {args.run_id!r} stopped at {kind!r}: {exc}",
                file=sys.stderr,
            )
            return 1
        state.resources.remove(kind)
        save_state(state)
    state_path(args.run_id).unlink(missing_ok=True)
    print(f"cleanup for run {args.run_id!r}: done, state removed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "plan", help="print the plan and its cost; no AWS call"
    ).set_defaults(func=cmd_plan)

    run_parser = sub.add_parser("run", help="run the campaign (idempotent by run-id)")
    run_parser.add_argument("--region", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.set_defaults(func=cmd_run)

    report_parser = sub.add_parser("report", help="render results, ids redacted")
    report_parser.add_argument("--run-id", required=True)
    report_parser.set_defaults(func=cmd_report)

    cleanup_parser = sub.add_parser(
        "cleanup", help="delete everything tagged with this run"
    )
    cleanup_parser.add_argument("--run-id", required=True)
    cleanup_parser.set_defaults(func=cmd_cleanup)

    return parser


def main(
    argv: list[str] | None = None, *, port: MeasurementAwsPort | None = None
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args, port=port)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
