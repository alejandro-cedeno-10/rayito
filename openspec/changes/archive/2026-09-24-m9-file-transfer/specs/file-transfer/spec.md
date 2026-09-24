## ADDED Requirements

### Requirement: FilesystemService exposes the transfer RPCs
`proto/rayito/v1/filesystem.proto` SHALL add to `FilesystemService` `rpc StartImport(StartImportRequest) returns (StartTransferResponse)`, `rpc StartExport(StartExportRequest) returns (StartTransferResponse)`, `rpc GetTransfer(GetTransferRequest) returns (TransferState)`, `rpc WatchTransfer(WatchTransferRequest) returns (stream TransferEvent)` and `rpc CancelTransfer(CancelTransferRequest) returns (CancelTransferResponse)`, with the messages `S3Object{bucket, key, region}`, `PresignedRequest{url, map headers}`, `PresignedMultipart{part_size, repeated parts}`, `StartImportRequest{path, optional user, optional mode, object, get, optional delete, wait_for_object, expires_at_unix_ms, max_bytes, expected_sha256, map metadata = 11}`, `StartExportRequest{path, optional user, object, oneof target {put, multipart}, expires_at_unix_ms}`, `StartTransferResponse{transfer_id}`, `GetTransferRequest`, `WatchTransferRequest`, `CancelTransferRequest` (each `{transfer_id}`), `CancelTransferResponse{}`, the enums `TransferDirection` and `TransferPhase` (`WAITING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`), `TransferState{transfer_id, direction, phase, bytes_done, bytes_total, probes, optional entry, sha256, repeated part_etags, duration_ms, optional error}` and `TransferEvent{oneof {state, keepalive}}`, exactly as design D1. `common.proto`'s closed `StreamError` list SHALL gain `resource_exhausted`, `failed_precondition`, `unavailable` and `cancelled`. The edits SHALL be applied by the M9 Contract agent only; `buf lint` SHALL pass with the existing `buf.yaml` exceptions, `buf breaking` against the pre-change proto SHALL report nothing, and the Rust, Python and TypeScript code SHALL be regenerated, never hand-written. All five RPCs SHALL require `x-access-token`.

#### Scenario: additive proto
- **WHEN** `buf lint` and `buf breaking --against <pre-change proto>` run on the edited files
- **THEN** both exit 0 and the regenerated Python and TypeScript `FilesystemService` expose the five new methods, with `WatchTransfer` server-streaming

#### Scenario: token required
- **WHEN** a client calls `GetTransfer` without `x-access-token`
- **THEN** the RPC fails with `UNAUTHENTICATED` like every RPC but `Health`

### Requirement: rayd holds no credentials and refuses any URL outside the named object before network I/O
`rayd` SHALL NOT read, derive or store AWS credentials for transfers and SHALL validate every `StartImport`/`StartExport` with a pure policy in `rayd-core` before any DNS lookup or connection, answering `INVALID_ARGUMENT` with a fixed message that contains no URL, host, bucket, key or path when: the bucket is not `^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$` (dots refused); the region does not match `^[a-z]{2}(-gov)?-[a-z]+-[0-9]$`; the key has a leading `/`, `//`, a `.`/`..` segment or NUL, or does not end with `/<this sandbox's id>/up/<32 lowercase hex>` for an import or `/<this sandbox's id>/down/<32 lowercase hex>` for an export; a URL exceeds 8 192 bytes, is not `https`, carries userinfo, an explicit port other than 443, or an IP-literal host; the host is not exactly `{bucket}.s3.{region}.amazonaws.com`, `{bucket}.s3.dualstack.{region}.amazonaws.com`, `{bucket}.s3-fips.{region}.amazonaws.com` or `{bucket}.s3-fips.dualstack.{region}.amazonaws.com`; the percent-decoded path differs from `/` + key; the query lacks exactly one `X-Amz-Algorithm=AWS4-HMAC-SHA256` and one each of `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires`, `X-Amz-SignedHeaders`, `X-Amz-Signature`, or carries a SigV2 `AWSAccessKeyId`/`Signature`; the URLs of one request address different hosts or paths; an `UploadPart` URL's `partNumber` differs from its position or the `uploadId`s differ; `PresignedRequest.headers` holds anything but `content-type: application/octet-stream`; `expires_at_unix_ms` is in the past or later than now + 604 800 s + 300 s; `mode` exceeds `0o7777`; `expected_sha256` is not empty or 64 lowercase hex; a multipart `part_size` is outside 5 MiB–5 GiB or it has 0 or more than 1 000 parts. Before the `/run` hook every transfer RPC SHALL fail with `FAILED_PRECONDITION`.

