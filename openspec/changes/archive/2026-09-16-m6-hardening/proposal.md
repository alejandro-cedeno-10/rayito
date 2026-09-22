## Why

M1–M5 are accepted against real AWS and `SECURITY.md` still carries four
rows whose mitigation column says "M6": T1 (sandbox code reads the
execution role's credentials from IMDS), T2 (the hook port is reachable
through the proxy with an `allPorts` JWE, so a forged `/run`, `/suspend`
or `/resume` cannot be told apart from AWS's by origin), T7 (no cgroup2
slices, so a runaway process only meets rlimits) and T8 (no egress
allowlist: `INTERNET_EGRESS` is the default). M5 left two operational
debts as well: the `reseed` that `/resume` queues behind a long in-flight
cell trips the sidecar's 15 s op timeout and counts towards its
three-strikes kill switch (`AWS_API_NOTES.md` §16 Q39), and every image
publish since M4 creates a version whose deletion later fails with
`ConflictException` while the image is `UPDATING` (§4). Finally the memory
snapshot grew from 572 MB to 919–935 MB with the scientific warm-up
(Q34) and AWS charges every launch and resume by bytes read (§12, §15):
nobody has measured which preloads earn their place.

This change is **Track A** of the M6 milestone in `MILESTONES.md`
("Endurecimiento"): `rayd`, the image, IAM/infra and the scripts. The
TypeScript client, the E2B shim, the cold-start burst benchmark and the
sidecar-in-Rust evaluation are other tracks and are not touched here.

## What Changes

Every decision is closed in `design.md`; the summary:

- **Hook origin defense (T2).** Origin cannot be validated (measured: hooks
  and proxied traffic both arrive from `127.0.0.1` over HTTP/1.1, and AWS
  gives no per-boot hook secret), so the control stays "port 9000 never in
  `allowedPorts`". `rayd` adds what is left: an audit log of every runtime
  hook after the first accepted `/run` (`hook_audit` events with a
  per-hook counter, never the body), a watchdog that recovers a
  `/suspend` nobody checkpointed, and a `hook_anomalies` counter in
  `HealthService.Health` that the SDK surfaces
  as a warning. The acceptance test **forges** hooks through the proxy with
  an `allPorts` token minted outside the SDK and measures that a forged
  `/run` is a no-op (token unchanged), a forged `/suspend` loses no data and
  a forged `/resume` only bumps the generation. Residual risk documented in
  `SECURITY.md` T2 with the numbers.
- **cgroup2 re-measured, then hardening with what exists.** The current
  image is probed once for `/sys/fs/cgroup` and `CapEff` (rayd now logs
  both at boot). If cgroup2 is still unavailable (expected: M0 Q20) it is
  recorded as a platform limit and T7 closes with: `RLIMIT_CPU` per spawned
  process/PTY (never the sidecar or kernels), configurable per sandbox
  through the run payload (`limits.cpu_seconds`, SDK
  `create(cpu_time_limit=...)`), a sandbox-wide **output byte budget**
  (128 MiB across every process, PTY and execution ring; over budget the
  oldest bytes are evicted first and ended entries are reaped early), and a
  **disk reserve check** before every `Write` file (`statvfs`, 256 MiB free
  required, `RESOURCE_EXHAUSTED` otherwise; `ENOSPC` mapped the same way).
- **IMDS block for uid 1000 (T1).** `scripts/publish_image.py` gains
  `--os-capabilities ALL` (`additionalOsCapabilities: ["ALL"]`, the only
  value in the model) and the Makefile a `image-publish-caps` target that
  publishes the variant under a separate image name. At startup `rayd`
  reads its own `CapEff`; with `CAP_NET_ADMIN` it installs
  `iptables -I OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j DROP`,
  re-checks the rule at `/run`, probes IMDS as root (TCP connect only) and
  as uid 1000 (must fail) and reports `imds_blocked` in `Health`; without
  the capability it logs `imds_block_unavailable` and continues
  (fail-open, documented). Acceptance launches from the ALL-capabilities
  version with an execution role: `/creds` as uid 1000 fails while `rayd`
  still reaches IMDS; if the variant fails to build or run, the fallback is
  documented and the default image keeps `imds_blocked = false`.
