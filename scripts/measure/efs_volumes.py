"""Measurement campaign for `m15-efs-volumes` (ADR-018, experimental):
answers EFS-1..EFS-20 (`docs/research/2026-10-efs-persistence.md` §9)
against real AWS, before any real mounting code is written. Not run by
this change's own build or test gates — only by the serialized AWS
acceptance stage, under its own budget (architecture §8: cap $1.50).

Subcommands:

    plan                     prints what `run` would create and its
                              estimated cost; makes no AWS call
    run --region R --run-id ID --caps-template T --execution-role-arn A
                              idempotent: resolves already-tagged
                              resources before creating anything new,
                              then measures the ★ stop criteria
    run ... --infra-only      only the infra (no MicroVM, no measurement)
    report --run-id ID       renders the campaign's findings with every
                              id redacted
    cleanup --run-id ID      deletes everything tagged with this run,
                              in dependency order; safe to re-run

`run` provisions its own throwaway VPC and one subnet (tagged
`rayito:run-id=<ID>`, discovered by that tag before ever creating a new
one) — or, with `--vpc-id`/`--subnet-id`, uses a network borrowed with its
owner's permission that it never creates, records or deletes (an
organization SCP can deny `ec2:CreateVpc`: AWS_API_NOTES.md §16 Q98) —
then deploys the already-reviewed `efs-volumes` `OptionalStack`
(`infra/efs-volumes.yaml`) into them through `rayito.OptionalStacks` —
this script never re-implements the file system, mount target, connector,
security groups or IAM resources that the stack already creates. The
stack always retains its file system (`DeletionPolicy: Retain`), so this
script tags it with the run id and `cleanup` deletes it explicitly, after
the stack, together with its access points.

It then measures the ★ stop criteria that are automated here (`AUTOMATED`:
EFS-2, EFS-3, EFS-8, EFS-11, EFS-13), in the research doc's §9 order,
through `rayito.Sandbox` against `--caps-template` (a throwaway
`rayito-base-caps` build that already carries `amazon-efs-utils`: that
build is EFS-7, done by hand first, see `tasks.md` §7). Each records
pass/fail in local state; a ★ failure stops the run and calls `cleanup`
right away, before anything further is spent. The non-★ questions
(EFS-4, 5, 7, 9, 12, 15, 16) stay manual, against the infra `run` leaves
up, following the checklist in `tasks.md` §7.

`cleanup` walks `CLEANUP_ORDER` and only drops a stage from local state
once its delete has actually succeeded (or the resource was already
gone): a failed `cleanup` leaves the rest of `state.resources` in place
for a retry, never clears blindly.

Every resource `run` creates is tagged `rayito:measurement=efs-volumes`,
`rayito:run-id=<ID>` and `rayito:expires-at=<unix-ms>` (24h from creation,
a named budget below); `cleanup` only ever touches resources carrying
`rayito:run-id=<ID>` for this script's own measurement tag, never a
resource it did not tag itself, and resolves them by that tag rather than
by an id kept on disk — no AWS-assigned resource id (VPC, subnet, file
system, ARN, IP...) is ever written to local state or printed by this
script (`redact` scrubs every summary), only the measurement's own
non-identifying run id, tag values and resource *kinds*
(`AWS_API_NOTES.md` §22 "nunca... un id de recurso real"). Local state is
never written into the repository: it goes to
`$XDG_STATE_HOME/rayito-measure/<run-id>.json` (`~/.local/state/...` if
unset).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
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
#: `state.resources` records *kinds*, never an AWS-assigned id (module
#: docstring). `cleanup` deletes them in this order: the role attachment
#: first (an attached managed policy blocks the stack's own delete), the
#: stack before its retained file system (mount targets must be gone
#: before `DeleteFileSystem`), and the network last.
CLEANUP_ORDER: Final = ("client-policy", "stack", "file-system", "subnet", "vpc")
#: Name of the one access point the MicroVM steps mount
#: (`VolumeStore.create` is idempotent by file system + name).
MEASUREMENT_VOLUME_NAME: Final = "rayito-measure"

#: `infra/efs-volumes.yaml`'s `Connector` reports this `State` once its
#: ENIs exist (AWS_API_NOTES.md §22); EFS-3 asks whether it gets there.
CONNECTOR_ACTIVE: Final = "ACTIVE"
#: NFS's port; the only one `infra/efs-volumes.yaml` opens.
NFS_PORT: Final = 2049
#: RFC 5737 TEST-NET-1: never routed, so `mount -t nfs4` against it can
#: only time out *if* the kernel let the mount start at all — which is
#: exactly how EFS-2 tells `EPERM`/`ENODEV` (instant) from a reachable
#: network stack (research doc §9, row EFS-2).
UNROUTABLE_TEST_IP: Final = "192.0.2.1"
#: Where every guest-side mount of this campaign lives.
GUEST_MOUNT_DIR: Final = "/mnt/rayito-measure"
#: `timeout(1)`'s exit status when it killed the command.
TIMEOUT_EXIT_CODE: Final = 124
#: Bounds for one guest command: an NFS mount attempt to TEST-NET (EFS-2),
#: a TCP connect to the mount target (EFS-3), and a real `mount -t efs`
#: (EFS-8: efs-utils' own default `retrans`/`timeo` give up well before).
EFS2_NFS_ATTEMPT_SECONDS: Final = 15
EFS3_CONNECT_SECONDS: Final = 5
#: Any other short guest command (`mount -t tmpfs`, `command -v`, `pgrep`).
QUICK_COMMAND_SECONDS: Final = 10
EFS_MOUNT_COMMAND_SECONDS: Final = 120
#: Extra seconds the SDK waits on top of a guest-side `timeout N`, so the
#: guest's own timeout always fires first and is the one recorded.
COMMAND_TIMEOUT_MARGIN_SECONDS: Final = 15
#: EFS-8: "latencia de montaje p50/p95 (20 muestras)" (research doc §9).
EFS8_MOUNT_SAMPLES: Final = 20
#: EFS-11: "pausas de 60 s, 10 min y 60 min ... 3 ciclos por duración"
#: (research doc §9). `--efs11-pauses 60` runs only the short one.
EFS11_PAUSE_SECONDS: Final = (60, 600, 3600)
EFS11_CYCLES_PER_PAUSE: Final = 3
#: EFS-11: how long after `resume()` the first correct read may take
#: before the cycle counts as failed, and how often it is retried.
EFS11_FIRST_READ_DEADLINE_SECONDS: Final = 120
EFS11_READ_POLL_SECONDS: Final = 1
#: EFS-13: a `pause()` that has not returned in this long, with the mount
#: target unreachable, is the hang the stop criterion is about (Lambda's
#: own `/suspend` deadline is undocumented; ten minutes is well past it).
PAUSE_DEADLINE_SECONDS: Final = 600
#: Lifetime (`Sandbox.create(timeout=)`: running + suspended) of every
#: measurement MicroVM: the longest active stretch is EFS-8's 20 mounts,
#: minutes at most; it only caps the bill if the script dies before its own
#: `kill()`. EFS-11's VMs add their pauses on top.
MEASUREMENT_VM_TIMEOUT_SECONDS: Final = 1800
#: A summary is one line in `report`; the redacted tail of a guest error.
SUMMARY_STDERR_CHARS: Final = 160

#: What `redact` scrubs from anything that reaches state or stdout: EFS,
#: EC2 and IAM ids, ARNs, IPv4 addresses and 12-digit account ids
#: (`AWS_API_NOTES.md` §22).
REDACTED: Final = "<redacted>"
REDACT_PATTERNS: Final = (
    re.compile(r"arn:aws[\w-]*:[^\s\"',]+"),
    re.compile(r"\b(?:fs|fsap|fsmt|vpc|subnet|sg|eni)-[0-9a-f]{8,17}\b"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"\b\d{12}\b"),
)


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
        "rayito stack deploy efs-volumes: file system, mount target, connector, SGs, IAM",
        0.02,
    ),
    PlannedResource(
        "caps-image",
        "throwaway rayito-base-caps build with efs-utils (EFS-7, by hand, tasks.md §7)",
        0.15,
    ),
    PlannedResource(
        "microvm-runs",
        "short-lived VMs for the automated ★ steps EFS-2/EFS-3/EFS-8/EFS-11/EFS-13",
        0.30,
    ),
    PlannedResource(
        "efs-throughput", "EFS-9 sustained read/write sample (by hand)", 0.25
    ),
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
    #: `True` = the question got the answer the design needs (a pass).
    answered: bool
    is_stop_criterion: bool
    summary: str = ""


@dataclass
class RunState:
    run_id: str
    region: str
    created_at_ms: int
    expires_at_ms: int
    #: Resource *kinds* already created (or about to be) this run; never an
    #: AWS id (module docstring). `cleanup` drops one only once its delete
    #: has succeeded.
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

    def result_for(self, question: str) -> QuestionResult | None:
        return next((r for r in self.results if r.question == question), None)

    def record(self, result: QuestionResult) -> None:
        self.results = [r for r in self.results if r.question != result.question]
        self.results.append(result)


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


def redact(text: str) -> str:
    for pattern in REDACT_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


# ------------------------------------------------------------- AWS port


class MeasurementAwsPort(Protocol):
    """What `run`/`cleanup` need from AWS: a throwaway VPC and subnet of
    this script's own, found by the `rayito:run-id` tag before ever being
    created, the `efs-volumes` `OptionalStack` (file system, mount target,
    connector, security groups, IAM), the retained file system it leaves
    behind, and the few out-of-band changes the ★ steps make. Real behind
    boto3/`rayito.OptionalStacks` (`real_port`), faked in
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
        """`efs-volumes` with its defaults; returns its `Outputs`."""
        ...

    def destroy_stack(self, stack_name: str) -> None:
        """Idempotent: destroying a stack that does not exist is a no-op."""
        ...

    def tag_file_system(
        self, file_system_id: str, *, tags: Mapping[str, str]
    ) -> None: ...
    def find_tagged_file_system(self, run_id: str) -> str | None: ...
    def delete_file_system(self, file_system_id: str) -> None:
        """Deletes its access points first, then the file system."""
        ...

    def ensure_access_point(self, file_system_id: str, name: str) -> str:
        """`VolumeStore.create` (idempotent by file system + name)."""
        ...

    def mount_target_ip(self, file_system_id: str) -> str: ...

    def attach_client_policy(self, role_arn: str, policy_arn: str) -> None: ...
    def detach_client_policy(self, policy_arn: str) -> None:
        """Detaches `policy_arn` from every role it is attached to (it is
        this run's own stack's policy, so every attachment is ours)."""
        ...

    def isolate_mount_targets(self, security_group_id: str) -> Any:
        """Revokes every ingress rule of the mount-target security group;
        returns an opaque token `restore_mount_targets` puts back."""
        ...

    def restore_mount_targets(self, security_group_id: str, token: Any) -> None: ...


