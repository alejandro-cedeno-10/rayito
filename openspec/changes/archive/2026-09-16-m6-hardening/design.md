## Context

State after M5 (accepted 2026-09-16 against real AWS, image `rayito-base`
10.0, SDK `rayito` 0.0.5): the six lifecycle hooks are real, `/run` is
accepted once per boot (`RunOutcome::AlreadyRan` afterwards), `/suspend`
closes streams without destroying anything and `/resume` bumps the
generation, probes kernels and spawns a background `reseed`. Spawned
processes get `RLIMIT_NPROC 512`, `RLIMIT_NOFILE` clamped to the inherited
hard 1024, `RLIMIT_CORE 0`; every process and PTY owns a 1 MiB output ring
(`DEFAULT_RING_CAPACITY_BYTES`), at most 256 live entries and 256 retained
ended entries; every execution owns a 4 MiB ring (≤ 32 retained).
`FilesystemService.Write` commits through a temporary in the destination
directory; `HealthService.Metrics` already reads `statvfs("/")`.
`scripts/publish_image.py` publishes `rayito-base` versions with a fixed
`IMAGE_HOOKS`; nothing prunes them (10 versions exist). The Python SDK
accepts `ingress=`/`egress=` on `create()` (managed names or ARNs,
`connector_arns`) but `SandboxInfo` does not report them back.

Measured facts that shape this design (`AWS_API_NOTES.md`):

- Hooks arrive from `127.0.0.1` over HTTP/1.1 and proxied client traffic
  also arrives from `127.0.0.1` (the proxy runs inside the VM); no
  `X-Forwarded-For`, no proxy header survives. A JWE with `allPorts`
  reaches `POST :9000/…/ready` (§7, §8, Q18, Q20, Q24). There is no
  per-boot secret AWS shares with the hooks: `/run`'s body is the only
  per-VM input and it is the one the attacker would be forging.
- A `/suspend` that answers non-200 terminates the VM in < 5 s (Q10):
  every runtime hook answers 200 whatever the phase machine decides.
- Root in the guest has `CapEff` without `sys_admin`, `net_admin` or
  `sys_ptrace`; `/sys/fs/cgroup` is not mounted; `iptables` is installed
  but unusable; `nft` and `sudo` are absent; `setrlimit(RLIMIT_NOFILE)`
  above 1024 fails with `EPERM` (Q20, Q30). `additionalOsCapabilities`
  accepts exactly `["ALL"]` and promises "mounts, netns, eBPF, iptables
  inside the VM" (§4). IMDS answers uid 1000 (Q1, Q20).
- `suspend-microvm` is 2 TPS and idempotent (§5, §11, Q38): a real
  `/suspend` never arrives faster than every 0.5 s and a real
  `/suspend`→`/resume` cycle takes ≥ 1.4 s + 1.2 s (Q4, §5).
- `update-microvm-image-version --status INACTIVE` while the image is
  `UPDATING` returns `ConflictException` (§4); `delete-microvm-image-version`
  is idempotent (model summary); versions are numbered `1.0`, `2.0`, …;
  50 versions per image, one week of storage minimum per version (§4, §11).
- Memory snapshot 572 MB without the scientific stack, 918–935 MB with it
  (Q34, M5 numbers); AWS reads ≈ 500 MB/s on launch and resume and bills
  $0.00155/GB read (§12, §15). Neither `/validate` nor any e2e test imports
  `scipy` or `sklearn`.
- The `reseed` op is queued behind the running cell in the sidecar
  (`run_internal` waits for its turn) and `OpTimeouts.reseed` is 15 s; a
  25 s cell in flight at `/resume` produced `sidecar op timed out`,
  `consecutive_timeouts 1` and later `reseeded 1` (Q39).
- `get-microvm` returns `ingressNetworkConnectors` and
  `egressNetworkConnectors` (§2, §6); own connectors need
  `lambda:PassNetworkConnector` (§10); VPC connectors are created in the
  `lambda-core` namespace or with CloudFormation `AWS::Lambda::NetworkConnector`
  (§1, §4).

Constraints: `openspec/project.md` hard rules (no invented AWS parameters,
`.proto` is the source of truth, hexagonal boundaries, ARM64 musl, clippy
pedantic, no `unwrap` outside tests, identifiers in English, no inline
comments in bodies, never log tokens, hook bodies, commands, output or
paths). This change is Track A of M6; it adds no SDK surface beyond what
each decision below names.

## Goals / Non-Goals

**Goals:**

- Close or honestly re-state every `SECURITY.md` row marked M6 (T1, T2,
  T7, T8) with a measurement in the acceptance test, not a promise.
- Make forged hooks measurably harmless and visible (`hook_anomalies`).
- Ship the IMDS block as an opt-in image variant that degrades to a
  logged no-op on the default image.
- Bound what a sandbox can consume of `rayd`'s memory (output rings), of
  CPU time per process (opt-in) and of the shared disk (`Write` reserve).
- Complete the egress story: a template a customer can deploy, the IAM it
  needs, the SDK fields that show what a sandbox got.
- Operational hygiene: prune image versions without hitting
  `ConflictException`; make the post-resume reseed never trip the kill
  switch; shrink the snapshot only where the numbers say so.

**Non-Goals:**

- The TypeScript client, the E2B shim, the cold-start burst benchmark, a
  pre-warmed pool, sidecar-in-Rust, per-sandbox metadata, S3/EFS
  persistence (other M6 tracks).
- cgroup2 slices when the platform still does not mount the controller
  (recorded as a limit; not emulated).
- A hook nonce, mTLS or any origin validation of hooks: not possible with
  what AWS exposes (measured, see D1).
- Blocking IMDS on the default image (no `CAP_NET_ADMIN`), or blocking it
  for root (`rayd` never reads credentials today, but the execution role
  is what CloudWatch logging and future outbound calls run under).
- An egress proxy inside the VM: the allowlist is the VPC security group.
- Changing `IMAGE_HOOKS` timeouts, `maximumDurationInSeconds` semantics
  (ADR-007) or any `run-microvm` parameter other than the connectors the
  SDK already passes.
- Deleting images (only versions), pruning across accounts or regions.

## Decisions

### D1. Hook origin: what can and cannot be defended (T2)

Origin validation is impossible with the platform as measured: AWS calls
the hooks from `127.0.0.1` over HTTP/1.1 without any authenticating
header, the proxy delivers client traffic from the same `127.0.0.1`, and
the only per-VM secret (`runHookPayload`) is delivered *by* the `/run`
hook, so it cannot authenticate `/run` itself. Considered and rejected:

- A per-boot hook nonce: AWS has no channel to share one (no env vars in
  `run-microvm`, image `environmentVariables` are cloned into every VM).
- Binding the hooks listener to a Unix socket or to a non-loopback
  address: the OpenAPI hook interface is TCP on `hooks.port`; nothing else
  is documented, and `/ready` in the build VM arrives with
  `Host: <endpoint>` through the same proxy path.
- Requiring the `smithy-java` user-agent or `x-amzn-requestid`: only
  `/ready` carries them; the runtime hooks arrive with `host:
  localhost:9000` and no user-agent (Q20), and a forger can copy both.
- A gRPC-side "attestation" of the client's JWE: the proxy strips
  `X-aws-proxy-*` before forwarding (§7); `rayd` never sees the token.

