## Why

E2B's `sbx.upload_url()` / `sbx.download_url()` hand a program a plain URL
that any HTTP client can `PUT` to or `GET` from. Rayito raises
`UnimplementedError` for both today (`docs/site/docs/e2b-compat.md`), and
the reason is structural, not a missing feature: the Lambda MicroVM proxy
accepts the JWE **only** in the `X-aws-proxy-auth` header or the WebSocket
subprotocol (`AWS_API_NOTES.md` §7), so a bare URL can never reach `rayd`.
The only URL a third party can use without headers is one that points
somewhere else. The evidence says where:

- `AWS_API_NOTES.md` §16 **Q59** (measured 2026-09-22, `rayito-base` 20.0):
  from inside the VM, a caller-presigned S3 `PUT` of 100 MB runs at
  **55.7 / 106.2 MB/s** and a `GET` at **99.0 / 84.2 MB/s**, while the same
  VM through the proxy gives `files.write` **0.68 MB/s** and `files.read`
  **4.76 MB/s** (Q32: the proxy never grows its 64 KiB HTTP/2 upload window).
  Moving bytes VM↔S3 is 80–150× faster than the gRPC path.
- **Q62**: with its default `Config`, botocore presigns **SigV2** on the
  global host in us-east-1 and accepts `ExpiresIn` 604 801, which S3 then
  rejects; SSO role credentials cap any URL at 8 h. The presigner must be
  forced.
- The S3 path needs **no execution role**: the SDK presigns with the
  caller's own credentials and `rayd` only follows URLs whose host and path
  match the named object, so T1 and the deferred audit row C-07 are
  untouched.

The same S3 path also fixes the slowest thing in the SDK: large
`files.write`/`files.read` through the proxy. With the plumbing in place the
change also closes the remaining E2B filesystem gaps that are cheap next to
it: gzip transfer, file metadata (`WriteInfo.metadata`/`EntryInfo.metadata`,
E2B 2.28.0), `files.read(stream_idle_timeout=)`, `files.write(use_octet_stream=)`
and JavaScript `files.read(format="blob")`.

This is change 1 of 6 of M9 ("paridad con E2B"), parallel with
`m9-deno-kernels`, before `m9-server-timeout`, `m9-sandbox-observability`,
`m9-egress-policy` and `m9-e2b-v2-surface`.

## What Changes

Every decision is closed in `design.md`. In short:

- **ADR-010 "Transferencias S3 prefirmadas con las credenciales del
  llamante; rayd no guarda credenciales".** The SDK presigns every S3
  request (SigV4, virtual-hosted, regional endpoint, `ExpiresIn` ≤
  `min(expires_in, max_expires_in, 604800)`); `rayd` receives URLs per
  request, never keys, checks each against a pure URL policy and moves the
  bytes with a new credential-free HTTPS client.
- **Proto** (applied by the M9 Contract agent from design D1, not here):
  `FilesystemService` gains `StartImport`, `StartExport`, `GetTransfer`,
  `WatchTransfer` (server-stream) and `CancelTransfer`, with `S3Object`,
  `PresignedRequest`, `PresignedMultipart`, `TransferState`,
  `TransferEvent`, `TransferDirection`, `TransferPhase`; `EntryInfo` gains
  `metadata = 11`, `WriteRequest` gains `metadata = 5`,
  `StartImportRequest` carries `metadata = 11`; the closed `StreamError`
  list gains `resource_exhausted`, `failed_precondition`, `unavailable`,
  `cancelled`. All additive, `buf breaking` FILE-clean.
