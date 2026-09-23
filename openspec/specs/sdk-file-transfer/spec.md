# sdk-file-transfer Specification

## Purpose
TBD - created by archiving change m9-file-transfer. Update Purpose after archive.

## Requirements

### Requirement: S3Staging configures transfers, with no hardcoded bucket
The Python SDK SHALL export `rayito.S3Staging(bucket, prefix="rayito-transfer", region=None, max_expires_in=86400, threshold_bytes=8 MiB, multipart_threshold_bytes=5 GiB)` (frozen, validated on construction) and `S3Staging.from_env()` reading `RAYITO_TRANSFER_BUCKET` (absent or empty → `None`), `RAYITO_TRANSFER_PREFIX` and `RAYITO_TRANSFER_REGION`. Validation SHALL raise `InvalidArgumentException` naming the field and never its value when: the bucket is not DNS-compatible without dots (`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`, not `xn--…`, not `…-s3alias`); the prefix is empty, longer than 256 bytes, has a leading or trailing `/`, an empty, `.` or `..` segment, a character outside `[A-Za-z0-9_.-/]`, or overlaps the `rayito` artifact namespace component-wise; the region does not match `^[a-z]{2}(-gov)?-[a-z]+-[0-9]$`; `max_expires_in` is outside 1–604 800; `threshold_bytes` is outside 1 MiB–5 GiB; `multipart_threshold_bytes` is outside 16 MiB–5 GiB. `Sandbox.create(..., transfer=None)` and `Sandbox.connect(..., transfer=None)` (sync and async) SHALL resolve `None` through `from_env()` and expose the result as `sbx.transfer`; when `persist` uses the same bucket the two key prefixes SHALL be component-disjoint or the call SHALL raise `InvalidArgumentException` before any AWS call. The TypeScript SDK SHALL offer the same through `Sandbox.create({ transfer })`, `Sandbox.connect(id, { transfer })` and `sandbox.transfer`. No bucket name SHALL be hardcoded in either SDK.

#### Scenario: dotted bucket refused
- **WHEN** a unit test builds `S3Staging("my.bucket")`
- **THEN** `InvalidArgumentException` is raised and its message does not contain `my.bucket`

#### Scenario: environment fallback
- **WHEN** `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` is set and `Sandbox.create()` runs against the stubbed plane without `transfer`
- **THEN** `sbx.transfer == S3Staging("amzn-s3-demo-bucket")`

#### Scenario: overlap with the persistence prefix
- **WHEN** `create(persist=S3Prefix("amzn-s3-demo-bucket", prefix="data"), transfer=S3Staging("amzn-s3-demo-bucket", prefix="data/tmp"))` runs
- **THEN** `InvalidArgumentException` is raised and no `run-microvm` is issued

### Requirement: Staging keys never carry the user's path
Every staging key SHALL be `<prefix>/<sandbox_id>/up/<32 hex>` for data going into the sandbox and `<prefix>/<sandbox_id>/down/<32 hex>` for data coming out, the hex drawn from a cryptographic random source; the user's path SHALL NOT appear in the key or in any S3 object metadata.

#### Scenario: key shape
- **WHEN** a unit test builds a key for sandbox `microvm-00000000-0000-0000-0000-000000000001` and path `/home/user/secret-plan.txt`
- **THEN** the key matches `^rayito-transfer/microvm-00000000-0000-0000-0000-000000000001/up/[0-9a-f]{32}$` and contains no part of the path

### Requirement: Presigning is forced to SigV4, virtual-hosted, regional and clamped
The Python SDK SHALL presign with a client configured `Config(signature_version="s3v4", s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"})` built from the sandbox's boto3 session, and the TypeScript SDK with an `S3Client` configured `requestChecksumCalculation: "WHEN_REQUIRED"`. The effective lifetime of a user-facing URL SHALL be `min(expires_in, max_expires_in, 604800)` seconds, reported as `expires_at`; `expires_in <= 0` SHALL raise `InvalidArgumentException`; the lifetimes of the `rayd`-facing URLs SHALL follow design D5. No URL SHALL sign `Content-Type`, a checksum or a length; `Content-Type: application/octet-stream` SHALL be recommended in `UploadTicket.headers`, sent by `rayd` on exports and passed on the SDK's own direct uploads. URLs SHALL travel only inside the transfer requests to `rayd` and SHALL never appear in logs, `repr`, exception messages, environment variables, argv, `runHookPayload` or metadata.

