## MODIFIED Requirements

### Requirement: The archive is uploaded to S3 with the execution role read from IMDSv2 as root
`rayd` SHALL obtain credentials only from IMDSv2 (`ImdsCredentialsProvider`: token `PUT`, profile discovered from `security-credentials/`, which the platform names `execution_role`) with no environment, profile or SSO provider in the chain, and SHALL resolve the region from `S3Location.region` when present, else the `AWS_REGION` variable, else answer `FAILED_PRECONDITION`. The objects SHALL be `<key_prefix>/home.tar.gz` and `<key_prefix>/manifest.json`, the manifest written only after the archive upload completed, with the v1 fields `version, archive, compression, sha256, archive_bytes, files, bytes, skipped, home, user, sandbox_id, agent_version, created_at, excluded`. Archives of at least 8 MiB SHALL use `CreateMultipartUpload` + sequential `UploadPart` (8 MiB parts, numbered from 1) + `CompleteMultipartUpload`; smaller ones `PutObject`; any failure, client cancellation or `/suspend` after `CreateMultipartUpload` SHALL call `AbortMultipartUpload` within 5 s. The only S3 parameters sent SHALL be those listed in `AWS_API_NOTES.md` §17 (`Bucket`, `Key`, `Body`, `ContentLength`, `ContentType`, `UploadId`, `PartNumber`, `MultipartUpload.Parts[].{ETag, PartNumber}`); no encryption, storage class, metadata, checksum or ACL parameter; no custom endpoint. `bucket` and `key_prefix` SHALL be validated (bucket 3–63 chars of `[a-z0-9.-]`, no `..`, not an IPv4 literal; prefix non-empty, ≤ 900 bytes, no leading/trailing `/`, no empty, `.` or `..` component, charset `[A-Za-z0-9!_.*'()/-]`) → `INVALID_ARGUMENT` before any message. Memory in flight SHALL be bounded to three 8 MiB parts plus the encoder buffers on checkpoint and nine 1 MiB chunks plus the decoder window on restore. A `--persistence-credentials imds|default` flag (default `imds`) SHALL exist for local development only and the image `CMD` SHALL NOT pass it.

#### Scenario: no execution role
- **WHEN** `Checkpoint` is called on a sandbox launched without `executionRoleArn`
- **THEN** the stream ends with gRPC `PERMISSION_DENIED` and the fixed message `no hay credenciales del rol de ejecución` before any message, within 5 s, and no S3 request is made

#### Scenario: multipart abort on cancellation
- **WHEN** the client cancels a `Checkpoint` after the second `UploadPart` in the fake-store test
- **THEN** the store recorded `CreateMultipartUpload`, two `UploadPart`, one `AbortMultipartUpload` and no `CompleteMultipartUpload`, no manifest was written, and the persistence lease is released (a new `Checkpoint` is accepted)

#### Scenario: integration round trip
- **WHEN** the ignored test `crates/rayd/tests/s3_store.rs` runs from WSL2 with the developer's credentials and `RAYITO_PERSIST_BUCKET`
- **THEN** a 20 MiB body uploads through three parts, downloads byte-identical, and the objects are deleted by the test

### Requirement: Restore extracts only safe entries into the user's home and verifies the checksum
`Restore` SHALL fetch `manifest.json` first (`NoSuchKey` → gRPC `NOT_FOUND`, `AccessDenied` → `PERMISSION_DENIED`, `version != 1` or missing `sha256` → `INVALID_ARGUMENT`, all before any message), emit `RestoreStarted` from the manifest, then stream `home.tar.gz` into one blocking thread running under the user's fs identity that unpacks into the user's home (same user rules as the checkpoint; the manifest's `home`/`user` are informative only). It SHALL accept only regular files, directories and symlinks (hard links, devices, FIFOs, sockets, sparse and PAX-global entries skipped and counted), SHALL refuse absolute paths, `..` components and any entry whose canonical parent lies outside the canonical home with `StreamError invalid_argument`, SHALL create everything under the user's uid/gid, SHALL apply header modes masked to `0o777`, SHALL preserve mtime, SHALL overwrite existing files, merge directories and replace (never follow) an existing symlink at an entry's path, and SHALL write symlink targets verbatim. At the end the sha256 of the downloaded bytes SHALL equal the manifest's, else `StreamError internal` with the fixed message `el checksum del archivo no coincide`; `ENOSPC` SHALL end the stream with `StreamError internal` and the message `disco lleno`. A failure after `started` leaves already-unpacked entries in place (documented; recovery is `kill()` + `create(persist=)`).

#### Scenario: crafted archive
- **WHEN** the adapter test restores an archive built with entries `../escape`, `/abs`, a character device `dev0`, a file `bin/tool` with mode `0o4755`, and `ok.txt`
- **THEN** the restore ends with `invalid_argument` at the first escaping entry (or, for the device-only variant, completes with `skipped == 1`), no file exists outside the home, and in the variant without escaping entries `bin/tool` has mode `0o755` and `ok.txt` is present

#### Scenario: missing checkpoint
- **WHEN** `Restore` is called with a `key_prefix` under which no `manifest.json` exists
- **THEN** the call fails with gRPC `NOT_FOUND` before any message and no `home.tar.gz` request is made

#### Scenario: checksum mismatch
- **WHEN** the fake store serves an archive whose bytes differ from the manifest's `sha256`
- **THEN** the stream emits `started`, then `StreamError{code: "internal", message: "el checksum del archivo no coincide"}`, and the log line has `outcome="checksum_mismatch"`

### Requirement: One persistence operation at a time, with the documented status and StreamError mapping
`rayd` SHALL hold one persistence lease per sandbox: a `Checkpoint` or `Restore` while another runs SHALL fail with gRPC `FAILED_PRECONDITION` (`la persistencia está ocupada`); the lease SHALL be released on every path (success, error, cancellation, suspend) by a drop guard. Other filesystem RPCs SHALL keep working during a checkpoint. Failures before the first message SHALL be gRPC statuses and after it `StreamError` codes: `invalid_argument` (bad input, bad manifest, `PermanentRedirect`), `permission_denied` (`user = "root"`, no credentials, `AccessDenied`, expired or invalid credentials), `not_found` (manifest missing; archive missing although the manifest exists), `internal` (network exhaustion after the SDK's retries, tar/gzip errors, checksum mismatch, disk full), `suspending` (`/suspend`). Messages SHALL be fixed Spanish sentences containing no path, entry name, key, bucket or request id. `rayd` SHALL NOT impose a duration or size cap: the client's `grpc-timeout` is the deadline. A `/suspend` during an operation SHALL close the stream with `suspending`, abort any multipart upload and never resume the operation automatically.

#### Scenario: busy
- **WHEN** a second `Checkpoint` arrives while the first is between `started` and `done`
- **THEN** the second fails immediately with `FAILED_PRECONDITION` and the first completes unaffected

#### Scenario: suspend during checkpoint
- **WHEN** `/suspend` is invoked while a `Checkpoint` is uploading its third part
- **THEN** the client receives `StreamError{code: "suspending"}`, the fake store records `AbortMultipartUpload`, `/suspend` still answers 200 within its budget, and after `/resume` a new `Checkpoint` is accepted
