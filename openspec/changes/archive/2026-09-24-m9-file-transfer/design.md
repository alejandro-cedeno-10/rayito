## Context

State on 2026-09-22 (M0–M8 accepted and archived, release 0.2.0 Apache-2.0,
branch `feature/m9-e2b-parity-file-urls` on the single sanitized root commit;
`AWS_API_NOTES.md` carries the uncommitted Q58–Q62 rows of the M9
measurement agent):

- **What exists.** `FilesystemService` = `Read` (256 KiB chunks,
  `O_NOFOLLOW`, regular files only), `Write` (client-stream, per-file temp +
  fsync + fchmod + fchown + rename, 256 MiB disk reserve, `disk_reserve` /
  `disk_full` details), `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`,
  `WatchDir`, `Checkpoint`, `Restore` (ADR-009). Every syscall runs under the
  user's filesystem identity (`FsIdentityGuard`), the canonical-path deny
  list applies, identities pass the positive check of C-05
  (`authorize_identity`: uid ≥ 1000, gid ≥ 1000, not in group 0). Every
  server-stream is wrapped in `SuspendableStream` (closed `UNAVAILABLE
  suspending` on `/suspend`) and emits `KeepAlive` after 30 s of silence.
  `rayd-core::filesystem` owns `RequestPath`, `DenyList`, `FsIdentity`, the
  `FileSystem`/`WriteSink`/`Watcher`/`NameResolver` ports; `rayd` has
  `StdFileSystem`. `rayd-core::persistence` shows the house style for async
  ports: native `async fn`/`impl Future` in traits, generics in `rayd`, never
  `dyn`, no executor type crossing.
- **The only TLS client** is `aws-sdk-s3` (ADR-009): `rustls` 0.23.45 on
  `aws-lc-rs` 1.18.1, `hyper` 1.11.1, `hyper-util` 0.1.20 (features
  `client-legacy`, `http1`, `http2`, `tokio`), `hyper-rustls` 0.27.9
  (`http1`, `http2`, `native-tokio`, `tls12`), `rustls-native-certs` 0.8.4,
  `tokio-rustls` 0.26.5, all already in `Cargo.lock` through
  `aws-smithy-http-client` 1.4.2. `flate2` 1.1.10 (`rust_backend`) is pinned.
  tonic is `=0.14.6` with `codegen`, `router`, `server`; its `gzip` feature
  only adds the optional `flate2` dependency.
- **tonic 0.14.6 compression facts** (source read in the cargo registry):
  `Server::accept_compressed`/`send_compressed` per generated service;
  `Response::disable_compression()` only affects unary and client-stream
  responses ("Response streams … will still be compressed according to the
  configuration of the server", `src/response.rs:119-127`,
  `src/server/grpc.rs:295`). The server compresses a response when the
  request's `grpc-accept-encoding` lists an encoding it was told to send.
  grpcio and Connect-ES both advertise `gzip` in `grpc-accept-encoding` by
  default, so enabling `send_compressed` alone would silently gzip every
  response to every existing client.
- **Measured facts this design rests on** (`AWS_API_NOTES.md`): §7 the proxy
  accepts the JWE only as `X-aws-proxy-auth` or the WebSocket subprotocol;
  §7/Q32 HTTP/2 uploads through the proxy are window-bound (≈ 0.65 MB/s);
  §7 idle counts only endpoint bytes (outbound polling never keeps a VM
  awake); §15 AWS kills every outbound connection on run and resume and
  corrects the wall clock; Q53 `rayd`→S3 31–35 MB/s up / 78–97 MB/s down;
  **Q59** caller-presigned `PUT`/`GET` from inside the VM 55.7–106.2 MB/s /
  84.2–99.0 MB/s, `urllib` stores `application/x-www-form-urlencoded` unless
  `Content-Type` is explicit, an SSO-signed SigV4 URL is 1 583 characters,
  `curl` 8.17.0 is in the image; **Q62** default botocore presigns SigV2 on
  `s3.amazonaws.com` in us-east-1, S3 refuses `X-Amz-Expires` ≥ 604 800 +1
  with `400 AuthorizationQueryParametersError`, SSO role credentials expire
  8 h after login; **Q58** any uid 1000 process reaches the hooks on
  loopback (T2 residual, decided by `m9-server-timeout`, untouched here).
- **Offline verification done while writing this design** (botocore/boto3
  1.43.94 from `clients/python`'s venv): the S3 model operations and input
  members used in D5/D20 exist with these exact names —
  `PutObject{Bucket, Key, Body, ContentLength, ContentType}`,
  `GetObject{Bucket, Key, Range, ResponseContentDisposition}`,
  `DeleteObject{Bucket, Key}`, `UploadPart{Bucket, Key, UploadId,
  PartNumber, Body, ContentLength}`, `CreateMultipartUpload{Bucket, Key,
  ContentType}`, `CompleteMultipartUpload{Bucket, Key, UploadId,
  MultipartUpload}`, `AbortMultipartUpload{Bucket, Key, UploadId}`,
  `HeadObject{Bucket, Key}`, `ListMultipartUploads{Bucket, Prefix}`;
  signatures `generate_presigned_url(ClientMethod, Params=None,
  ExpiresIn=3600, HttpMethod=None)`, `generate_presigned_post(Bucket, Key,
  Fields=None, Conditions=None, ExpiresIn=3600)`, `upload_fileobj(Fileobj,
  Bucket, Key, ExtraArgs=None, Callback=None, Config=None)`,
  `download_fileobj(Bucket, Key, Fileobj, ExtraArgs=None, Callback=None,
  Config=None)`, `TransferConfig(multipart_threshold, max_concurrency,
  multipart_chunksize, …)`; `botocore.config.Config` documents
  `addressing_style`, `us_east_1_regional_endpoint` and
  `request_checksum_calculation`. The JavaScript names of D19 are **not**
  verified yet: task 0.1 verifies them against the package docs before they
  enter §18 (hard rule 1).
- **Current SDK shape.** Python `Sandbox.create(...)` already takes
  `persist=`, `pool=`, `session=`, `region=`; `_filesystem_base.py` holds the
  pure helpers (`file_request_deadline`, `coerce_data`,
  `build_write_requests`, `entry_info_from_proto`, …); `translate_rpc_error`
  / `translate_stream_error` live in `_transport.py`. `HostAccess(str)` is
  the precedent for a `str` subclass that carries attributes.
  `rayito.e2b.UnimplementedError` exists only in the shim. TypeScript
  mirrors the Python tree; a `Sandbox` owns two `Http2SessionManager`s
  (`unary`, `stream`); `createGrpcTransport` accepts `sessionManager`,
  `sendCompression`, `acceptCompression` and `compressMinBytes`
  (`@connectrpc/connect-node` 2.2.0, read in `node_modules`).
- **E2B reference** (clone at `ccaf9fc`, digest `e2b-inventory`): E2B signs
  `v1_` + sha256 over the envd token into a query string on
  `https://<host>/files`; upload is multipart `POST` (field `file`) or a raw
  octet-stream body answered synchronously; download is the live file with
  Range; URLs are reusable and never expire unless
  `use_signature_expiration` is set; metadata lives in `user.e2b.<key>`
  xattrs, keys lowercased, an overwrite replaces the whole set.

Coordination with the other M9 changes (`m9-server-timeout`,
`m9-deno-kernels`, `m9-egress-policy`, `m9-sandbox-observability`,
`m9-e2b-v2-surface`), all written in parallel from the same Scope decision:
this change owns **ADR-010**, **`SECURITY.md` T16**, **`AWS_API_NOTES.md`
§18**, `EntryInfo.metadata = 11`, `WriteRequest.metadata = 5`, the five
transfer RPCs and the `StreamError` codes `resource_exhausted`,
`failed_precondition`, `unavailable`, `cancelled` (`m9-server-timeout` adds
`sandbox_timeout` to the same comment: whoever lands second merges the
list). Its §16 rows are written as **Q70–Q75**, a block left clear on
purpose because the sibling drafts already claim Q63–Q65
(`m9-server-timeout`) and Q63 (`m9-deno-kernels`); `m9-egress-policy` and
`m9-sandbox-observability` may add rows too, so task 0.0 still takes the
next free numbers at implementation time and records the mapping (the M6/M7
rule). `m9-e2b-v2-surface` re-exports
`FileUploadException` from `rayito.e2b` and builds the TypeScript
`rayito/e2b` shim on top of the native `uploadUrl`/`downloadUrl` defined
here. `m9-egress-policy` promises that `rayd`'s own (root) traffic is never
filtered, so transfers keep working under its policies. Shared MODIFIED
requirements: `e2b-compat` "E2B features without an AWS primitive raise
UnimplementedError" is also modified by `m9-server-timeout` (drops
`set_timeout`), `m9-deno-kernels` (languages), `m9-sandbox-observability`
(metrics range, `next_token`) and `m9-e2b-v2-surface`; `typescript-sdk`
"Filesystem surface" may be touched by `m9-e2b-v2-surface`. This delta is
written against the archived text of 2026-09-22; whoever archives second
rebases its MODIFIED text on the archived one (the M7 rule), keeping this
change's sentences: `upload_url`/`download_url` out of the list and the
re-export of the native `UnimplementedError`.

Constraints: `openspec/project.md` hard rules (no invented AWS parameters:
§18 is written and every JavaScript name verified **before** code; the
`.proto` is the source of truth and only the Contract agent edits it; ARM64
musl; hexagonal boundaries — `rayd-core` never sees `hyper`, `tokio`,
`tonic`; a port trait only with its adapter; identifiers in English, Spanish
only in user-facing strings and docs; no inline comments in function
bodies; `thiserror` in the domain, `anyhow` only in `main`; no
`unwrap`/`expect`/`panic` outside tests; sync and async Python over shared
pure helpers; TypeScript strict; the acceptance runs on real AWS).

## Goals / Non-Goals

**Goals:**
- E2B's `upload_url`/`download_url` as URLs any HTTP client can use without
  headers, with the only design the platform allows, written up as ADR-010.
- Large `files.write`/`files.read` at S3 speed when the developer configures
  a transfer bucket, with no change at all when they do not.
- gzip, file metadata, `stream_idle_timeout`, `use_octet_stream` and JS
  `format: "blob"`, so the E2B 2.x filesystem surface is complete.
- No credential inside the VM for any of it (T1, C-07 untouched), no new
  SSRF surface (`rayd` is root and reaches IMDS), no regression of the
  deferred audit rows (C-01, C-02, C-03, C-05 uidrange, C-06, C-07, C-08,
  C-09, C-10, C-12).

**Non-Goals:**
- An HTTP listener in `rayd` for `/files` (a T2-class surface, not needed
  for parity: E2B's URLs exist to avoid headers, and ours already do).
- Reusable upload URLs (tickets are single-use, D9), live-file downloads
  (download is a snapshot, D10), URLs without expiry (S3 caps at 7 days).
- A bucket created by the SDK or by `iam.yaml` (the account's SCP denies
  `s3:CreateBucket`; the operator owns the bucket, D20).
- `get_signature`, E2B's client-side envd signature (impossible: the proxy
  never reads it; mapped to `UnimplementedError` by `m9-e2b-v2-surface`).
- The TypeScript `rayito/e2b` entry point, `write_files` in the shim,
  `FileUploadException` in `rayito.e2b.__all__` (all `m9-e2b-v2-surface`).
