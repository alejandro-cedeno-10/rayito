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

### D6. A real `EfsUtilsMounter`, still gated by detection

Every stop criterion passed on 2026-10-04 (Q127–Q130), so the adapter the
port was built for replaces `UnavailableEfsMounter` (which would otherwise
be dead code). `support()` is `Supported` only with `CAP_SYS_ADMIN`, `nfs4`
in `/proc/filesystems`, `mount` on `PATH` and both `/usr/sbin/mount.efs` and
`/usr/sbin/efs-proxy` (Q122): no image Rayito publishes installs
`amazon-efs-utils`, so shipped images keep answering `UNSUPPORTED` and the
slot does not join the hooks (`participant()` is `None` while
unsupported). The SDK keeps `volumes=` in `UnimplementedError`; the image
layer and the `create()` wiring (sending the section, `DescribeMountTargets`
for `mount_target_ip`) are the next step, not this change.

The helper mounts on `/run/rayito/efs-staging` (root-only: `/run/rayito`
is created by `rayd` and is on `FilesystemService`'s deny list) and the
result is bind-mounted onto the requested path through the `O_NOFOLLOW`
walk `mounts=` uses — `mount(8)` would resolve a path argument that uid 1000
can swap for a symlink under `/home/user`. The walk moves out of
`fuse_device` into `adapters::mountpoint` (one implementation, two users).

### D7. `efs-proxy` is attributed, not spawned

`efs-proxy`'s command line and configuration are `efs-utils` internals that
`AWS_API_NOTES.md` does not document, so `rayd` keeps `mount.efs` as the
only thing that starts it. Mounts are serialized; the root processes whose
`/proc/<pid>/exe` is `/usr/sbin/efs-proxy` that appear between the snapshot
before the helper and the one after it are that mount's, pinned by pid and
start time (`rayd_core::volume::proxy`). Unmount sends `SIGTERM`, waits 400
ms, sends `SIGKILL` and waits 200 ms more (inside the 1 s `/terminate`
participant cap). The helper (`mount`, `mount.efs`) is spawned through
`ChildRegistry`, so the orphan reaper never takes its status; the proxy is
re-parented to PID 1 and `rayd` never `waitpid`s it, so its zombie is the
orphan reaper's by design and no status is ever contested.

### D8. `/resume` remounts on an expired lease, probes otherwise

Each mount records the execution-role lease expiry from the shared
`ImdsCredentialBroker` (the same IMDS set `efs-utils` read a moment
earlier, or an older one that expires no later). `/resume` remounts at once
when the lease has passed or is inside `REFRESH_MARGIN` (a probe would only
spend budget on a reconnection known to fail), otherwise probes with a
bounded `stat` child (800 ms) and remounts on failure. Remounts are tasks;
`on_resume` waits 1.5 s (inside the 2 s participant cap) and a slower one
keeps going, reported as `REMOUNTING` → `MOUNTED`/`DEGRADED` by
`ConfigureStatus`. Remounts and `Configure` share one async lock and each
entry carries the generation of the `Configure` that created it, so a
remount never writes into an entry a later `Configure` replaced. A
guest-side credential refresh of the running tunnel was rejected: it would
need `efs-utils` internals (the watchdog's certificate renewal) that are
neither documented nor measured.

### D9. `/suspend` flushes per volume; the loss is documented, not hidden

The slot asks for the whole sync deadline (`SuspendShares` caps it, and the
flushes run concurrently with the per-filesystem `syncfs`, so the hook never
waits longer) and runs one `syncfs` per volume on a throwaway thread
(`bounded_sync::spawn_syncfs_thread`), skipping a volume whose previous
flush never returned. A volume that does not finish becomes
`DEGRADED`/`flush_timeout` and is probed at `/resume`. With the mount
target unreachable the platform still terminates the VM (Q130); nothing in
the guest can prevent that, so the docs say it plainly.

### D10. Read-only in IAM; one egress connector enforced by the SDKs

uid 1000 reaches `efs-proxy`'s loopback port (Q133) and the guest kernel has
no `owner` match (Q48), so `ro` cannot be the control. The template gains
`ReadOnlyAccessPointArns`: an explicit `Deny` of
`elasticfilesystem:ClientWrite` conditioned on
`elasticfilesystem:AccessPointArn` (the key the file-system policy already
uses), which wins over `AllowWrite=true`. A guest-side `ip rule … dport
<port> prohibit` ahead of `local` (the M10 DNS-guard mechanism) was left out:
it must be coordinated with `NetworkManager`'s own guard and measured on AWS
first; T21 records it as pending hardening.

A MicroVM accepts one egress connector (Q131). `plan_features`/
`planFeatures` gain an optional `egress` argument (D2 anticipated the first
feature that needed it would add it) and `volumes=` requires exactly one
own connector, never `INTERNET_EGRESS`, before `run-microvm`; the error
names the alternative (internet through the customer's VPC NAT or transit
gateway and a connector that allows it). The shim always launches with
`INTERNET_EGRESS`, so its `volume_mounts` says so in its
`UnimplementedError`.

### D11. `create(volumes=)` goes through the single post-ready `Configure`

D2/D3/D6 kept `volumes=` in `UnimplementedError` until an image could
mount. With the opt-in image (D13) it joins the path every other 0.6
section uses, without `main.py`/`sandbox.ts` naming the feature:

- `plan_volumes`/`planVolumes` (pure) validates before anything else and
  adds what D2 deferred: the cap of 4 (`limits.json`
  `efsVolumesMaxPerSandbox`, the same cap as `rayd_core::volume`) and
  `execution_role_arn` (without it `efs-utils` would fail `iam_denied` after
  the MicroVM was paid for). `plan_features` gains the
  `execution_role_arn` argument D2 anticipated.
- `prepare_features`/`aprepare_features`/`prepareFeatures` is the new,
  generic pre-launch step (after the control plane, before `run-microvm`):
  with no `volumes=` it returns the plan untouched and builds no client;
  otherwise it resolves the missing mount target IPs and appends an
  `EfsVolumesSection` to `configure_sections`.
- `EfsVolumesSection` is a `ConfigureSection`, a `CapabilityGate` (its own
  message naming the opt-in image when `Health.features.efs_volumes` is
  false) and a new `SlowApplySection`: `rayd` mounts inside the
  `Configure` call itself (one volume at a time, 15 s helper timeout each),
  so the call deadline becomes `max(request_timeout, apply_timeout_s)` =
  at least 4 × 15 s + 5 s. `APPLIED` already means every volume `MOUNTED`
  (`rayd` stops at the first failure and answers `FAILED`); a future `rayd`
  answering `PENDING` is polled with the same bound (`check_status`).
- Failures map to `VolumeMountException(code)`/`VolumeMountError` (a
  closed list, never the helper's text); the shared path already terminates
  the sandbox unless `keep_on_failure`. `reincarnate()` re-enters `create()`
  with the stored feature options, so the successor re-resolves and
  re-sends the section. `sbx.volumes`/`volumes()` mirror `sbx.mounts`.

### D12. Mount target IP: one `DescribeMountTargets` per file system per `create()`

`rayd` mounts with `mounttargetip=` (research doc R3) so the guest never
depends on the VPC's DNS. The SDK resolves it with the caller's
credentials (`elasticfilesystem:DescribeMountTargets`, AWS_API_NOTES §22,
already listed) before launching, so a file system without mount targets
fails without paying for a MicroVM. `MountTargetResolver` is per
`create()` and caches per file system. EFS allows one mount target per AZ
and the SDK cannot know which subnet the connector uses, so it picks the
first `available` one by `AvailabilityZoneId` (research doc §4.5, "if
EFS-18 does not reveal the AZ, the first available"); every subnet of the
VPC reaches it (the mount-target group admits the connector's group), and
cross-AZ traffic is EC2 data transfer, documented. An `EfsVolume` that
carries `mount_target_ip` (validated as IPv4) skips the lookup.

### D13. `amazon-efs-utils` lives in an opt-in image variant

Q122: the RPM adds ≈ 198 MB of code install (37 packages, incl. Python 3.9
and `systemd`, which nobody starts) and re-points `/usr/bin/python3` to
3.9, which `rayd`'s IMDS probe runs by absolute path (Q48). The default
images must not pay for it, so `rayito image zip --with-efs` adds a marker
entry that the Dockerfile's conditional layer reads (the same mechanism as
`poly`), installs the pinned NEVRA and re-links `/usr/bin/python3` to 3.12;
`rayito image publish --with-efs` demands `--os-capabilities ALL` and the
marker, and names the image `rayito-base-caps-efs`, which the caps-variant
check accepts. `efs-utils` 3.1.3 takes the region from the `region` mount
option, then `AWS_REGION`/`AWS_DEFAULT_REGION`, then its configuration file
(`efs_utils_common/metadata.py:get_target_region`); `rayd` rebuilds the
helper's environment from scratch, so it now passes `AWS_REGION` (which
the platform injects, AWS_API_NOTES §9) and the image needs no
region-specific configuration.

### D14. The E2B shim mounts with a client-bound connector

The shim launches with `INTERNET_EGRESS` and a MicroVM takes one egress
connector (Q131), so `volume_mounts` cannot keep the E2B default. The
client gains `volume_connector_arn`/`volumeConnectorArn` (a Rayito
extension, off by default, next to `volume_store`): with `volume_mounts`
the sandbox's only egress is that connector, an explicit
`allow_internet_access=True` is refused, and `allow_internet_access=False`
is still forwarded (guest policy). The pure mapping validates (store,
connector, paths, caps) without I/O; the launch then maps a `Volume` with
its access point id and resolves a plain name with `VolumeStore.get`
(`asyncio.to_thread` in the async shim) before the native `volumes=` path.