- **`rayd`**: a `transfer` domain in `rayd-core` (URL policy, poll
  schedule, S3 XML error parser, registry with its bounds, import and
  export plans, read-after-upload barrier) over one new port `SignedHttp`;
  adapter `HyperSignedHttp` on `hyper-util` + `hyper-rustls` (already in
  `Cargo.lock`, now exact-pinned direct dependencies) with a resolver that
  refuses loopback, link-local, IMDS, unspecified and multicast addresses,
  no redirects, 5 s connect and 30 s idle timeouts. Imports go through the
  existing `WriteSink` path (identity policy C-05, `FsIdentityGuard`, temp +
  fsync + rename + fchown); exports read as the user with `O_NOFOLLOW`
  (T11). `/suspend` requeues transfers that move bytes and still answers
  200. gzip through tonic's `gzip` feature with a tower layer that makes
  response compression opt-in (`rayito-compress: gzip`). Metadata as
  `user.rayito.<key>` xattrs set on the temp file's descriptor before the
  rename, read back with `llistxattr`/`lgetxattr`.
- **SDK (Python sync + async over `_transfer_base.py`, TypeScript
  camelCase)**: `transfer=S3Staging(bucket, prefix="rayito-transfer",
  region=None, max_expires_in=86400, threshold_bytes=8 MiB,
  multipart_threshold_bytes=5 GiB)` or `RAYITO_TRANSFER_BUCKET` /
  `RAYITO_TRANSFER_PREFIX` / `RAYITO_TRANSFER_REGION`;
  `files.upload_url(...) -> UploadTicket` (a `str`, like `HostAccess`, with
  `headers`, `fields`, `wait()`, `status()`, `cancel()`);
  `files.download_url(...) -> DownloadLink`; `Sandbox.upload_url` /
  `download_url` with E2B's signature on the native `Sandbox` and in
  `rayito.e2b`; automatic S3 routing of `files.write`/`write_files`/`read`
  at or above `threshold_bytes` when staging is configured; `gzip=`,
  `metadata=`, `stream_idle_timeout=`, `use_octet_stream=` and JS
  `format: "blob"`; new `TransferException(code)` and
  `FileUploadException` (re-exported by `m9-e2b-v2-surface`).
- **Infra and docs**: `spike/m0/iam.yaml` `CallerPolicy` gains optional
  `TransferBucket`/`TransferPrefix` (objects + prefix-scoped
  `s3:ListBucket`, prefix disjoint from artifacts and persistence);
  `AWS_API_NOTES.md` §18 (every presign parameter with its source) and new
  §16 rows; `SECURITY.md` T16; `ARCHITECTURE.md` ADR-010;
  `docs/site/docs/files.md` (new) and `e2b-compat.md` divergences;
  operator guidance for lifecycle rules and a bucket policy denying SigV2
  and plain HTTP. The execution role needs nothing.

## Capabilities

### New Capabilities
- `file-transfer`: the transfer RPCs, the URL policy, the credential-free
  HTTPS adapter and its resolver filter, import (arm-and-poll) and export
  semantics, the registry bounds and retention, the read-after-upload
  barrier, suspend interplay, status and `StreamError` mapping, logging
  hygiene, the caller IAM statements and the operator guidance.
- `sdk-file-transfer`: `S3Staging`, the presigner rules, `upload_url`,
  `download_url`, `UploadTicket`/`DownloadLink`/`TransferStatus`, the
  large-file routing, the capability probe for older images, the exception
  mapping, sync/async parity, the TypeScript twins and the real-AWS
  acceptance.

### Modified Capabilities
- `filesystem`: `Write` carries per-file metadata stored as xattrs, `Stat`
  and `ListDir` report it, gzip message compression with a response opt-in,
  and the SDK filesystem surface gains `gzip`, `metadata`,
  `stream_idle_timeout`, `use_octet_stream` and the S3 routing.
- `e2b-compat`: `upload_url`/`download_url` leave the `UnimplementedError`
  list and map to the native transfer surface; the shim's `files.write` /
  `files.read` accept E2B 2.x's `gzip`, `metadata`, `use_octet_stream` and
  `stream_idle_timeout`.
- `typescript-sdk`: the filesystem surface (`format: "blob"`, `gzip`,
  `metadata`, `streamIdleTimeoutMs`, `useOctetStream`) and the transport
  rule (a lazily created gzip transport that reuses the unary HTTP/2
  session, so still two sessions at most).

