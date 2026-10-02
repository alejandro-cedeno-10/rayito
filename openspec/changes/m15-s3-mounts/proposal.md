## Why

Agentic code that needs to read or write a customer's own S3 data today has
no way to see it as ordinary files inside a Rayito sandbox: it must shell
out to the AWS CLI or the SDK, losing every tool (`pandas.read_csv`, `ls`,
`grep`) that expects a real path. E2B has no equivalent either (a known gap,
`e2b-parity.md` row 111), but AWS's own Mountpoint for Amazon S3
(`mount-s3`) makes a bucket-as-filesystem mount a solved problem on Linux —
exactly the capability `m15-foundations`' `ConfigureSandbox`,
`ChildRegistry`/orphan-reaping groundwork and `OptionalStack` convention
were built to let a feature add safely, off by default, with no Rayito-run
server.

## What Changes

- **`mounts=`/`mounts`, experimental, off by default**: zero or more
  `S3Mount(bucket, prefix="", read_only=True, allow_overwrite=False,
  allow_delete=False)` entries, keyed by an absolute guest path (the same
  `_mount_path` rule `volumes=` will share), sent to the agent only through
  `ConfigureSandbox`'s `s3_mounts` section — no new RPC, no new call when
  the option is absent.
- **Agent side (`rayd`)**: a real `s3_mounts` feature slot. `rayd` opens
  `/dev/fuse` and performs the kernel's FUSE `mount(2)` itself
  (`adapters::fuse_device`, raw `libc`, no new Cargo dependency), then
  launches `mount-s3 --foreground <bucket> /dev/fd/3 [...]` bound to that
  descriptor as the new dedicated system user `rayito-mount` (uid 990,
  `image/Dockerfile`) with an environment rebuilt from scratch holding only
  `AWS_REGION`/`PATH` (`adapters::mount_s3`): no credential ever reaches
  argv or the process environment (SEC-3). `mount-s3` resolves the
  execution role's credentials through its **own** IMDS call — uid 990 is
  deliberately below 1000, so M6's IMDS blackhole for the sandbox user
  range never blocks it, while uid 1000 stays blocked as always. Only
  `rayito-base-caps` (or a size-suffixed variant) can grant an execution
  role at all; `_role_policy.require_caps_for` rejects `mounts=` on any
  other named variant before `run-microvm`.
- **Bucket allowlist is image configuration, not a per-sandbox switch**: the
  image's `RAYITO_ALLOWED_MOUNT_BUCKETS` (comma-separated, baked in at
  `rayito image publish --env`) is the only thing that can ever widen what
  `mounts=` may touch; absent or empty denies every bucket (ADR-014 rule
  4 — no environment variable, in the guest or the image, is itself an
  activation switch for a cost feature; this one only narrows what an
  explicit `mounts=` call is still allowed to do).
- **`OptionalStack` component `s3-mounts`**: the managed policy
  `RayitoS3MountAccess` (`infra/s3-mounts.yaml`), scoped to one bucket per
  stack, `ListBucket` narrowed by an `s3:prefix` condition and
  `GetObject`/`PutObject`/`DeleteObject` scoped to the bucket (IAM has no
  native per-prefix object-action scoping across an arbitrary prefix list;
  real prefix containment is `mount-s3 --prefix` plus the image allowlist).
  $0 at rest; deployed only by an explicit `rayito stack deploy s3-mounts`.
- **No E2B shim surface**: E2B has nothing this maps to, so the shim is
  untouched; `e2b-parity.md` row 111 moves from "fuera por SPEC" to
  "divergente" (`docs-delta.md`).
- **Done in a review follow-up (§1 of "Non-blocking follow-ups" below, now
  applied)**: `Sandbox.create(mounts=)`/`AsyncSandbox.create`/TypeScript
  `create({ mounts })` now really send the `ConfigureSandbox` call and
  expose `sbx.mounts`/`sbx.mounts()`. Landing this needed a small
  foundations-level addition first (`_configure_base.py`/`configure-base.ts`
  gained `require_capabilities`/`requireCapabilities`,
  `build_configure_request`/`buildConfigureRequest` and
  `check_configure_response`/`checkConfigureResponse`, and
  `ConfigureSection` gained `check_result`/`checkResult`), then
  `create()`/`_open()` (`Sandbox.#open` in TypeScript) execute
  `plan.configure_sections` right after the first `Health`, inside the same
  failure path that already terminates the sandbox before `agent_ready` —
  so a missing capability or a rejected section terminates the VM (unless
  `keep_on_failure`) exactly like any other pre-`agent_ready` failure, with
  no separate termination path of its own.

## Non-blocking follow-ups (do not block this change)

1. ~~**`Sandbox.create(mounts=)` wiring.**~~ **Done** (review follow-up):
   `_feature_options.py`'s `mounts=`/`feature-options.ts`'s `mounts` branch
   now calls `_s3_mounts.plan_s3_mounts`/`planS3Mounts` (after
   `require_caps_for`/`requireCapsFor`), and `create()`/`_open()` send the
   post-`Health` `Configure` call and expose `sbx.mounts`
   (`sandbox_{sync,async}/main.py`) / `sbx.mounts()` (`sandbox/sandbox.ts`).
   A later feature (`volumes=`, `events=`, …) extends the same
   `configure_sections` seam in its own change rather than re-deciding this.
2. **Exact installed-size delta** for the `fuse` + `mount-s3` RPM layer
   (§Q80 estimate: +22.4 MB) needs confirming on a real image build —
   `image/Dockerfile`'s own comment documents how (`rpm -q --queryformat
   '%{SIZE}' mount-s3`), done once during this change's AWS acceptance.
3. **`AWS_API_NOTES.md` real question numbers** (S3M-1..S3M-4) are
   provisional until the serialized AWS acceptance stage hands out real
   `Q95+` numbers, per the M15 architecture's rule 9.