- Filtering or accounting of VM→S3 egress (T8 and `m9-egress-policy`).

## Decisions

### D1. Proto delta (exact text for the Contract agent)

> **Status: applied by the Contract step (2026-09-22), verbatim.** Final numbers: `EntryInfo.metadata = 11`, `WriteRequest.metadata = 5`; `FilesystemService` `StartImport`, `StartExport`, `GetTransfer`, `WatchTransfer`, `CancelTransfer` after `Restore`; new messages `S3Object` (1-3), `PresignedRequest` (1-2), `PresignedMultipart` (1-2), `StartImportRequest` (1-11), `StartExportRequest` (1-6, `oneof target` 4/5), `StartTransferResponse` (1), `GetTransferRequest` (1), `WatchTransferRequest` (1), `CancelTransferRequest` (1), `CancelTransferResponse`, `TransferState` (1-11), `TransferEvent` (`oneof event` 1/2), enums `TransferDirection` (0-2) and `TransferPhase` (0-5). The `StreamError` comment lists the four new codes plus `sandbox_timeout`. Compile stubs to replace: the five transfer RPCs of `FilesystemGrpc` (`crates/rayd/src/grpc/filesystem.rs`) answer `UNIMPLEMENTED`, `to_entry_info` sets an empty `metadata`, and `write_message` ignores `WriteRequest.metadata`. Regenerated with `buf generate` (Python and TypeScript, byte-identical to the committed gencode for untouched protos) and `crates/rayito-proto/build.rs` (`PROTO_FILES` now lists `common`, `lifecycle`, `network`, `health`, `process`, `filesystem`, `pty`, `code`). `buf lint` passes with the unchanged `buf.yaml`, and `buf breaking --against` a copy of the pre-M9 `proto/` (FILE) reports nothing. Do not regenerate Python with `python scripts/gen_python.py`: grpcio-tools 1.84.0 emits protobuf 7.35.1 gencode plus a gRPC version-check preamble, which differs from the committed 7.36.1 output of `buf generate`.

All edits are additive; `buf lint` passes with the existing `buf.yaml`
exceptions (`RPC_REQUEST_RESPONSE_UNIQUE`, `RPC_REQUEST_STANDARD_NAME`,
`RPC_RESPONSE_STANDARD_NAME` cover the shared `StartTransferResponse` and
`TransferState` as a response); `buf breaking --against <pre-change copy>`
(FILE) reports nothing. Comments are Spanish, like the rest of the proto.

`proto/rayito/v1/common.proto`:

```proto
// Códigos cerrados: "not_found", "permission_denied", "invalid_argument",
// "unimplemented", "deadline_exceeded", "output_truncated", "suspending",
// "resumed_state_lost", "kernel_died", "internal", "resource_exhausted",
// "failed_precondition", "unavailable", "cancelled". Un cliente trata
// cualquier código desconocido como "internal".
message StreamError { … unchanged fields … }

message EntryInfo {
  … fields 1-10 unchanged …
  // Metadatos del fichero: xattrs `user.rayito.<clave>` con la clave en
  // minúsculas; vacío si no hay o si el sistema de ficheros no los admite.
  map<string, string> metadata = 11;
}
```

`proto/rayito/v1/filesystem.proto` — `WriteRequest` gains:

```proto
  // Sólo en el primer mensaje de cada fichero (el que lleva `path`):
  // sustituye el conjunto entero de metadatos del fichero. Claves: caracteres
  // token de HTTP, se guardan en minúsculas; valores: ASCII imprimible; como
  // máximo 64 claves y 4000 bytes contando `user.rayito.` + clave + valor.
  map<string, string> metadata = 5;
```

`FilesystemService` gains, after `Restore`:

```proto
  // Transferencias por URLs prefirmadas de S3 (ADR-010). rayd no guarda
  // credenciales: el SDK firma cada petición con las credenciales del
  // llamante y rayd sólo llama a URLs cuyo host y ruta coinciden con el
  // S3Object de la petición (INVALID_ARGUMENT antes de cualquier I/O de red).
  // Como mucho 16 transferencias esperando o en curso por sandbox
  // (RESOURCE_EXHAUSTED); NOT_FOUND para un transfer_id desconocido.
  //
  // Importa un objeto a `path`: con `wait_for_object` sondea `get` hasta que
  // el objeto exista o venza `expires_at_unix_ms`; sin él lo pide una vez.
  rpc StartImport(StartImportRequest) returns (StartTransferResponse);
  // Exporta `path` tal como está ahora (se abre y se mide al aceptar la
  // petición): un PUT o las partes de una subida multiparte.
  rpc StartExport(StartExportRequest) returns (StartTransferResponse);
  rpc GetTransfer(GetTransferRequest) returns (TransferState);
  // Primero el estado actual; después una foto completa en cada cambio;
  // termina tras DONE, FAILED o CANCELLED.
  rpc WatchTransfer(WatchTransferRequest) returns (stream TransferEvent);
  rpc CancelTransfer(CancelTransferRequest) returns (CancelTransferResponse);
```

New messages (appended at the end of `filesystem.proto`):

```proto
message S3Object {
  // Nombre DNS sin puntos.
  string bucket = 1;
  string key = 2;
  string region = 3;
}

message PresignedRequest {
  // Nunca se registra en logs.
  string url = 1;
  // Cabeceras firmadas que rayd envía tal cual; hoy sólo `content-type`.
  map<string, string> headers = 2;
}

message PresignedMultipart {
  uint64 part_size = 1;
  // Una URL de UploadPart por parte, en orden (PartNumber 1..N).
  repeated PresignedRequest parts = 2;
}

message StartImportRequest {
  string path = 1;
  optional User user = 2;
  optional uint32 mode = 3;
  S3Object object = 4;
  PresignedRequest get = 5;
  optional PresignedRequest delete = 6;
  bool wait_for_object = 7;
  int64 expires_at_unix_ms = 8;
  // 0 = sin tope propio (sigue valiendo la reserva de disco).
  uint64 max_bytes = 9;
  // Hex en minúsculas; vacío = no se comprueba.
  string expected_sha256 = 10;
  // Mismas reglas que WriteRequest.metadata.
  map<string, string> metadata = 11;
}

message StartExportRequest {
  string path = 1;
  optional User user = 2;
  S3Object object = 3;
  oneof target {
    PresignedRequest put = 4;
    PresignedMultipart multipart = 5;
  }
  int64 expires_at_unix_ms = 6;
}

message StartTransferResponse {
  string transfer_id = 1;
}

message GetTransferRequest {
  string transfer_id = 1;
}

message WatchTransferRequest {
  string transfer_id = 1;
}

message CancelTransferRequest {
  string transfer_id = 1;
}

message CancelTransferResponse {}

enum TransferDirection {
  TRANSFER_DIRECTION_UNSPECIFIED = 0;
  TRANSFER_DIRECTION_IMPORT = 1;
  TRANSFER_DIRECTION_EXPORT = 2;
}

enum TransferPhase {
  TRANSFER_PHASE_UNSPECIFIED = 0;
  TRANSFER_PHASE_WAITING = 1;
  TRANSFER_PHASE_RUNNING = 2;
  TRANSFER_PHASE_DONE = 3;
  TRANSFER_PHASE_FAILED = 4;
  TRANSFER_PHASE_CANCELLED = 5;
}

message TransferState {
  string transfer_id = 1;
  TransferDirection direction = 2;
  TransferPhase phase = 3;
  uint64 bytes_done = 4;
  uint64 bytes_total = 5;
  // Sondeos del GET hechos por una importación.
  uint32 probes = 6;
  // Importación: el fichero escrito; exportación: el origen al aceptarla.
  optional EntryInfo entry = 7;
  // Hex del sha256 de los bytes transferidos (en DONE).
  string sha256 = 8;
  // ETag de cada parte, en orden (exportación multiparte en DONE).
  repeated string part_etags = 9;
  uint32 duration_ms = 10;
  optional StreamError error = 11;
}

message TransferEvent {
  oneof event {
    TransferState state = 1;
    KeepAlive keepalive = 2;
  }
}
```

Two deltas over the Scope's list, both required by rules above it:
(1) **`StartImportRequest.metadata = 11`**: without it `files.write(metadata=)`
of a payload routed through S3 (D15) would drop the metadata silently, which
hard rule 7 forbids; (2) nothing else. `gzip` needs no field (D16).

### D2. ADR-010 and why the design is forced

`ARCHITECTURE.md` gains **ADR-010 — Transferencias S3 prefirmadas con las
credenciales del llamante; rayd no guarda credenciales**, after ADR-009:

- **Contexto.** E2B's URLs carry their auth in the query string; the MicroVM
  proxy reads auth only from a header or a WebSocket subprotocol (§7), so no
  URL can reach `rayd`. The fastest byte path measured is VM↔S3 (Q59:
  55.7–106.2 MB/s up, 84.2–99.0 MB/s down vs 0.68 / 4.77 MB/s through the
  proxy).
- **Decisión.** The SDK presigns with the caller's credentials; `rayd`
  receives URLs per request (never in `envs`, argv, `runHookPayload` or
  logs), validates each against the named object and moves the bytes with a
  credential-free HTTPS client. Upload = arm-and-poll import through the
  existing write path; download = export of a snapshot; large files route
  through the same machinery.
- **Alternativas descartadas.** (a) `rayd` using the execution role for S3
  (ADR-009's path): needs a role on every sandbox, re-opens C-07 (the
  object is not bound to the sandbox) and T1 on the default image; (b) a
  `/files` HTTP listener in `rayd`: reachable only with the JWE header, so
  it does not deliver headerless URLs and adds a T2-class surface; (c) the
  sandbox user running `curl` with the URL: the URL would sit in argv and
  `/proc`, visible to user code, and the import would run with no size,
  disk-reserve or identity policy.
- **Consecuencias.** A transfer bucket is required; URLs are bearer tokens
  (T16); uploads land asynchronously (barrier D11, `ticket.wait()`); tickets
  are single-use; downloads are snapshots; expiry is always set; the binary
  grows only by the adapter code (the TLS stack is already linked; the
  delta is measured, Q75).

### D3. Configuration: `S3Staging`

Python (`rayito._models`, exported from `rayito`), frozen dataclass,
validated in `__post_init__` from `_limits.py`:

```python
@dataclass(frozen=True)
class S3Staging:
    bucket: str
    prefix: str = TRANSFER_DEFAULT_PREFIX            # "rayito-transfer"
    region: str | None = None                        # None = the sandbox region
    max_expires_in: int = TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS   # 86400
    threshold_bytes: int = TRANSFER_DEFAULT_THRESHOLD_BYTES         # 8 MiB
    multipart_threshold_bytes: int = TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES  # 5 GiB

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> S3Staging | None: ...
```