def _tag_list(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def _role_name(role_arn: str) -> str:
    # `arn:aws:iam::<account>:role/<path/>name`: the name is the last segment.
    return role_arn.rsplit("/", 1)[-1]


@dataclass
class _RealPort:
    """boto3 `ec2`/`efs`/`iam` plus `rayito.OptionalStacks`, all lazily
    imported by `real_port` so `plan`/`report`/the test module never need
    boto3 or a region to load."""

    region: str
    ec2: Any
    efs: Any
    iam: Any
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
        return None if status is None or not status.exists else status.outputs

    def deploy_stack(
        self, stack_name: str, *, vpc_id: str, subnet_id: str, tags: Mapping[str, str]
    ) -> Mapping[str, str]:
        status = self.stacks.deploy(
            EFS_STACK_COMPONENT,
            stack_name=stack_name,
            parameters={"VpcId": vpc_id, "SubnetId1": subnet_id},
            tags=dict(tags),
        )
        outputs: Mapping[str, str] = status.outputs
        return outputs

    def destroy_stack(self, stack_name: str) -> None:
        self.stacks.destroy(EFS_STACK_COMPONENT, stack_name=stack_name)

    def tag_file_system(self, file_system_id: str, *, tags: Mapping[str, str]) -> None:
        self.efs.tag_resource(ResourceId=file_system_id, Tags=_tag_list(tags))

    def find_tagged_file_system(self, run_id: str) -> str | None:
        for page in self.efs.get_paginator("describe_file_systems").paginate():
            for file_system in page.get("FileSystems", []):
                tags = {t["Key"]: t["Value"] for t in file_system.get("Tags", [])}
                if (
                    tags.get(RUN_ID_TAG_KEY) == run_id
                    and tags.get(MEASUREMENT_TAG_KEY) == MEASUREMENT_TAG_VALUE
                ):
                    file_system_id: str = file_system["FileSystemId"]
                    return file_system_id
        return None

    def delete_file_system(self, file_system_id: str) -> None:
        access_points = self.efs.describe_access_points(FileSystemId=file_system_id)
        for access_point in access_points.get("AccessPoints", []):
            self.efs.delete_access_point(AccessPointId=access_point["AccessPointId"])
        self.efs.delete_file_system(FileSystemId=file_system_id)

    def ensure_access_point(self, file_system_id: str, name: str) -> str:
        from rayito import VolumeStore

        store = VolumeStore(file_system_id=file_system_id, region=self.region)
        access_point_id: str = store.create(name).access_point_id
        return access_point_id

    def mount_target_ip(self, file_system_id: str) -> str:
        response = self.efs.describe_mount_targets(FileSystemId=file_system_id)
        ip: str = response["MountTargets"][0]["IpAddress"]
        return ip

    def attach_client_policy(self, role_arn: str, policy_arn: str) -> None:
        self.iam.attach_role_policy(RoleName=_role_name(role_arn), PolicyArn=policy_arn)

    def detach_client_policy(self, policy_arn: str) -> None:
        entities = self.iam.list_entities_for_policy(
            PolicyArn=policy_arn, EntityFilter="Role"
        )
        for role in entities.get("PolicyRoles", []):
            self.iam.detach_role_policy(RoleName=role["RoleName"], PolicyArn=policy_arn)

    def isolate_mount_targets(self, security_group_id: str) -> Any:
        groups = self.ec2.describe_security_groups(GroupIds=[security_group_id])
        permissions = groups["SecurityGroups"][0].get("IpPermissions", [])
        if permissions:
            self.ec2.revoke_security_group_ingress(
                GroupId=security_group_id, IpPermissions=permissions
            )
        return permissions

    def restore_mount_targets(self, security_group_id: str, token: Any) -> None:
        if token:
            self.ec2.authorize_security_group_ingress(
                GroupId=security_group_id, IpPermissions=token
            )


def real_port(*, region: str, session: Any | None = None) -> MeasurementAwsPort:
    """The real adapter: boto3 `ec2`/`efs`/`iam` plus
    `rayito.OptionalStacks`. Called only from inside `cmd_run`/`cmd_cleanup`
    when the caller did not inject a fake, never at import time."""
    import boto3
    from rayito import OptionalStacks

    factory = session or boto3
    return _RealPort(
        region=region,
        ec2=factory.client("ec2", region_name=region),
        efs=factory.client("efs", region_name=region),
        iam=factory.client("iam", region_name=region),
        stacks=OptionalStacks(region=region, session=session),
    )


# ------------------------------------------------------------- guest port


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class PauseOutcome:
    """`state` is `paused`, `not-paused` (`pause()` returned `False`),
    `timeout` (it had not returned after the deadline) or
    `error:<ExceptionType>` (never the exception's message: it may carry
    an id)."""

    state: str
    seconds: float


class GuestPort(Protocol):
    """MicroVMs for the ★ steps. Real behind `rayito.Sandbox`
    (`real_guest`), faked in the tests. A handle is this script's own
    counter, never a sandbox id."""

    def launch(
        self,
        *,
        template: str | None,
        lifetime_seconds: int,
        egress: Sequence[str] | None = None,
        execution_role_arn: str | None = None,
    ) -> str: ...

    def run_as_root(
        self, handle: str, command: str, *, timeout_seconds: float
    ) -> CommandOutcome: ...

    def pause(self, handle: str, *, deadline_seconds: float) -> PauseOutcome: ...
    def resume(self, handle: str) -> None: ...
    def kill(self, handle: str) -> None: ...


@dataclass
class _RealGuest:
    region: str
    sandboxes: dict[str, Any] = field(default_factory=dict)

    def launch(
        self,
        *,
        template: str | None,
        lifetime_seconds: int,
        egress: Sequence[str] | None = None,
        execution_role_arn: str | None = None,
    ) -> str:
        from rayito import Sandbox

        # `idle=None`: no auto-suspension of its own, so every pause in a
        # measurement is the one the step asked for.
        sandbox = Sandbox.create(
            template,
            timeout=lifetime_seconds,
            idle=None,
            egress=None if egress is None else list(egress),
            execution_role_arn=execution_role_arn,
            metadata={MEASUREMENT_TAG_KEY: MEASUREMENT_TAG_VALUE},
            region=self.region,
        )
        handle = f"vm-{len(self.sandboxes) + 1}"
        self.sandboxes[handle] = sandbox
        return handle

    def run_as_root(
        self, handle: str, command: str, *, timeout_seconds: float
    ) -> CommandOutcome:
        from rayito import CommandExitException

        started = time.monotonic()
        try:
            result = self.sandboxes[handle].commands.run(
                command, user="root", timeout=timeout_seconds
            )
            exit_code, stdout, stderr = result.exit_code, result.stdout, result.stderr
        except CommandExitException as exc:
            exit_code, stdout, stderr = exc.exit_code, exc.stdout, exc.stderr
        elapsed_ms = (time.monotonic() - started) * 1000
        return CommandOutcome(exit_code, stdout, stderr, elapsed_ms)

    def pause(self, handle: str, *, deadline_seconds: float) -> PauseOutcome:
        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.sandboxes[handle].pause)
        try:
            state = (
                "paused" if future.result(timeout=deadline_seconds) else "not-paused"
            )
        except FutureTimeoutError:
            state = "timeout"
        except Exception as exc:  # noqa: BLE001 - recorded by type, never by message
            state = f"error:{type(exc).__name__}"
        executor.shutdown(wait=False)
        return PauseOutcome(state, time.monotonic() - started)

    def resume(self, handle: str) -> None:
        self.sandboxes[handle].resume()

    def kill(self, handle: str) -> None:
        self.sandboxes.pop(handle).kill()


