## Why

A Rayito sandbox lives at most 8 h (`maximumDurationInSeconds` ≤ 28 800,
running **plus** suspended, `AWS_API_NOTES.md` §11) and that ceiling cannot
be moved: there is no `UpdateMicrovm`, so ADR-007 rules out `set_timeout`
and tells an agent that needs more than 8 h to "create another sandbox and
move its state by `files`". Today that means the client downloads
`/home/user` through the proxy at ≈ 6.7 MB/s and uploads it into the new
VM at **0.65 MB/s** (the 64 KiB HTTP/2 window of Q32): 1 GB of working
files costs ≈ 26 min of the client's time and bandwidth on every
reincarnation, and the state passes through the client machine. `kill()`
has the same problem in the other direction: everything in the VM is
gone.

`docs/research/2026-09-m7-oss-readiness.md` §3(g) compares three ways of
keeping `/home/user` beyond one VM: (1) an S3 sync run by the sandbox
user, which exposes the execution role to user code (threat T1); (2) an
S3 adapter **inside `rayd`**, which runs as root and reaches IMDS while
the `rayito-base-caps` route keeps IMDS blackholed for uid 1000–65535
(Q48), at the cost of compiling a TLS stack into the musl binary; (3) EFS
through a VPC egress connector, undocumented for MicroVMs. The report
recommends (2) and asks for two measurements before trusting it: the size
delta of the binary and the throughput of the VM's egress to S3. This
change is **Track 4** of M7 (report §5 item 4, `MILESTONES.md` M7 row 4,
size L).

## What Changes

Every decision is closed in `design.md`; the summary:

- **Proto.** `FilesystemService` gains `rpc Checkpoint(CheckpointRequest)
  returns (stream CheckpointEvent)` and `rpc Restore(RestoreRequest)
  returns (stream RestoreEvent)`; no new service. The messages
  (`S3Location`, `CheckpointStarted/Progress/Done`,
  `RestoreStarted/Progress/Done`, `StreamError` and `KeepAlive` inside the
  usual `oneof`) are added to `filesystem.proto`, `buf lint` stays clean
  with the existing exceptions, and the Python and TypeScript clients are
  regenerated (`make proto`); the Rust side regenerates on `cargo build`.
- **`rayd`.** A `persistence` domain module in `rayd-core` (archive plan
  over the user's home with the documented ignore list, key validation,
  progress accounting, the checkpoint/restore state machines, errors
  without paths) over two new ports, `ObjectStore` and `HomeArchiver`;
  adapters in `rayd`: `S3ObjectStore` (`aws-sdk-s3` + `aws-config`, IMDSv2
  credentials of the execution role read **as root**, region from
  `AWS_REGION`, multipart upload of 8 MiB parts, `AbortMultipartUpload`
  on any failure) and `TarHomeArchiver` (`tar` + `flate2` gzip level 1,
  streaming, files read under the user's fs identity, restore never
  follows or escapes the home). The archive is `<key_prefix>/home.tar.gz`
  plus `<key_prefix>/manifest.json` (sha256, sizes, counts, sandbox id,
  agent version). One checkpoint or restore at a time per sandbox;
  `/suspend` cancels an in-flight one like any server-stream.
- **Build and measurement.** `rustls` with `aws-lc-rs` (the SDK's default
  HTTPS client) compiled into the `aarch64-unknown-linux-musl` binary
  through `cargo zigbuild`; the clean build time and the binary size are
  measured before and after (baseline 4 700 984 B with `cargo auditable`)
  and recorded in `MILESTONES.md`. If the TLS stack does not cross-compile
  inside the budget of design D6 (first `aws-lc-rs`, then `ring`), the
  change falls back to **option 1** exactly as specified in D6b (a uid
  1000 helper in the sidecar package using `boto3`, IMDS reachable for the
  user, documented as exposing the role) and records why; it never shells
  out to an `aws` CLI.
- **SDK (Python sync + async, TypeScript).** `rayito.S3Prefix(bucket,
  prefix="rayito", name=None, region=None)`; `Sandbox.create(...,
  persist=S3Prefix(...), persist_timeout=600)` requires
  `execution_role_arn`, binds the prefix (the `name` defaults to the
  sandbox id at bind time) and auto-restores when a checkpoint exists
  under it; `sbx.checkpoint_files(exclude=(), timeout=600) ->
  CheckpointResult`; `sbx.restore_files(source=None, timeout=600) ->
  RestoreResult`; `sbx.reincarnate(exclude=(), persist_timeout=600) ->
  Sandbox` = checkpoint → `create(persist=)` with the same launch options
  (auto restore) → `kill()` of the old VM, documented as **the** answer to
  `set_timeout` (files survive, kernel memory does not). Same surface in
  TypeScript (`persist`, `persistTimeoutMs`, `checkpointFiles()`,
  `restoreFiles()`, `reincarnate()`). `rayito.e2b.Sandbox.set_timeout`
  stays `UnimplementedError`; its message now names `reincarnate()`.
- **IAM.** `spike/m0/iam.yaml` gains `PersistenceBucket` and
  `PersistencePrefix` parameters and, when the bucket is set, a
  `persistence` policy on the execution role: `s3:PutObject`,
  `s3:GetObject`, `s3:AbortMultipartUpload` on
  `arn:aws:s3:::<bucket>/<prefix>/*` and `s3:ListBucket` on the bucket
  restricted with `s3:prefix` to `<prefix>/*`. Nothing else.
- **Ground truth.** `AWS_API_NOTES.md` gains §17 (the S3 operations and
  parameters `rayd` uses, from the S3 API reference, so hard rule 1 keeps
  holding for a second AWS API) and §16 Q53 (S3 throughput from inside
  the VM through `INTERNET_EGRESS`), Q54 (the Rust SDK's IMDS provider
  against the `execution_role` profile), plus a Q1 note on credential
  expiry seen from a checkpoint after 55 min.
