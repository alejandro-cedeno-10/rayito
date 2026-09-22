## ADDED Requirements

### Requirement: FilesystemService exposes Checkpoint and Restore as server-streams
`proto/rayito/v1/filesystem.proto` SHALL add `rpc Checkpoint(CheckpointRequest) returns (stream CheckpointEvent)` and `rpc Restore(RestoreRequest) returns (stream RestoreEvent)` to `FilesystemService`, with `S3Location{bucket, key_prefix, optional region}`, `CheckpointRequest{target, optional user, repeated exclude}`, `CheckpointEvent` = `oneof {CheckpointStarted started = 1; CheckpointProgress progress = 2; CheckpointDone done = 3; StreamError error = 4; KeepAlive keepalive = 5}`, `CheckpointStarted{files, bytes}`, `CheckpointProgress{files_done, bytes_read, bytes_uploaded}`, `CheckpointDone{files, bytes_read, archive_bytes, sha256, skipped, duration_ms}`, `RestoreRequest{source, optional user}`, `RestoreEvent` with the same envelope shape (`RestoreStarted{archive_bytes, files}`, `RestoreProgress{files_done, bytes_downloaded}`, `RestoreDone{files, bytes_written, archive_bytes, sha256, skipped, duration_ms}`). No new service SHALL be introduced. `buf lint` SHALL pass with the existing `buf.yaml` exceptions and `buf breaking` against the pre-change proto SHALL report nothing; the Rust, Python and TypeScript clients SHALL be regenerated, never hand-edited. Both RPCs SHALL require `x-access-token`, SHALL be wrapped like every server-stream (`SuspendableStream`, `KeepAlive` after 30 s of silence) and SHALL emit `progress` at most once per second and only when a counter changed.

#### Scenario: additive proto
- **WHEN** `buf lint` and `buf breaking --against <pre-change proto>` run on the edited `filesystem.proto`
- **THEN** both exit 0 and the generated `filesystem_pb2_grpc.py` and `filesystem_pb.ts` expose `Checkpoint` and `Restore` on `FilesystemService`

#### Scenario: event order on success
- **WHEN** `Checkpoint` runs against the fake ports with a home of 3 files and a store that accepts every part
- **THEN** the client receives exactly `started`, zero or more `progress`, then `done`, and the stream ends with gRPC status OK

### Requirement: The checkpoint archives the user's home under the user's identity with the documented ignore list
`Checkpoint` SHALL archive the home of the requesting user (`user` absent = the default user; `user = "root"` → gRPC `PERMISSION_DENIED` regardless of `RAYITO_ALLOW_ROOT`; unknown user → `INVALID_ARGUMENT`) as a gzip level-1 tar stream produced on one blocking thread inside the user's fs identity guard (`setfsuid`/`setfsgid`), so that any entry the user cannot read is skipped and counted in `skipped`, never read as root and never fatal. The walk SHALL be depth-first with names sorted by byte value, SHALL `lstat` every entry, SHALL never descend into symlinked directories or into a directory whose `st_dev` differs from the home's, SHALL archive regular files (size fixed at `lstat`, reader capped at that size and zero-padded if the file shrank), directories and symlinks (target verbatim, never resolved), SHALL archive hard links as independent regular files, and SHALL skip and count sockets, FIFOs, character and block devices. Modes SHALL be stored as `st_mode & 0o7777`, mtime from `lstat`; xattrs and atime SHALL NOT be stored. The fixed ignore list SHALL be: name components at any depth `.cache`, `__pycache__`, `.ipynb_checkpoints` and any name starting with `.rayito-tmp-`; relative paths `.local/share/jupyter/runtime`, `.ipython/profile_default/history.sqlite` and `.ipython/profile_default/history.sqlite-journal`. `exclude` SHALL accept at most 64 relative paths (no NUL, no `..`, not absolute, ≤ 4096 bytes) matched by whole path components from the home; a violation SHALL be `INVALID_ARGUMENT` before any message. `CheckpointStarted{files, bytes}` SHALL come from a pre-walk that applies the same rules. `sha256` SHALL be computed over the compressed archive bytes and `archive_bytes` SHALL be the compressed size.

#### Scenario: ignore list and excludes
- **WHEN** the adapter test archives a home containing `data/blob.bin`, `.cache/x`, `proj/__pycache__/m.pyc`, `skipme/a`, `data/raw/b`, `data/raw2/c` with `exclude = ["skipme", "data/raw"]`
- **THEN** the archive lists `data/blob.bin` and `data/raw2/c` and none of `.cache/x`, `proj/__pycache__/m.pyc`, `skipme/a`, `data/raw/b`, and `CheckpointStarted.files` equals the number of archived entries