#### Scenario: SSRF attempts rejected without a connection
- **WHEN** the integration test sends `StartImport` requests whose `get` URL uses host `169.254.169.254`, the literal `10.0.0.5`, `http://`, the global `amzn-s3-demo-bucket.s3.amazonaws.com`, a different bucket, a different key, userinfo, or port 8443
- **THEN** each fails with `INVALID_ARGUMENT`, no transfer id is created and the fake `SignedHttp` recorded zero calls

#### Scenario: regional virtual-hosted URL accepted
- **WHEN** `StartImport` carries a SigV4 URL on `amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com` whose path is `/<prefix>/<sandbox id>/up/<32 hex>` and whose key and region match `S3Object`
- **THEN** the call returns a `transfer_id`

#### Scenario: URL bound to its sandbox
- **WHEN** the key ends with another sandbox's id or with `/down/` on an import
- **THEN** the call fails with `INVALID_ARGUMENT`

### Requirement: The HTTPS client refuses internal addresses, redirects and extra headers
The `SignedHttp` adapter SHALL be built on `hyper-util`'s legacy client and `hyper-rustls` (exact-pinned direct dependencies already in `Cargo.lock`, `aws-lc-rs` provider named explicitly, native roots, HTTP/1.1 only) and SHALL: resolve names through a filter that drops loopback, `0.0.0.0/8`, `169.254.0.0/16`, `fe80::/10`, `fd00:ec2::254`, unspecified, multicast, broadcast and IPv4-mapped forms of those while keeping RFC 1918 and other `fc00::/7` addresses, and connect only to the addresses that filter returned; never follow a redirect; fail after 5 s without a TCP connection and after 30 s without a byte sent or received; send only `host`, `user-agent: rayd/<version>`, and for `PUT` `content-length` and `content-type: application/octet-stream`; hold the URL in zeroizing memory. Error values SHALL carry a kind and never a URL, host or address.

#### Scenario: a name resolving to loopback is refused
- **WHEN** the production adapter is asked to GET a URL whose host resolves only to `127.0.0.1`
- **THEN** it fails with `ForbiddenAddress` and no socket was opened

#### Scenario: redirect not followed
- **WHEN** the local TLS test server answers `302` with a `Location` header
- **THEN** the adapter returns a `Redirect` error and makes no second request

#### Scenario: exact headers on a PUT
- **WHEN** the adapter PUTs 3 MiB to the local TLS test server
- **THEN** the server received exactly the headers `host`, `user-agent`, `content-length: 3145728` and `content-type: application/octet-stream`