Rules (each an `InvalidArgumentException` naming the field, never its
value): `bucket` matches `^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`, does not start
with `xn--` and does not end with `-s3alias` (DNS-compatible, **no dots**:
virtual-hosted TLS wildcard certificates never match a dotted bucket);
`prefix` is 1–256 bytes of `[A-Za-z0-9_.-]` segments joined by `/`, with no
leading or trailing `/`, no empty, `.` or `..` segment, and is
**component-disjoint** from the artifact namespace `rayito` (refused when
`prefix == "rayito"`, `prefix` starts with `rayito/`, or `rayito` starts
with `prefix + "/"`); `region`, when set, matches
`^[a-z]{2}(-gov)?-[a-z]+-[0-9]$`; `1 <= max_expires_in <= 604800`;
`1 MiB <= threshold_bytes <= 5 GiB`; `16 MiB <= multipart_threshold_bytes <=
5 GiB` (the lower bound exists so the e2e can lower it to 64 MiB).

`from_env` reads `RAYITO_TRANSFER_BUCKET` (absent or empty → `None`),
`RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`; the other fields keep
their defaults. There is **no hardcoded bucket** anywhere.

`Sandbox.create(..., transfer: S3Staging | None = None)` and
`Sandbox.connect(..., transfer=None)` (sync and async): `None` →
`S3Staging.from_env()`; the resolved value is exposed as the read-only
property `sbx.transfer: S3Staging | None`. When `persist` is also given and
`persist.bucket == transfer.bucket`, the two key prefixes SHALL be
component-disjoint (the lifecycle rule of D20 would otherwise expire
checkpoints) or `create`/`connect` raises `InvalidArgumentException` before
any AWS call. `create(pool=...)` accepts `transfer` (it is client-side
configuration; nothing is sent to the VM). The region used to presign is
`transfer.region or sandbox.region`; it MUST be the bucket's region — a
wrong region gives an S3 301 that `rayd` classifies as `wrong_region` (D9).

TypeScript: `interface S3Staging { bucket; prefix?; region?;
maxExpiresIn?; thresholdBytes?; multipartThresholdBytes? }` validated by
`resolveS3Staging(input | undefined, env = process.env)`;
`Sandbox.create({ transfer })`, `Sandbox.connect(id, { transfer })`,
`sandbox.transfer` getter. Same rules, same env vars.

### D4. Staging keys and their binding to the sandbox

`key = f"{prefix}/{sandbox_id}/{direction}/{token}"` with `direction` ∈
{`up`, `down`} and `token = secrets.token_hex(16)` (TS:
`randomBytes(16).toString("hex")`), 32 lowercase hex. The user's path never
enters S3 (not in the key, not in object metadata). `rayd` refuses (D6) any
key that does not end with `/<its own sandbox id>/up/<32 hex>` for an import
or `/<its own sandbox id>/down/<32 hex>` for an export, where the sandbox id
is the `microvmId` of the `/run` payload; before `/run` every transfer RPC
answers `FAILED_PRECONDITION`. This binds each URL to one sandbox and one
direction at no cost: a leaked URL cannot be replayed into another sandbox
even by someone holding that sandbox's access token.

### D5. Presigner rules and URL lifetimes

Python presigning client (built once per `Sandbox` from the sandbox's boto3
session, in `_transfer_base.presign_client_config()`):

```python
botocore.config.Config(
    signature_version="s3v4",
    s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
)
```

TypeScript presigning client: `new S3Client({ region, credentials,
requestChecksumCalculation: "WHEN_REQUIRED" })` with `getSignedUrl`
(`@aws-sdk/s3-request-presigner`) and `createPresignedPost`
(`@aws-sdk/s3-presigned-post`); the default `S3Client` (virtual-hosted,
regional, SigV4) needs nothing else. A separate data client (no
`requestChecksumCalculation` override) serves the SDK's own direct transfers
(D15).

Every presigned request and its lifetime (`e = min(expires_in,
max_expires_in, 604800)`; `B = 900 + ceil(size / 1 000 000)` s, the export
budget at a pessimistic 1 MB/s; every value finally capped at 604 800):

| URL | Operation / params | Signed for | Lifetime |
|---|---|---|---|
| user PUT (`upload_url`) | `put_object` {`Bucket`, `Key`} | the caller | `e` |
| user POST (`upload_url(form=True)`) | `generate_presigned_post(Bucket, Key, Fields=None, Conditions=[["content-length-range", 0, max_bytes or 5 368 709 120]])` | the caller | `e` |
| rayd GET (import poll) | `get_object` {`Bucket`, `Key`} | `rayd` | `e` |
| rayd DELETE (import cleanup) | `delete_object` {`Bucket`, `Key`} | `rayd` | `min(e + 3600, 604800)` |
| rayd GET (large write, no wait) | `get_object` {`Bucket`, `Key`} | `rayd` | `min(900, max_expires_in)` |
| rayd DELETE (large write) | `delete_object` {`Bucket`, `Key`} | `rayd` | `min(B, 604800)` |
| rayd PUT (export) | `put_object` {`Bucket`, `Key`} | `rayd` | `min(B, 604800)` |
| rayd UploadPart (export) | `upload_part` {`Bucket`, `Key`, `UploadId`, `PartNumber`} | `rayd` | `min(B, 604800)` |
| user GET (`download_url`) | `get_object` {`Bucket`, `Key`, `ResponseContentDisposition`} | the caller | `e` |

No `ContentType`, checksum or `ContentLength` is signed into any URL: a
signed header would force every E2B-style `requests.put(url, data=f)` to
send it or fail with `SignatureDoesNotMatch`. `Content-Type:
application/octet-stream` is instead (a) returned in `UploadTicket.headers`
for the uploader, (b) sent unsigned by `rayd` on every export PUT, so a
download serves octet-stream, and (c) passed as `ExtraArgs={"ContentType":
…}` / `params.ContentType` on the SDK's own direct uploads (Q59).
`ResponseContentDisposition` is `attachment; filename="<ascii>";
filename*=UTF-8''<pct>` where `<ascii>` is the basename (or `filename=`)
with every byte outside `0x20–0x7E` and every `"`/`\` replaced by `_`, and
`<pct>` is its RFC 3986 percent-encoding.

`expires_in <= 0` raises `InvalidArgumentException`; values above the cap
are clamped silently and the effective expiry is reported as `expires_at`
(aware UTC `datetime` / `Date`). The docs state the real ceiling is also the
caller's credential expiry (8 h with SSO in Q62); the SDK does not read
private botocore attributes to guess it — `rayd` classifies the resulting
`ExpiredToken` as `deadline_exceeded` (D9).

URLs are passed per request inside `StartImportRequest`/`StartExportRequest`
only; the SDK never logs them, never puts them in `repr`, exceptions, envs,
argv, `runHookPayload` or metadata.

### D6. `rayd-core::transfer::url_policy` (pure)

`validate_request(object: &S3ObjectSpec, urls: &[RequestUrl], sandbox_id,
direction) -> Result<(), UrlPolicyError>`, where `S3ObjectSpec` is parsed
from `S3Object`, runs before any network I/O and answers
`INVALID_ARGUMENT` with a fixed, value-free message per rule:

1. `bucket` matches the D3 bucket rule (no dots); `region` matches
   `^[a-z]{2}(-gov)?-[a-z]+-[0-9]$`; `key` is 1–1024 bytes, no NUL, no
   leading `/`, no `//`, no `.`/`..` segment, and ends with
   `/<sandbox_id>/<up|down>/<32 lowercase hex>` for the request's direction
   (D4).
2. Each URL is ≤ 8 192 bytes and parses with `http::Uri`: scheme `https`;
   no userinfo (`@` in the authority is refused); no explicit port other
   than `443`; host is not an IPv4 or IPv6 literal.
3. The host equals, byte for byte and lowercase, one of
   `{bucket}.s3.{region}.amazonaws.com`,
   `{bucket}.s3.dualstack.{region}.amazonaws.com`,
   `{bucket}.s3-fips.{region}.amazonaws.com`,
   `{bucket}.s3-fips.dualstack.{region}.amazonaws.com`. The global
   `{bucket}.s3.amazonaws.com`, path-style and accelerate hosts are refused.
4. The percent-decoded path equals `/` + `key` exactly (a decoded NUL or an
   invalid escape is refused).
5. The query contains exactly one `X-Amz-Algorithm=AWS4-HMAC-SHA256` and one
   each of `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires`,
   `X-Amz-SignedHeaders`, `X-Amz-Signature`, and contains neither
   `AWSAccessKeyId` nor `Signature` (SigV2). `UploadPart` URLs also carry
   `partNumber=<i>` equal to their 1-based position and one `uploadId`
   shared by every part.
6. Every URL of the request (get + delete, put, or all parts) addresses the
   same host and path.
7. `PresignedRequest.headers` is empty or holds only `content-type` (any
   case) with the value `application/octet-stream`.

Plus the address predicate used by the adapter's resolver (D7):
`is_forbidden_address(IpAddr) -> bool` = loopback (`127.0.0.0/8`, `::1`),
`0.0.0.0/8`, link-local (`169.254.0.0/16`, `fe80::/10`), the IPv6 IMDS
address `fd00:ec2::254`, unspecified (`0.0.0.0`, `::`), multicast
(`224.0.0.0/4`, `ff00::/8`), broadcast `255.255.255.255`, and any
IPv4-mapped or IPv4-compatible IPv6 address whose embedded IPv4 is one of
those. RFC 1918 and `fc00::/7` (other than `fd00:ec2::254`) stay allowed so
S3 interface endpoints behind a VPC egress connector keep working.

`StartImportRequest`/`StartExportRequest` also get the non-URL checks
before any I/O: `expires_at_unix_ms` in the future and at most
`now + 604 800 s + 300 s` (clock skew), `max_bytes` any value, `mode` ≤
`0o7777`, `expected_sha256` empty or 64 lowercase hex, `metadata` per D17,
multipart `part_size` in `[5 MiB, 5 GiB]`, `1 <= parts.len() <= 1000`.

### D7. Port `SignedHttp` and adapter `HyperSignedHttp`

Port (`rayd-core/src/transfer/ports.rs`), native async in traits like the
persistence ports, wired as a generic in `rayd`:

```rust
pub struct SignedRequest { pub method: HttpMethod, pub url: Zeroizing<String>,
    pub content_type: Option<&'static str>, pub content_length: Option<u64> }
pub enum HttpMethod { Get, Put, Delete }
pub struct HttpHead { pub status: u16, pub content_length: Option<u64>,
    pub etag: Option<String>, pub request_id: Option<String> }
pub trait ResponseBody: Send {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send;
}
pub trait RequestBody: Send + 'static {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, HttpError>> + Send;
}
pub trait SignedHttp: Send + Sync + 'static {
    type Body: ResponseBody;
    fn send<B: RequestBody>(&self, request: SignedRequest, body: Option<B>)
        -> impl Future<Output = Result<(HttpHead, Self::Body), HttpError>> + Send;
}
pub enum HttpErrorKind { ForbiddenAddress, Connect, Tls, Timeout, Redirect, Io }
```

`HttpError{kind}` carries no URL, host or address. The adapter
(`crates/rayd/src/adapters/signed_http.rs`):

- `hyper_util::client::legacy::Client` over
  `hyper_rustls::HttpsConnector<HttpConnector<FilteringResolver>>`, built
  with `HttpsConnectorBuilder::new().with_provider_and_native_roots(
  Arc::new(rustls::crypto::aws_lc_rs::default_provider()))?.https_only()
  .enable_http1().wrap_connector(http)`; HTTP/1.1 only; the provider is
  explicit so no process-wide default is installed and the S3 SDK's client
  is unaffected. Pool: 30 s idle, 2 idle connections per host.