def real_guest(*, region: str) -> GuestPort:
    return _RealGuest(region=region)


# ------------------------------------------------------------- ★ steps


@dataclass(frozen=True)
class CampaignOptions:
    caps_template: str
    execution_role_arn: str
    default_template: str | None = None
    efs11_pause_seconds: tuple[int, ...] = EFS11_PAUSE_SECONDS


@dataclass(frozen=True)
class MeasurementInfra:
    """The stack's outputs plus the access point, held in memory only for
    the duration of `run`: never saved, never printed (module docstring)."""

    file_system_id: str
    access_point_id: str
    mount_target_ip: str
    connector_arn: str
    connector_state: str
    mount_target_security_group_id: str
    deploy_seconds: float | None


@dataclass
class StepContext:
    aws: MeasurementAwsPort
    guest: GuestPort
    infra: MeasurementInfra
    options: CampaignOptions
    # Looked up when the context is built (not when this module loads), so
    # tests can swap `time.sleep` for the EFS-11 pauses.
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)


Step = Callable[[StepContext], QuestionResult]


@contextmanager
def launched(
    ctx: StepContext,
    *,
    template: str | None,
    with_connector: bool = False,
    with_role: bool = False,
    lifetime_seconds: int = MEASUREMENT_VM_TIMEOUT_SECONDS,
) -> Iterator[str]:
    """A MicroVM that is always killed, even when the step raises."""
    handle = ctx.guest.launch(
        template=template,
        lifetime_seconds=lifetime_seconds,
        egress=[ctx.infra.connector_arn] if with_connector else None,
        execution_role_arn=ctx.options.execution_role_arn if with_role else None,
    )
    try:
        yield handle
    finally:
        try:
            ctx.guest.kill(handle)
        except Exception as exc:  # noqa: BLE001 - its lifetime bounds it anyway
            print(
                f"kill failed ({type(exc).__name__}); VM lifetime bounds it",
                file=sys.stderr,
            )


