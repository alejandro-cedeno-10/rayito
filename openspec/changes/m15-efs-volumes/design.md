## Context

This change is `m15-efs-volumes`, branched from the integrated `main` after
`v06-foundations` merged. The architecture handed to this change says to
build everything that does not depend on the EFS-1..EFS-20 measurement
campaign, and to leave the campaign and the real mounting adapter for a
later change once its stop criteria (EFS-2, EFS-3, EFS-8) clear. This
`design.md` records the decisions internal to this change.

## Decisions

### D1. `features::efs_volumes::build()` stays `slot::Unsupported`, not a thin wrapper over `UnavailableEfsMounter`

The architecture names `adapters/efs_mount.rs` (`UnavailableEfsMounter`) as
this change's deliverable, alongside `features/efs_volumes.rs`. Two shapes
were possible: (a) keep the slot as the generic `slot::Unsupported` it
already is, with `UnavailableEfsMounter` existing as a standalone,
`VolumeMounter`-implementing, fully tested adapter that nothing wires up
yet; or (b) build a real `EfsVolumesFeature` that implements
`ConfigurableFeature<EfsVolumesConfig, EfsVolumesStatus>` by mapping the
proto section to `VolumeSpec`s, validating them with `VolumePlan`, and then
calling into `UnavailableEfsMounter` — which can only ever refuse.

(a) was chosen. (b) would be real-looking glue code with zero behavioural
difference from `slot::Unsupported` (both always answer
`SECTION_CODE_UNSUPPORTED`), built on a mapping (proto `EfsVolumeMount` ->
domain `VolumeSpec`) that the real adapter will need once it exists but
that nothing can yet exercise end-to-end (no mount ever succeeds to
validate the mapping against). Shipping it now means throwing it away or
reverifying it once a real `VolumeMounter` lands; `slot::Unsupported` is
honest, already covered by foundations' own test
(`every_slot_starts_unsupported`), and `UnavailableEfsMounter` still proves
the port is real and implementable.

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