- **Docs and constitution.** `docs/site/docs/persistence.md`, README
  section, `ARCHITECTURE.md` (service table, adapters, **ADR-009**,
  ADR-007 consequence), `SECURITY.md` (T1 update, new T14 for the data at
  rest and the role's S3 scope), `SPEC.md` §4 and `openspec/project.md`
  hard rule 3 drop "no cross-session filesystem persistence" (delivered
  here, like the metadata non-goal was in M6).

## Capabilities

### New Capabilities
- `filesystem-persistence`: the two RPCs, the archive rules (ignore list,
  identity, symlinks, special files, ownership and modes on restore), the
  S3 layout and manifest, credentials and region, multipart upload and
  abort, progress and keep-alive, error codes, concurrency and suspend
  interplay, logging hygiene, the build measurement and the option-1
  fallback contract, the execution-role policy.
- `sdk-persistence`: `S3Prefix`, `create(persist=)`, `checkpoint_files`,
  `restore_files`, `reincarnate`, result models, exception mapping,
  sync/async parity, the TypeScript twins, unit tests on the fakes and the
  real-AWS acceptance (50 MB round trip, `imds_blocked` still `true`,
  timings).

### Modified Capabilities
- `typescript-sdk`: "Sandbox lifecycle surface" gains `persist`,
  `persistTimeoutMs`, the instance `persist` getter, `checkpointFiles()`,
  `restoreFiles()` and `reincarnate()`.
- `e2b-compat`: the `set_timeout` `UnimplementedError` message names
  `reincarnate()` as the supported path (still raised before any AWS or
  agent call).

## Impact

- New: `crates/rayd-core/src/persistence/` (`plan.rs`, `keys.rs`,
  `manifest.rs`, `progress.rs`, `checkpoint.rs`, `restore.rs`,
  `error.rs`, `mod.rs`), `crates/rayd/src/adapters/s3_store.rs`,
  `crates/rayd/src/adapters/tar_archiver.rs`,
  `crates/rayd/src/grpc/persistence.rs`,
  `clients/python/src/rayito/_persistence_base.py`,
  `clients/python/tests/unit/fake_persistence.py`,
  `clients/python/tests/unit/test_persistence_{base,sync,async}.py`,
  `clients/python/tests/e2e/test_m7_persistence.py`,
  `clients/typescript/src/sandbox/persistence.ts`,
  `clients/typescript/tests/unit/persistence.test.ts`,
  `clients/typescript/tests/e2e/m7.e2e.test.ts`,
  `docs/site/docs/persistence.md`.
- Edited: `proto/rayito/v1/filesystem.proto` and every generated client
  (`clients/python/src/rayito/v1/filesystem_pb2*.py`,
  `clients/typescript/src/gen/rayito/v1/filesystem_pb.ts`), `Cargo.toml`
  (workspace pins for `aws-config`, `aws-sdk-s3`, `tar`, `flate2`),
  `Cargo.lock`, `crates/rayd/Cargo.toml`, `crates/rayd/src/main.rs`
  (wiring, `--persistence` flag), `crates/rayd/src/grpc/filesystem.rs`
  (delegation of the two RPCs), `deny.toml` (only if `cargo deny check`
  needs a named exception), `image/Dockerfile` (`ca-certificates` in the
  package list and a build-time check of the bundle path),
  `clients/python/src/rayito/{__init__,_models,exceptions,_limits}.py`,
  `clients/python/src/rayito/sandbox_sync/{main,filesystem}.py`,
  `clients/python/src/rayito/sandbox_async/{main,filesystem}.py`,
  `clients/python/src/rayito/e2b/*` (message only), `limits.json` and
  the generated `_limits.py`/`limits.ts`, `clients/typescript/src/{index,
  models,limits}.ts`, `clients/typescript/src/sandbox/{sandbox,launch}.ts`,
  `spike/m0/iam.yaml`, `infra/README.md`, `AWS_API_NOTES.md` (§16 Q1
  note, Q53, Q54; new §17), `ARCHITECTURE.md`, `SECURITY.md`, `SPEC.md`,
  `openspec/project.md`, `README.md`, `docs/site/mkdocs.yml`,
  `docs/site/docs/{e2b-compat,concepts,security}.md`, `MILESTONES.md`
  (M7 row 4 with the measured numbers), `clients/python/CHANGELOG.md`,
  `clients/typescript/CHANGELOG.md`, `crates/rayd/CHANGELOG.md`.
- Behaviour: none unless `Checkpoint`/`Restore` are called. A sandbox
  created without `persist` runs exactly as in 0.1.0; a sandbox created
  without an execution role answers both RPCs with `permission_denied`
  before touching the network. The binary grows by the measured delta;
  Q42 showed snapshot size is not the cold-start driver, and the first
  checkpoint's lazy page-in of the new code is measured in the e2e.
- Cost: the acceptance run ≈ $0.05 of compute (three `create()` on the
  caps image, one on the default image), S3 storage of ≈ 50 MB for
  minutes, intra-region S3 data transfer at the connector's data-transfer
  rate (`AWS_API_NOTES.md` §2/§12); the optional 60-minute credential
  probe (`RAYITO_E2E_SLOW=1`) ≈ $0.13. An incomplete multipart upload
  left by a killed VM costs storage until aborted: the docs tell
  operators to add an `AbortIncompleteMultipartUpload` lifecycle rule to
  the bucket (this change creates no bucket: the account's SCP denies
  `s3:CreateBucket`).