- `HttpConnector::new_with_resolver(FilteringResolver(GaiResolver::new()))`,
  `enforce_http(false)`, `set_connect_timeout(Some(5 s))`,
  `set_nodelay(true)`. `FilteringResolver` drops every resolved address for
  which `is_forbidden_address` is true and fails with `ForbiddenAddress`
  when none is left; the connector connects only to the addresses the
  resolver returned, so there is no check-then-connect gap.
- **No redirects**: the legacy client never follows them; any 3xx is
  returned as `HttpErrorKind::Redirect` (the domain maps 301/307 bodies that
  parse as S3 errors to `wrong_region`, D9).
- **Idle timeout 30 s**: one shared last-activity timestamp updated on every
  request-body frame written and every response frame read; a request fails
  with `Timeout` after 30 s without activity in either direction (so a long
  PUT is never cut by a total-duration timer).
- Headers sent: `host` (from the URL), `user-agent: rayd/<version>`,
  `content-length` for PUT, `content-type: application/octet-stream` for
  PUT. Nothing else, ever.
- The URL is held in `Zeroizing<String>` (`zeroize` is already a workspace
  dependency) from the gRPC message until the request is built; the
  `http::Uri` built from it is dropped right after the request is sent.
- Dependencies (workspace, exact pins, all already in `Cargo.lock`):
  `hyper-util = { version = "=0.1.20", default-features = false, features =
  ["client-legacy", "http1", "tokio"] }`, `hyper-rustls = { version =
  "=0.27.9", default-features = false, features = ["http1", "native-tokio",
  "tls12", "aws-lc-rs"] }`, `rustls = { version = "=0.23.45",
  default-features = false, features = ["aws_lc_rs", "std", "tls12"] }`
  (direct only to name the provider), and `hyper` gains the `client` and
  `http1` features. `cargo deny check` stays four `ok` with no allowlist
  change; `scripts/check_auditable.py` passes.

`rayd-core/src/transfer/s3_error.rs` parses at most the first 4 096 bytes of
an error body: the text of the first `<Code>`, `<Message>` and `<RequestId>`
elements (entities `&amp; &lt; &gt; &quot; &apos;` decoded, anything longer
than 256 bytes truncated), falling back to the `x-amz-request-id` header for
the request id. No XML crate.

### D8. Registry, phases, capability probe and `WatchTransfer`

`rayd-core/src/transfer/registry.rs` — `TransferRegistry` (pure, owned
behind a `Mutex` in `rayd`):

- `TransferId`: 32 lowercase hex from the existing random port.
- Record: id, direction, `armed` (import with `wait_for_object`), phase,
  bytes_done, bytes_total, probes, entry, sha256, part_etags, the normalised
  request path and the canonical destination (for the barrier, never
  logged), `started_at`/`finished_at` (monotonic), error.
- Phases: `Waiting → Running → Done | Failed`, `Waiting | Running →
  Cancelled`, `Running → Waiting` (suspend requeue, D12). Any other
  transition is a domain error (logged, state unchanged).
- Bounds: **16** records in `Waiting` or `Running` (`admit` fails with
  `RegistryError::Full` → `RESOURCE_EXHAUSTED` "demasiadas transferencias
  activas"); **2** in `Running` at once (the adapter holds a
  `tokio::sync::Semaphore` of 2 permits; the domain asserts the count);
  finished records kept until **30 min** after `finished_at` or until more
  than **64** finished records exist (oldest evicted first), evicted on
  every `admit` and every lookup.
- `GetTransfer`/`WatchTransfer`/`CancelTransfer` with an unknown or evicted
  id → `NOT_FOUND`. **`GetTransfer{transfer_id: ""}` → `NOT_FOUND`** always:
  this is the SDK's capability probe (an older `rayd` answers
  `UNIMPLEMENTED`).
- `CancelTransfer` on `Waiting`/`Running` → `Cancelled` (running task
  cancelled, temp file removed; an import whose object was already observed
  runs its DELETE once, best effort); on a finished record → OK, no change
  (idempotent).
- `WatchTransfer`: first message = the current snapshot; then a full
  snapshot on every phase change and on progress (bytes_done changed),
  sampled at most once per second; `KeepAlive` after 30 s of silence; the
  stream ends OK right after the terminal snapshot. Wrapped in
  `SuspendableStream` like every server-stream. Fed from one
  `tokio::sync::watch` channel per record.
- Phase gate: `StartImport`, `StartExport` and `WatchTransfer` answer
  `UNAVAILABLE` with details `suspending`/`terminating` during those
  phases, like `Read`/`Write`; `GetTransfer` and `CancelTransfer` are not
  gated. All five require `x-access-token` (the existing layer).

### D9. Import (arm-and-poll)

`StartImport`, synchronously, in this order (unary status on failure, no
record created, no network I/O): phase gate; `/run` seen; URL policy (D6);
non-URL checks (D6); `RequestPath` parse; identity resolution + C-05
(`authorize_identity`); deny list on the canonical deepest existing
ancestor (as `Write`); an existing final component that is a directory →
`INVALID_ARGUMENT`; `admit`. Then it returns the id and a task runs:

- **Poll schedule** (`transfer/poll.rs`, pure): the first GET immediately,
  then 1 s after the end of each attempt while fewer than 600 s have passed
  since the request was admitted, 5 s afterwards; the task stops when the
  wall clock (the `Clock` port, corrected by AWS on resume) reaches
  `expires_at_unix_ms` → `Failed deadline_exceeded` reason `expired`.
  `wait_for_object = false` → one GET only (network failures retried twice
  1 s apart).
- **Response classification** (`transfer/import.rs`, pure, inputs: status,
  parsed S3 error, `wait_for_object`):

| Response | Waiting (armed) | No wait |
|---|---|---|
| 200 | found → admit checks | found → admit checks |
| 404 `NoSuchKey` | pending | `not_found` reason `no_object` |
| 403 `AccessDenied` + Message `Request has expired` | `deadline_exceeded` reason `expired` | same |
| 400 `ExpiredToken` | `deadline_exceeded` reason `expired` | same |
| 403 `AccessDenied` (other) | pending (no `s3:ListBucket`: a missing key is 403) | `permission_denied` reason `access_denied` |
| 403 `SignatureDoesNotMatch`, 400 `AuthorizationQueryParametersError` | `invalid_argument` reason `signature_rejected` | same |
| 301/307 or `PermanentRedirect`/`TemporaryRedirect` | `invalid_argument` reason `wrong_region` | same |
| 404 `NoSuchBucket` | `invalid_argument` reason `bucket_missing` | same |
| 5xx, `SlowDown`, connect/TLS/timeout error | pending | retried twice, then `unavailable` reason `s3_unavailable` |
| `ForbiddenAddress` | `invalid_argument` reason `forbidden_address` | same |
| anything else | `internal` reason `unexpected_response` | same |

- **Found**: `probes` counts every GET. The task tries to take a byte
  permit; if none is free it drops the response without reading the body
  and waits for a permit (phase stays `Waiting`), then re-GETs. With the
  permit: phase `Running`, then the **admit checks** before any byte is
  written (`ImportPlan::admit`, pure): no `Content-Length` →
  `invalid_argument` reason `no_content_length`; `max_bytes > 0 &&
  content_length > max_bytes` → `invalid_argument` reason `too_large`;
  `free_bytes(dir) < DISK_RESERVE_BYTES + content_length` →
  `resource_exhausted` reason `disk_reserve`.
- **Write**: `FileSystem::begin_write(id, dir, mode.unwrap_or(0o644))` (the
  same `WriteSink` as `Write`: `FsIdentityGuard`, parents `0o755`,
  `.rayito-tmp-<random>`), chunks written as they arrive, sha256 over the
  bytes, `ENOSPC` → `resource_exhausted` reason `disk_full`; a body shorter
  or longer than `Content-Length` → requeue (below). After the body:
  `expected_sha256` set and different → `failed_precondition` reason
  `checksum_mismatch` (sink dropped, temp removed); metadata applied to the
  sink (D17); `commit(final_name, id)` → `Done` with `entry`, `sha256`,
  `bytes_done = bytes_total = content_length`.
- **Requeue on interruption**: a connection or body failure while `Running`
  drops the sink (temp removed) and goes back to `Waiting` with an attempt
  counter; the third failure ends in `unavailable` reason
  `s3_unavailable`. `/suspend` requeues without counting (D12).
- **Cleanup**: after `Done`, `too_large`, `no_content_length` and
  `checksum_mismatch` (every outcome where an object was observed), the
  presigned DELETE runs once (one retry on 5xx); its outcome is logged
  (`phase="cleanup"`, `http_status`) and never changes the transfer's
  result. A DELETE after the ticket expired fails with 403 and the object is
  left to the lifecycle rule (D20); this is documented.
- Single use: the task ends at its first terminal state; a second PUT to the
  same URL is never imported.

### D10. Export

