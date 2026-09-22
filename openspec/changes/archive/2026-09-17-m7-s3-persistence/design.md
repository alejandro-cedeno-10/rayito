## Context

State on 2026-09-16 (M1–M6 accepted on real AWS; `m7-oss-hygiene` and
`m7-supply-chain` implemented; `rayd` 0.1.0, Python `rayito` 0.1.0,
TypeScript `rayito` 0.0.5; no git repository yet):

- **What exists.** `FilesystemService` has `Read`, `Write`, `Stat`,
  `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir`, all running under the
  requesting user's fs identity (`FsIdentityGuard`: `setfsuid`/`setfsgid`
  on the blocking thread) with the canonical-path deny list and the
  `.rayito-tmp-*` temporaries. `rayd-core::filesystem` owns `RequestPath`,
  `DenyList`, `FsIdentity{uid, gid, home}`, `walk_listing`, and the
  `FileSystem`/`WriteSink`/`Watcher`/`NameResolver` ports; `rayd` has the
  `StdFileSystem`, `NotifyWatcher` and `NixNameResolver` adapters. Every
  server-stream is wrapped in `SuspendableStream` and closed with
  `suspending` on `/suspend`; `KeepAlive` fires after 30 s of silence.
  `Health` reports `agent_version`, `sandbox_id` and `imds_blocked`.
- **Credentials inside the VM** (`AWS_API_NOTES.md` §9): with an
  `executionRoleArn`, IMDSv2 at `http://169.254.169.254` serves the
  profile `execution_role` (the listing of `security-credentials/`
  returns exactly that name), `Expiration` = `run-microvm` + 55 min,
  rotation after expiry **not measured** (Q1). No `AWS_ACCESS_KEY_ID`,
  no container credential variables; `AWS_REGION` is injected. On
  `rayito-base-caps` (`additionalOsCapabilities: ["ALL"]`) `rayd`
  installs the policy route that blackholes IMDS for uid 1000–65535 and
  root keeps reaching it (Q48); on the default image IMDS is open to
  every uid (fail-open, T1). Without a role IMDS answers 404 on
  `security-credentials/`.
- **Network from the VM.** Egress goes through the image's connector
  (`INTERNET_EGRESS` by default; omitting it in `run-microvm` does not
  close the network, Q44). The proxy endpoint bandwidth is 4 MB/s at 2
  GB (Q27) and HTTP/2 uploads through it are window-bound to ≈ 0.65 MB/s
  (Q32). The bandwidth from the VM to S3 through the egress connector
  has **never been measured**: it is Q53 of this change.
- **Binary and build.** `cargo auditable zigbuild --release --locked
  --target aarch64-unknown-linux-musl -p rayd` produces 4 700 984 B
  (4 698 744 B plain), LTO fat, `codegen-units = 1`, `strip = "symbols"`,
  `panic = "unwind"`, mimalloc. The workspace has no TLS crate by design
  (`openspec/project.md`: "no TLS crate, the AWS proxy terminates TLS"),
  which this change amends for one outbound client. The image installs
  `tar gzip procps-ng shadow-utils util-linux iproute` and Python 3.12;
  Python reached `https://example.com` as uid 1000 (Q44), so the OS CA
  bundle (`ca-certificates`, `/etc/pki/tls/certs/ca-bundle.crt` on AL2023)
  is present at runtime; the Dockerfile does not name the package.
- **Crate versions resolved 2026-09-16 (crates.io):** `aws-sdk-s3`
  1.148.0 (Apache-2.0; default features `sigv4a`, `http-1x`, `rustls`,
  `default-https-client`, `rt-tokio`), `aws-config` 1.12.0 (Apache-2.0),
  `aws-smithy-http-client` 1.4.2 (features `rustls-aws-lc`, `rustls-ring`,
  `s2n-tls`; `default-https-client` of the SDK crates selects
  `rustls-aws-lc`), `aws-lc-rs` 1.18.1 / `aws-lc-sys` 0.45.0 (licence
  expression composed of ISC, Apache-2.0, MIT, BSD-3-Clause and
  `Apache-2.0 OR ISC OR MIT-0` groups: satisfiable by the `deny.toml`
  allowlist without an exception), `tar` 0.4.46 (MIT OR Apache-2.0),
  `flate2` 1.1.10 (MIT OR Apache-2.0, default backend `miniz_oxide`, pure
  Rust), `ring` 0.17.14 (the fallback TLS provider). `aws-lc-sys` ships
  pre-generated bindings for `aarch64-unknown-linux-musl` and builds its C
  and assembly sources with the `cc` crate, which `cargo-zigbuild` points
  at `zig cc`; whether that link succeeds on this toolchain (zig 0.16,
  Rust 1.98) is the first thing the implementation measures (D6).
- **Account facts.** Test account, `us-east-1`; the org SCP denies
  `s3:CreateBucket`, so every S3 test uses the account's existing
  artifact bucket under a prefix; the IAM
  stack `rayito-m0-iam` (`spike/m0/iam.yaml`) owns the execution role
  `rayito-m0-execution-us-east-1` (logs only today).
- **SDK shape.** Python `Sandbox.create(...)` takes `execution_role_arn`,
  `ingress`, `egress`, `logging`, `metadata`, `cpu_time_limit`, …;
  sync and async trees share pure helpers (`_filesystem_base.py`,
  `_sandbox_base.py`); unit tests run against fake servicers
  (`tests/unit/fake_filesystem.py`) and a fake control plane. TypeScript
  mirrors it (`sandbox.ts`, `launch.ts`, `filesystem.ts`, Connect-ES
  transports, fakes in `tests/unit`). `limits.json` → `_limits.py` /
  `limits.ts` through `scripts/gen_limits.py`.