def result(question: str, passed: bool, summary: str) -> QuestionResult:
    return QuestionResult(
        question=question,
        answered=passed,
        is_stop_criterion=question in STOP_CRITERIA,
        summary=redact(summary),
    )


def classify(outcome: CommandOutcome) -> str:
    """A guest command's failure, by kind: what the stop criteria care
    about is *why* it failed, never the raw text (which may carry ids)."""
    if outcome.ok:
        return "ok"
    if outcome.exit_code == TIMEOUT_EXIT_CODE:
        return "timeout"
    if "Operation not permitted" in outcome.stderr:
        return "EPERM"
    if "No such device" in outcome.stderr:
        return "ENODEV"
    tail = outcome.stderr.strip()[-SUMMARY_STDERR_CHARS:]
    return f"exit {outcome.exit_code}: {tail}"


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile; `samples` must not be empty."""
    ordered = sorted(samples)
    rank = max(1, round(fraction * len(ordered)))
    return ordered[rank - 1]


def efs_mount_command(infra: MeasurementInfra, mount_dir: str) -> str:
    """`mount -t efs` the way rayd's future adapter would (research doc
    §4.3): TLS, IAM, the access point and the mount target's IP."""
    return (
        f"mkdir -p {mount_dir} && mount -t efs -o tls,iam,"
        f"accesspoint={infra.access_point_id},mounttargetip={infra.mount_target_ip} "
        f"{infra.file_system_id}:/ {mount_dir}"
    )


def mount_efs(
    ctx: StepContext, handle: str, mount_dir: str = GUEST_MOUNT_DIR
) -> CommandOutcome:
    return ctx.guest.run_as_root(
        handle,
        efs_mount_command(ctx.infra, mount_dir),
        timeout_seconds=EFS_MOUNT_COMMAND_SECONDS,
    )