### Requirement: Import arms, polls and writes through the filesystem write path
`StartImport` SHALL first apply the phase gate, the URL policy, the path rules, identity resolution with the positive identity check and the deny list of the canonical deepest existing ancestor (unary status on failure, nothing created), refuse an existing directory at the destination with `INVALID_ARGUMENT`, register the transfer and return its id. With `wait_for_object` the task SHALL GET the presigned URL at once, then 1 s after each attempt during the first 600 s and 5 s afterwards, until the wall clock reaches `expires_at_unix_ms` (then `FAILED` `deadline_exceeded`); without it, one GET (network failures retried twice). `404 NoSuchKey`, `403 AccessDenied` other than `Request has expired`, 5xx and connection failures SHALL count as pending while waiting; `403 Request has expired` and `400 ExpiredToken` SHALL end in `deadline_exceeded`; signature errors, redirects, `NoSuchBucket` and filtered addresses SHALL end in `invalid_argument`. On `200` the transfer SHALL take one of two byte permits (dropping the unread response and staying `WAITING` while none is free), move to `RUNNING`, and before writing any byte refuse a missing `Content-Length` or one above a non-zero `max_bytes` (`invalid_argument`, reasons `no_content_length`/`too_large`) and one that would leave less than the 256 MiB disk reserve free (`resource_exhausted`, `disk_reserve`). Bytes SHALL go through the same `WriteSink` as `Write` (user's filesystem identity, `.rayito-tmp-*` temp in the destination directory, parents `0o755`, `mode` default `0o644`, `fsync`, `fchmod`, `fchown`, rename, directory `fsync`), with sha256 computed on the way; `ENOSPC` SHALL end in `resource_exhausted` `disk_full`; a non-empty `expected_sha256` that differs SHALL end in `failed_precondition` `checksum_mismatch`; any failure SHALL remove the temp file and leave the destination untouched; metadata SHALL be applied before the rename. A connection or body failure while `RUNNING` SHALL requeue the transfer to `WAITING`, and the third such failure SHALL end in `unavailable`.

#### Scenario: armed ticket imports when the object appears
- **WHEN** the fake S3 answers `404 NoSuchKey` three times and then `200` with 1 MiB
- **THEN** `probes` is 4, the file exists with the user's uid, mode `0o644` and the object's bytes, and the terminal state is `DONE` with the sha256 of those bytes

#### Scenario: too large is refused before writing
- **WHEN** `max_bytes` is 1 048 576 and the object's `Content-Length` is 2 097 152
- **THEN** the state is `FAILED` with code `invalid_argument` and a message starting `too_large`, no `.rayito-tmp-*` file was created and the destination does not exist

#### Scenario: expiry without an object
- **WHEN** an armed import's `expires_at_unix_ms` passes while S3 keeps answering 404
- **THEN** the state becomes `FAILED` with code `deadline_exceeded` and the poller stops

### Requirement: Imports are single-use and clean up their staging object
An import SHALL end at its first terminal state and never import a second object from the same URL. After `DONE`, `too_large`, `no_content_length` and `checksum_mismatch`, `rayd` SHALL send the presigned `DELETE` once (one retry on 5xx) when one was given; the DELETE's outcome SHALL be logged and SHALL NOT change the transfer's result.

#### Scenario: staging object deleted after import
- **WHEN** an import completes against the fake S3
- **THEN** the fake received exactly one `DELETE` on the staging key after the `GET`

#### Scenario: second PUT ignored
- **WHEN** a second object is uploaded to the same key after the import finished
- **THEN** the fake received no further `GET` for that transfer and the file keeps the first content

### Requirement: Export sends a snapshot of a regular file opened as the user
`StartExport` SHALL apply the phase gate, the URL policy, the path rules, the identity check and the deny list, then open the path under the user's filesystem identity with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK`, accept only a regular file (`NOT_FOUND` when missing, `INVALID_ARGUMENT` for a symlink, directory or special file, `PERMISSION_DENIED` on `EACCES`) and `fstat` the descriptor. A single `put` SHALL require a size of at most 5 GiB (`INVALID_ARGUMENT` otherwise); a `multipart` target SHALL have exactly `max(1, ceil(size / part_size))` parts or fail with `FAILED_PRECONDITION` (the file changed since the client's `Stat`). The transfer SHALL wait for a byte permit, then send exactly `size` bytes read from the kept descriptor (`pread` in 1 MiB chunks), part by part in order, each `PUT` with its exact `Content-Length`, retrying a part twice on 5xx or a connection failure; an early EOF SHALL end in `failed_precondition` `file_shrank`; `403 Request has expired`/`400 ExpiredToken` in `deadline_exceeded`; other 403 in `permission_denied`. `DONE` SHALL carry the sha256 of the bytes sent in order (a retried part never hashed twice), `bytes_done = bytes_total = size`, the source `entry`, and for multipart the parts' `ETag`s in part order exactly as returned. `rayd` SHALL NOT complete or abort multipart uploads.

#### Scenario: multipart export returns ETags in order
- **WHEN** a 20 MiB file is exported with `part_size` 8 MiB and three part URLs
- **THEN** the fake S3 received three PUTs of 8 MiB, 8 MiB and 4 MiB with `partNumber` 1, 2, 3 and the `DONE` state lists their three ETags in that order with the file's sha256

#### Scenario: file shrinking mid-read aborts
- **WHEN** the file is truncated while its export is running
- **THEN** the state is `FAILED` with code `failed_precondition` and a message starting `file_shrank`

#### Scenario: symlink refused at the start
- **WHEN** `StartExport` names a symlink
- **THEN** the RPC fails with `INVALID_ARGUMENT` and no transfer is registered