- **Egress allowlist end-to-end (T8).** `Sandbox.create(egress=...)` already
  exists; it is completed with `SandboxInfo.egress`/`.ingress` (from
  `get-microvm`'s `egressNetworkConnectors`/`ingressNetworkConnectors`),
  the caller IAM (`lambda:PassNetworkConnector` on own connectors) and
  `infra/egress-connector.yaml`: an `AWS::Lambda::NetworkConnector` with
  `VpcEgressConfiguration`, a security group whose egress is a parameter
  allowlist, and the operator role. Validated with
  `aws cloudformation validate-template` and `cfn-lint`; deployed in
  acceptance **only** if the account has a VPC with a subnet and the
  connector has no charge on the pricing page, otherwise shipped as a
  dry-run-validated template.
- **`scripts/image_prune.py`** keeps the N newest launchable versions
  (default 5), never deletes a version a live MicroVM runs, deletes
  serially and waits for the image to leave `UPDATING`/`DELETING` between
  deletes (retrying the observed `ConflictException` with backoff).
  Makefile target `image-prune`, unit tests with `botocore.stub.Stubber`.
- **Reseed after resume tolerates a busy kernel.** The sidecar's `reseed`
  op answers immediately: idle kernels are reseeded inline, busy ones are
  deferred to a background task that takes its turn after the running
  cell, and the reply lists `reseeded`, `deferred` and `failed`. `rayd`
  additionally treats a `reseed` timeout as advisory (never counted
  towards the kill switch). Unit tests on both sides.
- **Snapshot diet, measured.** One MicroVM measures the RSS each warm-up
  import adds (`numpy`, `pandas`, `matplotlib.pyplot`, `scipy.stats`,
  `sklearn.linear_model`). If `scipy` + `sklearn` add more than 100 MB
  (they are not used by any acceptance test nor by `/validate`), they leave
  the warm-up (packages stay installed) and the new version's
  `memorySnapshotSizeInBytes` is reported next to 10.0's 919 146 496 B,
  together with `run-microvm → kernel_ready` before/after.

Proto: two additive fields on `HealthResponse` (`imds_blocked = 9`,
`hook_anomalies = 10`); nothing else changes on the wire. No new AWS
parameter beyond `additionalOsCapabilities` (§4 of `AWS_API_NOTES.md`),
`egressNetworkConnectors`/`ingressNetworkConnectors` on `get-microvm` (§2,
§6), `delete-microvm-image-version` / `update-microvm-image-version` (§4)
and the CloudFormation `AWS::Lambda::NetworkConnector` properties quoted
from the official template reference in `design.md`.

## Capabilities

### New Capabilities
- `hook-defense`: hook audit log after `/run`, no transition rate limit
  (a session-changing hook is never refused), the stale-suspend watchdog,
  `hook_anomalies` in `Health`, the forged-hook acceptance measurements and
  the honest residual-risk statement.
- `guest-isolation`: capability detection at boot, the IMDS block for
  uid 1000 with `imds_blocked` in `Health`, the cgroup2 re-measurement and
  the fail-open contract.
- `egress-control`: `SandboxInfo` connector fields, the VPC egress
  connector template and IAM, the allowlist recipe and its validation.
- `image-lifecycle`: `publish_image.py` variants (`--os-capabilities`),
  `image_prune.py` with serialized deletes, the warm-up measurement and the
  snapshot size report.

### Modified Capabilities
- `process-lifecycle`: resource limits gain `RLIMIT_CPU` (per sandbox,
  spawned processes and PTYs only); rings and retention are bounded by the
  sandbox-wide output budget.
- `filesystem`: `Write` checks the disk reserve before each file and maps
  `ENOSPC` to `RESOURCE_EXHAUSTED`.
- `code-execution`: the `reseed` reply gains `deferred`; the warm-up list is
  decided by measurement instead of a fixed 1.2 GB knob.
- `suspend-resume`: `/resume` reseed is advisory and never counts towards
  the sidecar kill switch; `Health`/`SandboxHealth` expose `imds_blocked`
  and `hook_anomalies`.

## Impact

- `proto/rayito/v1/health.proto` (+2 fields), `clients/python/src/rayito/
  v1/health_pb2*` regenerated, Rust at `cargo build`.
- `crates/rayd-core`: `hooks` (audit counters as pure
  functions), `capabilities` (CapEff parsing, `imds` plan), `run_payload`
  (`limits.cpu_seconds`), `process/limits` (`cpu_seconds`), `process/
  budget` (shared output budget), `process/registry` and `code/executions`
  (charge/release), `filesystem/ports` (`free_bytes`) and `filesystem/
  write` (reserve rule), `health` (`imds_blocked`, `hook_anomalies`).
- `crates/rayd`: `adapters/netfilter.rs` (`cfg(unix)`, iptables + probes),
  `hooks/mod.rs` (audit, watchdog, `/run` IMDS re-check), `code/
  supervisor.rs` (advisory ops), `adapters/process_spawner.rs`
  (`RLIMIT_CPU`), `adapters/std_filesystem.rs` (`statvfs`), `grpc/
  health.rs`, `main.rs` (boot capability log), `logging.rs` allowlist,
  integration suites `m6_hooks.rs`, `m6_limits.rs`, `m6_imds.rs`.
- `kernel-sidecar`: `server.py` (`_op_reseed` inline/deferred), tests.
- `clients/python`: `_models.py` (`SandboxInfo.egress/ingress`,
  `SandboxHealth.imds_blocked/hook_anomalies`), `_aws.py` (connector
  fields from `get-microvm`), `_payload.py`/`_sandbox_base.py`
  (`cpu_time_limit`), `sandbox_sync`/`sandbox_async` (`create` kwarg,
  `_record_health` warning on `hook_anomalies`), unit tests, e2e
  `test_m6_hardening.py`, version `0.0.6`.
- `scripts/publish_image.py` (`--os-capabilities`), new `scripts/
  image_prune.py` + `scripts/tests/test_image_prune.py`, `Makefile`
  (`image-publish-caps`, `image-prune`, `test-scripts`), `infra/
  egress-connector.yaml`, `infra/README.md`, `image/Dockerfile` (iptables
  sanity, M6 header), `kernel-sidecar/ipython/startup/0004_warmup.py`.
- Docs: `SECURITY.md` (T1, T2, T7, T8 rows closed or re-stated with the
  measured residual), `ARCHITECTURE.md` (hooks table, Health row, ports and
  adapters tables, image pipeline `image-prune` paragraph, warm-up list),
  `AWS_API_NOTES.md` §16 new rows Q42–Q46, `MILESTONES.md` M6 Track A
  acceptance state, `README.md`.
- Sibling M6 changes (`m6-typescript-sdk`, `m6-e2b-compat`,
  `m6-benchmark-pool`) share `publish_image.py`, `0004_warmup.py` and
  `SandboxHealth` with this one; `design.md` D18 fixes how the edits
  compose.
- Governed by measured facts: hooks arrive from `127.0.0.1` HTTP/1.1 and
  the hook port is reachable with `allPorts` (§7, §8, Q20, Q24); root in
  the guest has no `CAP_NET_ADMIN`/`CAP_SYS_ADMIN` and `/sys/fs/cgroup` is
  not mounted (Q20); `additionalOsCapabilities: ["ALL"]` is the only value
  (§4); `RLIMIT_NOFILE` cannot be raised (Q30); a `/suspend` non-200
  terminates the VM (Q10) so every runtime hook always answers 200;
  `update-microvm-image-version` during `UPDATING` gives
  `ConflictException` (§4); snapshot sizes 919–935 MB (Q34, M5).