**Decision.** The primary control stays ADR-006 (hooks on 9000, never in
`allowedPorts`, the SDK never mints `allPorts`; `get_host(9000)` refused).
On top of it `rayd` adds three things that are defensible without origin:

1. **Audit log after `/run`.** Once a `/run` has been accepted this boot,
   every runtime hook call (`/run`, `/suspend`, `/resume`, `/terminate`)
   is logged at `info` as `hook_audit` with `hook`, `outcome`,
   `calls_since_run` (per-hook monotone counter) and `anomaly: true` when
   the call is anomalous. Anomalous = a `/run` after the accepted one
   (`already_ran`), the only call the audit can tell apart from a
   genuine one. Bodies, headers and payload characters are
   never logged (`payload_chars` on the first `/run` stays as today).
2. **No transition rate limit: a session-changing hook is never refused.**
   The first draft of this design put a per-hook token bucket (one
   accepted `/suspend` and one `/resume` per 2 s, `rate_limited` answers
   200) in front of the phase machine. The review found the sequence it
   breaks: a forged `/suspend` + `/resume` pair followed, inside the
   interval, by the platform's **real** `/suspend`. The real one would be
   refused (no stream close, no quiesce, no `sync`, phase left `Resumed`)
   while AWS checkpoints anyway, and the real `/resume` after the restore
   would then be an `unchanged` repeat: no generation bump, no kernel
   probe, no reseed, no `clock_offset`, and the frozen span absorbed by
   `running_time`, so every server deadline (process timeouts, the 30 s
   retention) fires early on restore. Repeating the forged pair every 2 s
   would starve the bucket for good. `rayd` cannot tell the forged call
   from the genuine one behind it, so the only rule that keeps the
   genuine one correct is to accept every session-changing call:
   `session.suspend()` and `session.resume()` are the plain phase machine
   (idempotent repeats stay `unchanged`). What bounds a `/suspend`
   `/resume` flood from an `allPorts` holder is the client, not `rayd`:
   every cut costs the SDK one reconnect on the 0.5 s → 4 s backoff and
   `ReconnectBudget` classifies the fourth futile cut as a failure, and
   the holder minted the token with an IAM that can already
   `TerminateMicrovm` (same blast radius, see the table). The audit still
   makes every call visible.
