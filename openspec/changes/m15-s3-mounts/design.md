## Context

Builds on `v06-foundations`'s `ConfigureSandbox`/`OptionalStack`/role-policy
groundwork. This change is the first of the eight parallel M15 features to
merge a real `FeatureSet` slot, so a few of its decisions are "first mover"
choices other features will want to look at (or deliberately diverge from)
when they fill their own slot.

## Decisions

### D1. The FUSE `mount(2)` and the `mount-s3` daemon are reused ports/adapters pair, not folded into one adapter

`rayd_core::s3_mount::ports` splits `FuseDevice` (attach/detach the kernel
mount) from `FuseDaemon` (spawn/probe/kill the user-space process), even
though today only one adapter pair exists for each. Kept separate because
they fail independently and in different ways (`EACCES` from `mount(2)`
vs. `mount-s3` exiting immediately for a missing IAM permission), and a
later change that wants to reuse just one side (say, a different mount
helper behind the same FUSE attach) does not have to touch the other.

### D2. `mount-s3` is spawned with `tokio::process::Command` directly, not through `rayd_core::process::ProcessSpawner`

The existing `ProcessSpawner` port plans a *sandboxed user's* command:
output streaming to `ProcessRegistry`, a server-side timeout, a CPU
`rlimit`, a request-supplied identity gated by `UserPolicy`. None of that
applies to an internal, long-lived daemon with a fixed, non-request-derived
identity (`rayito-mount`, uid 990) and no output anyone subscribes to.
Reusing it would mean either relaxing `UserPolicy` to allow a uid below
1000 (widening a security boundary meant for user-requested commands) or
duplicating most of `SpawnSpec`'s machinery for a single call site. A
dedicated, much smaller adapter (`adapters::mount_s3`) that reaps its own
child via `Child::wait()` was simpler and kept the boundary intact.

### D3. The adapter reaps its own child directly, independent of `ChildRegistry`/`OrphanReaper`

`v06-foundations` shipped `ChildRegistry`/`OrphanReaper` fully tested but
**not wired into `main.rs`'s PID-1 loop** (its own non-blocking follow-up:
activating it needs every direct spawn path to register first, see that
change's design.md). Since nothing consumes `ChildRegistry` yet, registering
the `mount-s3` pid there would do nothing beyond raising false confidence.
Instead `adapters::mount_s3::TokioMountS3Daemon` keeps an `Arc<Mutex<HashSet<i32>>>`
of pids it owns and spawns one `tokio::spawn(... child.wait() ...)` task per
daemon the moment it starts: the pid is removed from the set the instant
`wait()` resolves, so `is_alive()` never needs `/proc` or a signal-0 probe
and the daemon is reaped the moment it exits, with no window for it to
become an unreaped zombie. When `OrphanReaper` is eventually wired in, this
daemon's pid is still never double-waited: it is never a zombie to begin
with, because this adapter's own task always wins the race to reap it.

### D4. IAM policy scopes object actions to the bucket, not to `Prefixes`

`infra/s3-mounts.yaml`'s `Prefixes` parameter narrows `s3:ListBucket` (IAM's
`s3:prefix` condition key natively accepts a list via `StringLike`), but
`GetObject`/`PutObject`/`DeleteObject` are scoped to `<bucket>/*`: an IAM
resource ARN has no native "any of these N prefixes" form without one
statement per prefix, and CloudFormation has no map-over-a-parameter-list
primitive without a macro (`CAPABILITY_AUTO_EXPAND`, which this template
avoids, matching the rest of M15 foundations' stacks). This mirrors AWS's
own published example IAM policy for Mountpoint, which does the same
bucket-level scoping. Real prefix containment is `mount-s3 --prefix` plus
`RAYITO_ALLOWED_MOUNT_BUCKETS`; `ListBucket`'s own narrowing is defense in
depth, not the only thing standing between a mount and the rest of the
bucket.

### D5. `mount-s3` is fetched by pinned RPM download, not `dnf install` from an AL2023 repo

The original M15 architecture text assumed AL2023's own repos carried
`mount-s3`; they do not — AWS distributes Mountpoint for Amazon S3 as a
signed RPM/DEB/tar.gz from its own release bucket
(`s3.amazonaws.com/mountpoint-s3-release/...`), the same way the existing
`image/Dockerfile` already fetches Deno for `rayito-base-poly`. This change
follows that established pin-by-version-and-sha256,
`curl -o ... && sha256sum -c -` pattern instead, verified 2026-10-01 against
`mount-s3` 1.24.0 arm64
(sha256 `3636465c56908c7f26182d6f31aaa77e4e145330833863104f4cca0db1788343`).

## What only the AWS acceptance stage can confirm

- The exact installed-size delta of the new image layer (Q80 estimated
  +22.4 MB).
- S3M-1..S3M-4 (read/write mounts, SEC-3's uid-1000 boundary, `/suspend`
  with S3 unreachable, `allow_internet_access=False`) and real `Q95+`
  numbers, per the M15 architecture's acceptance budget (§8, capped at
  $0.30 for this feature).
