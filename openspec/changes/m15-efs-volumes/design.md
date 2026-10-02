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
component real, so both now use `"sizes-guard"` instead (tasks.md 5.6).
`tests/unit/test_m15_feature_options.py` / `test_m15_create_kwargs.py` and
their TypeScript mirrors used a bare `object()`/`{}` as the `volumes=`
value to test the generic "any 0.6 option raises `UnimplementedError`"
behaviour; `require_volume_support` now type-checks that value before
reaching `UnimplementedError`, so those rows now use a well-formed
`EfsVolume` (tasks.md 5.5).