## Impact

- New: `crates/rayd-core/src/transfer/` (`mod.rs`, `url_policy.rs`,
  `poll.rs`, `s3_error.rs`, `registry.rs`, `import.rs`, `export.rs`,
  `barrier.rs`, `ports.rs`, `error.rs`, `fake.rs`),
  `crates/rayd-core/src/filesystem/metadata.rs`,
  `crates/rayd/src/adapters/signed_http.rs`,
  `crates/rayd/src/transfer/` (`mod.rs`, `manager.rs`, `import.rs`,
  `export.rs`, `barrier.rs`), `crates/rayd/src/grpc/transfer.rs`,
  `crates/rayd/src/grpc/compression.rs`, `crates/rayd/tests/m9_transfer.rs`,
  `clients/python/src/rayito/_transfer_base.py`,
  `clients/python/src/rayito/sandbox_{sync,async}/transfer.py`,
  `clients/python/tests/unit/{fake_transfer.py,fake_s3.py,test_transfer_base.py,test_transfer_sync.py,test_transfer_async.py}`,
  `clients/python/tests/e2e/test_m9_transfer.py`,
  `clients/typescript/src/sandbox/transfer.ts`,
  `clients/typescript/tests/unit/transfer.test.ts`,
  `clients/typescript/tests/e2e/m9-transfer.e2e.test.ts`,
  `docs/site/docs/files.md`.
- Edited (production, by the implementer): `Cargo.toml`, `Cargo.lock`,
  `crates/rayd/Cargo.toml`, `crates/rayd/src/{main.rs,grpc/mod.rs,grpc/filesystem.rs,grpc/process.rs,grpc/code.rs,grpc/pty.rs,adapters/std_filesystem.rs,adapters/mod.rs,filesystem/write.rs,lifecycle/suspend.rs}`,
  `crates/rayd-core/src/{lib.rs,filesystem/*.rs}`, `limits.json` and the
  generated `_limits.py`/`limits.ts`, the Python `_models.py`,
  `exceptions.py`, `__init__.py`, `_filesystem_base.py`,
  `_transport.py`, `sandbox_{sync,async}/{main,filesystem}.py`,
  `e2b/{_sync,_async,_compat}.py`, the TypeScript `index.ts`, `errors.ts`,
  `models.ts`, `sandbox/{sandbox,core,filesystem,launch}.ts`,
  `transport/transport.ts`, `package.json` + `pnpm-lock.yaml`.
- Edited (proto, by the Contract agent only): `proto/rayito/v1/common.proto`,
  `proto/rayito/v1/filesystem.proto` and every generated client.
- Docs/infra: `spike/m0/iam.yaml`, `scripts/tests/test_iam_template.py`,
  `infra/README.md`, `AWS_API_NOTES.md` (§18, §16 rows), `ARCHITECTURE.md`
  (FilesystemService row, hexagonal tables, ADR-010), `SECURITY.md` (T16,
  logging paragraph), `docs/site/docs/{files,e2b-compat,security,limits}.md`,
  `docs/site/mkdocs.yml`, `README.md`, three `CHANGELOG.md`.
- Behaviour: nothing changes for a sandbox whose SDK has no staging
  configured and never passes `gzip`/`metadata`: the gRPC paths, deadlines
  and channels stay byte-for-byte as in 0.2.0, and grpcio/Connect clients
  that already advertise `grpc-accept-encoding: gzip` keep receiving
  identity responses. Against an image older than M9 every new surface
  fails with `UnimplementedError("actualiza la imagen")` before any bytes
  move (capability probe, design D8).
- Cost: presign is local; S3 requests at the caller's rates (an armed
  ticket polls ≈ 1 200 GETs per hour); staging objects live until the
  import deletes them or the operator's 1-day lifecycle rule; the acceptance
  run ≈ $0.10 of compute plus < 1 GB of S3 traffic for minutes.