### Requirement: The transfer registry is bounded, retained and observable
Transfer ids SHALL be 32 lowercase hex. At most 16 transfers SHALL be `WAITING` or `RUNNING` per sandbox (`RESOURCE_EXHAUSTED` beyond), at most 2 `RUNNING` at once; finished transfers SHALL be retained until 30 minutes after they finished or until more than 64 finished ones exist (oldest evicted first). `GetTransfer`, `WatchTransfer` and `CancelTransfer` SHALL answer `NOT_FOUND` for an unknown, evicted or empty id. `WatchTransfer` SHALL send the current state first, then a full state on every phase change and on progress at most once per second, `KeepAlive` after 30 s of silence, and end with status OK right after a terminal state; it SHALL be closed like every server-stream on `/suspend`. `CancelTransfer` SHALL move a `WAITING` or `RUNNING` transfer to `CANCELLED` (removing any temp file) and SHALL be a no-op on a finished one. `StartImport`, `StartExport` and `WatchTransfer` SHALL answer `UNAVAILABLE` with details `suspending` or `terminating` during those phases.

#### Scenario: seventeenth transfer refused
- **WHEN** sixteen armed imports are waiting and a seventeenth `StartImport` arrives
- **THEN** it fails with `RESOURCE_EXHAUSTED` and the sixteen keep waiting

#### Scenario: capability probe
- **WHEN** a client calls `GetTransfer` with an empty `transfer_id`
- **THEN** the RPC fails with `NOT_FOUND`

#### Scenario: watch ends after the terminal state
- **WHEN** a client watches an import that completes
- **THEN** it receives a `WAITING` state first, later a `DONE` state, and then the stream ends with status OK

### Requirement: Reads and new workloads wait for armed uploads that already landed
Before `Read` and `Stat` whose canonical target equals an armed ticket's destination, `ListDir` whose canonical root is a component-wise ancestor of one, and before `Process.Start`, `Code.Execute` and `Pty.Create` for every armed ticket, `rayd` SHALL make each selected `WAITING` ticket poll at once and wait up to 2 s in total for those polls; a ticket whose object was found, and any selected `RUNNING` ticket, SHALL be waited for until its terminal state (bounded only by the RPC's own deadline and cancellation); pending or unanswered probes SHALL let the RPC proceed (fail open), and a failed import SHALL NOT fail the RPC. When no ticket is armed the barrier SHALL cost one atomic load and no network call. `Write`, `WatchDir`, `Move`, `Remove` and `MakeDir` SHALL NOT wait.

#### Scenario: read right after the PUT
- **WHEN** an armed ticket's object appears in the fake S3 and `Read` of its path arrives before the next scheduled poll
- **THEN** the `Read` returns the new content

#### Scenario: process start waits for the import
- **WHEN** an object for an armed ticket exists and `Process.Start` runs `sha256sum` on the path
- **THEN** the command prints the hash of the imported bytes

#### Scenario: nothing armed
- **WHEN** no ticket is armed and `Stat` is called
- **THEN** the fake `SignedHttp` records no call

### Requirement: Suspend requeues moving transfers and still answers 200
On the first `/suspend` of a cycle `rayd` SHALL cancel every `RUNNING` transfer (removing import temp files), return it to `WAITING` without counting an attempt, stop every poller until `/resume`, wait at most 1 s for the tasks to acknowledge and answer `/suspend` with 200 in every case. On `/resume` armed pollers SHALL poll at once and waiting exports SHALL restart from offset 0 on their kept descriptor. On `/terminate` every unfinished transfer SHALL become `CANCELLED` without a DELETE. Expiry SHALL keep counting in wall-clock time across a suspension.

#### Scenario: import interrupted by a suspend completes after resume
- **WHEN** an import is `RUNNING` when `/suspend` is posted, and `/resume` is posted later
- **THEN** `/suspend` answered 200, no `.rayito-tmp-*` file remained while suspended, the transfer went back to `WAITING`, and after `/resume` it reached `DONE` with the full content