3. **`hook_anomalies` in `Health`.** `HealthSnapshot.hook_anomalies: u64`
   (`HealthResponse.hook_anomalies = 10`, proto3 `uint64`) = number of
   anomalous hook calls this boot. The SDK maps it into
   `SandboxHealth.hook_anomalies` and `_record_health` logs a `warning`
   the first time it becomes non-zero for a generation ("hooks forjados o
   repetidos detectados: %d") — the operator's signal that someone holds
   an `allPorts` token for that VM.

What a forger with an `allPorts` token can still do, and why each is
acceptable (this text goes to `SECURITY.md` T2 with the measured numbers
from the acceptance test, D19):

| Forged call | Effect on `rayd` | Bound |
|---|---|---|
| `/run` | `already_ran`: the installed digest is untouched, the kernel is not rotated, `hook_anomalies += 1` | none needed |
| `/suspend` | closes client streams with `suspending`, quiesces the sidecar, `sync`; **no process, PTY, kernel, execution or file is lost**; the stream gate stays closed until the watchdog of D2 reopens it (≤ 20 s) and the SDK's reconnection contract re-subscribes every handle | not refused by `rayd` (a refusal would also hit the genuine hook behind it); each cut costs the client one reconnect (0.5–4 s backoff), the fourth futile cut in a row ends the handle (`ReconnectBudget`), and the holder can already `TerminateMicrovm` |
| `/resume` | only after an accepted `/suspend`: bumps `resume_generation`, probes kernels (1–3 ms each), reseeds | same as `/suspend`: never refused, bounded by the client and by the IAM the holder already has |
| `/terminate` | schedules shutdown of both listeners: the sandbox becomes unreachable until AWS terminates it by `maximumDurationInSeconds` or the operator kills it | the same attacker can already `TerminateMicrovm` with the IAM that minted the token |

`/terminate` stays as it is: making it a no-op would risk AWS treating a
non-drained shutdown as a failure, and its blast radius equals what the
token holder already has.

### D2. A forged `/suspend` without a checkpoint: the session must recover

Today a `/suspend` moves the phase to `Suspending` and the stream gate
refuses new streams until `/resume`. If the `/suspend` was forged, AWS
never checkpoints and never calls `/resume`: the sandbox would stay
gated forever while `Health` still answers `agent_ready`. That is a DoS
the acceptance test would catch, so it is fixed here with a watchdog
that distinguishes "the VM was frozen" from "nothing happened":

- `rayd::lifecycle::suspend_watchdog`, spawned by every accepted
  `/suspend` with its `suspend_generation`: a loop of
  `tokio::time::sleep(WATCHDOG_TICK = 1 s)` that measures the monotonic
  `elapsed` between wakes. A real suspension freezes the VM after the
  checkpoint (≈ 1.4 s after the 200, Q38) and `CLOCK_MONOTONIC` then
  jumps by the suspended time at restore (Q19): a tick with `elapsed >
  FREEZE_THRESHOLD = 5 s` means "real suspend, AWS will call `/resume`"
  and the watchdog exits. If `SUSPEND_GATE_TIMEOUT = 20 s` of unfrozen
  ticks pass and the phase is still `Suspending` with the same
  generation, it calls `session.recover_from_stale_suspend(generation)`:
  phase to `Resumed`, gate reopened, `resume_generation` and
  `suspended_total` **untouched**, the suspend marked `stale_recovered`,
  `hook_anomalies += 1`, log `stale_suspend_recovered`. If `/resume`
  arrived first (the real case), the generation check makes it a no-op.
- The one race left — a VM suspended for less than 5 s whose `/resume`
  then takes more than 20 s to arrive — is closed on the other side:
  `session.resume()` from `Resumed` accepts the call when the last
  suspend is marked `stale_recovered` (bumps the generation, accumulates
  the suspended span from the original `suspended_at`, logs
  `resume_after_stale_recovery`); the anomaly already counted stays (one
  miscounted warning in a case that needs a > 20 s hook delay AWS has
  never shown, documented). `Transition` gains the `from: Resumed`
  case only for this path; every other `resume()` from `Resumed` stays
  `unchanged`.
- SDK: a handle cut with `suspending` follows the M5 contract (dormant
  while `get-microvm` says `SUSPENDING|SUSPENDED`, then the `Health`
  poll). D2 changes one rule: when `get-microvm` reports `RUNNING` the
  reconnect no longer waits for a **new** `resume_generation`; it
  re-subscribes as soon as `agent_ready && kernel_ready`, and a
  re-subscribe refused with `UNAVAILABLE suspending` (gate still closed)
  is retried on the same jittered backoff (0.5 s → 4 s) inside
  `reconnect_timeout` instead of failing. Re-subscription forms are
  unchanged (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`,
  `Reattach`); `reconnects` still counts one per cut.

### D3. Capability detection at boot (`rayd-core::capabilities`)

Pure module, host-tested: `parse_cap_eff(status: &str) -> Result<CapSet,
CapabilityError>` reads the `CapEff:` line of `/proc/self/status`
(hex mask); `CapSet::has(Capability)` for `CAP_NET_ADMIN` (bit 12),
`CAP_SYS_ADMIN` (21), `CAP_SYS_RESOURCE` (24), `CAP_SYS_PTRACE` (19).
`main.rs` logs once at boot `capabilities` with `net_admin`,
`sys_admin`, `sys_resource`, `sys_ptrace` (booleans) and `cgroup2_root`
(whether `/sys/fs/cgroup/cgroup.controllers` exists and is readable).
This is the re-measurement task (2) asks for, done by the agent itself on
every boot and visible in the build logs of the next publish; the e2e
reads the same facts through `commands.run("cat /proc/self/status")` as
uid 1000 only for `NoNewPrivs`/`CapEff` of the child, and through the
CloudWatch build log for `rayd`'s line.

**cgroup2 decision rule.** If `cgroup2_root` is `false` on the default
image and on the `ALL` variant, T7 is closed with D5–D7 and `SECURITY.md`
T7 says "cgroup2: platform limit, `/sys/fs/cgroup` not mounted (Q20,
re-measured M6)". If it is `true` on the `ALL` variant, this change still
does **not** implement slices (out of scope by the rule "one milestone,
measured first"); it records the fact as Q43 and the follow-up is a new
change. No code path depends on cgroups.

### D4. IMDS block for uid 1000 (`rayd::adapters::netfilter`, `cfg(unix)`)

**Image variant.** `scripts/publish_image.py --os-capabilities ALL` adds
`"additionalOsCapabilities": ["ALL"]` to `desired_configuration` (the
only value in `CapabilityList`, §4). `configuration_matches` already
compares every desired key, so a variant never reuses a default version.
The variant is published under its own image name so `rayito-base`'s
version history stays homogeneous: Makefile `image-publish-caps` =
`image-publish` with `PUBLISH_ARGS="--os-capabilities ALL --image-name
rayito-base-caps"`; the log group follows the name (`/rayito/
rayito-base-caps`). `image/Dockerfile` adds `iptables-nft` to the `dnf`
list (M0 saw `iptables` present in the probe image, but the product image
must not depend on the base's incidental contents) and the sanity loop
checks `iptables`. No other image difference: one Dockerfile, two names.

**Rule.** With `CAP_NET_ADMIN` present at boot (D3), `main.rs` calls
`netfilter::install_imds_block()`:

```
iptables -w 5 -I OUTPUT -d 169.254.169.254/32 -m owner ! --uid-owner 0 -j DROP
```

`-m owner --uid-owner` matches the socket owner: `rayd` (uid 0) keeps
IMDS, every process, PTY, kernel and the sidecar (uid 1000) lose it. The
rule is inserted before `/ready`, so it is part of the memory snapshot
(Firecracker snapshots the guest kernel state, the same reason loopback
sockets survive, §15). Failure modes are logged and never fatal:
`iptables` missing (`imds_block_unavailable`, `reason: "iptables not
found"`), non-zero exit (`reason: "iptables exit N"`, stderr never
logged), `xt_owner` unavailable (same, exit code path). IPv6: the guest's
IMDS IPv6 endpoint is not measured; `ip6tables` for `fd00:ec2::254` is
attempted only if `ip6tables` exists and its failure is logged at
`debug`, never affecting `imds_blocked`.

**Verification, `imds_blocked` semantics.** `imds_blocked` is `true` only
when all three hold, evaluated by a background task spawned by the first
accepted `/run` (after the 200, like the kernel rotation, budget 10 s):

1. `iptables -w 5 -C OUTPUT -d 169.254.169.254/32 -m owner ! --uid-owner
   0 -j DROP` exits 0 (re-inserted first if `-C` fails: a resumed or
   cloned VM must not trust the snapshot blindly);
2. a TCP connect to `169.254.169.254:80` **as root** succeeds within
   1 s (connect only, nothing is sent: `rayd` never reads credentials);
3. the same connect **as uid 1000** — through the existing
   `ProcessSpawner` with the M2 posture, program `/usr/bin/python3 -c
   "import socket,sys; s=socket.socket(); s.settimeout(2)…"` — fails
   (`TimeoutError`/`EHOSTUNREACH` → non-zero exit).

Logged as `imds_probe` with `rule_present`, `root_reachable`,
`user_reachable`, `imds_blocked`. `/resume` repeats only step 1 (cheap,
`-C`) and clears `imds_blocked` if the rule vanished (logged
`imds_rule_missing`). Without `CAP_NET_ADMIN` nothing runs, one `info`
line `imds_block_unavailable` at boot, `imds_blocked = false` forever.
`HealthSnapshot.imds_blocked: bool`, `HealthResponse.imds_blocked = 9`,
`SandboxHealth.imds_blocked`.

**Why not block for root too / why not a network namespace.** Root is
`rayd` only (T6), it never touches IMDS today, and CloudWatch runtime
logging is delivered by the platform, not by the guest; a namespace would
need `CAP_SYS_ADMIN` and would also cut the loopback path the proxy uses.

**Fallback.** If the `ALL` variant fails to build (`CONTAINER_BUILD_FAILED`
or any non-launchable gate) or a MicroVM from it never reaches
`agent_ready` in 90 s, the acceptance records the `stateReason`/`Health`
outcome as Q44, keeps `rayito-base` as the only supported image,
`SECURITY.md` T1 stays open with that measurement, and the e2e IMDS test
is skipped by the missing `RAYITO_TEMPLATE_CAPS`. The code path stays
(it is a no-op without the capability).

**Measured outcome (2026-09-16, ratified mechanism).** The `ALL` variant
builds and boots (`rayito-base-caps` 1.0–2.0: `CapEff` full,
`cgroup2_root: true`), but the owner rule cannot exist on this platform:
the Firecracker guest kernel (6.1 amzn2023, no loadable modules) ships
`x_tables` with only `conntrack icmp addrtype udplite udp tcp` as matches
and no `nf_tables`, so both `iptables-legacy` and the `nft` front-end fail
with "Extension owner revision 0 not supported". The block is therefore a
**policy route** (`rayd::adapters::imds_block`): `ip -4 rule add uidrange
1000-65535 lookup 100 priority 100` + `ip -4 route replace blackhole
169.254.169.254/32 table 100` (same pair for `fd00:ec2::254/128`, best
effort), `iproute` in the image instead of `iptables`. Same verification
(rule/route present via `ip … show`, root connect ok, uid-1000 connect
fails — with `EINVAL` at once), same `imds_probe` line, same fail-open.
The range starts at 1000, not 1: the platform's in-VM agent owns sockets as
uids 991–994 and keeps its own connection to `169.254.169.254:80`; a
blackhole for every non-root uid cut it and the image build's `/validate`
timed out after 10 min (`rayito-base-caps` 3.0, `UPDATE_FAILED`). Both
facts are Q48. This is a change of mechanism inside D4, not of contract:
`Health.imds_blocked`, the SDK warning and the e2e stay as specified.

### D5. `RLIMIT_CPU` per spawned process, per sandbox

`ResourceLimits` gains `cpu_seconds: Option<u64>` (`None` = unlimited,
the default): applied in `pre_exec` before the privilege drop as
`RLIMIT_CPU` soft = `N`, hard = `N + CPU_LIMIT_KILL_GRACE_SECONDS (5)`.
Linux delivers `SIGXCPU` at the soft limit (default action terminates)
and `SIGKILL` at the hard limit, so an ignoring process still dies 5 s
later. A process killed this way ends with `EndEvent{exited:false,
status:"signaled", signal: 24 (SIGXCPU) or 9}` — no new status.

Scope: `ProcessService.Start` and `PtyService.Create` children only. The
sidecar and the kernels are **never** limited (`RLIMIT_CPU` counts
cumulative CPU time: a limited kernel would die mid-session), so
`TokioSidecarLauncher` keeps `ResourceLimits { cpu_seconds: None, .. }`
explicitly and a host test asserts it.

Source of the value: the run payload. `RunPayloadWire` gains an optional
`limits: {"cpu_seconds": N}` object (`v` stays 1: an unknown or absent
`limits` means unlimited; `cpu_seconds` must be `1..=28800`, else the
payload is rejected as `MissingField`-style `InvalidLimits` → `tokenless`
mode, never a 4xx). `RunDefaults.cpu_seconds: Option<u64>` feeds
`plan_spawn`/`plan_pty` through `RunDefaults` exactly like `user` and
`workdir`. SDK: `Sandbox.create(cpu_time_limit: int | None = None)` (sync
and async), validated in `_payload.build_run_hook_payload` (`1..=28800`,
`InvalidArgumentException` otherwise), serialized only when set so the
4096-char budget is untouched for everybody else. Per-request overrides
are out of scope (E2B has none).

### D6. Sandbox-wide output byte budget (`rayd-core::process::budget`)

`OutputBudget` is a shared `Arc<AtomicUsize>` with
`SANDBOX_OUTPUT_BUDGET_BYTES = 128 MiB` and two operations: `charge(n) ->
bool` (true if the total stays under budget) and `release(n)`. Every
ring that retains bytes for replay charges what it holds: process/PTY
rings (`process::ring::OutputRing`, 1 MiB each) and execution rings
(`code::ring::ExecuteRing`, 4 MiB each). Rules, all pure and host-tested:

1. A ring `push` first evicts its own oldest chunks as today (per-ring
   capacity), then charges the chunk against the budget; if the budget
   is exhausted it evicts its own oldest chunks until the charge fits;
   if the ring is already empty and the chunk still does not fit, the
   chunk is delivered live to subscribers but **not retained**
   (`oldest_seq` moves past it) — a later `Connect(from_seq)` gets
   `OUT_OF_RANGE`, which the SDK already turns into a `from_seq=0`
   fallback with a warning.
2. Ended entries are reaped by the existing 30 s retention; while the
   budget is above `OUTPUT_BUDGET_HIGH_WATER = 96 MiB` the reaper
   additionally drops ended entries oldest-first until below it,
   regardless of age (retention is a courtesy, memory is a limit).
3. Dropping an entry releases its bytes; the counter never underflows
   (`fetch_sub` guarded by the entry's own recorded size).

Worst case before D6: 256 × 1 MiB + 32 × 4 MiB = 384 MiB of rings in a
2 GB VM shared with kernels; after: 128 MiB. Per-subscriber channels
(64 × 32 KiB, 256 events for execute) are unchanged: they are bounded per
subscriber and released when it leaves. `Health` is not extended; the
budget's level is logged by the reaper only when it crosses the high
water mark (`output_budget_bytes`, `entries_dropped`).

### D7. Disk reserve before `Write` (`FileSystem::free_bytes`)

The `FileSystem` port gains `free_bytes(&self, id: &FsIdentity,
canonical_dir: &str) -> Result<u64, FsIoError>` (`StdFileSystem`:
`nix::sys::statvfs::statvfs(dir)` → `f_bavail * f_frsize`, the space a
non-root user may use; the fake returns a configurable number).
`WriteSession::begin` calls it once per file after canonicalisation and
the deny list, before the temporary is created, and refuses the file with
`FilesystemError::DiskReserve` → gRPC `RESOURCE_EXHAUSTED` with details
`disk_reserve` when free < `DISK_RESERVE_BYTES = 256 MiB`. Files already
committed in the same stream stay committed (per-file atomicity, M3).
`ENOSPC` from `write_chunk`/`commit` maps to the same status with details
`disk_full` (today it surfaces as `INTERNAL`). The reserve keeps
`/run/rayito` (ipc sockets, connection files), the sidecar's stderr and
`rayd`'s temporaries working when a sandbox fills its home through
`files.write`; a shell can still fill the disk (T7 residual, documented),
`Metrics.disk_used_bytes` shows it and `Write` refuses to make it worse.
The SDK maps `RESOURCE_EXHAUSTED` to `RateLimitException` today; D7 adds
`DiskFullException(SandboxException)` chosen by the status details
`disk_reserve`/`disk_full` (details are already read for `suspending`).

### D8. `image_prune.py`: keep N newest launchable versions, serialize deletes

`scripts/image_prune.py --image-name rayito-base [--keep 5] [--dry-run]
[--region-from-env]` (boto3 only, same `Aws` helper shape as
`publish_image.py`, `--stack-name` not needed):

1. `list_microvm_image_versions` (paginator) → all versions; sort by
   `createdAt` descending.
2. **Keep set** = the newest `--keep` versions with `state == SUCCESSFUL
   and status == ACTIVE`, plus every version `imageVersion` referenced by
   a MicroVM whose state is not `TERMINATED`/`TERMINATING`
   (`list_microvms(imageIdentifier=arn)` paginated; items carry
   `imageVersion` and `state`, §6), plus any version in `DELETING`,
   `DELETED` or `PENDING`/`IN_PROGRESS` (a build in flight is never
   touched).
3. **Prune set** = everything else (older `SUCCESSFUL` versions,
   `INACTIVE` ones, `FAILED` and `DELETE_FAILED` ones), oldest first.
4. For each candidate, serially: `delete_microvm_image_version(
   imageIdentifier=arn, imageVersion=v)`; on `ConflictException` wait
   until `get_microvm_image(arn).state` is not `UPDATING`/`DELETING`
   (poll every 5 s, `--wait-timeout` 600 s) and retry, at most 5 attempts
   with 5/10/20/40/80 s backoff; then wait until
   `get_microvm_image_version` reports `DELETED` or raises
   `ResourceNotFoundException` **and** the image state has left
   `DELETING`, before the next delete. `ThrottlingException` uses the
   client's standard retries plus the same backoff.
5. Print a table (version, state, status, createdAt, action) and a JSON
   summary; `--dry-run` prints the plan and exits 0 without calling any
   mutating API. Exit 1 if any candidate is still present after its
   attempts (with the last `ConflictException` message).

Deactivating (`update-microvm-image-version --status INACTIVE`) before
deleting is **not** required by the model and is not done; if the
acceptance run shows `delete` refusing an `ACTIVE` version
(`ValidationException`/`ConflictException` with a status reason), the
script deactivates first, waits for the image to leave `UPDATING`, then
deletes — recorded as Q45 either way. Unit tests in
`scripts/tests/test_image_prune.py` with `botocore.stub.Stubber` cover:
keep-set selection (newest N, live MicroVM pin, in-flight build), the
`ConflictException` → wait → retry path, dry-run makes no mutating call,
and the summary shape. Makefile: `image-prune` (`PRUNE_ARGS ?= --keep 5`)
and `test-scripts` (`uv run --with "$(BOTO3_SPEC)" --with pytest pytest
scripts/tests`); `make lint` already covers `scripts` with ruff.

### D9. Reseed after resume: inline when idle, deferred when busy

Sidecar (`server._op_reseed`): for every context, `context.reseed_plan()`
returns `idle` (state `ready`, nothing running, empty queue), `busy`
(state `ready` with a running or queued cell) or `unavailable`
(`restarting`/`dead`). Idle contexts are reseeded inline (the cell takes
milliseconds); busy ones get `asyncio.create_task(context.reseed())` and
are listed as `deferred` (the task takes its turn after the running cell
through the existing `run_internal` queue, logs `reseeded_deferred` with
`context_id` and `outcome`); unavailable ones are `failed`. The reply is
`{"reseeded": [...], "deferred": [...], "failed": [...]}` and goes out
within the op's first pass over the contexts (bounded by 8 contexts × one
silent cell). Golden `protocol_v1.jsonl` gains the new reply shape;
`ReseedPayload.deferred: Vec<String>` (`serde(default)` so older replies
still decode).

`rayd` (`code::supervisor`): `SidecarOp::is_advisory()` is `true` for
`Reseed` only; a timeout on an advisory op is logged
(`advisory_op_timeout: true`) and neither increments
`consecutive_timeouts` nor kills the sidecar. `spawn_resume_reseed` logs
`reseeded`, `deferred`, `failed` counts. Two unit tests: sidecar
`test_kernel.py::test_reseed_replies_immediately_while_a_cell_runs`
(start `time.sleep(3)` on the real kernel, send `reseed`, reply within
1 s with `deferred: ["default"]`, then the sequence differs after the
cell) and `rayd` `code::supervisor` host test with the fake sidecar
(`--reseed-delay-ms 20000`: `Reseed` times out, `consecutive_timeouts`
stays 0, the next op succeeds).

### D10. Snapshot diet by measurement

Measurement (one MicroVM of 10.0, ≈ $0.01, e2e helper
`measure_warmup_rss`): as uid 1000 run `python3 -c` that imports in the
warm-up order and prints `resource.getrusage(RUSAGE_SELF).ru_maxrss`
after each step: baseline interpreter, `numpy`, `pandas`,
`matplotlib.pyplot` + one `savefig(BytesIO)`, `scipy.stats`,
`sklearn.linear_model`. Record the deltas as Q46.

Decision rule (fixed here, applied by the implementer with the numbers):
`scipy.stats` + `sklearn.linear_model` leave `0004_warmup.py` **iff**
their combined delta exceeds 100 MB. They are not imported by
`VALIDATE_CELL`, by any e2e test or by the sidecar; the packages stay
pinned and installed (a user cell `import sklearn` still works, paying
its import once from the lazily-restored disk, exactly like any package
outside the warm-up). The M4 rule "trim when memory > 1.2 GB" is replaced
by this measured rule; the warm-up docstring and `ARCHITECTURE.md` list
follow. If the rule fires, the next publish reports
`memorySnapshotSizeInBytes` before (10.0: 919 146 496 B) and after, plus
`run-microvm → Health.kernel_ready` p50 over the e2e session's MicroVMs
before/after, in `MILESTONES.md`. If it does not fire, the numbers are
reported and the list stays.

### D11. Egress allowlist: SDK fields, template, IAM

**SDK.** `SandboxInfo` gains `ingress: tuple[str, ...] = ()` and
`egress: tuple[str, ...] = ()` filled from `ingressNetworkConnectors` /
`egressNetworkConnectors` of `run-microvm` and `get-microvm` (§2, §6;
absent → empty). `SandboxListItem` is unchanged (items do not carry
them). `create(egress=[...])` keeps accepting managed names
(`INTERNET_EGRESS`) or ARNs; the README recipe shows
`egress=["arn:aws:lambda:<region>:<acct>:network-connector:rayito-egress"]`
and `ingress` left at the default. The e2e asserts `get_info().egress`
echoes what was passed.

**Template** `infra/egress-connector.yaml` (CloudFormation, fields
quoted from the official template reference for
`AWS::Lambda::NetworkConnector`, `Config` and `VpcEgressConfiguration`):

```yaml
Parameters:
  SubnetIds:          List<AWS::EC2::Subnet::Id>
  VpcId:              AWS::EC2::VPC::Id
  AllowedCidrs:       CommaDelimitedList   # default 0.0.0.0/32 (nothing)
  AllowedPort:        Number               # default 443
  ConnectorName:      String               # default rayito-egress
Resources:
  OperatorRole:       AWS::IAM::Role       # trust lambda.amazonaws.com,
                                           # inline policy ec2:CreateNetworkInterface,
                                           # ec2:DescribeNetworkInterfaces, ec2:DeleteNetworkInterface,
                                           # ec2:DescribeSubnets, ec2:DescribeSecurityGroups,
                                           # ec2:DescribeVpcs, ec2:AssignPrivateIpAddresses,
                                           # ec2:UnassignPrivateIpAddresses
  EgressSecurityGroup: AWS::EC2::SecurityGroup
                                           # no ingress; SecurityGroupEgress = one rule per
                                           # AllowedCidrs entry, tcp AllowedPort (via Fn::Select
                                           # on a fixed max of 5 entries, blanks skipped with Conditions)
  Connector:          AWS::Lambda::NetworkConnector
    Properties:
      Name: !Ref ConnectorName
      OperatorRole: !GetAtt OperatorRole.Arn
      Configuration:
        VpcEgressConfiguration:
          AssociatedComputeResourceTypes: [MicroVm]
          NetworkProtocol: IPv4
          SecurityGroupIds: [!Ref EgressSecurityGroup]
          SubnetIds: !Ref SubnetIds
Outputs:
  ConnectorArn: !GetAtt Connector.Arn
  CallerPolicyStatement: the `lambda:PassNetworkConnector` statement to add
```

The operator role's trust policy carries no `aws:SourceAccount`
condition (`AWS_API_NOTES.md` §10 notes aws-samples' operator role omits
it; recorded as the reason). The security group is the allowlist: with
`AllowedCidrs` at its default nothing leaves; the subnet needs no NAT for
the allowlist to hold (traffic to non-allowed destinations is dropped by
the SG before routing matters); allowing a destination that is on the
internet additionally needs a NAT gateway or an egress-only path the
customer owns (documented, not provisioned: NAT costs money).

**IAM.** `infra/README.md` documents the caller statement
(`lambda:PassNetworkConnector` on the connector ARN) next to the existing
`spike/m0/iam.yaml` `CallerPolicy`, which is extended with a
`NetworkConnectorArns` parameter (list, default empty) so one stack
covers both; `SECURITY.md` "IAM" section points at `infra/`.

**Validation and deploy gate.** `make infra-lint` runs
`aws cloudformation validate-template --template-body file://infra/
egress-connector.yaml` (server-side, free) and `uvx cfn-lint infra/
egress-connector.yaml`; if cfn-lint's bundled spec predates
`AWS::Lambda::NetworkConnector` the E3006 for that one type is ignored
with `--ignore-checks E3006` and the cfn-lint version is recorded. The
acceptance deploys the stack **only** when all three hold: (a)
`ec2:DescribeVpcs` in `us-east-1` returns a VPC with at least one subnet
**that belongs to this project or was explicitly lent by its owner**
(the account is the shared testing account: the stack creates a security
group and the connector's ENIs *inside* that VPC, which is not something
to do in another workload's network without asking; nothing is created
outside the stack), (b) the Lambda pricing page section for MicroVMs
lists no charge for network connectors (checked at acceptance time; ENIs
themselves are free), and (c) nothing else: the deny-all default needs
**no NAT gateway** (the security group drops the traffic before routing
matters), so the absence of a NAT is never a reason to skip. Then: stack
`rayito-egress-e2e`, `AllowedCidrs`
default (deny all), e2e `test_egress_allowlist` creates a sandbox with
`egress=[ConnectorArn]` and asserts `commands.run("python3 -c 'import
urllib.request; urllib.request.urlopen(\"https://example.com\",
timeout=5)'")` exits non-zero within 10 s while the same command in a
default sandbox exits 0; `get_info().egress == (ConnectorArn,)`; the
stack is deleted in teardown. Whether AWS still attaches
`INTERNET_EGRESS` when only an own connector is passed is what that test
measures (Q42). If (a) or (b) fails, the test is skipped by the missing
`RAYITO_EGRESS_CONNECTOR_ARN` and the template ships with the dry-run
validation only; Q42 is recorded as "not measured: <reason>" and
`SECURITY.md` T8 says the allowlist is validated, not measured.

### D12. `Health` additions and the SDK mirror

`HealthResponse` gains `bool imds_blocked = 9` and `uint64
hook_anomalies = 10` (comments in Spanish per the proto convention).
`HealthSnapshotBuilder` gains both setters; `grpc/health.rs` fills them
from `SandboxSession` (`hook_anomalies`) and from the netfilter state
(`imds_blocked`, an `Arc<AtomicBool>` owned by `main.rs`, `false` on
non-unix). `SandboxHealth` gains `imds_blocked: bool` and
`hook_anomalies: int`; `_record_health` warns once per generation when
`hook_anomalies > 0` and once at first sight when `imds_blocked` is
`False` **and** the sandbox was created with an `execution_role_arn`
("las credenciales del execution role son legibles por el código del
sandbox: usa la imagen con capabilities o quita el rol"). `imds_blocked`
is `false` until the D4 verification finishes, and that verification is
spawned at `/run` with a 10 s budget while readiness (`kernel_ready`)
lands right after `/run` (measured: `imds_blocked=True` 0.10 s *after*
`kernel_ready` on the capabilities image), so a warning evaluated on the
readiness `Health` would be a false positive on the very image built to
close T1. The SDK therefore evaluates the warning only on a `Health`
whose `uptime_ms` is at least `IMDS_VERIFY_BUDGET_MS = 10_000` past the
`uptime_ms` of the first `Health` it recorded (the readiness one, which
is after the `/run` that started the verification): before that, `False`
means "not verified yet"; after it, `False` is a verdict. `buf breaking`
reports only the two additive fields.

### D13. Logging allowlist additions

`logging.rs` gains: `hook_audit`, `calls_since_run`, `anomaly`,
`hook_anomalies`, `stale_suspend_recovered`,
`capabilities`, `net_admin`, `sys_admin`, `sys_resource`, `sys_ptrace`,
`cgroup2_root`, `imds_probe`, `imds_blocked`, `rule_present`,
`root_reachable`, `user_reachable`, `imds_block_unavailable`,
`imds_rule_missing`, `cpu_seconds`, `output_budget_bytes`,
`entries_dropped`, `disk_reserve`, `disk_full`, `free_bytes`,
`deferred`, `advisory_op_timeout`. Still never logged: hook bodies,
iptables stderr, the probe's output, paths, credentials (the root probe
never sends a request).

### D14. Tests

**rayd-core (host, Windows):** `SandboxSession` never refuses a
transition (a forged `/suspend` + `/resume` pair then a `/suspend` 1 s
later is `changed`, and the `/resume` after a 300 s jump bumps the
generation and accumulates `suspended_total`; back-to-back cycles with
no gap are all `changed`; idempotent repeats change nothing), `HookAudit`
counters and `hook_anomalies`, `recover_from_stale_suspend` (generation unchanged,
gate reopened, no-op after `/resume`), `parse_cap_eff` (M0's mask →
no `net_admin`; a mask with bit 12 → `net_admin`), `RunPayload` with and
without `limits` (bounds, rejection), `ResourceLimits` `cpu_seconds`
soft/hard derivation, `OutputBudget` rules 1–3 with two rings sharing a
small budget, `ExecuteRing` charging, `WriteSession` reserve refusal
with the fake `free_bytes`, `ENOSPC` mapping, `ReseedPayload` with and
without `deferred`, `SidecarOp::is_advisory`.

**rayd integration (`cfg(unix)`):** `m6_hooks.rs` (forged `/run` after
`/run` → `already_ran`, digest unchanged: a token minted before still
authenticates; ten `/suspend` in 1 s → one `changed`, nine
`unchanged`, all 200, no anomaly; forged `/suspend` + `/resume` then a
`/suspend` at +1 s → `changed` with the streams closed, and the `/resume`
after a 30 s clock jump → `changed`, generation bumped, kernels probed,
`suspended_total` ≥ 30 s, `hook_anomalies` 0; five back-to-back cycles
all `changed`; `/suspend` then no `/resume` for
`SUSPEND_GATE_TIMEOUT` (test-scaled through a `SessionSettings` knob) →
`Start` works again and `resume_generation` unchanged, `Health.
hook_anomalies` counts the recovery; `/suspend` then a simulated
freeze — the test's fake clock jumps 30 s — then `/resume` → `changed`,
no recovery logged; `/suspend`, recovery, then `/resume` → `changed`
with `resume_after_stale_recovery`), `m6_limits.rs` (`cpu_seconds:
1` on `python3 -c "while True: pass"` → `signaled` with signal 24 or 9
within 7 s; sidecar spawn spec has `cpu_seconds: None`; output budget
scaled to 256 KiB → third process's replay is `OUT_OF_RANGE` while live
delivery is complete; `Write` with the fake filesystem reporting 10 MiB
free → `RESOURCE_EXHAUSTED` `disk_reserve` before any temporary),
`m6_imds.rs` (only when the test process has `CAP_NET_ADMIN` — skipped
otherwise, exercised in the Docker loop with `--cap-add NET_ADMIN`:
rule installed, `-C` passes, uid-1000 connect fails, `Health.
imds_blocked` true; without the capability `imds_block_unavailable` and
`false`).

**Sidecar:** D9's tests plus the golden update.

**SDK unit (fakes):** `SandboxHealth` new fields, `hook_anomalies`
warning once per generation, `imds_blocked` warning only with a role,
`cpu_time_limit` validation and payload shape, `SandboxInfo.egress/
ingress` from a stubbed `get-microvm`, `DiskFullException` mapping,
reconnect after a `suspending` end when the VM stays `RUNNING` and the
generation does not change (D2), sync and async.

**Scripts:** D8's Stubber tests; `publish_image.py` gains a test that
`--os-capabilities ALL` adds exactly `additionalOsCapabilities: ["ALL"]`
and nothing else to `desired_configuration`.

**e2e (real AWS, `tests/e2e/test_m6_hardening.py`):** the list in
"Acceptance test list".

### D15. Image, scripts and dev loop

- `image/Dockerfile`: M6 header; `iptables-nft` in the `dnf` list;
  sanity loop adds `iptables`; the warm-up file per D10; `CMD` unchanged.
- `scripts/publish_image.py`: `--os-capabilities {ALL}` (argparse
  `choices`), `Settings.os_capabilities`, `desired_configuration` adds
  the key only when set; docstring usage updated.
- `scripts/hooks-sim.py`: `--only forged` posts `/run` twice, `/suspend`
  three times in a row and asserts `already_ran`, one `changed` + two
  `unchanged`, then `/resume` `changed`, a `/suspend` right behind it
  `changed` again and a final `/resume`; prints `hook_anomalies` from
  `Health` (1: the forged `/run`).
- Makefile: `image-publish-caps`, `image-prune`, `test-scripts`,
  `infra-lint`; `test` runs `test-scripts` too.
- Docker loop (`rust:1.98-slim-bookworm`): `m6_imds.rs` runs with
  `--cap-add NET_ADMIN` and `iptables` installed in the container; the
  rest as M5.

### D16. Performance budgets

- Audit: O(1) per hook, no allocation on the hot path.
- IMDS probe task after `/run`: ≤ 3 s in the worst case (two 1–2 s
  connects), off the hook's budget; `kernel_ready` is independent of it.
- `free_bytes` per `Write` file: one `statvfs` (microseconds) on the
  blocking thread already used for the write.
- `OutputBudget`: one atomic add/sub per chunk.
- `image_prune.py`: bounded by AWS (each delete + wait ≈ the time the
  image takes to leave `DELETING`, measured in acceptance).
- Snapshot: only shrinks or stays.

### D17. Docs alignment

`SECURITY.md`: T1 (mitigation now "image variant with `additionalOsCapabilities`
+ `iptables` owner rule, `imds_blocked` in `Health`; default image
fail-open"), T2 (residual-risk table from D1 with the acceptance numbers,
D2 watchdog), T7 (cgroup2 platform limit; `RLIMIT_CPU`, output budget,
disk reserve), T8 (`infra/egress-connector.yaml`, `SandboxInfo.egress`,
Q42 outcome), IAM section → `infra/`. `ARCHITECTURE.md`: hooks table
(audit, no rate limit, watchdog), `Health` row (`imds_blocked`,
`hook_anomalies`), Capa 1 (`image-prune` paragraph now true, variant
name, warm-up list per D10), domain/ports/adapters tables
(`capabilities`, `budget`, `free_bytes`, `netfilter`), Python layout.
`AWS_API_NOTES.md` §16: Q42 (own egress connector vs `INTERNET_EGRESS`),
Q43 (cgroup2 and `CapEff` on 11.0 and on the `ALL` variant), Q44 (`ALL`
variant build/boot, IMDS block measured), Q45 (`delete-microvm-image-version`
behaviour: `ACTIVE` deletable?, `ConflictException` wait times), Q46
(warm-up RSS deltas and snapshot size before/after). `MILESTONES.md` M6:
a "Track A" block with the acceptance state. `README.md`: `cpu_time_limit`,
`egress` recipe, `get_health().imds_blocked`, image variants and
`image-prune`.

### D18. Coordination with the sibling M6 changes

Three other M6 changes exist in `openspec/changes/` (`m6-typescript-sdk`,
`m6-e2b-compat`, `m6-benchmark-pool`). Touch points, resolved so the four
can land in any order:

- `scripts/publish_image.py`: `m6-benchmark-pool` adds `--variant
  full|slim` (default image name per variant, a `warmup_variant` marker
  in the zip); this change adds `--os-capabilities ALL`. Both are
  independent `Settings` fields and independent keys of
  `desired_configuration`; the capabilities variant is published with
  `--variant full` (the default) and `--image-name rayito-base-caps`,
  never combined with `slim`.
- `kernel-sidecar/ipython/startup/0004_warmup.py`: `m6-benchmark-pool`
  short-circuits the whole function when the `warmup_variant` marker says
  `slim`; D10 only edits the import list of the full path. Whichever
  lands second rebases on the other's file; both docstrings survive.
- `Health`/`SandboxHealth`: `m6-typescript-sdk` mirrors the Python model
  in camelCase; the two fields added here (`imdsBlocked`,
  `hookAnomalies`) are additive and that change's drift test against
  the proto will pick them up. No other change edits `health.proto`.
- `image_prune.py` keeps the newest launchable versions **per image
  name**, so `rayito-base-slim` and `rayito-base-caps` are pruned only
  when named explicitly.

## Risks / Trade-offs

- **No rate limit means an `allPorts` holder can cut the client streams
  as often as it likes.** Accepted on purpose (D1): the refusal the first
  draft used would also refuse the genuine `/suspend` that follows a
  forged pair and leave a real checkpoint unprepared, which is worse than
  the pre-M6 behaviour. The cost of a flood is a reconnect per cut on the
  SDK's backoff, `ReconnectBudget` ending the handle after four futile
  cuts, and `hook_anomalies` staying at what the audit can prove (`/run`
  repeats and stale recoveries); a holder of that token already has the
  IAM to terminate the VM, so nothing new is gained.
- **A forged `/resume` between a real `/suspend` and the freeze** (≈ 1.4 s
  window, Q38) makes the real `/resume` after the restore an `unchanged`
  repeat; nothing in `rayd` can tell the two apart and the SDK's reconnect
  rule needs no new generation, so the client recovers, but the kernel
  probe, the reseed, `clock_offset_ms` and the `suspended_total` span of
  that pause are skipped. Recorded as residual in `SECURITY.md` T2.
- **The stale-suspend watchdog and real suspensions.** The freeze
  detector (a tick with `elapsed > 5 s`) relies on Q19 (`CLOCK_MONOTONIC`
  advances during the pause, measured twice); if AWS ever stopped
  advancing it, the watchdog would recover a real suspend at restore and
  the `resume_after_stale_recovery` path would still make the real
  `/resume` correct, at the cost of one spurious anomaly per pause. The
  integration test covers both orders.
- **`iptables -m owner` inside a Firecracker guest with `ALL`
  capabilities is unmeasured.** The whole D4 path is fail-open and the
  acceptance measures it; the fallback is written down before starting.
- **`RLIMIT_CPU` is wall-clock-independent** (CPU seconds), so a sleeping
  process never hits it; that is the point, but users may expect a
  wall-clock cap — the docstring says "segundos de CPU" and points at
  `timeout` for wall clock.
- **The output budget can make `Connect(from_seq)` lose bytes under
  memory pressure** (rule 1); the SDK already degrades to `from_seq=0`
  with a warning, and 128 MiB is far above any e2e output.
- **`free_bytes` reflects the whole root filesystem**, not a per-user
  quota; a sandbox shell can still fill the disk. Stated in T7.
- **`image_prune.py` in the shared account** could delete a version a
  colleague launched a MicroVM from a second ago: the live-MicroVM pin
  reads `list-microvms` right before deleting, and `--dry-run` is the
  documented first step.
- **cfn-lint may not know the resource type** yet; the server-side
  `validate-template` is the authoritative dry run and the ignore is
  scoped to E3006.
- **Dropping `scipy`/`sklearn` from the warm-up moves their first-import
  cost (lazy disk) to the user's first cell** — measured in D10 as part of
  the decision, and the packages stay installed.

## Migration Plan

1. Proto fields, `rayd-core` modules and `rayd` adapters with host tests
   green on Windows and Linux; the default image behaviour changes only
   in the audit log, the watchdog, the reserve check
   and the output budget (all invisible to a well-behaved client).
2. Sidecar reseed change (golden updated) before the image is rebuilt.
3. Scripts (`publish_image.py` flag, `image_prune.py`, Makefile), `infra/`
   template with `infra-lint` green.
4. SDK 0.0.6 with the new fields; `SandboxInfo`/`SandboxHealth` are
   dataclasses with defaults, older agents (10.0) still parse (proto3
   defaults).
5. Publish `rayito-base` 11.0 (after the D10 measurement decided the
   warm-up) and `rayito-base-caps` 1.0; run `image_prune.py --dry-run`
   then for real on `rayito-base` (keep 5), recording Q45.
6. e2e: M1–M5 suites still green on 11.0 (regression), `test_m6_hardening.py`
   green; the IMDS and egress tests gated by their env vars per D4/D11.
7. Docs (D17), `openspec validate --strict`, archive.

Rollback: every new behaviour is either additive (proto, SDK fields), a
no-op without a capability (D4) or a constant that can be raised
(`SUSPEND_GATE_TIMEOUT`, `FREEZE_THRESHOLD`,
`SANDBOX_OUTPUT_BUDGET_BYTES`, `DISK_RESERVE_BYTES`); the previous image
version stays `ACTIVE` until pruned.

## Open Questions

None left open at design time; the four that depend on the platform are
converted into measurements with a decision rule fixed above: cgroup2
availability on the `ALL` variant (D3, Q43), the `ALL` variant building
and blocking IMDS (D4, Q44), `delete-microvm-image-version` on an
`ACTIVE` version (D8, Q45), and the warm-up RSS deltas (D10, Q46). Q42
(own egress connector vs `INTERNET_EGRESS`) is measured only if the
deploy gate of D11 passes, otherwise recorded as not measured.

## Acceptance test list

`clients/python/tests/e2e/test_m6_hardening.py` (marker `e2e`; session
fixtures from `conftest.py`; `RAYITO_TEMPLATE` = `rayito-base` 11.0 ARN;
`RAYITO_TEMPLATE_CAPS` = `rayito-base-caps` ARN, optional;
`RAYITO_EGRESS_CONNECTOR_ARN` optional; `RAYITO_EXECUTION_ROLE_ARN` for
the IMDS test). Timings printed like M5.

`test_forged_hooks` (default image, `timeout=1200`, `IdlePolicy(600,
600, auto_resume=True)`):

1. `commands.run("echo before > /home/user/m6.txt")`; background
   `commands.run("i=0; while true; do echo tick $i; i=$((i+1)); sleep 1; done", background=True)`
   and a PTY with `echo hola`; `run_code("x = 42")`; `h0 = get_health()`
   (`hook_anomalies == 0`).
2. Mint an `allPorts` JWE **outside the SDK** (`boto3
   create_microvm_auth_token(expirationInMinutes=5, allowedPorts=[{"allPorts": {}}])`,
   the M0 Q24 procedure) and post through the proxy with `httpx`
   (`x-aws-proxy-auth`, `x-aws-proxy-port: 9000`, HTTP/1.1) to
   `/aws/lambda-microvms/runtime/v1/run` with a body carrying a
   **different** `token_sha256`: expect 200 with `outcome ==
   "already_ran"`; then `commands.run("echo still")` with the original
   token succeeds (**forged `/run` is a no-op**) and `get_health()
   .hook_anomalies == 1`.
3. Post `/suspend` three times within a second: bodies `changed`,
   `unchanged`, `unchanged`; the background handle and the PTY
   see their `suspending` end; `get-microvm` stays `RUNNING`.
4. Within `reconnect_timeout` the handle receives ticks again
   (`reconnects == 1`, first tick after the cut ≤ 30 s = watchdog +
   poll; the test passes `reconnect_timeout=90`), `files.read("/home/user/m6.txt") == "before\n"`,
   `run_code("x").text == "42"`, `commands.list()` still shows the loop and
   the PTY (**forged `/suspend` loses no data**); `get_health()`:
   `resume_generation` unchanged, `hook_anomalies == 2` (1 run + 1
   stale-suspend recovery); the SDK logged the warning once.
5. Post `/resume`: `changed` (the real resume after a recovery,
   generation +1). Then the sequence the rate limiter of the first draft
   broke: forged `/suspend` + `/resume` (`changed`, `changed`,
   generation +2), a `/suspend` 1 s later must be `changed` with
   `streams_closed` (never `rate_limited`), its `/resume` `changed`
   (generation +3), a repeated `/resume` `unchanged`;
   `hook_anomalies` stays 2. Then a real `pause()`/`resume()` cycle
   still works (`resume_generation` +1, `x == 42`).
6. `commands.run("sleep 2", timeout=10)` fine; `kill()`.

`test_cpu_time_limit` (default image, `cpu_time_limit=2`):
`commands.run("python3 -c 'while True: pass'", timeout=30)` raises
`CommandExitException` with `exit_code == 128 + 24` or `137` in ≤ 8 s;
`commands.run("sleep 3")` succeeds (sleeping is not CPU); `run_code("sum(range(10**7))")`
succeeds (kernel not limited).

`test_output_budget_and_disk_reserve` (default image):
`commands.run("head -c 200000000 /dev/zero | tr '\\0' x", background=True)`
× 2 → both complete with `output_truncated`-free live delivery and
`commands.connect(pid, from_seq=1)` on the first one returns
`OUT_OF_RANGE` → SDK falls back with a warning (budget); `files.write` of
a 1 MiB file succeeds; `get_metrics().disk_total_bytes - disk_used_bytes
> 256 MiB` holds so the reserve is not hit — the reserve is exercised in
integration only (filling 8 GB on AWS costs time, not measured).

`test_imds_block` (skipped without `RAYITO_TEMPLATE_CAPS` and
`RAYITO_EXECUTION_ROLE_ARN`): `Sandbox.create(template=<caps ARN>,
execution_role_arn=<role>)`; `get_health().imds_blocked is True` within
10 s of readiness; `commands.run("python3 -c 'import urllib.request as u; r=u.Request(\"http://169.254.169.254/latest/api/token\", method=\"PUT\", headers={\"X-aws-ec2-metadata-token-ttl-seconds\": \"60\"}); u.urlopen(r, timeout=3)'")`
raises `CommandExitException` (timeout/unreachable) in ≤ 5 s;
`commands.run("cat /proc/self/status | grep CapEff")` recorded; the same
snippet on the **default** image sandbox (control) exits 0 and
`imds_blocked is False`. `rayd`'s `imds_probe` line with
`root_reachable: true` is read from CloudWatch (`/rayito/rayito-base-caps`).

`test_egress_allowlist` (skipped without `RAYITO_EGRESS_CONNECTOR_ARN`):
D11's assertions.

`test_reseed_during_long_cell`: `run_code("import time; time.sleep(25)")`
in a thread, `pause()` at 3 s, `resume()`; the cell completes via
`Reattach`; a second `pause()`/`resume()` and a third; CloudWatch (or
`Health.kernel_ready` staying true and `run_code("1+1")` answering after
each) show no sidecar restart: `get_health().kernel_ready` and
`run_code("import random; random.random()")` succeed after the three
cycles (the kill switch would have restarted the sidecar and lost `x`).

`test_snapshot_measurements` (always runs, prints only): D10's RSS deltas
and `get_health()` after readiness with the boot timing; the implementer
copies the numbers into Q46/M6 notes.

Image prune is exercised by the acceptance agent by hand
(`make image-prune PRUNE_ARGS="--dry-run"` then `--keep 5`) with the
console output pasted into the task notes; egress template validation by
`make infra-lint`.
