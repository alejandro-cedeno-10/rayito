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

### D4. IAM policy scopes object actions to the declared prefixes too

`infra/s3-mounts.yaml`'s `Prefixes` parameter narrows `s3:ListBucket`
(IAM's `s3:prefix` condition key accepts the list via `StringLike`) **and**
`GetObject`/`PutObject`/`DeleteObject`/`AbortMultipartUpload`, whose
resources are object ARNs that carry the prefix
(`arn:<partition>:s3:::<bucket>/team7/*`). Containment must not rest only
on `mount-s3 --prefix` inside a guest that runs untrusted code: a sandbox
meant to write `runs/42/` must not be able to overwrite or delete another
prefix's objects with the execution role. CloudFormation has no
map-over-a-list primitive without the `AWS::LanguageExtensions` transform
(`CAPABILITY_AUTO_EXPAND`, which `OptionalStack` never requests), and
`Fn::Join`'s delimiter cannot be an intrinsic, so the template pads the
list with its own first entry to four slots and picks each one with
`Fn::Select` inside an `Fn::Sub`: up to four prefixes per stack (a second
stack for more); a shorter list only repeats a resource, the same grant.

### D6. The mountpoint is resolved without following symlinks

`rayd` runs as root, and uid 1000 owns `/home/user`: a lexically valid path
could be swapped for a symlink (before `Configure`, or between an unmount
and a watcher/`/resume` relaunch) so that root mounts a writable bucket
over `/usr/local/bin` or `/etc/cron.d`. `adapters::fuse_device` therefore
never passes the path to a syscall that follows links: it walks it from `/`
with `openat(O_PATH|O_DIRECTORY|O_NOFOLLOW)` (creating missing components
with `mkdirat` and re-opening them the same way), rejects any symlink as
`invalid_path`, mounts on `/proc/self/fd/<dirfd>` and unmounts through
`/proc/self/fd/<parentfd>/<last>` with `UMOUNT_NOFOLLOW`. A component-wise
walk was preferred to `openat2(RESOLVE_NO_SYMLINKS)` because it needs no
kernel-version probe and is unit-testable beneath a temporary root.

### D7. Support is decided by `CAP_SYS_ADMIN`, not by what the image ships

One `image/Dockerfile` builds all four variants, so `mount-s3`, `fuse` and
the `rayito-mount` user exist everywhere; only `rayito-base-caps` is
published with `additionalOsCapabilities: ["ALL"]`. `supported()` (and so
`Health.features.s3_mounts`) also requires `CAP_SYS_ADMIN` in rayd's
effective set (`adapters::capabilities`), the same `CapEff` reading M6
already logs; without it the SDK's after-boot gate terminates the VM and
raises `UnimplementedError` even for a custom image name.

### D8. `create()` waits for every mount to settle

`rayd` answers `SECTION_CODE_PENDING` for a new mount and attaches in the
background, so the SDK polls `ConfigureStatus` (every 250 ms, up to
`MOUNT_SETTLE_TIMEOUT` = rayd's own 10 s readiness bound plus 5 s) until
each requested mount is `mounted`; a `failed` one raises
`MountException(code=last_error_class)` and a still-`pending` one at the
deadline raises `code="timeout"`, both inside `_open`'s existing
terminate-on-failure window. The polling loop lives in the generic
Configure plumbing (`ConfigureSection.settle_timeout_s`/`check_status`), so
any later section that also answers `PENDING` reuses it.

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

Confirmed in the serialized AWS acceptance on 2026-10-02 (`AWS_API_NOTES.md`
Q100–Q104). Two defects surfaced there and are fixed in this change: the
image's `dnf` is `microdnf`, which cannot install a local RPM (now
`rpm -i` after `dnf install fuse fuse-libs`, Q100), and `mount-s3` needs
`--allow-other` or its FUSE session answers only uid 990 and the guest
gets `EACCES` (Q101). Installed size is 72 677 112 B for `mount-s3`
1.24.0, not Q80's +22.4 MB (that was AL2023's 1.22.3 package).

- The exact installed-size delta of the new image layer (Q80 estimated
  +22.4 MB).
- S3M-1..S3M-4 (read/write mounts, SEC-3's uid-1000 boundary, `/suspend`
  with S3 unreachable, `allow_internet_access=False`) and real `Q95+`
  numbers, per the M15 architecture's acceptance budget (§8, capped at
  $0.30 for this feature).