def measure_efs2(ctx: StepContext) -> QuestionResult:
    """Can root mount at all in the caps image (tmpfs), and does
    `mount -t nfs4` get past the kernel (a timeout against TEST-NET, not
    `EPERM`/`ENODEV`)? The default image's `EPERM` is recorded too."""
    nfs_command = (
        f"mkdir -p {GUEST_MOUNT_DIR}/nfs && timeout {EFS2_NFS_ATTEMPT_SECONDS} "
        f"mount -t nfs4 -o nfsvers=4.1,addr={UNROUTABLE_TEST_IP} "
        f"{UNROUTABLE_TEST_IP}:/ {GUEST_MOUNT_DIR}/nfs"
    )
    tmpfs_command = f"mkdir -p {GUEST_MOUNT_DIR}/tmpfs && mount -t tmpfs tmpfs {GUEST_MOUNT_DIR}/tmpfs"
    with launched(ctx, template=ctx.options.caps_template) as vm:
        tmpfs = ctx.guest.run_as_root(
            vm, tmpfs_command, timeout_seconds=QUICK_COMMAND_SECONDS
        )
        nfs = ctx.guest.run_as_root(
            vm,
            nfs_command,
            timeout_seconds=EFS2_NFS_ATTEMPT_SECONDS + COMMAND_TIMEOUT_MARGIN_SECONDS,
        )
    nfs_kind = classify(nfs)
    passed = tmpfs.ok and nfs_kind not in {"EPERM", "ENODEV"}
    summary = f"caps: tmpfs {classify(tmpfs)}, nfs4 to TEST-NET {nfs_kind}"
    if ctx.options.default_template is not None:
        summary += f"; default image: tmpfs {default_image_tmpfs(ctx, tmpfs_command)}"
    return result("EFS-2", passed, summary)


def default_image_tmpfs(ctx: StepContext, tmpfs_command: str) -> str:
    """EFS-2's informational half: the default image's `mount -t tmpfs`
    (expected `EPERM`). Never fails the ★ step: an image without
    `RAYITO_ALLOW_ROOT=1` rejects `user="root"` before the mount even runs,
    which is recorded by the exception's type only."""
    try:
        with launched(ctx, template=ctx.options.default_template) as vm:
            outcome = ctx.guest.run_as_root(
                vm, tmpfs_command, timeout_seconds=QUICK_COMMAND_SECONDS
            )
    except Exception as exc:  # noqa: BLE001 - informational, recorded by type only
        return f"not measured ({type(exc).__name__})"
    return classify(outcome)


def measure_efs3(ctx: StepContext) -> QuestionResult:
    """Does the dedicated connector reach `ACTIVE`, and does a VM launched
    with it open TCP 2049 on the mount target?"""
    active = ctx.infra.connector_state == CONNECTOR_ACTIVE
    probe_command = (
        f"timeout {EFS3_CONNECT_SECONDS} bash -c "
        f"'</dev/tcp/{ctx.infra.mount_target_ip}/{NFS_PORT}'"
    )
    with launched(ctx, template=ctx.options.caps_template, with_connector=True) as vm:
        probe = ctx.guest.run_as_root(
            vm,
            probe_command,
            timeout_seconds=EFS3_CONNECT_SECONDS + COMMAND_TIMEOUT_MARGIN_SECONDS,
        )
    deploy = (
        "unknown (resumed run)"
        if ctx.infra.deploy_seconds is None
        else f"{ctx.infra.deploy_seconds:.0f}s"
    )
    summary = (
        f"connector {'ACTIVE' if active else 'not ACTIVE'} (stack deploy {deploy}); "
        f"TCP {NFS_PORT} to the mount target: {classify(probe)} "
        f"in {probe.duration_ms:.0f} ms"
    )
    return result("EFS-3", active and probe.ok, summary)


def measure_efs8(ctx: StepContext) -> QuestionResult:
    """`mount -t efs -o tls,iam,accesspoint,mounttargetip` without
    systemd, `EFS8_MOUNT_SAMPLES` times: p50/p95, and whether `efs-proxy`
    runs while mounted."""
    with launched(
        ctx, template=ctx.options.caps_template, with_connector=True, with_role=True
    ) as vm:
        helper = ctx.guest.run_as_root(
            vm, "command -v mount.efs", timeout_seconds=QUICK_COMMAND_SECONDS
        )
        if not helper.ok:
            return result(
                "EFS-8",
                False,
                "mount.efs absent: the caps template lacks amazon-efs-utils (EFS-7)",
            )
        latencies: list[float] = []
        proxies = "unknown"
        for sample in range(1, EFS8_MOUNT_SAMPLES + 1):
            mount = mount_efs(ctx, vm)
            if not mount.ok:
                return result(
                    "EFS-8", False, f"mount sample {sample}: {classify(mount)}"
                )
            latencies.append(mount.duration_ms)
            if sample == EFS8_MOUNT_SAMPLES:
                count = ctx.guest.run_as_root(
                    vm,
                    "pgrep -c efs-proxy || true",
                    timeout_seconds=QUICK_COMMAND_SECONDS,
                )
                proxies = count.stdout.strip() or "0"
            ctx.guest.run_as_root(
                vm,
                f"umount {GUEST_MOUNT_DIR}",
                timeout_seconds=EFS_MOUNT_COMMAND_SECONDS,
            )
    summary = (
        f"{len(latencies)} mounts OK without systemd; p50 {percentile(latencies, 0.5):.0f} ms, "
        f"p95 {percentile(latencies, 0.95):.0f} ms; efs-proxy processes while mounted: {proxies}"
    )
    return result("EFS-8", True, summary)


def first_correct_read_seconds(
    ctx: StepContext, handle: str, path: str, expected: str
) -> float | None:
    """Seconds from now until `cat path` returns `expected`, or `None`
    past `EFS11_FIRST_READ_DEADLINE_SECONDS`."""
    started = ctx.clock()
    while ctx.clock() - started <= EFS11_FIRST_READ_DEADLINE_SECONDS:
        read = ctx.guest.run_as_root(
            handle, f"cat {path}", timeout_seconds=EFS_MOUNT_COMMAND_SECONDS
        )
        if read.ok and read.stdout.strip() == expected:
            return ctx.clock() - started
        ctx.sleep(EFS11_READ_POLL_SECONDS)
    return None