#### Scenario: unreadable and special entries are skipped, not fatal
- **WHEN** the home contains a file with mode `0000` owned by another uid, a FIFO and a symlink to `/etc/passwd`
- **THEN** the checkpoint completes, `skipped` is 2, the symlink is archived as a symlink whose target is the literal `/etc/passwd`, and no bytes of `/etc/passwd` are in the archive

#### Scenario: a file that shrinks during the read
- **WHEN** a 4 MiB file is truncated to 1 MiB after the pre-walk but before its bytes are read
- **THEN** the archive entry is 4 MiB (1 MiB of data and 3 MiB of zero padding) and the checkpoint ends with `done`

### Requirement: The archive is uploaded to S3 with the execution role read from IMDSv2 as root
`rayd` SHALL obtain credentials only from IMDSv2 (`ImdsCredentialsProvider`: token `PUT`, profile discovered from `security-credentials/`, which the platform names `execution_role`) with no environment, profile or SSO provider in the chain, and SHALL resolve the region from `S3Location.region` when present, else the `AWS_REGION` variable, else answer `FAILED_PRECONDITION`. The objects SHALL be `<key_prefix>/home.tar.gz` and `<key_prefix>/manifest.json`, the manifest written only after the archive upload completed, with the v1 fields `version, archive, compression, sha256, archive_bytes, files, bytes, skipped, home, user, sandbox_id, agent_version, created_at, excluded`. Archives of at least 8 MiB SHALL use `CreateMultipartUpload` + sequential `UploadPart` (8 MiB parts, numbered from 1) + `CompleteMultipartUpload`; smaller ones `PutObject`; any failure, client cancellation or `/suspend` after `CreateMultipartUpload` SHALL call `AbortMultipartUpload` within 5 s. The only S3 parameters sent SHALL be those listed in `AWS_API_NOTES.md` §17 (`Bucket`, `Key`, `Body`, `ContentLength`, `ContentType`, `UploadId`, `PartNumber`, `MultipartUpload.Parts[].{ETag, PartNumber}`); no encryption, storage class, metadata, checksum or ACL parameter; no custom endpoint. `bucket` and `key_prefix` SHALL be validated (bucket 3–63 chars of `[a-z0-9.-]`, no `..`, not an IPv4 literal; prefix non-empty, ≤ 900 bytes, no leading/trailing `/`, no empty, `.` or `..` component, charset `[A-Za-z0-9!_.*'()/-]`) → `INVALID_ARGUMENT` before any message. Memory in flight SHALL be bounded to three 8 MiB parts plus the encoder buffers on checkpoint and nine 1 MiB chunks plus the decoder window on restore. A `--persistence-credentials imds|default` flag (default `imds`) SHALL exist for local development only and the image `CMD` SHALL NOT pass it.

#### Scenario: no execution role
- **WHEN** `Checkpoint` is called on a sandbox launched without `executionRoleArn`
- **THEN** the stream ends with gRPC `PERMISSION_DENIED` and the fixed message `no execution role credentials` before any message, within 5 s, and no S3 request is made

#### Scenario: multipart abort on cancellation
- **WHEN** the client cancels a `Checkpoint` after the second `UploadPart` in the fake-store test
- **THEN** the store recorded `CreateMultipartUpload`, two `UploadPart`, one `AbortMultipartUpload` and no `CompleteMultipartUpload`, no manifest was written, and the persistence lease is released (a new `Checkpoint` is accepted)

#### Scenario: integration round trip
- **WHEN** the ignored test `crates/rayd/tests/s3_store.rs` runs from WSL2 with the developer's credentials and `RAYITO_PERSIST_BUCKET`
- **THEN** a 20 MiB body uploads through three parts, downloads byte-identical, and the objects are deleted by the test