`StartExport`, synchronously (unary status on failure): phase gate; `/run`
seen; URL policy; non-URL checks; path parse; identity + C-05; deny list on
the canonical path; `FileSystem::open_snapshot(id, path)` — a new port
method that opens `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK` under
the user's identity, accepts regular files only, and returns a
`SnapshotFile` (`read_at(&self, buf, offset)` = `pread`) plus the `fstat`
entry — missing → `NOT_FOUND`, symlink/dir/other → `INVALID_ARGUMENT`,
`EACCES` → `PERMISSION_DENIED`; then the plan checks
(`transfer/export.rs`, pure) against `size = fstat.st_size`: a single `put`
requires `size <= 5 GiB` (else `INVALID_ARGUMENT` reason
`too_large_for_put`); a `multipart` requires `parts.len() ==
max(1, ceil(size / part_size))` (else `FAILED_PRECONDITION` reason
`file_changed`: the file changed since the SDK's `Stat`); `admit`. The
record's `entry` is the fstat entry and `bytes_total = size`.

The task waits for a byte permit (`Waiting`), then `Running`: parts are sent
sequentially, each `PUT` with `Content-Length` = its length and
`Content-Type: application/octet-stream`, the body read with `read_at` in
1 MiB chunks on a blocking thread through a bounded channel (4 chunks); a
read returning EOF before `size` bytes → `failed_precondition` reason
`file_shrank` (growth beyond `size` is ignored: exactly `size` bytes are
sent). sha256 is computed over the whole file in order: the running hasher
is cloned at each part's start and the clone is kept only when that part's
PUT succeeds, so a retried part never hashes twice. Each part (or the single
PUT) is retried twice on 5xx or a connection error, re-reading from its
offset. S3 answers: 200 with `ETag` → next part; 403 `Request has expired`
or 400 `ExpiredToken` → `deadline_exceeded` reason `expired`; other 403 →
`permission_denied` reason `access_denied`; 5xx after retries →
`unavailable` reason `s3_unavailable`; redirect → `invalid_argument` reason
`wrong_region`. `Done` carries `sha256`, `bytes_done = size` and, for
multipart, `part_etags` in part order exactly as S3 returned them
(quotes included). The SDK completes or aborts the multipart upload (D14);
`rayd` never holds a multipart credential.

### D11. Read-after-upload barrier

So that E2B's "PUT, then read or run" is deterministic without `wait()`:

- Scope: armed tickets (imports with `wait_for_object`) in `Waiting` or
  `Running`.
- Triggers: `Read` and `Stat` whose canonical target equals a ticket's
  canonical destination (or whose normalised request path equals the
  ticket's); `ListDir` whose canonical root is a component-wise ancestor of
  a ticket's destination; `Process.Start`, `Code.Execute` and `Pty.Create`
  for **every** armed ticket. Not `Write`, `WatchDir`, `Move`, `Remove` or
  `MakeDir` (a racing `Write` simply loses or wins the rename).
- Mechanics (`rayd/src/transfer/barrier.rs`; the selection is
  `rayd-core/src/transfer/barrier.rs`, pure): if the registry's armed
  counter is zero the barrier returns immediately (an atomic load: zero cost
  when nothing is armed). Otherwise, for each selected ticket in parallel: a
  `Waiting` ticket is told to poll now and the barrier waits up to **2 s**
  (one budget for the whole call) for that poll's result — pending or no
  answer within the budget → continue (fail open); found → wait for the
  import's terminal state; a `Running` ticket → wait for its terminal
  state. Waiting for a terminal state has no budget of its own: it is
  bounded by the RPC's deadline and cancellation. A failed import does not
  fail the RPC (it then sees the old file or `NOT_FOUND`).
- Order in each handler: after validation, identity and deny-list checks,
  immediately before the operation (before the spawn, before handing the
  code to the kernel, before `open`).
- Each barrier run logs `barrier` with `rpc`, `tickets`, `waited_ms`,
  `outcome` (`none` | `probed` | `waited` | `budget_exhausted`).

### D12. Suspend, resume and terminate

The transfer manager subscribes to `SuspendSignal` like the persistence
manager. On the first `/suspend` of a cycle: every `Running` task is
cancelled (import: sink dropped, temp removed; export: stop sending), goes
back to `Waiting` without counting an attempt, and every poller stops
issuing requests until `/resume`; the handler waits at most 1 s for the
tasks to acknowledge and **always answers 200** (the invariant of §8).
`WatchTransfer` streams close with `UNAVAILABLE suspending` through
`SuspendableStream`. On `/resume` every armed poller polls at once and
`Waiting` exports re-acquire permits and restart from offset 0 (the outbound
connections were killed anyway, §15; S3 accepts re-uploaded parts under the
same part number). A requeued export re-reads the file it keeps open: its
snapshot is the file content as read after the resume, which the docs state.
On `/terminate` every non-finished record becomes `Cancelled` and no DELETE
runs. Expiry keeps counting in wall-clock time across the suspension.

### D13. Error mapping

Unary (`StartImport`, `StartExport`, `GetTransfer`, `CancelTransfer`):
`INVALID_ARGUMENT` (URL policy, request checks, not-regular export source,
`too_large_for_put`), `PERMISSION_DENIED` (deny list, identity policy,
`EACCES`), `NOT_FOUND` (missing export source, unknown id, empty id),
`RESOURCE_EXHAUSTED` (16-cap), `FAILED_PRECONDITION` (`file_changed`,
before `/run`), `UNAVAILABLE` + `suspending`/`terminating` (phase gate).
Messages are fixed Spanish sentences, never a path, URL, bucket or key.

In-state `TransferState.error = StreamError{code, message}` with `message =
"<reason>: <frase fija>"` where `<reason>` is one of the tokens used in D9
and D10 (`expired`, `no_object`, `access_denied`, `signature_rejected`,
`wrong_region`, `bucket_missing`, `s3_unavailable`, `forbidden_address`,
`unexpected_response`, `no_content_length`, `too_large`, `disk_reserve`,
`disk_full`, `checksum_mismatch`, `file_shrank`, `cancelled`) and `code` one
of `deadline_exceeded`, `not_found`, `permission_denied`,
`invalid_argument`, `resource_exhausted`, `failed_precondition`,
`unavailable`, `cancelled`, `internal`.

SDK (Python; TypeScript mirrors with its classes): new
`rayito.exceptions.TransferException(SandboxException)` with `.code` and
`.reason` (both `str`, parsed from the state), and
`FileUploadException(TransferException)`; new
`rayito.exceptions.UnimplementedError(NotImplementedError)` with `feature`
and `reason` and an optional doc reference; `rayito.e2b.UnimplementedError`
becomes a re-export of it (the shim passes `doc=COMPAT_DOC_PATH`, so its
messages are unchanged in substance). Terminal state → exception:

| `code` | import (`UploadTicket.wait`, large write) | export (`download_url`, large read) |
|---|---|---|
| `deadline_exceeded` | `TimeoutException` | `TimeoutException` |
| `invalid_argument` | `InvalidArgumentException` | `InvalidArgumentException` |
| `resource_exhausted` | `DiskFullException` | `DiskFullException` |
| `permission_denied` | `AuthenticationException(proxy_rejected=False)` | same |
| `not_found` | `FileNotFoundException` | `FileNotFoundException` |
| `failed_precondition`, `unavailable`, `cancelled`, `internal`, unknown | `FileUploadException(code, reason)` | `TransferException(code, reason)` |

Every message starts with `"<reason>: "`, so the acceptance's
"`InvalidArgumentException` (`too_large`)" is `str(exc).startswith(
"too_large")`. Unary errors keep the existing `translate_rpc_error` table
(`RESOURCE_EXHAUSTED` without disk details → `RateLimitException`), except
`UNIMPLEMENTED` from a transfer RPC → `UnimplementedError(feature,
"actualiza la imagen: este rayd no tiene transferencias")`. TypeScript:
`TransferError extends SandboxError` (`code`, `reason`), `FileUploadError
extends TransferError`, `UnimplementedError extends Error` (outside the
hierarchy, like Python's `NotImplementedError`), same table with
`TimeoutError`, `InvalidArgumentError`, `DiskFullError`… — TypeScript has no
`DiskFullError` today, so `resource_exhausted` maps to a new `DiskFullError
extends SandboxError` exported from `errors.ts`.

### D14. SDK surface (Python sync and async)

Pure helpers in `clients/python/src/rayito/_transfer_base.py` (no I/O):
`resolve_staging`, `validate_staging_against_persist`,
`presign_client_config`, `staging_key(prefix, sandbox_id, direction,
token)`, `effective_expires_in(expires_in, staging)`,
`export_budget_seconds(size)`, `content_disposition(filename)`,
`part_plan(size, staging) -> PartPlan | None` (`part_size =
max(8 MiB, ceil_to_mib(ceil(size / 1000)))`, `None` below
`multipart_threshold_bytes`), request builders for the five RPCs,
`state_from_proto`, `failure_from_state(state, direction)` (D13 table),
`probe_result(exc)` (capability), `upload_ticket_headers(form)`, and the
routing predicate `should_route(size, staging)` (D15). `sandbox_sync/transfer.py`
and `sandbox_async/transfer.py` hold the I/O (boto3 calls in the async tree
run through `asyncio.to_thread`).

`Filesystem` (sync; `AsyncFilesystem` identical with `async def`):

```python
def upload_url(self, path: str, *, user: str | None = None, expires_in: int = 3600,
               max_bytes: int | None = None, form: bool = False,
               request_timeout: float | None = None) -> UploadTicket
def download_url(self, path: str, *, user: str | None = None, expires_in: int = 3600,
                 filename: str | None = None, request_timeout: float | None = None) -> DownloadLink
```

`Sandbox.upload_url(path, user=None, use_signature_expiration=None) ->
UploadTicket` and `Sandbox.download_url(path, user=None,
use_signature_expiration=None) -> DownloadLink` on the native `Sandbox`
(E2B's positional shape; `use_signature_expiration` → `expires_in`, `None`
→ 3600, `<= 0` → `InvalidArgumentException`).

`upload_url` steps: staging resolved (else `UnimplementedError("upload_url",
"configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET")`); path
validated; `max_bytes` `None` or `>= 1`; capability probe (cached per
sandbox: `GetTransfer("")` → `NOT_FOUND` = capable, `UNIMPLEMENTED` =
`UnimplementedError("actualiza la imagen")`); key (D4); user URL (PUT, or
POST with `content-length-range [0, max_bytes or 5 368 709 120]`); rayd GET
and DELETE URLs; `StartImport(path, user, object, get, delete,
wait_for_object=True, expires_at_unix_ms, max_bytes or 0)`; returns the
ticket. The ticket is armed before the caller can `PUT`.

`UploadTicket(str)` — the string value is the URL (`requests.put(ticket,
data=f, headers=ticket.headers)` works) — attributes `url`, `method`
(`"PUT"` | `"POST"`), `headers` (`{"Content-Type":
"application/octet-stream"}` for PUT, `{}` for POST), `fields` (the POST
form fields, `{}` for PUT), `path`, `expires_at`, `transfer_id`; methods
`wait(timeout: float | None = None) -> EntryInfo` (`WatchTransfer` on the
stream channel; re-issued after the reconnection poll on `UNAVAILABLE
suspending`, a reset or EOF, exactly like `WatchHandle`; raises per D13;
`timeout` elapsed → `TimeoutException` with the ticket still armed),
`status() -> TransferStatus` (`GetTransfer`), `cancel() -> None`
(`CancelTransfer`). `AsyncUploadTicket(str)` has the same attributes and
awaitable methods. `__repr__` = `UploadTicket(path=…, method=…,
expires_at=…)` — never the URL; `__reduce__` raises `TypeError` (a ticket is
bound to a live sandbox and a credential).

`download_url` steps: staging; capability probe; `Stat` (missing →
`FileNotFoundException` **immediately**; directory or symlink →
`InvalidArgumentException`); key; `part_plan(size)`: `None` → presign one
PUT; else `create_multipart_upload(Bucket, Key, ContentType=
"application/octet-stream")`, presign one `upload_part` URL per part;
`StartExport`; `WatchTransfer` to the terminal state; multipart `Done` →
`complete_multipart_upload(Bucket, Key, UploadId, MultipartUpload={"Parts":
[{"ETag": e, "PartNumber": i}]})`; any failure after
`create_multipart_upload` → `abort_multipart_upload(Bucket, Key, UploadId)`
(best effort, logged) and the D13 exception; then presign the user GET with
`ResponseContentDisposition`. Returns `DownloadLink(str)` (value = URL) with
`url`, `path`, `expires_at`, `size`, `sha256`, `transfer_id`; redacted
`repr`. The SDK never deletes a download object (the link must work until
expiry; the lifecycle rule removes it).

`TransferStatus` (frozen dataclass): `transfer_id`, `direction:
Literal["import", "export"]`, `phase: Literal["waiting", "running", "done",
"failed", "cancelled"]`, `bytes_done`, `bytes_total`, `probes`,
`error_code: str | None`, `error_reason: str | None`.

`rayito.e2b` (`_sync.py`, `_async.py`): `upload_url(self, path: str | None
= None, user: str | None = None, use_signature: bool = False,
use_signature_expiration: int | None = None) -> str` returns the native
`UploadTicket` (so `ticket.headers`/`wait()` stay available); `path=None`
→ `InvalidArgumentException` ("indica la ruta de destino: una URL de S3 no
lleva nombre de fichero"); `use_signature` is accepted and ignored (Rayito
URLs are always signed); `use_signature_expiration <= 0` →
`InvalidArgumentException`. `download_url(self, path, user=None,
use_signature=False, use_signature_expiration=None) -> str` returns the
native `DownloadLink`. The async shim keeps `async def` (a network call is
unavoidable; E2B's are sync — documented divergence). Both leave the
`UnimplementedError` list.

### D15. Large-file routing

Only when `sbx.transfer` is set and the capability probe succeeded:

- `files.write(path, data, …)` / `write_files(entries, …)`: an entry is
  **routed** when its size is known and `>= threshold_bytes`, or when
  `data` is a non-seekable binary IO (size unknown: streamed, never
  materialised). Seekable IO is measured with `seek`/`tell`. Routed entries:
  the SDK uploads with its own credentials — Python
  `client.upload_fileobj(Fileobj=<hashing reader>, Bucket, Key,
  ExtraArgs={"ContentType": "application/octet-stream"},
  Config=TransferConfig(multipart_chunksize=8 MiB, max_concurrency=8))`,
  TypeScript `new Upload({ client, params: { Bucket, Key, Body,
  ContentType }, partSize: 8 MiB, queueSize: 8 })` from
  `@aws-sdk/lib-storage` — hashing sha256 on the way, then
  `StartImport(wait_for_object=False, expected_sha256=<hex>,
  max_bytes=<uploaded size>, mode, metadata)` and `WatchTransfer` to the
  terminal state; the committed `EntryInfo` is the result. Non-routed
  entries of a `write_files` call still share one gRPC `Write` stream;
  results keep request order. `gzip` is ignored for routed entries (S3 is
  already fast; documented).
- `files.read(path, …)`: `read` already `Stat`s first; when `size >=
  threshold_bytes` the SDK exports (`StartExport` with a single PUT or a
  multipart plan, completed/aborted as in D14) and then downloads with its
  own credentials — every format reads
  `get_object(Bucket, Key)["Body"].iter_chunks(262144)`, bounded by the
  operation deadline and by `stream_idle_timeout` between chunks
  (`_s3.ObjectFetch`, whose `cancel()` closes the body);
  `format="bytes"`/`"text"` collects the chunks (TS: `GetObjectCommand` body
  collected) and `format="stream"` yields them (TS: the body as a
  `ReadableStream`). `download_fileobj` is not used: its worker threads
  cannot be bounded by the idle guard nor cancelled — verifies the export's sha256 (mismatch →
  `TransferException(code="failed_precondition", reason="checksum_mismatch")`),
  and deletes the staging object with `delete_object` (best effort, logged).
- Deadline: `request_timeout` when given covers the whole routed
  operation; otherwise `60 s + 1 s per 1 000 000 bytes`, the existing
  vocabulary.
- Without staging, or below the threshold, the gRPC paths, deadlines and
  channels are exactly those of the `filesystem` spec today.

### D16. gzip

- `rayd`: tonic workspace feature `gzip`; `FilesystemServiceServer::new(…)
  .accept_compressed(CompressionEncoding::Gzip)
  .send_compressed(CompressionEncoding::Gzip)`; no other service changes.
  New tower layer `CompressionOptInLayer`
  (`crates/rayd/src/grpc/compression.rs`) on the server builder, before the
  access-token layer: when the request carries `rayito-compress: gzip`
  (value compared case-insensitively) it leaves `grpc-accept-encoding`
  untouched; otherwise it replaces it with `identity`. Existing clients that
  advertise gzip therefore keep receiving identity responses.
- Python: `files.read(..., gzip=True)` adds the call metadata
  `("rayito-compress", "gzip")` to `Read` (grpcio decompresses); 
  `files.write(..., gzip=True)` / `write_files(..., gzip=True)` open the
  `Write` stream with `compression=grpc.Compression.Gzip`. Same channels as
  today.
- TypeScript: `read(path, { gzip: true })` adds the header
  `rayito-compress: gzip` on the unary transport (its default
  `acceptCompression` includes gzip); `write`/`writeFiles` with `gzip: true`
  use a **gzip transport created lazily** with `createGrpcTransport({ …,
  sendCompression: compressionGzip, compressMinBytes: 1024, sessionManager:
  <the unary transport's Http2SessionManager> })`, so no third HTTP/2
  session exists.
- `gzip` on an older image: write with gzip is refused by the capability
  probe (`UnimplementedError("actualiza la imagen")`) before any bytes; read
  with gzip on an older image is harmless (identity response) and is not
  probed.
- Deadlines and the 1 MiB chunk rule apply to the uncompressed data.

### D17. Metadata as xattrs

`rayd-core/src/filesystem/metadata.rs` (pure): `FileMetadata::parse(map)`:
keys 1–255 bytes of HTTP token characters (`!#$%&'*+-.^_`|~`, digits,
letters), stored lowercased; two keys equal after lowercasing →
`INVALID_ARGUMENT`; values printable ASCII `0x20–0x7E` (empty allowed); at
most 64 keys; `Σ(len("user.rayito.") + len(key) + len(value)) <= 4000`.
`xattr_name(key) = "user.rayito." + key`. Invalid → `INVALID_ARGUMENT` with
the fixed message "metadatos inválidos" (never the key or value).

- `Write`: `WriteMessage` gains `metadata`; `WriteSession::accept` refuses
  `metadata` on a message without `path` (`INVALID_ARGUMENT`, like
  `user`/`mode`) and passes it with the file start. `WriteSink` gains
  `set_metadata(&mut self, metadata: &FileMetadata) -> Result<(),
  FsIoError>`: `fsetxattr` on the temp file's descriptor, one call per key,
  **before** the rename in `commit`, so content and metadata appear
  together and no path-based syscall is added (C-06 not widened). Because
  every write creates a new inode, an overwrite replaces the whole set and
  an overwrite without metadata clears it (envd semantics). `ENOTSUP` →
  `FAILED_PRECONDITION` details `metadata_unsupported`; `E2BIG`/`ENOSPC`
  from `fsetxattr` → `INVALID_ARGUMENT` details `metadata_too_large`; the
  temp file is removed in both cases. `StartImport` uses the same path.
- `Stat`/`ListDir`: new port method `FileSystem::read_metadata(id, path) ->
  Result<FileMetadata, FsIoError>` = `llistxattr` + `lgetxattr` of every
  `user.rayito.*` name (never following symlinks), under the request's
  `FsIdentityGuard`; `ENOTSUP`, `ENODATA`, `EACCES` and `EPERM` → empty (a
  listing never fails because one file's metadata is unreadable). Filled
  into `EntryInfo.metadata` for every entry.
- Logging: metadata keys and values are never logged (only a `metadata_keys`
  count on `Write`).
- SDK: `EntryInfo.metadata: Mapping[str, str]` (read-only mapping, empty by
  default; TS `metadata: Readonly<Record<string, string>>`);
  `files.write(..., metadata=None)` and `write_files(..., metadata=None)`
  apply the same mapping to every file written; client-side validation
  mirrors the domain rules (`InvalidArgumentException` before any RPC) and
  the SDK sends the keys already lowercased (`rayd` lowercases again, so an
  older-style client is still normalised);
  non-empty `metadata` requires the capability probe (an older `rayd` would
  ignore the unknown field silently, which hard rule 7 forbids).
- Q73 measures that `user.*` xattrs work on the guest's `/home/user`
  filesystem before code; if they do not, the implementer stops and
  reports (design rule: never improvise).

### D18. `stream_idle_timeout`, `use_octet_stream`, JS `format: "blob"`

- `files.read(..., stream_idle_timeout: float | None = None)` (TS
  `streamIdleTimeoutMs`): `None`/`0` = none; otherwise, if no chunk arrives
  for that many seconds the SDK cancels the call and raises
  `TimeoutException("stream_idle_timeout: …")`. Sync: a
  `threading.Timer` reset on every chunk that calls `call.cancel()`; async:
  `asyncio.wait_for` around each `__anext__`; TS: an `AbortController`
  timer reset on every chunk. On the S3 route the same guard wraps the
  `WatchTransfer` stream and the S3 body iteration (closing the botocore
  `StreamingBody` / aborting the TS request). Values `< 0` →
  `InvalidArgumentException`.
- `files.write(..., use_octet_stream: bool = False)` / `write_files(...)`
  (TS `useOctetStream`): accepted with no effect (gRPC has no multipart
  form); documented.
- TS `files.read(path, { format: "blob" })` → `Blob` with type
  `application/octet-stream` built from the same bytes as `"bytes"`.

### D19. TypeScript surface

`src/sandbox/transfer.ts` (the I/O) over pure helpers in the same module
namespace (`stagingKey`, `effectiveExpiresIn`, `partPlan`,
`contentDisposition`, `failureFromState`, …):

```ts
files.uploadUrl(path: string, opts?: { user?: string; expiresIn?: number; maxBytes?: number;
  form?: boolean; requestTimeoutMs?: number }): Promise<UploadTicket>
files.downloadUrl(path: string, opts?: { user?: string; expiresIn?: number; filename?: string;
  requestTimeoutMs?: number }): Promise<DownloadLink>
sandbox.uploadUrl(path: string, opts?: { user?: string; useSignatureExpiration?: number }): Promise<string>
sandbox.downloadUrl(path: string, opts?: { user?: string; useSignatureExpiration?: number }): Promise<string>

class UploadTicket { readonly url; method: "PUT" | "POST"; headers; fields; path;
  expiresAt: Date; transferId; wait(opts?: { timeoutMs?: number }): Promise<EntryInfo>;
  status(): Promise<TransferStatus>; cancel(): Promise<void>; toString(): string; toJSON(): never }
class DownloadLink { readonly url; path; expiresAt: Date; size: number; sha256; transferId;
  toString(): string; toJSON(): never }
```

`sandbox.uploadUrl`/`downloadUrl` return the URL string (E2B JS shape) and
are what `m9-e2b-v2-surface`'s shim delegates to. `toJSON()` throws so a
ticket is never serialised by accident; `util.inspect` shows the redacted
form. New dependencies in `package.json` `dependencies` (caret floors of the
versions `pnpm add` resolves on the implementation day, the four at the same
3.x minor, exact in `pnpm-lock.yaml`): `@aws-sdk/client-s3`,
`@aws-sdk/s3-request-presigner`, `@aws-sdk/s3-presigned-post`,
`@aws-sdk/lib-storage`; loaded with dynamic `import()` inside
`transfer.ts`, so `import { Sandbox } from "rayito"` does not load them.
`pack:check` still lists only `dist/**`, `README.md`, `package.json`,
`LICENSE`, `NOTICE`.

### D20. IAM, operator guidance, documentation

`spike/m0/iam.yaml`:

```yaml
  TransferBucket:
    Type: String
    Default: ""
    Description: >-
      S3 bucket for upload_url/download_url and large-file staging
      (transfer=S3Staging, ADR-010); empty = no transfer statements.
  TransferPrefix:
    Type: String
    Default: rayito-transfer
    AllowedPattern: "^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$"
    Description: >-
      Key prefix under TransferBucket (S3Staging(prefix=)). Never `rayito`,
      a prefix of it, or PersistencePrefix: the 1-day lifecycle rule of the
      transfer prefix would expire image artifacts or checkpoints.
Rules:
  TransferPrefixIsDisjoint:
    RuleCondition: !Not [!Equals [!Ref TransferBucket, ""]]
    Assertions:
      - Assert: !Not [!Equals [!Ref TransferPrefix, rayito]]
        AssertDescription: TransferPrefix cannot be the image artifact namespace.
      - Assert: !Not [!Equals [!Ref TransferPrefix, !Ref PersistencePrefix]]
        AssertDescription: TransferPrefix cannot equal PersistencePrefix.
Conditions:
  HasTransferBucket: !Not [!Equals [!Ref TransferBucket, ""]]
# in CallerPolicy.Statement:
          - !If
            - HasTransferBucket
            - Sid: TransferObjects
              Effect: Allow
              Action: [s3:PutObject, s3:GetObject, s3:DeleteObject, s3:AbortMultipartUpload]
              Resource: !Sub arn:aws:s3:::${TransferBucket}/${TransferPrefix}/*
            - !Ref AWS::NoValue
          - !If
            - HasTransferBucket
            - Sid: TransferMissingKeyIs404
              Effect: Allow
              Action: s3:ListBucket
              Resource: !Sub arn:aws:s3:::${TransferBucket}
              Condition:
                StringLike:
                  s3:prefix: !Sub ${TransferPrefix}/*
            - !Ref AWS::NoValue
```

`CreateMultipartUpload`, `UploadPart` and `CompleteMultipartUpload` are
authorised by `s3:PutObject`. The execution role gets nothing (T1, C-07
unchanged). `scripts/tests/test_iam_template.py` gains
`test_transfer_prefix_default_is_disjoint_from_artifacts_and_persistence`,
`test_transfer_statements_are_conditional_on_the_bucket` and
`test_execution_role_has_no_transfer_statement`; `uvx cfn-lint==1.56.3`
clean.

`infra/README.md` "Transferencias de ficheros" (placeholders only:
`amzn-s3-demo-bucket`, `123456789012`, `<tu-perfil>`): stack update with
`TransferBucket`/`TransferPrefix`; lifecycle configuration on the prefix
with `Expiration.Days = 1` and `AbortIncompleteMultipartUpload.
DaysAfterInitiation = 1` (JSON shown, rounded to the next midnight UTC by
S3, so objects live 24–48 h); a bucket policy denying requests whose
`s3:signatureversion` is not `AWS4-HMAC-SHA256` and requests with
`aws:SecureTransport = false`; CORS only for browsers (`PUT`, `POST`,
`GET`, explicit origins, `ExposeHeaders: ["ETag"]`); SSE-KMS buckets need
`kms:GenerateDataKey`/`kms:Decrypt` for the caller.

`AWS_API_NOTES.md` §18 "Transferencias por URLs prefirmadas (M9,
`m9-file-transfer`, **contrato de parámetros**)" — the only S3 operations and
parameters the SDKs may use for transfers, and the raw-HTTP contract of
`rayd`, one row each with its source:

| Caller | Call | Parameters | Source |
|---|---|---|---|
| Python | `generate_presigned_url` | `ClientMethod` ∈ {`put_object`, `get_object`, `delete_object`, `upload_part`}, `Params` = the D5 table, `ExpiresIn` | <https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_url.html> |
| Python | `generate_presigned_post` | `Bucket`, `Key`, `Conditions=[["content-length-range", 0, N]]`, `ExpiresIn` | …/`generate_presigned_post.html`, <https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-HTTPPOSTConstructPolicy.html> |
| Python | `create_multipart_upload` / `complete_multipart_upload` / `abort_multipart_upload` | `Bucket`, `Key`, `ContentType` / + `UploadId`, `MultipartUpload{Parts[{ETag, PartNumber}]}` / `Bucket`, `Key`, `UploadId` | API_CreateMultipartUpload / API_CompleteMultipartUpload / API_AbortMultipartUpload |
| Python | `upload_fileobj` / `get_object` / `delete_object` | `Fileobj`, `Bucket`, `Key`, `ExtraArgs={"ContentType"}`, `Config=TransferConfig(multipart_chunksize, max_concurrency)` / `Bucket`, `Key` / `Bucket`, `Key` | …/`upload_fileobj.html`, <https://docs.aws.amazon.com/boto3/latest/reference/customizations/s3.html> |
| Python | `botocore.config.Config` | `signature_version="s3v4"`, `s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"}` | <https://docs.aws.amazon.com/botocore/latest/reference/config.html> |
| TS | `getSignedUrl` / `createPresignedPost` / `Upload` / `S3Client` | `(client, command, { expiresIn })` / `(client, { Bucket, Key, Conditions, Expires })` / `{ client, params: { Bucket, Key, Body, ContentType }, partSize, queueSize }` / `requestChecksumCalculation: "WHEN_REQUIRED"` | AWS SDK for JavaScript v3 package docs (URLs recorded in task 0.1) |
| e2e only | `head_object`, `list_multipart_uploads` | `Bucket`, `Key` / `Bucket`, `Prefix` | API_HeadObject / API_ListMultipartUploads |
| `rayd` | raw HTTPS | `GET`/`PUT`/`DELETE` on the presigned URL; headers `host`, `user-agent`, `content-length`, `content-type: application/octet-stream` | <https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html> |

plus the limits and behaviours with their sources: 604 800 s maximum and the
credential-expiry ceiling (`using-presigned-url.html`, Q62), 5 GiB single
PUT, 10 000 parts of 5 MiB–5 GiB (`qfacts.html`), POST form ≤ 20 KB, the
`Request has expired` 403 body, 403 vs 404 depending on `s3:ListBucket`
(`API_GetObject.html#API_GetObject_Errors`), `ExpiredToken`, virtual-hosted
and endpoint host forms (`VirtualHosting.html`, general reference `s3.html`),
the `s3:signatureversion` condition key
(`bucket-policy-s3-sigv4-conditions.html`), lifecycle rounding.

`SECURITY.md` **T16** "URLs prefirmadas y SSRF del agente": URLs are bearer
credentials (never logged, redacted `repr`, scoped to one opaque key, one
method, a bounded expiry; a leaked user PUT URL lets a third party store up
to 5 GB until lifecycle expiry, and `rayd` refuses it above `max_bytes`; a
download URL signed with long-lived IAM-user keys can live 7 days); `rayd`'s
SSRF guard (exact host/path binding, https only, port 443, no IP literals,
no redirects, resolver filter keeping IMDS out of reach although `rayd` is
root); no execution role (T1 unchanged, C-07 not applicable); imports
through C-05 + `FsIdentityGuard` + the temp/rename path (C-06 unchanged);
exports as the user with `O_NOFOLLOW` (T11); size caps and disk reserve
(T7); metadata through the fd (C-06 not widened). The logging paragraph
gains transfers and metadata.

`docs/site/docs/files.md` (new, Spanish, in `mkdocs.yml` after
`persistence.md`): the filesystem surface, `S3Staging` and the env vars,
`upload_url`/`download_url` with `requests`, `curl -T`, `fetch` examples and
the POST form, the barrier and `wait()`, large-file routing with the
measured numbers (Q59, Q75), gzip, metadata, `stream_idle_timeout`, the IAM
and bucket guidance, and "Diferencias con E2B": raw-body PUT instead of a
multipart POST (`form=True` gives POST fields); asynchronous landing
covered by the barrier and `ticket.wait()`; single-use tickets; download is
a snapshot taken at call time (a requeued export after a suspend re-reads
the file); expiry always set (≤ 7 days, ≤ the credential lifetime);
`use_signature_expiration <= 0` raises; a transfer bucket is required; a
missing file raises at `download_url`; `upload_url(path=None)` raises; the
async shim methods are coroutines. `e2b-compat.md` moves both rows from
"Lanza `UnimplementedError`" to "Se mapea, con una nota" with those
divergences, and adds rows for `gzip`, `metadata`, `stream_idle_timeout`,
`use_octet_stream`.

### D21. Logging hygiene

`rayd` transfer log lines carry only `transfer_id`, `direction`, `phase`,
`bytes`, `duration_ms`, `http_status`, `s3_error_code`, `s3_request_id`,
`attempt`, `probes`, `reason`, `outcome`; barrier lines per D11. Never a
URL, host, bucket, key, path, header value, metadata key or value. The
Python and TypeScript SDKs log only `transfer_id`, `direction`, `bytes`,
durations and outcomes at `debug`. Tests: `crates/rayd/tests/m9_transfer.rs::
transfer_logs_never_carry_signatures_or_names` (a full import and export
against a local fake S3 with the log capture of `tests/common`, asserting
the captured text contains neither `X-Amz-Signature`, `X-Amz-Credential`,
the bucket, the key nor the path); Python `test_transfer_sync.py::
test_logs_never_carry_urls` (caplog at `DEBUG`); TypeScript
`transfer.test.ts` "logger never receives a URL".

### D22. Limits

`limits.json` gains (camelCase; `gen_limits.py` renders Python/TS; a new
`rayd-core` unit test `transfer_limits_match_limits_json` parses
`../../limits.json` with `serde_json` and asserts the Rust constants):
`transferDefaultPrefix "rayito-transfer"`,
`transferDefaultExpiresInSeconds 3600`,
`transferDefaultMaxExpiresInSeconds 86400`,
`transferPresignMaxSeconds 604800`,
`transferDefaultThresholdBytes 8388608`,
`transferThresholdMinBytes 1048576`,
`transferDefaultMultipartThresholdBytes 5368709120`,
`transferMultipartThresholdMinBytes 16777216`,
`transferSinglePutMaxBytes 5368709120`,
`transferPartSizeMinBytes 8388608`, `transferMaxParts 1000`,
`transferMaxActive 16`, `transferMaxRunning 2`, `transferRetainedMax 64`,
`transferRetainedSeconds 1800`, `transferProbeBudgetMs 2000`,
`transferPollFastIntervalMs 1000`, `transferPollSlowIntervalMs 5000`,
`transferPollFastWindowSeconds 600`, `transferUrlMaxBytes 8192`,
`transferInternalExpiresInSeconds 900`, `transferConnectTimeoutMs 5000`,
`transferIdleTimeoutMs 30000`, `metadataMaxBytes 4000`,
`metadataMaxKeys 64`, `metadataXattrPrefix "user.rayito."`,
`compressionOptInHeader "rayito-compress"`.

`transferMaxParts` is **1 000**, not the 10 000 of S3: `rayd` keeps tonic's
4 MiB decoding limit and 10 000 URLs of ≈ 1.6 KB each would not fit one
`StartExportRequest`; with `part_size = max(8 MiB, ceil_to_mib(size /
1000))` the largest file a 32 GB disk can hold exports in ≤ 1 000 parts of
≤ 33 MiB (≈ 1.7 MB of URLs). This replaces the Scope's `ceil(size/10000)`.

### D23. Unit tests that fail without the change

Rust (`rayd-core`, Windows and Linux): `url_policy` table (every D6 rule,
accepted: the four host forms, `partNumber`/`uploadId` consistency; refused:
`http://`, `169.254.169.254`, `[::1]`, `10.0.0.5` literal, global host,
path-style, accelerate, dotted bucket, mismatched bucket, key or region,
userinfo, `:8443`, `:443` accepted, SigV2 query, missing `X-Amz-Signature`,
mixed hosts between get/delete, wrong sandbox id or direction, extra
header); `is_forbidden_address` table (v4, v6, mapped, IMDS v6, RFC 1918
allowed); `poll` (1 s until 600 s, then 5 s, stop at expiry); `s3_error`
(real S3 bodies for `NoSuchKey`, `AccessDenied`/`Request has expired`,
`ExpiredToken`, `SignatureDoesNotMatch`, `PermanentRedirect`, truncated
body, entities); import classification table (D9); `ImportPlan::admit`
(`too_large`, `no_content_length`, disk reserve with a fake free-space);
export plan (`too_large_for_put`, `file_changed`, part counts at 0, 1,
exact multiples); `registry` (16-cap, 2 running, retention 64/30 min with a
fake clock, illegal transitions, idempotent cancel, empty id `NOT_FOUND`);
barrier selection (path equality, ancestor, workload); `FileMetadata`
(token chars, space refused, duplicate after lowercasing, 4000-byte and
64-key limits, lowercase output); `WriteSession` refusing `metadata`
without `path`; `transfer_limits_match_limits_json`.

Rust (`rayd`): `signed_http.rs` unit tests against a local
`tokio-rustls` TLS server on `127.0.0.1` with committed test certificates
(`crates/rayd/tests/fixtures/tls/` — a test-only CA and a leaf for
`amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com`, generated once with
`openssl`, never trusted outside the test) through a test-only constructor
that injects that root and allows loopback: GET body round trip, PUT with
exact `Content-Length`, 302 → `Redirect`, idle stall → `Timeout` (with a
shrunk timeout), no extra headers sent; the production constructor refusing
a host that resolves to `127.0.0.1` (`ForbiddenAddress`) without connecting.
`std_filesystem.rs` `cfg(unix)` tests: `set_metadata` before commit is
visible after the rename, overwrite clears, `read_metadata` on a symlink
does not follow, unreadable file → empty; `open_snapshot` refuses a
symlink and a FIFO. `grpc/compression.rs`: header rewritten without the
opt-in, kept with it. `crates/rayd/tests/m9_transfer.rs` (loopback tonic +
a fake `SignedHttp` + `StdFileSystem` on a temp dir under `cfg(unix)`):
import happy path (owner, mode, metadata, sha256, DELETE issued), armed
poll 404×3 then 200, expiry → `deadline_exceeded`, `too_large` with DELETE
issued and no file, `checksum_mismatch`, suspend requeue then resume
completes, export single and multipart (ETags in order), export aborting
on a file truncated mid-read (`file_shrank`), `RESOURCE_EXHAUSTED` at the
17th, `INVALID_ARGUMENT` policy failures with **zero** calls recorded on
the fake `SignedHttp`, barrier: `Read` right after an armed object appears
returns the new content, `Start` waits for an import, zero-armed barrier
performs no fake call, gzip response only with the opt-in header, the log
test of D21.

Python: `test_transfer_base.py` (presign config produces
`X-Amz-Algorithm=AWS4-HMAC-SHA256` and host
`amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com` for us-east-1 with fake
credentials — catches botocore's SigV2/global default; the 604 801 → 604 800
and `max_expires_in` clamps; `expires_in <= 0`; staging key shape;
`S3Staging` rules incl. dotted bucket and the `rayito` overlap;
`validate_staging_against_persist`; `part_plan`; `content_disposition`;
`failure_from_state` for every D13 row; metadata validation; routing
predicate); `test_transfer_sync.py` / `test_transfer_async.py` against
`fake_transfer.py` (a fake `rayd` transfer servicer) and `fake_s3.py` (a
local HTTP server speaking the S3 subset over presigned URLs or an injected
botocore stubber): `upload_url` without staging → `UnimplementedError`;
capability probe `UNIMPLEMENTED` → `UnimplementedError("actualiza la
imagen")` and cached; ticket is a `str` equal to the URL with redacted
`repr` and `__reduce__` refusing; `wait()` success, expiry, `too_large`,
reconnect on `UNAVAILABLE suspending`; `download_url` missing file raises
before any presign; multipart complete and abort paths; large write routes
at the threshold and not below, with `expected_sha256`; a transport spy
proving no `StartImport` when staging is unset; `gzip` metadata/compression
on the wire; `stream_idle_timeout`; e2b shim mapping incl.
`use_signature_expiration=-1`; sync/async parity lists the new names.
`test_files_sync.py`/`test_files_async.py` gain the metadata round trip on
`fake_filesystem.py`.

TypeScript: `transfer.test.ts` mirroring the Python cases one to one on the
fake `rayd` (new transfer servicer) and a local fake S3; `filesystem.test.ts`
gains `format: "blob"`, `gzip`, `metadata`, `streamIdleTimeoutMs`,
`useOctetStream`; `sandbox.test.ts` "gzip transport reuses the unary
session" (the fake counts two sessions after a gzip write, a background run
and a watch); `errors.test.ts` the new classes.

`scripts/tests/test_iam_template.py` per D20.

### D24. Real-AWS acceptance

`clients/python/tests/e2e/test_m9_transfer.py` on the default M9
`rayito-base` image, **no execution role** (`get_info().execution_role_arn
is None`), bucket and prefix from `RAYITO_E2E_TRANSFER_BUCKET` /
`RAYITO_E2E_TRANSFER_PREFIX` (skip when unset), teardown deleting every
object under `<prefix>/<sandbox_id>/` and aborting multipart uploads with
the developer's boto3 client. Each test prints `report(...)` lines (MB/s,
latencies) for §16:

1. `test_upload_url_put_and_wait`: 10 MiB `os.urandom`; `requests.put(
   ticket, data=f, headers=ticket.headers)`; `ticket.wait()` → sha256 equal
   (`sha256sum` inside), owner uid 1000 (`stat -c %u`), `head_object` → 404.
2. `test_barrier_without_wait`: `requests.put(...)` immediately followed by
   `files.read()` (bytes equal) and, on a second ticket, by
   `commands.run("sha256sum <path>")` (hash equal), neither calling
   `wait()`; PUT-to-visible latency reported.
3. `test_download_url_snapshot_and_range`: 50 MB `dd if=/dev/urandom`;
   `urllib.request.urlopen(link)` without headers → sha256 equal; `Range:
   bytes=0-99` → 206; the file overwritten after the call still serves the
   snapshot; MB/s reported.
4. `test_large_files_route_through_s3`: 200 MB `files.write` and
   `files.read` with staging → sha256 equal, throughput ≥ 10× a 20 MB gRPC
   baseline measured in the same run (staging disabled on a second
   `Sandbox.connect(..., transfer=None)` with the env unset); a transport
   spy on that connection asserts no `StartImport`.
5. `test_multipart_export`: `S3Staging(multipart_threshold_bytes=64 MiB)`,
   100 MB file → `download_url` sha256 equal, `list_multipart_uploads(
   Prefix=<prefix>/<sandbox_id>/)` empty afterwards.
6. `test_expiry`: `upload_url(expires_in=5)` without PUT → `wait()` raises
   `TimeoutException` within 5–12 s; `download_url(expires_in=1)` fetched
   after 3 s → 403 with `Request has expired`.
7. `test_size_caps`: `max_bytes=1 MiB` + 2 MiB PUT → `InvalidArgumentException`
   starting with `too_large`, no file, `head_object` 404; `form=True,
   max_bytes=1024` + 2 KiB POST → S3 400 `EntityTooLarge`; 512 B POST
   imports.
8. `test_ssrf_policy`: through the raw `FilesystemService` stub on the
   sandbox's unary channel with the access token, `StartImport` with host
   `169.254.169.254`, an IP literal, `http://`, the global
   `<bucket>.s3.amazonaws.com`, a mismatched bucket, a mismatched key,
   userinfo, and port 8443 → all `INVALID_ARGUMENT`; a background
   `ss -Htan` sampler (every 50 ms) shows no socket to `169.254.169.254`,
   the literal or port 8443 during the calls.
9. `test_suspend_requeue`: arm a ticket, `pause()`, `PUT` while suspended,
   `Sandbox.connect(id)` → `ticket.wait()` completes; `rayd` log (when
   logging is on) shows `/suspend` answered 200.
10. `test_gzip`: 20 MB compressible text, `files.write(gzip=True)` and
    `files.read(gzip=True)` → identical bytes and effective throughput ≥ 3×
    the uncompressed baseline of the same run; the §16 row records that
    `grpc-encoding`/`grpc-accept-encoding` cross the proxy.
11. `test_metadata`: `write(metadata={"Owner": "alice"})` →
    `get_info().metadata == {"owner": "alice"}` and the `list()` entry
    carries it; overwrite without metadata → `{}`; key `"a b"` →
    `InvalidArgumentException`.
12. `test_e2b_shim` (sync and async): `from rayito.e2b import Sandbox`;
    `upload_url` + `requests.put` + `files.read`; `urlopen(download_url(...))`;
    `use_signature_expiration=-1` → `InvalidArgumentException`.
13. `test_log_hygiene` (needs `RAYITO_EXECUTION_ROLE_ARN`, the logs-only
    role): one upload and one download with `logging="cloudwatch"`; the
    rayd stream (read with the developer's `logs` client) and the SDK's
    caplog contain no `X-Amz-Signature`, bucket, key or path.

`clients/typescript/tests/e2e/m9-transfer.e2e.test.ts`: `fetch` PUT and GET
of `uploadUrl`/`downloadUrl`, `files.uploadUrl` + `wait()`, 200 MB
`writeFiles`/`read` through S3, `gzip` round trip, same env vars and
cleanup through the developer's `aws` CLI (the SDK now depends on
`@aws-sdk/client-s3`, so the test uses it directly).

## Risks / Trade-offs

- **Async landing** → the barrier covers files.* reads and new processes;
  a long-running process that reads the path in a loop may still see the
  old file until the import ends. Documented; `wait()` is the explicit form.
- **Polling cost** → ≈ 1 200 GETs per armed hour (≈ $0.0005 at S3 Standard
  GET prices) and the poll never keeps the VM awake (outbound traffic is
  not idle traffic, §7).
- **403 without `s3:ListBucket`** → a missing object and a real denial look
  the same; the armed poll treats both as pending until expiry. The IAM
  template grants the prefix-scoped `ListBucket`; Q70 measures both cases.
- **Leaked URLs** → bearer until expiry; mitigated by opaque keys,
  single-use import with DELETE, `max_bytes`, POST `content-length-range`,
  the lifecycle rule, and T16's documentation.
- **Snapshot vs requeue** → an export restarted after a suspend re-reads the
  file; documented.
- **Binary size** → only the adapter code is new (the TLS stack is linked);
  Q75 records the delta; if it exceeds +1 MB the implementer records why.
- **xattrs unsupported on the rootfs** → Q73 before code; the fallback is
  to stop and report, never to store metadata elsewhere silently.
- **Two S3 clients in TS** → the presigning client and the data client
  differ only by `requestChecksumCalculation`; Q72 confirms the default
  CRC32 behaviour against real S3.

## Migration Plan

Additive. SDK ≥ this change against an older image: every new surface is
refused by the capability probe with `UnimplementedError("actualiza la
imagen")` before bytes move; gzip reads degrade to identity; everything else
behaves as in 0.2.0. Older SDKs against the new image: unchanged behaviour
(identity responses thanks to the opt-in layer; unknown fields absent).
Rollback = previous image version; no stored state changes.

## Open Questions

None blocking. Owner confirmations inherited from the Scope: single-use
tickets, the barrier, `s3:ListBucket` in the caller policy, and the TS S3
packages as regular (lazily imported) dependencies.