### Requirement: Transfer failures carry a code and a reason, never a name
Unary transfer RPCs SHALL use standard gRPC statuses (`INVALID_ARGUMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `RESOURCE_EXHAUSTED`, `FAILED_PRECONDITION`, `UNAVAILABLE` with the phase details). A failed or cancelled transfer SHALL carry `TransferState.error` with `code` one of `deadline_exceeded`, `not_found`, `permission_denied`, `invalid_argument`, `resource_exhausted`, `failed_precondition`, `unavailable`, `cancelled`, `internal` and `message` of the form `<reason>: <fixed Spanish sentence>` with `<reason>` one of `expired`, `no_object`, `access_denied`, `signature_rejected`, `wrong_region`, `bucket_missing`, `s3_unavailable`, `forbidden_address`, `unexpected_response`, `no_content_length`, `too_large`, `disk_reserve`, `disk_full`, `checksum_mismatch`, `file_shrank`, `cancelled`. No status message or `StreamError` message SHALL contain a URL, host, bucket, key, path or metadata.

#### Scenario: messages are name-free
- **WHEN** the unit test renders every transfer error variant
- **THEN** no message contains `/`, `http`, `amazonaws` or the test bucket name, and each `StreamError` message starts with its reason token

### Requirement: Transfer logging hygiene
`rayd` transfer log lines SHALL carry only `transfer_id`, `direction`, `phase`, `bytes`, `duration_ms`, `http_status`, `s3_error_code`, `s3_request_id`, `attempt`, `probes`, `reason` and `outcome`, and barrier lines only `rpc`, `tickets`, `waited_ms` and `outcome`; they SHALL never contain a URL, host, bucket, key, path, header value, metadata key or value.

#### Scenario: no signature in the captured logs
- **WHEN** the integration test runs an import and an export against the fake S3 with the log capture installed
- **THEN** the captured text contains neither `X-Amz-Signature`, `X-Amz-Credential`, the bucket, the key nor the path

### Requirement: The caller policy grants the transfer prefix and the execution role gets nothing
`spike/m0/iam.yaml` SHALL add the parameters `TransferBucket` (default empty) and `TransferPrefix` (default `rayito-transfer`, the same pattern as `PersistencePrefix`), a `Rules` assertion that `TransferPrefix` is neither `rayito` nor equal to `PersistencePrefix` when the bucket is set, and, only when `TransferBucket` is not empty, two `CallerPolicy` statements: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` on `arn:aws:s3:::<TransferBucket>/<TransferPrefix>/*`, and `s3:ListBucket` on `arn:aws:s3:::<TransferBucket>` with `StringLike s3:prefix <TransferPrefix>/*`. The `ExecutionRole` SHALL gain no statement. `infra/README.md` SHALL document, with placeholders only, a lifecycle rule on the prefix with `Expiration` of 1 day and `AbortIncompleteMultipartUpload` of 1 day, a bucket policy denying requests whose `s3:signatureversion` is not `AWS4-HMAC-SHA256` and requests without TLS, and CORS for browser use.

#### Scenario: template tests
- **WHEN** `scripts/tests/test_iam_template.py` and `uvx cfn-lint==1.56.3 spike/m0/iam.yaml` run
- **THEN** the default transfer prefix is disjoint from `rayito` and from the default persistence prefix, both transfer statements are conditional on the bucket, the execution role has no transfer statement, and cfn-lint reports nothing

### Requirement: The transfer contract is written down before code
Before any production code, `AWS_API_NOTES.md` SHALL gain §18 with every S3 call and parameter the SDKs and `rayd` use for transfers, each with its official source (the JavaScript names verified against the AWS SDK for JavaScript v3 documentation, the Python names against the botocore model), and the §16 rows for 403 vs 404 with and without `s3:ListBucket`, PUT-to-visible latency, the JavaScript default checksum against real S3 and `user.*` xattr support on the guest measured; the rows for import/export throughput, gzip through the proxy and the binary size delta SHALL be filled by the acceptance run. `ARCHITECTURE.md` SHALL gain ADR-010 and `SECURITY.md` threat T16 (presigned URLs as bearer credentials and `rayd`'s SSRF guard). Every value SHALL use placeholders (`123456789012`, `amzn-s3-demo-bucket`, `<tu-perfil>`, `microvm-<id>`), and `python scripts/check_hygiene.py` SHALL exit 0.

#### Scenario: hygiene of the new ground truth
- **WHEN** `python scripts/check_hygiene.py` runs after §18, the new §16 rows, ADR-010 and T16 are written
- **THEN** it exits 0