def measure_efs11(ctx: StepContext) -> QuestionResult:
    """Suspend/resume with the volume mounted, `EFS11_CYCLES_PER_PAUSE`
    cycles per pause length: does the data read back after resume, and
    how long until the first correct read?"""
    worst: list[str] = []
    for pause_seconds in ctx.options.efs11_pause_seconds:
        reads: list[float] = []
        with launched(
            ctx,
            template=ctx.options.caps_template,
            with_connector=True,
            with_role=True,
            lifetime_seconds=MEASUREMENT_VM_TIMEOUT_SECONDS
            + EFS11_CYCLES_PER_PAUSE * pause_seconds,
        ) as vm:
            mount = mount_efs(ctx, vm)
            if not mount.ok:
                return result("EFS-11", False, f"mount before pause: {classify(mount)}")
            for cycle in range(1, EFS11_CYCLES_PER_PAUSE + 1):
                path = f"{GUEST_MOUNT_DIR}/efs11-{pause_seconds}-{cycle}"
                expected = f"cycle-{pause_seconds}-{cycle}"
                ctx.guest.run_as_root(
                    vm,
                    f"echo {expected} > {path} && sync",
                    timeout_seconds=EFS_MOUNT_COMMAND_SECONDS,
                )
                paused = ctx.guest.pause(vm, deadline_seconds=PAUSE_DEADLINE_SECONDS)
                if paused.state != "paused":
                    return result(
                        "EFS-11",
                        False,
                        f"pause {pause_seconds}s cycle {cycle}: {paused.state}",
                    )
                ctx.sleep(pause_seconds)
                ctx.guest.resume(vm)
                seconds = first_correct_read_seconds(ctx, vm, path, expected)
                if seconds is None:
                    return result(
                        "EFS-11",
                        False,
                        f"pause {pause_seconds}s cycle {cycle}: no correct read within "
                        f"{EFS11_FIRST_READ_DEADLINE_SECONDS}s of resume",
                    )
                reads.append(seconds)
        worst.append(f"{pause_seconds}s: max {max(reads):.1f}s to first read")
    return result("EFS-11", True, "data intact after every resume; " + ", ".join(worst))


def measure_efs13(ctx: StepContext) -> QuestionResult:
    """`pause()` with the mount target unreachable (its security group's
    ingress revoked mid-session): does it return (either way) or hang?
    The ingress is always restored, even when the pause hangs."""
    security_group = ctx.infra.mount_target_security_group_id
    with launched(
        ctx, template=ctx.options.caps_template, with_connector=True, with_role=True
    ) as vm:
        mount = mount_efs(ctx, vm)
        if not mount.ok:
            return result("EFS-13", False, f"mount before isolation: {classify(mount)}")
        ctx.guest.run_as_root(
            vm,
            f"echo dirty > {GUEST_MOUNT_DIR}/efs13",
            timeout_seconds=EFS_MOUNT_COMMAND_SECONDS,
        )
        token = ctx.aws.isolate_mount_targets(security_group)
        try:
            paused = ctx.guest.pause(vm, deadline_seconds=PAUSE_DEADLINE_SECONDS)
        finally:
            ctx.aws.restore_mount_targets(security_group, token)
    passed = paused.state != "timeout"
    summary = (
        f"pause with the mount target unreachable: {paused.state} after "
        f"{paused.seconds:.0f}s (deadline {PAUSE_DEADLINE_SECONDS}s)"
    )
    return result("EFS-13", passed, summary)


#: The ★ steps this script measures itself; every other question in
#: `QUESTION_ORDER` stays manual (tasks.md §7 checklist).
AUTOMATED: Final[Mapping[str, Step]] = {
    "EFS-2": measure_efs2,
    "EFS-3": measure_efs3,
    "EFS-8": measure_efs8,
    "EFS-11": measure_efs11,
    "EFS-13": measure_efs13,
}


def run_step(question: str, step: Step, ctx: StepContext) -> QuestionResult:
    """A step that raises (an SDK or AWS error) is a failed step, recorded
    by the exception's type only."""
    try:
        return step(ctx)
    except Exception as exc:  # noqa: BLE001 - any failure is a recorded result
        return result(question, False, f"raised {type(exc).__name__}")


def run_campaign(
    state: RunState, ctx: StepContext, *, on_stop: Callable[[], int]
) -> int:
    """Runs every automated step not already answered, in `QUESTION_ORDER`.
    A ★ failure marks the run stopped and calls `on_stop` (`cleanup`)."""
    for question in QUESTION_ORDER:
        step = AUTOMATED.get(question)
        previous = state.result_for(question)
        if step is None or (previous is not None and previous.answered):
            continue
        outcome = run_step(question, step, ctx)
        state.record(outcome)
        save_state(state)
        print(
            f"{question}: {'OK' if outcome.answered else 'FAILED'} - {outcome.summary}"
        )
        if not outcome.answered and outcome.is_stop_criterion:
            state.stopped = True
            state.stop_reason = f"{question} failed: {outcome.summary}"
            save_state(state)
            print(f"STOP: ★ {question} failed; cleaning up now", file=sys.stderr)
            on_stop()
            return 1
    return 0


# ------------------------------------------------------------- run/cleanup


def _ensure_recorded(state: RunState, kind: str) -> None:
    if kind not in state.resources:
        state.resources.append(kind)
        save_state(state)


def _delete_one(aws: MeasurementAwsPort, kind: str, run_id: str) -> None:
    """Deletes the one resource of `kind` tagged with `run_id`, if any.
    Raised exceptions (a real AWS failure, never "already gone") propagate
    so the caller leaves `kind` in `state.resources` for a retry."""
    stack_name = measurement_stack_name(run_id)
    if kind == "client-policy":
        outputs = aws.stack_status(stack_name)
        if outputs is not None and "CallerPolicyArn" in outputs:
            aws.detach_client_policy(outputs["CallerPolicyArn"])
    elif kind == "stack":
        aws.destroy_stack(stack_name)
    elif kind == "file-system":
        file_system_id = aws.find_tagged_file_system(run_id)
        if file_system_id is not None:
            aws.delete_file_system(file_system_id)
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


