## ADDED Requirements

### Requirement: Write stores per-file metadata as user.rayito xattrs atomically with the content
`WriteRequest.metadata` (and `StartImportRequest.metadata`) SHALL be accepted only on a message that carries `path` (`INVALID_ARGUMENT` otherwise, like `user` and `mode`) and SHALL be validated before the file's temporary is created: keys of 1–255 HTTP token characters (`!#$%&'*+-.^_`|~`, digits, letters), stored lowercased, two keys equal after lowercasing refused; values of printable ASCII `0x20–0x7E`; at most 64 keys and at most 4 000 bytes counting `user.rayito.` plus key plus value for every entry; a violation SHALL fail with `INVALID_ARGUMENT` and a fixed message that names neither a key nor a value. `rayd` SHALL store each entry as the xattr `user.rayito.<key>` with `fsetxattr` on the temporary file's descriptor after the data is written and before the rename, so content and metadata appear together; since every write creates a new file, an overwrite SHALL replace the whole set and an overwrite without metadata SHALL leave none. `ENOTSUP` from `fsetxattr` SHALL fail the file with `FAILED_PRECONDITION` and details `metadata_unsupported`, and `E2BIG`/`ENOSPC` with `INVALID_ARGUMENT` and details `metadata_too_large`, the temporary removed and the destination untouched in both cases. Metadata keys and values SHALL never be logged; the `Write` log line MAY carry a `metadata_keys` count.

#### Scenario: metadata stored lowercased
- **WHEN** the SDK calls `files.write("/home/user/m9/a.txt", "x", metadata={"Owner": "alice"})` and then `commands.run("python3 -c \"import os; print(os.getxattr('/home/user/m9/a.txt', 'user.rayito.owner'))\"")`
- **THEN** the command prints `b'alice'`

#### Scenario: invalid key refused before the temporary
- **WHEN** a `Write` carries `metadata` with the key `a b`
- **THEN** the RPC fails with `INVALID_ARGUMENT`, no `.rayito-tmp-*` file was created and the message does not contain `a b`

#### Scenario: metadata on a continuation message
- **WHEN** the second message of a file (without `path`) carries `metadata`
- **THEN** the RPC fails with `INVALID_ARGUMENT`

### Requirement: Stat and ListDir report metadata read without following symlinks
`Stat` and every `ListDir` entry SHALL fill `EntryInfo.metadata` with the `user.rayito.*` xattrs of the entry (the prefix removed), read with `llistxattr` and `lgetxattr` under the request's filesystem identity so a symlink's target is never read; `ENOTSUP`, `ENODATA`, `EACCES` and `EPERM` SHALL yield an empty map and SHALL NOT fail the RPC. `EntryInfo.metadata` SHALL be empty for entries without such xattrs. The SDK SHALL expose it as `EntryInfo.metadata` (a read-only mapping, empty by default; TypeScript `metadata: Readonly<Record<string, string>>`), so `WriteInfo.metadata` equals what was written.

#### Scenario: metadata round trip through the SDK
- **WHEN** the SDK writes with `metadata={"Owner": "alice"}`, then calls `files.get_info(path)` and `files.list(parent)`
- **THEN** `get_info(path).metadata == {"owner": "alice"}` and the listed entry for that name carries the same mapping

#### Scenario: overwrite clears
- **WHEN** the same path is written again without `metadata`
- **THEN** `files.get_info(path).metadata == {}`

#### Scenario: symlink not followed
- **WHEN** `/home/user/m9/link` points to a file that has metadata and `Stat` is called on the link
- **THEN** the entry is a symlink and its metadata is empty

### Requirement: gzip message compression with a response opt-in
`rayd` SHALL build with tonic's `gzip` feature and configure `FilesystemService` to accept and send gzip-compressed messages. A tower layer on the server SHALL leave `grpc-accept-encoding` untouched when the request carries the metadata `rayito-compress: gzip` (value compared case-insensitively) and SHALL replace it with `identity` otherwise, so responses are compressed only on request, even for clients that advertise gzip by default. Request compression (`grpc-encoding: gzip`) SHALL be accepted on every `FilesystemService` RPC. Size limits, the 1 MiB chunk rule and deadlines SHALL apply to the uncompressed data.

#### Scenario: response compressed only on request
- **WHEN** the integration test sends two `Read` calls advertising `grpc-accept-encoding: gzip`, the second with `rayito-compress: gzip`
- **THEN** the first response carries no `grpc-encoding: gzip` and the second does, and both deliver the same bytes

