## Context

This change is `m15-efs-volumes`, branched from the integrated `main` after
`v06-foundations` merged. The architecture handed to this change says to
build everything that does not depend on the EFS-1..EFS-20 measurement
campaign, and to leave the campaign and the real mounting adapter for a
later change once its stop criteria (EFS-2, EFS-3, EFS-8) clear. This
`design.md` records the decisions internal to this change.

## Decisions

### D1. `features::efs_volumes::build()` wires an `EfsVolumesSlot` over the `VolumeMounter` port

The architecture (§2/§7.1) wants `Health.features.efs_volumes` to come
from the mounter's own `support()`, not a hard-coded flag. `build()`
therefore returns `EfsVolumesSlot::new(Arc::new(UnavailableEfsMounter))`:
`supported()` is `mounter.support() == Supported`, and `apply()` answers
`SECTION_CODE_UNSUPPORTED` before reading the section while the mounter is
unsupported — so every shipped build behaves exactly as `slot::Unsupported`
did. Behind a mounter that does report support (only the fake in the
module's tests today) the slot validates the section into a `VolumePlan`
(`Invalid`, nothing touched, on a bad path, id or overlap), lazily
unmounts entries the new plan drops or changes, and mounts the rest in plan
order (`Failed` with the closed class on the first failure). A real
`mount -t efs` adapter is then `build()`'s one line plus whatever
`/suspend`/`/resume` participation EFS-11/EFS-13 call for; the dispatch
itself does not change. An earlier revision kept `slot::Unsupported` and
left `UnavailableEfsMounter` unused, which made the adapter dead code and
the "one call site" promise untrue.

### D2. `volumes=`'s SDK gate does not thread `execution_role_arn`/`egress` through `_feature_options.plan_features`

The architecture describes `volumes=` as needing `execution_role_arn=` and
a non-empty `egress=` before launch. `plan_features(options, *,
image_variant=None)` is a shared seam foundations built for all seven 0.6
options and explicitly commits to a stable signature ("ni esta firma... ni
FeatureOptions/FeaturePlan cambian") so the eight parallel feature changes
never collide on it. Extending it with `execution_role_arn`/`egress` only
for this one feature would break that commitment and could conflict with
another feature doing the same for a different reason.

Since this change's outcome is *always* `UnimplementedError` regardless of
whether the role/connector are present (no build of `rayd` can mount
anything yet), checking those two fields here would not change what the
caller sees — only reorder which message they get. `require_volume_support`
instead validates what it can from information `plan_features` already has
(value shape, mount paths via the shared `_mount_path`/`mount-path`
module, and the image variant via `require_caps_for`) and raises
`UnimplementedError` naming both the pending campaign and the
`execution_role_arn=`/`egress=` requirement the eventual real adapter will
enforce. The real adapter's own change is free to thread those fields
through once it actually needs to launch a VM and inspect `Health.features`
after boot — at that point `plan_features`'s signature is extended once, by
whichever change needs it first, same as `image_variant` was.

### D3. `sandbox_sync/main.py` / `sandbox_async/main.py` / `sandbox.ts` are not touched

A fully "real" `volumes=` (launch with the connector, wait for `Health`,
send `ConfigureSandbox`, terminate on `Unsupported`) would need these
foundations-owned files. Since the outcome is deterministically
`UnimplementedError` in this build (`UnavailableEfsMounter` never reports
support), launching a real VM only to discover that would cost AWS quota
for no behavioural gain and risk destabilizing the zero-cost golden test
and the other seven features' own `create()` integration points. This
change fails closed before `run-microvm`, which is strictly cheaper and
safer than the deferred-decision path other caps-gated features use for an
*unknown* image variant.

### D4. Two pre-existing shared-file test fixtures needed updating, not just `grpc/configure.rs`

Three tests assumed properties that making `efs-volumes` real broke:
`crates/rayd/src/grpc/configure.rs`'s own test constructed
`EfsVolumesStatus {}` as a struct literal (now needs `::default()` since
the message gained a field) and moved `request.efs_volumes` out of a
shared reference (now needs `.clone()`, since prost stops deriving `Copy`
once a message has a non-scalar field) — both covered in tasks.md 2.4.
`tests/unit/test_m15_stacks_service.py` /
`tests/unit/m15-stacks.test.ts` used `"efs-volumes"` as their example of
an *unsupported* `OptionalStack` component; this change made that
component real, so both now used `"sizes-guard"`; after merging main
(where `sizes-guard` became real too) they use `"custom-domain"`, the last
stub (tasks.md 5.6).
`tests/unit/test_m15_feature_options.py` / `test_m15_create_kwargs.py` and
their TypeScript mirrors used a bare `object()`/`{}` as the `volumes=`
value to test the generic "any 0.6 option raises `UnimplementedError`"
behaviour; `require_volume_support` now type-checks that value before
reaching `UnimplementedError`, so those rows now use a well-formed
`EfsVolume` (tasks.md 5.5).

### D5. Existing VPC first: `EfsVolumes` (check, deploy, destroy)

Most accounts cannot create a VPC (the test account's organization SCP
denies `ec2:CreateVpc`, Q124), so the quick path is an existing VPC:

- **Template**: `SubnetIds` is one `List<AWS::EC2::Subnet::Id>` (typed, so
  CloudFormation rejects a foreign id before creating anything) instead of
  `SubnetId1..3`. CloudFormation has no list length function; `HasSubnet2`/
  `HasSubnet3` read the n-th element of the list joined with two trailing
  empty elements (`Fn::Select` over `Fn::Split` of `Fn::Join`), which is
  `""` exactly when fewer subnets were passed. `MAX_SUBNETS = 3` matches the
  three mount-target slots; EFS allows one mount target per AZ. The stack
  never declares a network resource and every security-group rule hangs off
  its own two groups (template tests pin both).
- **Encryption**: `Encrypted: true` with the AWS-managed key. A
  `KmsKeyId` parameter was rejected: it is create-only, so a redeploy that
  re-sends its empty default would replace (and, with `Retain`, orphan)
  the file system — the same hazard D1 of the review removed.
- **IAM scope**: `AccessPointArns` (optional) narrows `RayitoEfsVolumeClient`
  to exact access points with `ArnEquals`; empty, it uses `ArnLike` on the
  stack's own account/Region access-point prefix, with `Resource` still this
  file system only. Access points are created after the stack by
  `VolumeStore`, so exact scoping is a redeploy with the new list.
- **Connector egress stays NFS-only**: the client group's only egress is TCP
  2049 to the mount targets. Internet egress through the VPC would depend on
  the VPC's own NAT and a connector that allows it (none here); combining
  with `INTERNET_EGRESS` is EFS-4. `check()` reports how many subnets route
  to a NAT, informationally.
- **Preflight**: `check()` is pure evaluation (`_network.py`/`network.ts`)
  over facts from a read-only `NetworkInspector` port (`Describe*` only);
  `deploy()` always runs it and refuses on `FAIL`. `rayito doctor` reuses
  the same evaluation (`efs-network`, only with `--efs-vpc-id`), so CHECK_NAMES
  and the default doctor run are unchanged.
- **Destroy**: the file system stays `Retain` (D1). `destroy(delete_file_system=True)`
  and `delete_file_system(id)` remove it explicitly, only when it carries the
  template's literal `rayito=efs-volumes` tag (never a foreign file system),
  after polling `DescribeMountTargets` empty (120 s budget), deleting its
  access points. Generic `rayito stack destroy` keeps it, documented.
- **Measurement script**: the throwaway-VPC path is gone; the VPC comes only
  from args/env, is never recorded, and `cleanup` has no network stage.