def _new_or_resumed_state(args: argparse.Namespace) -> RunState:
    existing = load_state(args.run_id)
    if existing is not None:
        print(
            f"run {args.run_id!r} already exists in {state_path(args.run_id)}; resuming"
        )
        return existing
    now_ms = int(time.time() * 1000)
    state = RunState(
        run_id=args.run_id,
        region=args.region,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + MEASUREMENT_TTL_HOURS * 3600 * 1000,
    )
    save_state(state)
    return state


@dataclass(frozen=True)
class BorrowedNetwork:
    """A VPC and subnet the caller already has permission to use
    (`--vpc-id`/`--subnet-id`): `run` deploys into them but never creates,
    records or deletes them, so `cleanup` cannot touch them."""

    vpc_id: str
    subnet_id: str


def own_network(
    state: RunState, aws: MeasurementAwsPort, tags: Mapping[str, str]
) -> tuple[str, str]:
    """This run's throwaway VPC and subnet, each resolved by tag first."""
    vpc_id = aws.find_tagged_vpc(state.run_id)
    if vpc_id is None:
        vpc_id = aws.create_vpc(tags=tags)
    _ensure_recorded(state, "vpc")

    subnet_id = aws.find_tagged_subnet(state.run_id)
    if subnet_id is None:
        subnet_id = aws.create_subnet(vpc_id, tags=tags)
    _ensure_recorded(state, "subnet")
    return vpc_id, subnet_id


def provision(
    state: RunState,
    aws: MeasurementAwsPort,
    tags: Mapping[str, str],
    borrowed: BorrowedNetwork | None = None,
) -> tuple[Mapping[str, str], float | None]:
    """The network (own or `borrowed`) and the `efs-volumes` stack, each
    resolved first. The stack and its retained file system are recorded
    *before* the deploy, so a deploy that fails half-way is still cleaned
    up. Returns the stack's outputs and how long a fresh deploy took
    (`None` if resumed)."""
    if borrowed is None:
        vpc_id, subnet_id = own_network(state, aws, tags)
    else:
        vpc_id, subnet_id = borrowed.vpc_id, borrowed.subnet_id

    stack_name = measurement_stack_name(state.run_id)
    outputs = aws.stack_status(stack_name)
    deploy_seconds: float | None = None
    if outputs is None:
        _ensure_recorded(state, "file-system")
        _ensure_recorded(state, "stack")
        started = time.monotonic()
        outputs = aws.deploy_stack(
            stack_name, vpc_id=vpc_id, subnet_id=subnet_id, tags=tags
        )
        deploy_seconds = time.monotonic() - started
    _ensure_recorded(state, "file-system")
    _ensure_recorded(state, "stack")
    aws.tag_file_system(outputs["FileSystemId"], tags=tags)
    return outputs, deploy_seconds


def prepare_infra(
    state: RunState,
    aws: MeasurementAwsPort,
    outputs: Mapping[str, str],
    deploy_seconds: float | None,
    options: CampaignOptions,
) -> MeasurementInfra:
    """What the MicroVM steps need on top of the stack: the access point
    they mount and the client policy on the execution role (recorded
    before attaching, so `cleanup` always detaches it)."""
    file_system_id = outputs["FileSystemId"]
    _ensure_recorded(state, "client-policy")
    aws.attach_client_policy(options.execution_role_arn, outputs["CallerPolicyArn"])
    return MeasurementInfra(
        file_system_id=file_system_id,
        access_point_id=aws.ensure_access_point(
            file_system_id, MEASUREMENT_VOLUME_NAME
        ),
        mount_target_ip=aws.mount_target_ip(file_system_id),
        connector_arn=outputs["ConnectorArn"],
        connector_state=outputs.get("ConnectorState", ""),
        mount_target_security_group_id=outputs["MountTargetSecurityGroupId"],
        deploy_seconds=deploy_seconds,
    )


# ------------------------------------------------------------- subcommands


def cmd_plan(
    _args: argparse.Namespace,
    *,
    port: MeasurementAwsPort | None = None,
    guest: GuestPort | None = None,
) -> int:
    """Prints what `run` would create and its estimated cost; no AWS call."""
    del port, guest
    total = sum(resource.approx_cost_usd for resource in PLANNED_RESOURCES)
    print(
        f"m15-efs-volumes measurement campaign: {len(QUESTION_ORDER)} questions, "
        f"{len(STOP_CRITERIA)} stop criteria ({', '.join(STOP_CRITERIA)})"
    )
    print(f"Automated by run: {', '.join(AUTOMATED)}; the rest by hand (tasks.md §7)")
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


def borrowed_network(args: argparse.Namespace) -> BorrowedNetwork | None:
    if args.vpc_id is None or args.subnet_id is None:
        return None
    return BorrowedNetwork(vpc_id=args.vpc_id, subnet_id=args.subnet_id)