Coordination with the M7 tracks written in parallel (`m7-suspended-pool`,
`m7-mcp-server`, `m7-cli`, `m7-poly-kernels`): this design names
`AWS_API_NOTES.md` §16 rows **Q53/Q54** and `SECURITY.md` row **T14**
assuming it lands first; `m7-suspended-pool` claims Q53 and T14 too, so
whoever archives second renumbers (the M6 rule) — the implementer checks
the live numbering in task 0.0. `m7-poly-kernels` modifies the same
`e2b-compat` requirement ("E2B features without an AWS primitive raise
UnimplementedError") and `m7-suspended-pool`/`m7-poly-kernels` touch the
`typescript-sdk` spec: whoever archives second rebases its MODIFIED text
on the archived one. `docs/site/mkdocs.yml` nav: `persistence.md` goes
right after `cost.md` (and after `pool.md` if that page exists by then).
`Sandbox.create()` gains `pool=` there and `persist=` here; the kwargs
are independent and the pool change makes `pool=` reject `persist=`
(a pooled slot cannot restore a named home on take); nothing here
depends on the pool.

Constraints: `openspec/project.md` hard rules (no invented AWS parameters:
the S3 parameters this change uses are written into `AWS_API_NOTES.md`
§17 **before** the adapter is coded; proto is the source of truth; ARM64
musl only; hexagonal boundaries; identifiers in English; no inline
comments in function bodies; never log file contents, paths, tokens),
`pnpm` only, no git operations, Apache-2.0 everywhere, the brief's three
vetoes (no `PersistenceService`, no `aws` CLI shell-out, fallback is
option 1 not option 3).

## Goals / Non-Goals

Goals:

- `/home/user` survives `kill()` and the 8 h wall through S3, driven by
  `rayd` as root with the execution role, while user code (uid 1000) on
  `rayito-base-caps` still cannot reach IMDS.
- One proto extension on `FilesystemService`, regenerated into Rust,
  Python and TypeScript; identical sync/async/TypeScript SDK surface.
- The binary size delta, the clean-build time delta, the cross-compile
  outcome and the VM→S3 throughput are measured and written down.
- `reincarnate()` gives the honest answer to `set_timeout`.
- Least-privilege S3 policy on the execution role, as a template
  parameter.

Non-goals:

- EFS, NFS, any VPC connector work, presigned URLs, `upload_url`/
  `download_url` (report §3a), a bucket created by this change, bucket
  lifecycle or encryption configuration (documented for the operator
  only), server-side encryption parameters in requests.
- Incremental or deduplicated checkpoints, multiple named snapshots per
  prefix with history, scheduled/automatic checkpoints, checkpoint of
  anything outside the user's home, preserving kernel memory or running
  processes, consistency with processes writing during the checkpoint.
- Any change to `Health`, hooks, the suspended pool (`m7-suspended-pool`),
  the MCP server or the CLI. No `rayd` self-update, no compatibility
  table beyond the `UNIMPLEMENTED` mapping of D9.
- A Rust client SDK, S3-compatible third-party stores (the endpoint is
  never configurable), IMDSv1.

## Decisions

### D1. Proto: two server-stream RPCs on `FilesystemService`, one envelope each

`proto/rayito/v1/filesystem.proto` gains, after `WatchDir`:

```proto
  // Empaqueta el HOME del usuario (tar.gz, lista de exclusión documentada)
  // y lo sube a S3 con las credenciales del execution role, como root.
  // Un solo checkpoint o restore a la vez por sandbox (FAILED_PRECONDITION).
  rpc Checkpoint(CheckpointRequest) returns (stream CheckpointEvent);
  // Descarga <key_prefix>/manifest.json y <key_prefix>/home.tar.gz y los
  // extrae en el HOME del usuario (sin seguir ni escapar del HOME).
  // NOT_FOUND si no hay manifest bajo el prefijo.
  rpc Restore(RestoreRequest) returns (stream RestoreEvent);
```

Messages (field numbers final):

```proto
message S3Location {
  string bucket = 1;
  // Prefijo de clave sin `/` inicial ni final; rayd añade
  // `/home.tar.gz` y `/manifest.json`.
  string key_prefix = 2;
  // Región del bucket; ausente = AWS_REGION del MicroVM.
  optional string region = 3;
}

message CheckpointRequest {
  S3Location target = 1;
  optional User user = 2;
  // Rutas relativas al HOME (sin `..`, sin `/` inicial), máx. 64.
  repeated string exclude = 3;
}
message CheckpointEvent {
  oneof event {
    CheckpointStarted started = 1;
    CheckpointProgress progress = 2;
    CheckpointDone done = 3;
    StreamError error = 4;
    KeepAlive keepalive = 5;
  }
}
message CheckpointStarted {
  uint64 files = 1;   // entradas que se van a archivar (ficheros + dirs + symlinks)
  uint64 bytes = 2;   // suma de tamaños de los ficheros regulares
}
message CheckpointProgress {
  uint64 files_done = 1;
  uint64 bytes_read = 2;
  uint64 bytes_uploaded = 3;
}
message CheckpointDone {
  uint64 files = 1;
  uint64 bytes_read = 2;
  uint64 archive_bytes = 3;   // tamaño del tar.gz subido
  string sha256 = 4;          // hex del tar.gz
  uint64 skipped = 5;         // no legibles + tipos no soportados
  uint32 duration_ms = 6;
}

message RestoreRequest {
  S3Location source = 1;
  optional User user = 2;
}
message RestoreEvent {
  oneof event {
    RestoreStarted started = 1;
    RestoreProgress progress = 2;
    RestoreDone done = 3;
    StreamError error = 4;
    KeepAlive keepalive = 5;
  }
}
message RestoreStarted {
  uint64 archive_bytes = 1;   // del manifest
  uint64 files = 2;           // del manifest
}
message RestoreProgress {
  uint64 files_done = 1;
  uint64 bytes_downloaded = 2;
}
message RestoreDone {
  uint64 files = 1;
  uint64 bytes_written = 2;
  uint64 archive_bytes = 3;
  string sha256 = 4;
  uint64 skipped = 5;
  uint32 duration_ms = 6;
}
```

`buf lint` passes without new exceptions (request/response names are
unique and standard; `buf.yaml` already excepts the shared
`StreamError`/`KeepAlive` envelopes). `buf breaking` against the current
proto is clean (additive). The comment block of `common.proto` on
`StreamError` codes is unchanged: this change uses only `permission_denied`,
`not_found`, `invalid_argument`, `deadline_exceeded`, `suspending` and
`internal`.

### D2. S3 layout, manifest and the exact S3 operations (`AWS_API_NOTES.md` §17)

Objects under `s3://<bucket>/<key_prefix>/`:

| Key | Content | Written when |
|---|---|---|
| `home.tar.gz` | gzip (level 1) of a POSIX ustar/GNU tar of the home, D3 | during checkpoint (multipart) |
| `manifest.json` | the JSON below | after `CompleteMultipartUpload` (or `PutObject`) of the archive succeeded |

A manifest therefore implies a complete archive. `manifest.json` v1:

```json
{"version": 1, "archive": "home.tar.gz", "compression": "gzip",
 "sha256": "<hex of home.tar.gz>", "archive_bytes": 0, "files": 0,
 "bytes": 0, "skipped": 0, "home": "/home/user", "user": "user",
 "sandbox_id": "<microvmId>", "agent_version": "0.2.0",
 "created_at": "2026-09-16T12:00:00Z", "excluded": ["…"]}
```

Unknown keys are ignored on read; `version != 1` → `invalid_argument`.

`key_prefix` validation (in `rayd-core::persistence::keys`, mirrored in
both SDKs through `limits.json`): non-empty, ≤ 900 bytes UTF-8, no
leading or trailing `/`, no empty component (`//`), no `.` or `..`
component, characters only `[A-Za-z0-9!_.*'()/-]` (the S3 "safe"
set). `bucket`: 3–63 characters, `[a-z0-9.-]`, starts and ends with
`[a-z0-9]`, no `..`, not an IPv4 literal. Violations → gRPC
`INVALID_ARGUMENT` before any message.

S3 operations `rayd` issues, with **every** parameter it sends, are
written into `AWS_API_NOTES.md` §17 (source: the Amazon S3 API
Reference, cited by URL) before the adapter exists; the table is the
contract for reviewers:

| Operation | Parameters sent | Used for |
|---|---|---|
| `HeadObject` | `Bucket`, `Key` | none (not used; listed to say so) |
| `GetObject` | `Bucket`, `Key` | manifest and archive download |
| `PutObject` | `Bucket`, `Key`, `Body`, `ContentLength`, `ContentType` (`application/json` / `application/gzip`) | manifest; archive when it fits in one part (< 8 MiB) |
| `CreateMultipartUpload` | `Bucket`, `Key`, `ContentType` | archive ≥ 8 MiB |
| `UploadPart` | `Bucket`, `Key`, `UploadId`, `PartNumber`, `Body`, `ContentLength` | 8 MiB parts, sequential part numbers from 1 |
| `CompleteMultipartUpload` | `Bucket`, `Key`, `UploadId`, `MultipartUpload{Parts[{ETag, PartNumber}]}` | end of archive |
| `AbortMultipartUpload` | `Bucket`, `Key`, `UploadId` | any failure or cancellation after `CreateMultipartUpload` |

No `ServerSideEncryption`, `StorageClass`, `Metadata`, `ChecksumAlgorithm`
or ACL parameters: bucket defaults apply (SSE-S3 is the default of every
bucket since 2023; the operator's bucket policy is theirs). No
`ListObjectsV2`: `s3:ListBucket` is granted only so that a missing key
answers 404 (`NoSuchKey`) instead of 403, which the restore path relies
on. The client is built with `BehaviorVersion::latest()`, `region` from
D5, `force_path_style` unset, default retries (3 attempts) and the
default connect timeout; no custom endpoint ever.

### D3. Checkpoint archive rules

Root: the requesting user's home from `FsIdentity.home` (`/home/user` for
the default user); `user` absent = default user; `user = "root"` →
`PERMISSION_DENIED` always (persistence never archives `/root`, whatever
`RAYITO_ALLOW_ROOT` says); unknown user → `INVALID_ARGUMENT`. The whole
walk, every `open`/`read`/`lstat`, runs on one blocking thread inside an
`FsIdentityGuard` for the user, so an entry the user cannot read is an
entry `rayd` cannot read: it is skipped and counted in `skipped`, never
an error and never a root read.

Walk: depth-first, entries sorted by byte value of their names, `lstat`
per entry, never descending into a symlinked directory, never crossing
into another filesystem (`st_dev` differs from the home's) — a mounted
tmpfs inside the home is skipped as a whole. Archived kinds: regular
files (`append_file` with the `lstat` size; the reader is capped at that
size and zero-padded if the file shrank meanwhile, so a live-changing
file yields a truncated/padded copy and never aborts the checkpoint),
directories (header only, with mode and mtime), symlinks (`append_link`
with the target verbatim, never resolved). Hard links are archived as
independent regular files (duplicate bytes, no `Link` entries). Sockets,
FIFOs, character and block devices are skipped and counted. Modes are
stored as `st_mode & 0o7777`, owner/group names as `user`/`user`, uid/gid
as the identity's, mtime from `lstat`; `atime`/`ctime`/xattrs are not
stored. Long names use the GNU extensions of the `tar` crate (default).

Ignore list (fixed, documented in `persistence.md`), applied before any
`exclude`:

- name components at any depth: `.cache`, `__pycache__`,
  `.ipynb_checkpoints`, any name starting with `.rayito-tmp-`;
- relative paths from the home: `.local/share/jupyter/runtime`,
  `.ipython/profile_default/history.sqlite`,
  `.ipython/profile_default/history.sqlite-journal`.

`exclude`: ≤ 64 entries (`PERSIST_EXCLUDE_MAX`), each validated like a
`RequestPath` (non-empty, no NUL, no `..`, not absolute, ≤ 4096 bytes)
and interpreted as a relative path from the home matched by whole
components (`data/raw` excludes `data/raw` and everything below it, not
`data/raw2`). No globs: the SDK documents that one excludes directories,
not patterns. The pre-walk (D7) counts what will be archived so
`CheckpointStarted{files, bytes}` is exact for the state at that
instant.

Compression: `flate2::write::GzEncoder` level 1 (`Compression::fast()`),
pure-Rust `miniz_oxide` backend; `sha256` (workspace `sha2`) computed over
the compressed stream as it is produced; `archive_bytes` is the compressed
size. Gzip over zstd because the workspace stays free of C for the
compressor and the level-1 throughput on one Graviton vCPU (well above the
upload bandwidth of Q53's expected range) is enough; it can be revisited
with numbers.

### D4. Restore rules

Order: `GetObject(manifest.json)` first — `NoSuchKey`/404 → gRPC
`NOT_FOUND` before any message (the SDK turns it into "nothing to
restore" on auto-restore and into `NotFoundException` on an explicit
call); `AccessDenied` → `PERMISSION_DENIED`; parse and validate the
manifest (D2) → `INVALID_ARGUMENT` on a bad one. Then `RestoreStarted`
with the manifest's `archive_bytes` and `files`, then
`GetObject(home.tar.gz)` streamed into the unpack thread.

Unpack: on one blocking thread under the user's `FsIdentityGuard`, into
the user's home (same root and `user` rules as D3), through
`flate2::read::GzDecoder` and `tar::Archive::entries()`, with sha256 of
the compressed bytes accumulated as they arrive. Per entry:

- accepted kinds: regular file, directory, symlink (plus the GNU long
  name/link header entries the crate consumes internally); every other
  kind (hard link, device, FIFO, socket, sparse, PAX global) is skipped
  and counted in `skipped` — `rayd` runs with `CAP_MKNOD` in `CapEff`, so
  a crafted archive must never reach `mknod`;
- path safety: an absolute entry name is refused by an explicit check on
  `entry.path()` before the crate sees it (`unpack_in` would silently
  re-root it under the home, which the spec does not allow), then the
  crate's `unpack_in(home)` semantics refuse `..` components and require
  the canonical parent to lie inside the canonical home (a symlinked
  parent that escapes refuses the entry); a refused entry ends the
  restore with `StreamError invalid_argument` (the archive is not one
  `rayd` wrote);
- ownership is whatever the fs identity creates (uid/gid of the user);
  `preserve_ownerships` off, `preserve_mtime` on, `unpack_xattrs` off,
  `overwrite` on (an existing file is replaced, directories merge,
  an existing symlink at the path is removed first, never followed);
  the mode from the header is applied masked to `0o777` (setuid/setgid/
  sticky bits dropped), so a restored file can never be a setuid binary;
- symlink targets are written verbatim (absolute or relative, inside or
  outside the home) and never followed;
- the manifest's `home` and `user` are informative only; the destination
  is always the requesting user's home.

At the end the computed sha256 must equal the manifest's, else
`StreamError internal` with the fixed message `archive checksum mismatch`.
A failure after `started` leaves the home **partially restored** (entries
already unpacked stay); the SDK documents that the recovery is `kill()` +
`create(persist=)` again, and `restore_files()` never pre-verifies the
archive (that would double the download). `RestoreDone{files,
bytes_written, archive_bytes, sha256, skipped, duration_ms}` closes the
stream.

### D5. Credentials and region: IMDSv2 as root, nothing else

`S3ObjectStore` is built once at startup with an
`aws_config::imds::credentials::ImdsCredentialsProvider` (default
endpoint `http://169.254.169.254`, IMDSv2 token, profile discovered by
listing `security-credentials/`, which returns `execution_role`, §9),
wrapped in the SDK's `LazyCredentialsCache`; no environment, profile or
SSO providers are in the chain, so nothing on disk is ever read. Region:
`S3Location.region` when present, else the `AWS_REGION` variable the
platform injects (§9), else gRPC `FAILED_PRECONDITION` with the fixed
message `region unknown`. A `rayd --persistence-credentials imds|default`
flag (default `imds`) exists only for `make dev-run` and the ignored
adapter test of D7 (`default` = the SDK's default chain); the image's
`CMD` never passes it.

Credential outcomes: IMDS 404 on `security-credentials/` (no execution
role) → gRPC `PERMISSION_DENIED` with `no execution role credentials`
before any message; the request fails in ≈ 1 RTT to IMDS, the network is
never touched. `ExpiredToken`/`InvalidAccessKeyId` from S3 →
`permission_denied` (`execution role credentials rejected`), whether as
status (before `started`) or `StreamError` (after). The provider refreshes
from IMDS before the cached expiry; if the platform keeps serving the
same expired credentials (Q1 unmeasured), S3 rejects and the caller sees
`permission_denied`; the optional slow e2e of D15 measures it and Q1 gets
the answer. Because `rayd` is uid 0, the D-route of Q48 does not apply to
it; the caps image keeps `imds_blocked = true` for uid 1000–65535
unchanged, which the e2e re-asserts on the same sandbox that checkpoints.

### D6. TLS stack, pins, cross-compile ladder and the measurement

Workspace pins (exact, like every dependency here):

```toml
aws-config = { version = "=1.12.0", default-features = false, features = ["rt-tokio", "default-https-client"] }
aws-sdk-s3 = { version = "=1.148.0", default-features = false, features = ["rt-tokio", "default-https-client", "http-1x"] }
tar = "=0.4.46"
flate2 = { version = "=1.1.10", default-features = false, features = ["rust_backend"] }
```

`sigv4a` (S3 multi-region access points) is left out. `default-https-client`
= `aws-smithy-http-client` with `rustls-aws-lc` (`rustls` + `aws-lc-rs`,
post-quantum preference on) and `rustls-native-certs` for the trust
store, which probes the OS bundle; D12 makes the Dockerfile guarantee
`/etc/pki/tls/certs/ca-bundle.crt`. If `aws-config`'s `imds` module
needs `client-hyper` to compile with these features, that feature is
added and noted; nothing else from its default set (`sso`,
`credentials-process`) is enabled.

Ladder, in this order, with a fixed budget:

1. Baseline: a clean `cargo zigbuild --release --locked --target
   aarch64-unknown-linux-musl -p rayd` (cold `target-linux/`), wall
   time `T0` and size `S0` (4 700 984 B expected with `cargo auditable`)
   recorded in the task note.
2. `aws-lc-rs` (the pins above). Budget: **8 working hours** of
   implementer effort from the first failing link. Known knobs, tried in
   order: `AWS_LC_SYS_CMAKE_BUILDER=0` (force the `cc` builder),
   `AWS_LC_SYS_NO_ASM=1`, `CFLAGS_aarch64_unknown_linux_musl` additions
   through the zig wrapper; never `bindgen` on the build machine.
3. If (2) does not link within budget: switch the HTTPS client to
   `ring` — `aws-smithy-http-client = { version = "=1.4.2",
   default-features = false, features = ["rustls-ring"] }` pinned in the
   workspace, `default-https-client` dropped from `aws-config` and
   `aws-sdk-s3`, the client installed explicitly with
   `aws_smithy_http_client::Builder::new().tls_provider(Ring)`. Budget:
   **4 working hours**.
4. If (3) does not link either: **option 1 fallback, D6b**, and the
   reason (the exact link error and the knobs tried) goes into the task
   note, `MILESTONES.md` M7 row 4 and ADR-009.

Measurements recorded in `MILESTONES.md` (row 4) and ADR-009 whatever
the outcome: `T0`/`T1` (clean build), `S0`/`S1` (binary), the provider
that linked, the `cargo deny check` result (a named exception is added
only if the check fails, following `m7-supply-chain` D4), `cargo bloat
--release --target aarch64-unknown-linux-musl -n 15` top crates when
`S1 > 3 × S0`. There is no size cap: Q42 measured that snapshot size is
not the cold-start driver, and the S3 code pages are not touched until
the first `Checkpoint`; the e2e reports the first-checkpoint latency
(lazy page-in) separately from the second.

### D6b. Option-1 fallback contract (only if D6 step 4 is reached)

Same proto, same SDK surface, same S3 layout, same IAM template, same
manifest. What changes:

- `rayd` keeps `rayd-core::persistence` (keys, exclude validation,
  manifest, event translation, the one-at-a-time rule) but the
  `ObjectStore`/`HomeArchiver` ports are replaced by one port
  `PersistenceHelper { spawn(op, spec) -> HelperRun }` whose adapter
  spawns `python3 -m rayito_kernel_sidecar.persist checkpoint|restore`
  **as uid 1000** through the existing `ProcessSpawner` (scratch env:
  `HOME`, `PATH`, `AWS_REGION`, `PYTHONPATH=/opt/rayito/sidecar/src`;
  stdin closed; JSON-lines events on stdout with the same fields as D1's
  messages; stderr reemitted only through the sidecar's allowed-fields
  filter).
- The helper (`kernel-sidecar/src/rayito_kernel_sidecar/persist.py`,
  fully typed, tested on the host with `moto`-free fakes of the boto3
  client) implements D3/D4 verbatim with `tarfile` + `gzip` and
  `boto3` (pinned in `kernel-sidecar/requirements.txt`, default
  credential chain → IMDS as uid 1000).
- Consequence, documented in `SECURITY.md` T1 and `persistence.md`:
  persistence in fallback mode works only where IMDS is reachable for
  uid 1000, i.e. the **default** image, and the execution role is
  exposed to user code for the sandbox's whole life. On the caps image
  the helper fails with `permission_denied`. The acceptance keeps both
  halves of the criterion on different sandboxes: the 50 MB round trip
  on the default image and `imds_blocked == true` on the caps image.
- `MILESTONES.md` M7 row 4 says "opción 1 (fallback)", ADR-009 records
  the reason, and a follow-up change is listed in the ADR's
  consequences to return to option 2 when the toolchain allows.

### D7. Hexagonal placement, memory bounds, cancellation

`rayd-core::persistence` (pure, tested on Windows with fakes):

| Module | Responsibility |
|---|---|
| `keys` | `BucketName`, `KeyPrefix` (D2 rules), `ObjectKeys{archive, manifest}` |
| `plan` | `ArchivePlan{root: FsIdentity, excludes: Vec<RelativePath>}`, `IgnoreRules` (D3 list), `should_skip(component_path)`, `ExcludeList::parse(&[String])` with `PERSIST_EXCLUDE_MAX` |
| `manifest` | `Manifest` v1 serde model, `validate()` |
| `progress` | `Counters{files_done, bytes_read, bytes_uploaded, …}` as `AtomicU64`s shared with the blocking thread, `snapshot()` |
| `checkpoint` / `restore` | the state machines that turn port outcomes into the ordered event sequence (`Started → Progress* → Done | Error`), the one-per-sandbox `PersistenceGate` (an `AtomicBool` lease with a guard), `PersistenceError` (no paths, no keys, no bucket in messages) → `StreamError` code / gRPC status per D8 |

Ports (traits in `rayd-core`, introduced here because their adapters
ship here):

```rust
pub trait HomeArchiver: Send + Sync {
    fn archive(&self, plan: &ArchivePlan, sink: Box<dyn Write + Send>, counters: Arc<Counters>) -> Result<ArchiveSummary, ArchiveError>;   // runs on the caller's blocking thread
    fn extract(&self, plan: &ArchivePlan, source: Box<dyn Read + Send>, counters: Arc<Counters>) -> Result<ExtractSummary, ArchiveError>;
    fn count(&self, plan: &ArchivePlan) -> Result<ArchiveSummary, ArchiveError>;          // the pre-walk for CheckpointStarted
}
pub trait ObjectStore: Send + Sync + 'static {   // native async fn in traits; wired as a generic `S: ObjectStore`, never `dyn`, no `async-trait` crate
    fn get(&self, bucket: &BucketName, key: &str) -> impl Future<Output = Result<ObjectBody, StoreError>> + Send;   // ObjectBody = size hint + AsyncRead
    fn put(&self, bucket: &BucketName, key: &str, body: Bytes, content_type: &str) -> impl Future<Output = Result<(), StoreError>> + Send;
    fn put_multipart(&self, bucket: &BucketName, key: &str, content_type: &str, parts: impl PartSource) -> impl Future<Output = Result<PutSummary, StoreError>> + Send;  // aborts on error/cancel
}
pub trait PartSource: Send + 'static {           // the bounded channel, abstracted: rayd-core has `bytes` but no `tokio`
    fn next_part(&mut self) -> impl Future<Output = Option<Bytes>> + Send;
}
```

`rayd-core` gains no new dependency (`bytes`, `serde`, `serde_json`,
`sha2`, `thiserror` are already there); `tokio::sync::mpsc` stays in
`rayd`, which implements `PartSource` for the receiver.

Adapters in `crates/rayd/src/adapters/`: `tar_archiver.rs`
(`TarHomeArchiver`, `std::fs` + `tar` + `flate2` + `sha2`, entered
through `FsIdentityGuard` exactly like `StdFileSystem`) and
`s3_store.rs` (`S3ObjectStore`, `aws-sdk-s3`; the multipart loop
uploads parts sequentially as they arrive, remembers `ETag`s, calls
`CompleteMultipartUpload`, and `AbortMultipartUpload` in every error and
drop path with a 5 s budget; a body smaller than one part is sent with
`PutObject`). `crates/rayd/src/grpc/persistence.rs` implements the two
RPCs and `grpc/filesystem.rs` delegates to it (same service, separate
file). Pipelines:

- Checkpoint: `spawn_blocking(archive)` writes into a `PartWriter` that
  fills 8 MiB `BytesMut` parts and sends them on a bounded `mpsc`
  (capacity 2) → `put_multipart` on the async side; the gRPC task samples
  `Counters` every 1 s for `progress` and awaits the two halves. Bound:
  ≤ 3 parts (24 MiB) + the encoder's buffers.
- Restore: the `GetObject` body is read in 1 MiB chunks into a bounded
  `mpsc` (capacity 8) → a `ChunkReader: Read` on the blocking side feeds
  `GzDecoder` → `tar`. Bound: ≤ 9 MiB + the decoder's window.

Cancellation: client disconnect or deadline cancels the gRPC task, which
drops the channels; the blocking thread notices the closed channel on its
next write/read and returns `Cancelled`; `put_multipart` aborts the
upload. `/suspend` uses the same path through `SuspendableStream`
(`StreamError suspending` emitted, the multipart aborted, the lease
released); a checkpoint is never resumed automatically. The lease is
released by the guard's `Drop` in every path.

Tests: domain tests for keys, exclude parsing, ignore rules, manifest,
the state machines with fake ports (an in-memory `ObjectStore`, a fake
`HomeArchiver`); adapter tests under `cfg(unix)` for `TarHomeArchiver`
on a `tempfile` tree (skips, symlinks, unreadable file, exclude, live
shrink, restore path safety with a crafted `..` and a device entry
built with the `tar` crate, mode masking, overwrite); one `#[ignore]`
integration test `crates/rayd/tests/s3_store.rs` that round-trips 20
MiB against `RAYITO_PERSIST_BUCKET` with `--persistence-credentials
default` semantics (the developer's chain), run by hand from WSL2.

### D8. Concurrency, timing, error codes, keep-alive

- One persistence operation per sandbox: a second `Checkpoint`/`Restore`
  while one runs → gRPC `FAILED_PRECONDITION` (`persistence busy`). Other
  filesystem RPCs keep working during a checkpoint; the docs recommend
  quiescing the sandbox's own processes for a consistent copy.
- No server-side cap on duration or size; the client's `grpc-timeout`
  is the deadline (the SDK defaults to 600 s); reaching it ends with
  `deadline_exceeded` from the client's side and the abort of D7.
- `progress` at most once per second and only when a counter changed;
  the generic `KeepAlive` after 30 s of silence still applies (a huge
  single file compresses for a while without a counter change).
- Status before the first message vs `StreamError` after it:

| Cause | Before `started` | After `started` |
|---|---|---|
| bad bucket / prefix / exclude / manifest, unknown user, region mismatch (`PermanentRedirect`, 301) | `INVALID_ARGUMENT` | `invalid_argument` |
| `user = "root"`, no execution role, `AccessDenied`, expired/invalid credentials | `PERMISSION_DENIED` | `permission_denied` |
| manifest missing (restore), archive missing though manifest exists | `NOT_FOUND` | `not_found` |
| another operation running | `FAILED_PRECONDITION` | — |
| `AWS_REGION` unknown | `FAILED_PRECONDITION` | — |
| network, 5xx after the SDK's retries, tar/gzip error, checksum mismatch, disk full (restore) | `INTERNAL` / `RESOURCE_EXHAUSTED` (disk) | `internal` (message `disk full` for `ENOSPC`) |
| `/suspend` | — | `suspending` |

Messages are fixed sentences without paths, keys, bucket names or the
S3 request id (the request id goes to the log, D13).

### D9. Python SDK surface (sync and async identical)

`rayito.S3Prefix` (frozen dataclass in `_models.py`):
`bucket: str`, `prefix: str = "rayito"`, `name: str | None = None`,
`region: str | None = None`; `__post_init__` validates `bucket` and the
joined key prefix with the D2 rules (from `_limits.py`); properties
`key_prefix -> str` (`f"{prefix}/{name}"`, `InvalidArgumentException` when
`name` is `None`), `archive_key`, `manifest_key`, `uri`
(`s3://bucket/key_prefix`); `with_name(name) -> S3Prefix`. `name` is the
identity of one persisted home; `prefix` is the operator's base that the
IAM policy scopes.

`Sandbox.create(..., persist: S3Prefix | None = None, persist_timeout:
float = DEFAULT_PERSIST_TIMEOUT_SECONDS)` (600 s):

1. `persist` without `execution_role_arn` → `InvalidArgumentException`
   before any AWS call (the role is what makes S3 reachable; the SDK does
   not guess one).
2. After readiness, bind: `self._persist = persist if persist.name else
   persist.with_name(self.sandbox_id)`; exposed as `sbx.persist`.
3. If `persist.name` was given, auto-restore: `restore_files(timeout=
   persist_timeout)`; a `NotFoundException` from it is swallowed and
   recorded as `sbx.last_restore = None` (first life of that name); any
   other error closes the sandbox and, unless `keep_on_failure`,
   terminates the VM, then re-raises (same policy as a readiness
   failure). A successful restore is kept in `sbx.last_restore:
   RestoreResult | None`.
4. `connect(..., persist=S3Prefix | None)` only binds (no restore): a
   later `checkpoint_files()`/`reincarnate()` needs it; `name` must be
   set there (`InvalidArgumentException` otherwise).

Instance methods (on `Sandbox`; `sbx.files` stays E2B-shaped and does not
grow):

- `checkpoint_files(*, target: S3Prefix | None = None, exclude:
  Sequence[str] = (), timeout: float = 600) -> CheckpointResult` —
  `target` defaults to `sbx.persist` (`InvalidArgumentException` when
  neither); validates `exclude` client-side (count and `RequestPath`
  rules) then opens `Checkpoint` on the **stream channel** with deadline
  `timeout`, consumes `started`/`progress` (available to a `on_progress:
  Callable[[CheckpointProgress], None] | None` callback), returns
  `CheckpointResult(bucket, key_prefix, files, bytes_read,
  archive_bytes, sha256, skipped, duration)` on `done`.
- `restore_files(*, source: S3Prefix | None = None, timeout: float =
  600, on_progress=None) -> RestoreResult(files, bytes_written,
  archive_bytes, sha256, skipped, duration)`; explicit calls raise
  `NotFoundException` when there is nothing under the prefix.
- `reincarnate(*, exclude=(), persist_timeout: float = 600) -> Sandbox`:
  requires `sbx.persist` and the launch options recorded by `create()`
  (`LaunchOptions` frozen dataclass kept on the instance: template ARN,
  `template_version`, `timeout`, `idle`, `envs`, `metadata`,
  `cpu_time_limit`, `execution_role_arn`, `allowed_ports`, `ingress`,
  `egress`, `logging`, `access_token` if it was explicit, `ready_timeout`,
  `request_timeout`, `reconnect_timeout`, `keep_on_failure`, the control
  plane and transport objects) — a `connect()` handle raises
  `InvalidArgumentException` naming `create(persist=)`. Order:
  `checkpoint_files(exclude=exclude, timeout=persist_timeout)` → `new =
  Sandbox.create(**options, persist=self.persist, persist_timeout=
  persist_timeout)` (which restores) → `self.kill()` (`SandboxNotFound`
  suppressed) and `self.close()` → return `new`. If `create` fails, the
  old sandbox is left running and the exception propagates with a note
  that the checkpoint under `sbx.persist.uri` is complete; the caller
  decides. Docstring and docs: the new sandbox has a fresh 8 h, a new
  `sandbox_id`, a new access token unless the original was explicit,
  the same `metadata`; kernel variables, processes and PTYs are gone
  (ADR-007's honest limit).

Exceptions: `PersistenceException(SandboxException)` with `code: str`
(the `StreamError` code or the status name in lower case) for
`permission_denied`, `internal`, `failed_precondition`; `NotFoundException`
for the missing checkpoint; `InvalidArgumentException` for local
validation and `INVALID_ARGUMENT`; `TimeoutException` on the deadline;
gRPC `UNIMPLEMENTED` (an image with an older `rayd`) →
`PersistenceException(code="unimplemented")` whose message says the image
must be republished with an agent that implements `Checkpoint`. TS:
`PersistenceError extends SandboxError` with `code`.

Channels and re-mint: both RPCs on the stream channel, proxy 403 retried
once by re-minting before the first message (like `WatchDir`); a cut
mid-stream (`suspending`, `UNAVAILABLE`) is **not** reconnected: the
exception says the operation was interrupted and must be re-run.
`limits.json` gains `PERSIST_KEY_PREFIX_MAX_BYTES: 900`,
`PERSIST_EXCLUDE_MAX: 64`, `S3_BUCKET_NAME_MIN: 3`, `S3_BUCKET_NAME_MAX:
63`, `DEFAULT_PERSIST_TIMEOUT_SECONDS: 600`.

`rayito.e2b`: `set_timeout` (instance and class) keeps raising
`UnimplementedError(feature="set_timeout")`; `reason` now reads `no existe
UpdateMicrovm; usa rayito.Sandbox.reincarnate() (checkpoint en S3 + VM
nueva)`. Nothing else in the shim changes (no `persist` kwarg there: it
is not E2B surface).

### D10. TypeScript twins

`S3Prefix` class (`src/sandbox/persistence.ts`; constructor `{ bucket,
prefix = "rayito", name?, region? }`, same validation from `limits.ts`,
`keyPrefix`, `archiveKey`, `manifestKey`, `uri`, `withName()`);
`Sandbox.create({ ..., persist?, persistTimeoutMs = 600_000 })` with
rules 1–4 of D9 (`InvalidArgumentError`; `sbx.persist`, `sbx.lastRestore`);
`Sandbox.connect(id, { persist })`; `sbx.checkpointFiles({ target?,
exclude = [], timeoutMs = 600_000, onProgress? }) → CheckpointResult`
(`durationMs`), `sbx.restoreFiles({ source?, timeoutMs, onProgress? }) →
RestoreResult`, `sbx.reincarnate({ exclude, persistTimeoutMs }) →
Sandbox` with the same ordering and the same launch-options record;
`PersistenceError` with `code`; `NotFoundError` for the missing
checkpoint; both RPCs on the stream transport with the 403 re-mint before
the first message. Unit tests on the fake `rayd` servicer
(`tests/unit/persistence.test.ts`) mirror the Python ones one-to-one.

### D11. IAM template and operator guidance

`spike/m0/iam.yaml`: parameters `PersistenceBucket` (String, default
`""`) and `PersistencePrefix` (String, default `rayito`, pattern
`^[A-Za-z0-9!_.*'()/-]+$`, no leading/trailing slash); condition
`HasPersistenceBucket`; on `ExecutionRole`, a second inline policy
`persistence` (conditional) with:

```yaml
- Effect: Allow
  Action: [s3:PutObject, s3:GetObject, s3:AbortMultipartUpload]
  Resource: !Sub arn:aws:s3:::${PersistenceBucket}/${PersistencePrefix}/*
- Effect: Allow
  Action: s3:ListBucket
  Resource: !Sub arn:aws:s3:::${PersistenceBucket}
  Condition:
    StringLike:
      s3:prefix: !Sub ${PersistencePrefix}/*
```

No `s3:DeleteObject` (the role never deletes), no `s3:ListMultipartUploadParts`
(not used). The caller policy is untouched (the developer's own
credentials clean up test objects with `s3:DeleteObject`, which the
`AdministratorAccess` profile already has; the e2e documents it).
`infra/README.md` and `persistence.md` tell the operator: same region as
the image; SSE-S3 default is enough (SSE-KMS requires adding `kms:*` to
the role: documented, not templated); a lifecycle rule
`AbortIncompleteMultipartUpload` at 1 day for the prefix (a killed VM
can leave parts behind); with a custom VPC egress connector, S3 needs a
gateway endpoint or a NAT; `cfn-lint` + `validate-template` (`make
infra-lint` gains the template).

### D12. Image

`image/Dockerfile`: `ca-certificates` added to the `dnf install` list
(idempotent on AL2023 minimal; makes the dependency explicit) and the
tool check gains `test -f /etc/pki/tls/certs/ca-bundle.crt`. No hook,
port, capability or sidecar change. The next image version is published
by this change (the binary changed) and the e2e runs on it:
`rayito-base` (default) and `rayito-base-caps` (same zip,
`--os-capabilities ALL`).

### D13. Logging hygiene

`rayd` logs for persistence contain only: `rpc` (`Checkpoint`/`Restore`),
`outcome`, `files`, `bytes_read`/`bytes_written`, `archive_bytes`,
`skipped`, `parts`, `duration_ms`, `s3_error_code` (the S3 error code
string, e.g. `AccessDenied`), `s3_request_id`, `retry_attempts`, and the
credential source (`imds`). Never the bucket, the key prefix, a path, an
entry name, a symlink target, a manifest field other than `version`, or
any credential material. The SDKs log at `info` the `uri` on
checkpoint/restore start and end (bucket and prefix are configuration,
not secrets, and the operator needs them to find the objects) and never
the exclude list or the sha256 at `info`.

### D14. Docs and constitution

- `ARCHITECTURE.md`: `FilesystemService` row gains the two RPCs and the
  one-line rules; "Diseño hexagonal" tables gain `persistence`, the two
  ports and the two adapters; **ADR-009 — Persistencia de `/home/user`
  por S3 desde `rayd` (root + IMDS), no desde el sandbox**: context
  (8 h wall, options 1/2/3 of the report, T1), decision (option 2 with
  the D6 ladder and the D6b fallback rule), consequences (TLS crate in
  the binary and its measured cost, role scope, partial restore
  semantics, `reincarnate()` as the `set_timeout` answer, what stays
  out: EFS, presigned URLs); ADR-007's consequence sentence becomes
  "… crea otro sandbox y mueve su estado con `reincarnate()` (ADR-009)".
  The "no TLS crate" sentence in `openspec/project.md` tech stack becomes
  "no TLS on the listeners (the AWS proxy terminates TLS); the only TLS
  client is the S3 adapter of ADR-009".
- `SECURITY.md` T1: mitigation gains "con `persist=` el rol lleva
  `s3:PutObject/GetObject/AbortMultipartUpload` sobre
  `<bucket>/<prefix>/*` y `s3:ListBucket` acotado al prefijo
  (`spike/m0/iam.yaml`); `rayd` lee IMDS como root, uid 1000 sigue
  bloqueado en caps"; new **T14** (datos del cliente / S3): archive at
  rest in the operator's bucket (SSE default, bucket policy theirs),
  crafted archives (D4 kinds and path rules, mode mask, `CAP_MKNOD`
  never reached), leftover multipart parts, the fallback-mode exposure
  if D6b applies; "Execution role: por defecto ninguno" gains the
  `persist=` paragraph.
- `SPEC.md` §4: the bullet "Persistencia de filesystem entre sesiones
  (S3/EFS): candidato M6" becomes struck-through with "entregada en M7
  (`m7-s3-persistence`, ADR-009): checkpoint/restore de `/home/user` en
  S3 desde `rayd`; EFS sigue fuera"; `openspec/project.md` hard rule 3
  drops "no cross-session filesystem persistence".
- `docs/site/docs/persistence.md` (Spanish; nav entry "Persistencia"):
  why (8 h, `kill()`), the three-line quickstart, `S3Prefix`, what is and
  is not archived (ignore list, excludes, symlinks, special files, live
  files), what a restore does to an existing home, `reincarnate()`, IAM
  snippet and bucket guidance (D11), costs, the measured numbers (Q53,
  binary delta), the fallback-mode note if D6b applies. README section
  "Persistencia más allá de las 8 h" with the quickstart;
  `e2b-compat.md` `set_timeout` row points to `reincarnate()`;
  `concepts.md` lifetime paragraph likewise; `security.md` mirrors T14.
- `MILESTONES.md` M7 row 4: status, the numbers (T0/T1, S0/S1, provider,
  Q53 throughput, checkpoint/restore timings for 50 MB, first vs second
  checkpoint), image versions used.
- `AWS_API_NOTES.md`: §17 (D2 table with the S3 API Reference URLs and
  the sentence that these are the only S3 parameters `rayd` may send),
  §16 **Q53** (VM→S3 upload and download throughput through
  `INTERNET_EGRESS` at 2 GB, from the e2e), **Q54** (the Rust SDK's IMDS
  provider against `execution_role`: token PUT, profile discovery,
  refresh behaviour observed), Q1 note (what the slow e2e measured, or
  "sin medir" if it was not run).
- `CHANGELOG.md`s: `[Unreleased]` entries for the three components.

### D15. Acceptance e2e

`clients/python/tests/e2e/test_m7_persistence.py` (marker `e2e`; skipped
unless `RAYITO_E2E=1`, `RAYITO_TEMPLATE_CAPS`, `RAYITO_EXECUTION_ROLE_ARN`
and `RAYITO_PERSIST_BUCKET` are set; `RAYITO_PERSIST_PREFIX` defaults to
`rayito-e2e`; every S3 object created is deleted in teardown with the
developer's boto3 client, and the sweeper terminates leftovers):

1. `test_checkpoint_kill_restore_roundtrip`: `Sandbox.create(caps,
   execution_role_arn, ingress=["ALL_INGRESS"], persist=S3Prefix(bucket,
   prefix, name=f"e2e-{uuid4().hex}"), timeout=1800)` → assert
   `sbx.last_restore is None`; generate **inside the VM**
   `head -c 52428800 /dev/urandom > /home/user/data/blob.bin` (50 MB,
   never uploaded through the 0.65 MB/s window) plus `notes.txt`, a
   nested `proj/src/a.py`, a symlink `link -> data/blob.bin`, an
   executable `run.sh` (0755), a file under `.cache/` and one under
   `skipme/`; record `sha256sum` of every regular file and `stat -c
   '%a %U:%G'`; `wait_until(imds_blocked)` and the uid 1000 IMDS probe
   of `test_m6_hardening` **on this sandbox** (must be blocked);
   `r1 = sbx.checkpoint_files(exclude=["skipme"])` timed (first
   checkpoint, lazy page-in) → `r1.files`, `r1.archive_bytes`; a
   second `checkpoint_files()` timed (warm); `sbx.kill()`; `sbx2 =
   Sandbox.create(..., persist=sbx.persist)` → `sbx2.last_restore` not
   `None`, timed; same sha256 for `blob.bin`, `notes.txt`, `a.py`; the
   symlink is a symlink with the same target; `run.sh` is `755
   user:user`; `.cache/…` and `skipme/…` absent; `imds_blocked` still
   `true` on `sbx2`; report bytes, seconds and MB/s for both directions
   (→ Q53 and `MILESTONES.md`).
2. `test_reincarnate`: on `sbx2` write a marker, `sbx3 =
   sbx2.reincarnate()` → new `sandbox_id`, marker present, the old id is
   `TERMINATED`/`TERMINATING` on `get-microvm`, `sbx3.persist ==
   sbx2.persist`; kill `sbx3`.
3. `test_restore_missing_is_not_found`: explicit `restore_files()` on a
   fresh name → `NotFoundException` in < 5 s; `create(persist=)` with
   the same fresh name → `last_restore is None` and no exception.
4. `test_no_role_is_permission_denied`: on the **default** image
   without a role, `checkpoint_files(target=…)` →
   `PersistenceException(code="permission_denied")` in < 5 s and
   `imds_blocked is False` (fail-open unchanged).
5. `test_credentials_after_55_minutes` (marker `slow`, only with
   `RAYITO_E2E_SLOW=1`, ≈ $0.13, 62 min): a caps sandbox with
   `idle=None`, `time.sleep` until `uptime ≥ 3 600 s`, then
   `checkpoint_files()` → pass/fail recorded in Q1 either way (a
   `permission_denied` is a finding, not a test bug: the test asserts
   only that the outcome is one of the two documented ones and prints
   it).

`clients/typescript/tests/e2e/m7.e2e.test.ts`: the round trip of (1)
with a 20 MB blob and `reincarnate()`, same env vars, same cleanup.
Budgets in the tests: checkpoint and restore of 50 MB ≤ 300 s each
(fails otherwise), well under the 600 s default; the real numbers go to
the report lines.

### D16. Gates

`buf lint`, `buf breaking --against` the pre-change proto (run by hand
against a copy), `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, `cargo test --workspace` (Windows: domain
tests; WSL2: adapter tests), `cargo deny check`, `cargo auditable
zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd`
+ `scripts/check_auditable.py`, `python scripts/gen_limits.py --check`,
`uvx ruff check scripts`; `cd clients/python && uv run pytest tests/unit
&& uv run ruff check . && uv run ruff format --check . && uv run mypy src
tests`; `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm
test && pnpm build && pnpm pack:check`; `uvx cfn-lint==1.56.3
spike/m0/iam.yaml` + `validate-template`; `mkdocs build --strict`;
`python scripts/check_license.py`; then the real-AWS e2e of D15 on the
newly published image versions, zero MicroVMs alive afterwards, and
`openspec validate m7-s3-persistence --strict --no-interactive`.

## Risks / Trade-offs

- **`aws-lc-sys` does not link with zig for aarch64-musl.** Mitigated by
  the D6 ladder (`ring`, then D6b) with fixed budgets; the outcome is
  recorded whichever rung is reached.
- **Binary growth.** Expected several MB (rustls + aws-lc + hyper + the
  S3 client's serializers survive LTO). Accepted and measured; no cap
  (D6). The image zip and `codeInstallSizeInBytes` (1.3 GB) dwarf it.
- **VM→S3 bandwidth unknown (Q53).** If it turns out to be the 4 MB/s
  class, 50 MB ≈ 13 s and 1 GB ≈ 4.5 min per direction, which the
  600 s default covers up to ≈ 2 GB; larger homes need a bigger
  `timeout`. Documented with the measured number.
- **Credential expiry at 55 min (Q1).** A checkpoint after that may fail
  with `permission_denied` if the platform does not rotate; the slow e2e
  measures it. Either answer is recorded; no design depends on rotation
  except the documentation of when to checkpoint.
- **Partial restore on failure (D4).** Accepted over a staged restore
  (double disk, non-atomic directory swap); the recovery is one
  `create()`.
- **Inconsistent snapshot while processes write (D3).** Accepted; the
  padding rule keeps the archive valid; the docs say quiesce first.
- **Crafted archives (D4).** Only regular/dir/symlink entries, masked
  modes, `unpack_in` path rules, everything under the user's fs identity
  — the confused-deputy surface of T11 is not widened; T14 records the
  residual (a user who can write to the prefix can plant files in the
  next incarnation's home, which is exactly what the prefix is for).
- **Multipart leftovers.** Storage cost until aborted; mitigated by the
  documented lifecycle rule and the 5 s abort on every failure path.
- **`Sandbox.create` grows two kwargs and the instance keeps launch
  options.** Accepted for `reincarnate()`; `connect()` handles are
  explicitly excluded.

## Migration Plan

1. `AWS_API_NOTES.md` §17 written first; `spike/m0/iam.yaml` updated and
   the stack `rayito-m0-iam` updated by the maintainer with
   `PersistenceBucket=<bucket>` and
   `PersistencePrefix=rayito-e2e` (`CAPABILITY_NAMED_IAM`).
2. Proto, regen, `rayd` domain + adapters, SDKs, docs (tasks order).
3. `make image-zip`, `make image-publish` and `make image-publish-caps`
   with `BASE_IMAGE_VERSION=1` (two new versions, ≈ $0.074 storage);
   `RAYITO_TEMPLATE_VERSION` / `RAYITO_TEMPLATE_CAPS` pointed at them.
4. e2e (D15) green; numbers copied into `MILESTONES.md`, Q53/Q54/Q1,
   ADR-009; `openspec archive m7-s3-persistence --yes` by the acceptance
   agent.
5. Rollback: none needed at runtime (additive RPCs); an image version
   without the feature stays `ACTIVE` and the SDK maps `UNIMPLEMENTED`
   honestly.

## Open Questions

None. The unknowns (cross-compile outcome, binary delta, Q53, Q1) are
measurements with decided consequences, not open design questions.