#### Scenario: compressed write accepted
- **WHEN** a client sends a `Write` stream with `grpc-encoding: gzip`
- **THEN** the file is committed with the uncompressed bytes

## MODIFIED Requirements

### Requirement: SDK filesystem surface, deadlines and channels
The SDK SHALL expose `sbx.files` with `read(path, format="text"|"bytes"|"stream", gzip=False, stream_idle_timeout=None)`, `write(path, data: str | bytes | IO, mode=None, gzip=False, metadata=None, use_octet_stream=False) -> EntryInfo`, `write_files(files: Sequence[WriteEntry], gzip=False, metadata=None, use_octet_stream=False) -> list[EntryInfo]`, `list(path, depth=1)`, `exists(path)`, `get_info(path)`, `remove(path, recursive=True)`, `rename(old_path, new_path)`, `make_dir(path) -> bool`, `watch_dir(...)`, `upload_url(...)` and `download_url(...)` (capability `sdk-file-transfer`), every method accepting `user` and `request_timeout`, with an identical async surface on `AsyncSandbox.files`. `read` SHALL call `Stat` first and refuse directories and symlinks client-side, then open `Read` with a deadline of `60 s + 1 s per 1 000 000 bytes` of the file size; `write`/`write_files` SHALL materialise the data as bytes, send 1 MiB chunks with `path`/`user`/`mode`/`metadata` only on each file's first message, and use a deadline of `60 s + 1 s per 1 000 000 bytes` of the total; an explicit `request_timeout` SHALL replace both. When `sbx.transfer` is set, payloads at or above its `threshold_bytes` SHALL take the S3 route of `sdk-file-transfer` instead, with the same results and exceptions. `format="text"` SHALL decode strict UTF-8 and raise `InvalidArgumentException` on invalid data. `gzip=True` SHALL add the call metadata `rayito-compress: gzip` to `Read` and open `Write` with gzip request compression; it SHALL be ignored on the S3 route. `metadata` SHALL be validated client-side with the rules of the `Write` metadata requirement (`InvalidArgumentException` before any RPC) and apply to every file of the call. `stream_idle_timeout` (seconds, `None` or `0` = none, negative → `InvalidArgumentException`) SHALL cancel a `read` whose next chunk does not arrive in time and raise `TimeoutException`. `use_octet_stream` SHALL be accepted and have no effect. Unary RPCs, `Write` and the foreground `Read` SHALL use the unary channel; `WatchDir` and `WatchTransfer` SHALL use the stream channel; a sandbox SHALL never open more than two channels. A proxy 403 SHALL be retried once by re-minting the JWE: for `Write` by re-creating the request iterator, for `Read`/`WatchDir` only before the first message.

#### Scenario: stream format
- **WHEN** the SDK calls `files.read(path, format="stream")` on the 8 MB file
- **THEN** it yields at least 30 chunks of at most 262 144 bytes whose concatenation equals the written data

#### Scenario: non-UTF-8 text read
- **WHEN** the SDK calls `files.read("/home/user/m3/big.bin")` with the default `format="text"`
- **THEN** `InvalidArgumentException` is raised

#### Scenario: deadline arithmetic
- **WHEN** the SDK writes 8 000 000 bytes without `request_timeout`
- **THEN** the `Write` RPC deadline is 68 s, and with `request_timeout=5` it is 5 s

#### Scenario: thirty stats stay under the connection cap
- **WHEN** the SDK runs `files.exists(path)` thirty times sequentially
- **THEN** every call returns `True`, no `RateLimitException` is raised and only the unary channel was used

#### Scenario: gzip on the wire
- **WHEN** a unit test calls `files.write(path, data, gzip=True)` and `files.read(path, gzip=True)` against the fake `rayd`
- **THEN** the fake saw the `Write` stream with `grpc-encoding: gzip`, the `Read` with `rayito-compress: gzip`, the bytes round-trip, and still at most two channels exist

#### Scenario: idle stream cut
- **WHEN** the fake `Read` sends one chunk and then stalls, and the SDK reads with `stream_idle_timeout=1`
- **THEN** `TimeoutException` is raised about 1 s after the first chunk and the call was cancelled

#### Scenario: invalid metadata refused locally
- **WHEN** the SDK calls `files.write(path, "x", metadata={"a b": "1"})`
- **THEN** `InvalidArgumentException` is raised and no RPC reached the fake