def cmd_run(
    args: argparse.Namespace,
    *,
    port: MeasurementAwsPort | None = None,
    guest: GuestPort | None = None,
) -> int:
    """Idempotent: provisions (or resolves) the infra, then measures the
    automated ★ steps not already answered. A ★ failure stops the run and
    cleans up; `--infra-only` stops after provisioning."""
    state = _new_or_resumed_state(args)
    if state.stopped:
        print(
            f"run {args.run_id!r} stopped earlier ({state.stop_reason}); run cleanup",
            file=sys.stderr,
        )
        return 1
    if (args.vpc_id is None) != (args.subnet_id is None):
        print(
            "--vpc-id and --subnet-id go together (a borrowed network)", file=sys.stderr
        )
        return 2
    if not args.infra_only and (
        args.caps_template is None or args.execution_role_arn is None
    ):
        print(
            "run needs --caps-template and --execution-role-arn (or --infra-only)",
            file=sys.stderr,
        )
        return 2

    aws = port or real_port(region=state.region)
    tags = resource_tags(args.run_id, state.expires_at_ms)
    outputs, deploy_seconds = provision(state, aws, tags, borrowed_network(args))
    print(f"state at {state_path(args.run_id)}; tags {tags}")
    print(
        "infra ready: throwaway VPC/subnet and the efs-volumes stack. No resource id is "
        "printed or stored here (AWS_API_NOTES.md §22) — use `rayito stack status "
        f"efs-volumes --stack-name {measurement_stack_name(args.run_id)}` to read "
        "FileSystemId/ConnectorArn when you need them."
    )
    if args.infra_only:
        return 0

    options = CampaignOptions(
        caps_template=args.caps_template,
        execution_role_arn=args.execution_role_arn,
        default_template=args.default_template,
        efs11_pause_seconds=tuple(args.efs11_pauses),
    )
    infra = prepare_infra(state, aws, outputs, deploy_seconds, options)
    ctx = StepContext(
        aws=aws,
        guest=guest or real_guest(region=state.region),
        infra=infra,
        options=options,
    )
    cleanup_args = argparse.Namespace(run_id=args.run_id)
    status = run_campaign(
        state, ctx, on_stop=lambda: cmd_cleanup(cleanup_args, port=aws)
    )
    if status == 0:
        print(
            "★ steps done; the manual questions (tasks.md §7) use this infra, then "
            f"`cleanup --run-id {args.run_id}`."
        )
    return status


def cmd_report(
    args: argparse.Namespace,
    *,
    port: MeasurementAwsPort | None = None,
    guest: GuestPort | None = None,
) -> int:
    """Renders results with every id redacted; reads only local state."""
    del port, guest
    state = load_state(args.run_id)
    if state is None:
        print(
            f"no state for run {args.run_id!r} at {state_path(args.run_id)}",
            file=sys.stderr,
        )
        return 1
    print(f"Run {args.run_id!r} ({state.region}, redacted)")
    if state.stopped:
        print(f"STOPPED: {redact(state.stop_reason or '')}")
    for question in QUESTION_ORDER:
        recorded = state.result_for(question)
        star = "*" if question in STOP_CRITERIA else " "
        if recorded is None:
            how = "automated" if question in AUTOMATED else "manual, tasks.md §7"
            print(f"  [{star}] {question}: pending ({how})")
        else:
            status = "OK" if recorded.answered else "FAILED"
            print(f"  [{star}] {question}: {status} - {redact(recorded.summary)}")
    return 0


def cmd_cleanup(
    args: argparse.Namespace,
    *,
    port: MeasurementAwsPort | None = None,
    guest: GuestPort | None = None,
) -> int:
    """Deletes everything tagged with this run id, in `CLEANUP_ORDER`;
    safe to re-run. A `kind` leaves `state.resources` only once its delete
    has succeeded (or the resource was already gone) — a real failure stops
    here and keeps the rest of the list for the next `cleanup` to retry, it
    never clears blindly. Recorded results survive for `report`."""
    del guest
    state = load_state(args.run_id)
    if state is None:
        print(f"no state for run {args.run_id!r}; nothing to clean up")
        return 0
    aws = port or real_port(region=state.region)
    for kind in [kind for kind in CLEANUP_ORDER if kind in state.resources]:
        try:
            _delete_one(aws, kind, args.run_id)
        except Exception as exc:  # noqa: BLE001 - any AWS failure stops cleanup here
            print(
                f"cleanup for run {args.run_id!r} stopped at {kind!r}: "
                f"{type(exc).__name__}: {redact(str(exc))}",
                file=sys.stderr,
            )
            return 1
        state.resources.remove(kind)
        save_state(state)
    if state.results:
        print(f"cleanup for run {args.run_id!r}: done; results kept for report")
    else:
        state_path(args.run_id).unlink(missing_ok=True)
        print(f"cleanup for run {args.run_id!r}: done, state removed")
    return 0


def _pause_list(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "plan", help="print the plan and its cost; no AWS call"
    ).set_defaults(func=cmd_plan)

    run_parser = sub.add_parser("run", help="run the campaign (idempotent by run-id)")
    run_parser.add_argument("--region", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument(
        "--caps-template",
        help="throwaway rayito-base-caps build with amazon-efs-utils (EFS-7)",
    )
    run_parser.add_argument(
        "--execution-role-arn",
        help="infra/iam.yaml's execution role; run attaches CallerPolicyArn to it",
    )
    run_parser.add_argument(
        "--default-template", help="rayito-base, to record EFS-2's EPERM there too"
    )
    run_parser.add_argument(
        "--efs11-pauses",
        type=_pause_list,
        default=list(EFS11_PAUSE_SECONDS),
        help="comma-separated EFS-11 pause lengths in seconds (default: research doc §9)",
    )
    run_parser.add_argument(
        "--vpc-id",
        help="borrowed VPC (with its owner's permission) instead of a throwaway one; "
        "never created, recorded or deleted (needs --subnet-id)",
    )
    run_parser.add_argument(
        "--subnet-id", help="borrowed subnet inside --vpc-id; never created or deleted"
    )
    run_parser.add_argument(
        "--infra-only",
        action="store_true",
        help="provision only; no MicroVM, no measurement",
    )
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
    argv: list[str] | None = None,
    *,
    port: MeasurementAwsPort | None = None,
    guest: GuestPort | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    status: int = args.func(args, port=port, guest=guest)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