#### Scenario: SigV4 on the regional host in us-east-1
- **WHEN** the unit test presigns a `put_object` for `amzn-s3-demo-bucket` in `us-east-1` with fake credentials
- **THEN** the URL's host is `amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com` and its query has `X-Amz-Algorithm=AWS4-HMAC-SHA256` and no `AWSAccessKeyId`

#### Scenario: the seven-day clamp
- **WHEN** `upload_url(path, expires_in=604801)` runs with `max_expires_in=604800`
- **THEN** the user URL carries `X-Amz-Expires=604800` and `ticket.expires_at` is 604 800 s after signing

### Requirement: upload_url returns an armed, single-use UploadTicket
`sbx.files.upload_url(path, *, user=None, expires_in=3600, max_bytes=None, form=False, request_timeout=None)` (identical coroutine on `AsyncSandbox.files`) SHALL, without staging, raise `UnimplementedError` with the reason `configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET`; otherwise it SHALL presign the user URL (a `PUT`, or with `form=True` a `POST` policy with `content-length-range [0, max_bytes or 5368709120]`), presign the `rayd` GET and DELETE, call `StartImport(wait_for_object=True, max_bytes=max_bytes or 0)` and only then return. `UploadTicket` SHALL subclass `str` with the URL as its value and expose `url`, `method` (`"PUT"` or `"POST"`), `headers` (`{"Content-Type": "application/octet-stream"}` for PUT, `{}` for POST), `fields` (the POST form fields, `{}` for PUT), `path`, `expires_at`, `transfer_id`, `wait(timeout=None) -> EntryInfo` (following `WatchTransfer`, re-issued after the sandbox's reconnection poll on `UNAVAILABLE suspending`, a reset or EOF), `status() -> TransferStatus` and `cancel()`; its `repr` SHALL never show the URL and pickling SHALL raise `TypeError`. `AsyncUploadTicket` SHALL offer the same with awaitables. The native `Sandbox.upload_url(path, user=None, use_signature_expiration=None)` SHALL delegate with `expires_in = use_signature_expiration or 3600` and raise `InvalidArgumentException` when `use_signature_expiration <= 0`.

#### Scenario: PUT then wait
- **WHEN** a unit test PUTs 1 MiB to `str(ticket)` on the fake S3 with `ticket.headers` and calls `ticket.wait()`
- **THEN** it returns the `EntryInfo` of the written path and the fake `rayd` received one `StartImport` with `wait_for_object == True` before the PUT

#### Scenario: no staging
- **WHEN** `upload_url` runs with no `transfer` and no `RAYITO_TRANSFER_BUCKET`
- **THEN** `UnimplementedError` is raised naming `S3Staging` and no RPC reached the fake

#### Scenario: redacted representation
- **WHEN** a unit test formats `repr(ticket)` and logs the ticket at `DEBUG`
- **THEN** neither the repr nor any captured log line contains `X-Amz-Signature`

### Requirement: download_url exports a snapshot and returns a DownloadLink
`sbx.files.download_url(path, *, user=None, expires_in=3600, filename=None, request_timeout=None)` SHALL, without staging, raise the same `UnimplementedError`; otherwise it SHALL `Stat` the path (raising `FileNotFoundException` immediately when missing and `InvalidArgumentException` for a directory or symlink), export it with one presigned PUT below `multipart_threshold_bytes` or, at or above it, with `create_multipart_upload` plus one presigned `upload_part` URL per part (`part_size = max(8 MiB, ceil_to_MiB(ceil(size / 1000)))`), follow the transfer to its terminal state, complete the multipart upload with the returned ETags or abort it on any failure, then presign the user GET with `ResponseContentDisposition = attachment; filename="<ascii>"; filename*=UTF-8''<pct>` and return a `DownloadLink` (`str` subclass with `url`, `path`, `expires_at`, `size`, `sha256`, `transfer_id`, redacted `repr`). The SDK SHALL NOT delete download objects. The native `Sandbox.download_url(path, user=None, use_signature_expiration=None)` SHALL delegate like `upload_url`.

#### Scenario: missing file raises before any presign
- **WHEN** `download_url("/home/user/missing")` runs against the fake
- **THEN** `FileNotFoundException` is raised and no presign, `StartExport` or S3 request happened

#### Scenario: multipart abort on failure
- **WHEN** a multipart export ends `FAILED` in the fake `rayd`
- **THEN** the SDK called `abort_multipart_upload` for that upload id and raised the mapped exception

### Requirement: Large payloads route through S3 only when staging is configured
With `sbx.transfer` set and the capability probe passed, `files.write`/`write_files` SHALL upload every entry whose known size is at least `threshold_bytes`, or whose data is a non-seekable binary stream, directly to S3 with the caller's credentials (Python `upload_fileobj` with `ExtraArgs={"ContentType": "application/octet-stream"}`, TypeScript `@aws-sdk/lib-storage` `Upload`), hashing sha256 on the way, then call `StartImport(wait_for_object=False, expected_sha256, max_bytes=<uploaded size>, mode, metadata)` and follow it; the remaining entries SHALL share one gRPC `Write` stream and the results SHALL keep request order. `files.read` SHALL, after its `Stat`, export files of at least `threshold_bytes` and download them with the caller's credentials (Python `get_object` body chunks of 262 144 bytes for every format, collected for `bytes`/`text` and yielded for `stream`, bounded by the operation deadline and by `stream_idle_timeout` between chunks; TypeScript `GetObjectCommand`), verify the export's sha256 and delete the staging object. Without staging, below the threshold, or with the probe failing, the gRPC paths, deadlines and channels SHALL be exactly those of the `filesystem` capability.

#### Scenario: threshold boundary
- **WHEN** staging uses `threshold_bytes = 8 MiB` and the unit test writes 8 MiB − 1 byte and then exactly 8 MiB
- **THEN** the first went through one gRPC `Write` and the second through the fake S3 plus one `StartImport` carrying the sha256 of the data

#### Scenario: no staging, no transfer RPC
- **WHEN** staging is unset and a transport spy watches a 50 MB `files.write` and `files.read`
- **THEN** the spy saw `Write`, `Stat` and `Read` and no `StartImport` or `StartExport`

### Requirement: Older images are detected before any byte moves
Before the first transfer, the first write with non-empty `metadata` and the first write with `gzip=True` on a sandbox, the SDK SHALL call `GetTransfer` with an empty id once and cache the result for the sandbox's life: `NOT_FOUND` means the agent supports M9 transfers, metadata and gzip; `UNIMPLEMENTED` SHALL raise `UnimplementedError` with the reason `actualiza la imagen` for that call and every later one. `UNIMPLEMENTED` from any transfer RPC SHALL map to the same error.

#### Scenario: pre-M9 agent
- **WHEN** the fake `rayd` answers `UNIMPLEMENTED` to `GetTransfer`
- **THEN** `upload_url`, `write(metadata={"a": "1"})` and `write(gzip=True)` each raise `UnimplementedError` mentioning `actualiza la imagen`, and the probe was sent once

### Requirement: Transfer failures map to the E2B exception vocabulary
The SDKs SHALL export `TransferException(SandboxException)` (TypeScript `TransferError extends SandboxError`) with `code` and `reason` parsed from the transfer state, `FileUploadException(TransferException)` (`FileUploadError`), and a native `UnimplementedError(NotImplementedError)` (TypeScript `UnimplementedError extends Error`) with `feature` and `reason`, of which `rayito.e2b.UnimplementedError` SHALL be a re-export; TypeScript SHALL add `DiskFullError extends SandboxError`. A terminal transfer state SHALL map `deadline_exceeded` → `TimeoutException`, `invalid_argument` → `InvalidArgumentException`, `resource_exhausted` → `DiskFullException`, `permission_denied` → `AuthenticationException(proxy_rejected=False)`, `not_found` → `FileNotFoundException`, and every other code to `FileUploadException` for imports or `TransferException` for exports, each message starting with `<reason>: `. Unary transfer errors SHALL use the existing unary table.

#### Scenario: mapping table
- **WHEN** `failure_from_state` receives a failed import state for each code
- **THEN** each yields the listed class, and `too_large` yields an `InvalidArgumentException` whose message starts with `too_large`

### Requirement: Sync, async and TypeScript surfaces are identical
`rayito.Sandbox` and `rayito.AsyncSandbox` SHALL expose the same transfer names and parameters over the pure helpers of `rayito._transfer_base` (boto3 calls in the async tree through `asyncio.to_thread`), and the parity unit test SHALL list `upload_url`, `download_url`, `transfer`, `UploadTicket`/`AsyncUploadTicket`, `DownloadLink`, `TransferStatus`, `S3Staging`, `TransferException`, `FileUploadException`. The TypeScript SDK SHALL expose `files.uploadUrl(path, { user, expiresIn, maxBytes, form, requestTimeoutMs })`, `files.downloadUrl(path, { user, expiresIn, filename, requestTimeoutMs })`, `sandbox.uploadUrl(path, { user, useSignatureExpiration })` and `sandbox.downloadUrl(...)` returning the URL string, and classes `UploadTicket`/`DownloadLink` whose `toString()` is the URL and whose `toJSON()` throws; `@aws-sdk/client-s3`, `@aws-sdk/s3-request-presigner`, `@aws-sdk/s3-presigned-post` and `@aws-sdk/lib-storage` SHALL be dependencies loaded with dynamic `import()` only when a transfer runs.

#### Scenario: parity lists
- **WHEN** the parity unit test compares the public names of the sync and async trees
- **THEN** the transfer names are present in both with the same parameters

#### Scenario: S3 packages load lazily
- **WHEN** a TypeScript unit test imports `rayito` and creates a sandbox against the fake without calling a transfer
- **THEN** no `@aws-sdk/client-s3` module was loaded

### Requirement: Real-AWS acceptance of file transfers
`clients/python/tests/e2e/test_m9_transfer.py` SHALL pass against the M9 `rayito-base` image with no execution role and the bucket and prefix from `RAYITO_E2E_TRANSFER_BUCKET` / `RAYITO_E2E_TRANSFER_PREFIX`, covering the 13 tests of design D24: upload PUT + `wait()` with sha256, uid 1000 and the staging object gone; the barrier for `files.read` and `commands.run` without `wait()`; a 50 MB `download_url` read without headers, a 206 Range response and the snapshot kept after an overwrite; 200 MB `files.write`/`files.read` through S3 at ≥ 10× the gRPC baseline of the same run, with no `StartImport` when staging is unset; a 100 MB multipart export at a 64 MiB threshold with no multipart upload left; expiry of an unused ticket in about 5 s as `TimeoutException` and a 403 `Request has expired` for a 1 s link fetched after 3 s; `max_bytes` refusal with nothing written and the object deleted, and a POST form refused by S3 with `EntityTooLarge` above 1 024 bytes while 512 bytes import; `INVALID_ARGUMENT` for every SSRF URL with no socket observed; a ticket completing after `pause()`, a PUT while suspended and `connect()`; gzip at ≥ 3× the uncompressed baseline; metadata round trip, clearing and refusal; the E2B shim sync and async; log hygiene with the logs-only role. `clients/typescript/tests/e2e/m9-transfer.e2e.test.ts` SHALL mirror the upload/download, large-file and gzip cases with `fetch`. Every run SHALL leave zero MicroVMs, zero objects and zero multipart uploads under the prefix, and its numbers SHALL be recorded in `AWS_API_NOTES.md` §16 with placeholders.

#### Scenario: acceptance run
- **WHEN** the e2e suites run with `RAYITO_E2E=1` and the transfer bucket variables set
- **THEN** every test passes, the reported import and export MB/s are recorded, and the teardown finds nothing left under `<prefix>/<sandbox_id>/`