### Requirement: Restore extracts only safe entries into the user's home and verifies the checksum
`Restore` SHALL fetch `manifest.json` first (`NoSuchKey` → gRPC `NOT_FOUND`, `AccessDenied` → `PERMISSION_DENIED`, `version != 1` or missing `sha256` → `INVALID_ARGUMENT`, all before any message), emit `RestoreStarted` from the manifest, then stream `home.tar.gz` into one blocking thread running under the user's fs identity that unpacks into the user's home (same user rules as the checkpoint; the manifest's `home`/`user` are informative only). It SHALL accept only regular files, directories and symlinks (hard links, devices, FIFOs, sockets, sparse and PAX-global entries skipped and counted), SHALL refuse absolute paths, `..` components and any entry whose canonical parent lies outside the canonical home with `StreamError invalid_argument`, SHALL create everything under the user's uid/gid, SHALL apply header modes masked to `0o777`, SHALL preserve mtime, SHALL overwrite existing files, merge directories and replace (never follow) an existing symlink at an entry's path, and SHALL write symlink targets verbatim. At the end the sha256 of the downloaded bytes SHALL equal the manifest's, else `StreamError internal` with the fixed message `archive checksum mismatch`; `ENOSPC` SHALL end the stream with `StreamError internal` and the message `disk full`. A failure after `started` leaves already-unpacked entries in place (documented; recovery is `kill()` + `create(persist=)`).

#### Scenario: crafted archive
- **WHEN** the adapter test restores an archive built with entries `../escape`, `/abs`, a character device `dev0`, a file `bin/tool` with mode `0o4755`, and `ok.txt`
- **THEN** the restore ends with `invalid_argument` at the first escaping entry (or, for the device-only variant, completes with `skipped == 1`), no file exists outside the home, and in the variant without escaping entries `bin/tool` has mode `0o755` and `ok.txt` is present

#### Scenario: missing checkpoint
- **WHEN** `Restore` is called with a `key_prefix` under which no `manifest.json` exists
- **THEN** the call fails with gRPC `NOT_FOUND` before any message and no `home.tar.gz` request is made

#### Scenario: checksum mismatch
- **WHEN** the fake store serves an archive whose bytes differ from the manifest's `sha256`
- **THEN** the stream emits `started`, then `StreamError{code: "internal", message: "archive checksum mismatch"}`, and the log line has `outcome="checksum_mismatch"`

### Requirement: One persistence operation at a time, with the documented status and StreamError mapping
`rayd` SHALL hold one persistence lease per sandbox: a `Checkpoint` or `Restore` while another runs SHALL fail with gRPC `FAILED_PRECONDITION` (`persistence busy`); the lease SHALL be released on every path (success, error, cancellation, suspend) by a drop guard. Other filesystem RPCs SHALL keep working during a checkpoint. Failures before the first message SHALL be gRPC statuses and after it `StreamError` codes: `invalid_argument` (bad input, bad manifest, `PermanentRedirect`), `permission_denied` (`user = "root"`, no credentials, `AccessDenied`, expired or invalid credentials), `not_found` (manifest missing; archive missing although the manifest exists), `internal` (network exhaustion after the SDK's retries, tar/gzip errors, checksum mismatch, disk full), `suspending` (`/suspend`). Messages SHALL be fixed sentences containing no path, entry name, key, bucket or request id. `rayd` SHALL NOT impose a duration or size cap: the client's `grpc-timeout` is the deadline. A `/suspend` during an operation SHALL close the stream with `suspending`, abort any multipart upload and never resume the operation automatically.

#### Scenario: busy
- **WHEN** a second `Checkpoint` arrives while the first is between `started` and `done`
- **THEN** the second fails immediately with `FAILED_PRECONDITION` and the first completes unaffected

#### Scenario: suspend during checkpoint
- **WHEN** `/suspend` is invoked while a `Checkpoint` is uploading its third part
- **THEN** the client receives `StreamError{code: "suspending"}`, the fake store records `AbortMultipartUpload`, `/suspend` still answers 200 within its budget, and after `/resume` a new `Checkpoint` is accepted

### Requirement: Persistence logging hygiene
`rayd` SHALL log for `Checkpoint`/`Restore` only `rpc`, `outcome`, `files`, `bytes_read`/`bytes_written`, `archive_bytes`, `skipped`, `parts`, `duration_ms`, `s3_error_code`, `s3_request_id`, `retry_attempts` and the credential source, and SHALL never log the bucket, the key prefix, a path, an entry name, a symlink target, manifest fields other than `version`, or credential material.

#### Scenario: access denied log line
- **WHEN** S3 answers `AccessDenied` to `CreateMultipartUpload`
- **THEN** the log line contains `rpc="Checkpoint"`, `outcome="permission_denied"`, `s3_error_code="AccessDenied"` and a request id, and neither the bucket nor the key prefix, while the gRPC status message is the fixed sentence

### Requirement: The TLS stack is compiled into the musl binary, measured, with a fixed fallback ladder
`rayd` SHALL link `aws-sdk-s3` and `aws-config` with exact workspace pins (`=1.148.0`, `=1.12.0`), `default-features = false` plus `rt-tokio`, `default-https-client` (and `http-1x` for the S3 crate), `tar =0.4.46` and `flate2 =1.1.10` (`rust_backend`), for `aarch64-unknown-linux-musl` through `cargo zigbuild`; `sigv4a`, `sso` and `credentials-process` SHALL stay disabled; `cargo deny check` SHALL pass (a named crate exception only if needed, never a widened allowlist); `rayd-core` SHALL gain no dependency. The implementer SHALL record the clean-build wall time and binary size before (`T0`, `S0`) and after (`T1`, `S1`), the TLS provider that linked, and, when `S1 > 3 × S0`, a `cargo bloat` breakdown. The ladder SHALL be: `aws-lc-rs` (8 working hours from the first failing link, trying `AWS_LC_SYS_CMAKE_BUILDER=0`, `AWS_LC_SYS_NO_ASM=1` and zig `CFLAGS` first), then `ring` via `aws-smithy-http-client = "=1.4.2"` `rustls-ring` (4 hours), then the option-1 fallback: the same proto, SDK, layout, manifest and IAM policy, implemented by a fully typed `rayito_kernel_sidecar.persist` helper (`tarfile` + `gzip` + pinned `boto3`) spawned by `rayd` as uid 1000 through `ProcessSpawner` with JSON-lines events, working only where IMDS is open to uid 1000 and documented in `SECURITY.md` T1, `persistence.md`, ADR-009 and `MILESTONES.md` as exposing the role. `rayd` SHALL never shell out to an `aws` CLI.

#### Scenario: numbers recorded
- **WHEN** the auditable zigbuild of the final binary completes
- **THEN** `MILESTONES.md` M7 row 4 and ADR-009 state `T0`/`T1`, `S0`/`S1` in bytes, the provider rung reached, and `scripts/check_auditable.py` still finds `tonic`, `tokio`, `axum` and `nix` in `.dep-v0`

#### Scenario: fallback is documented, not silent
- **WHEN** neither `aws-lc-rs` nor `ring` links within the budgets
- **THEN** the task note holds the link errors and knobs tried, the helper of the fallback is what the RPCs call, `Health.imds_blocked` remains `true` on the caps image where the helper fails with `permission_denied`, and every named document says "opción 1 (fallback)" with the reason

### Requirement: Execution role policy scoped to the persistence prefix
`spike/m0/iam.yaml` SHALL accept `PersistenceBucket` (default empty) and `PersistencePrefix` (default `rayito`, pattern `^[A-Za-z0-9!_.*'()/-]+$`) and, only when the bucket is set, attach to `ExecutionRole` an inline policy `persistence` allowing `s3:PutObject`, `s3:GetObject` and `s3:AbortMultipartUpload` on `arn:aws:s3:::<bucket>/<prefix>/*` and `s3:ListBucket` on `arn:aws:s3:::<bucket>` with `Condition: StringLike: s3:prefix: <prefix>/*`; no `s3:DeleteObject`, no other resource, no change to the build role or the caller policy. The template SHALL stay `cfn-lint` clean and accepted by `validate-template`, `make infra-lint` SHALL lint it, and `infra/README.md` SHALL document the parameters, the `AbortIncompleteMultipartUpload` lifecycle rule, the SSE-S3 default and the extra `kms:*` needed for SSE-KMS, and the gateway endpoint or NAT needed with a custom VPC egress connector.

#### Scenario: template validates and scopes
- **WHEN** `uvx cfn-lint==1.56.3 spike/m0/iam.yaml` and `aws cloudformation validate-template` run, and after the stack update `aws iam simulate-principal-policy` is queried for the execution role
- **THEN** both tools report nothing, `s3:PutObject` on `arn:aws:s3:::<bucket>/rayito-e2e/x/home.tar.gz` is `allowed` and on `arn:aws:s3:::<bucket>/rayito/x` is `implicitDeny`

### Requirement: The image guarantees the CA bundle and ships the new binary
`image/Dockerfile` SHALL list `ca-certificates` in its `dnf install` line and fail the build if `/etc/pki/tls/certs/ca-bundle.crt` is absent; no hook, port, capability or sidecar change SHALL be made for persistence. The change SHALL publish new `rayito-base` and `rayito-base-caps` versions from the same zip with `--base-image-version` and run the whole existing e2e suite on them in addition to the persistence tests.

#### Scenario: image build check
- **WHEN** `make image-publish` builds the Dockerfile
- **THEN** the build log shows the bundle check passing and the version reaches `SUCCESSFUL`/`ACTIVE`

#### Scenario: regression on the new image
- **WHEN** `RAYITO_TEMPLATE_VERSION=<new> uv run pytest tests/e2e -m e2e` runs
- **THEN** every pre-existing e2e test passes as on 17.0 and zero MicroVMs are alive afterwards
