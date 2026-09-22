# filesystem Specification

## Purpose
`FilesystemService` (`Read`, `Write`, `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `WatchDir`) as served by `rayd` and exposed by the SDK as `sbx.files`: path policy, per-request filesystem identity, atomic writes, bounded listings and watches, and the `WatchHandle` contract. Accepted against real AWS on 2026-09-15 (change `m3-filesystem`, image `rayito-base` 4.0).
## Requirements
### Requirement: Request paths are normalised and parent references are refused
Every `FilesystemService` request path SHALL be parsed as a `/`-separated Unix path: an empty path or one containing NUL SHALL fail with `INVALID_ARGUMENT`; a relative path SHALL be resolved against the requesting user's home directory; repeated `/`, `.` components and a trailing `/` SHALL be normalised away; any `..` component SHALL fail with `INVALID_ARGUMENT`. `EntryInfo.path` SHALL report the normalised request path (or `<normalised root>/<relative>` for listing entries), never the symlink-resolved path.

#### Scenario: relative path resolves to home
- **WHEN** the SDK calls `files.write("m3/rel.txt", "r")` as the default user
- **THEN** `files.exists("/home/user/m3/rel.txt")` returns `True`

#### Scenario: parent reference refused
- **WHEN** the SDK calls `files.read("/home/user/m3/../../etc/passwd")`
- **THEN** `InvalidArgumentException` is raised and nothing is opened

### Requirement: Deny list on the canonical path
`rayd` SHALL refuse with `PERMISSION_DENIED` any operation whose canonical target (`realpath` of the parent plus the final name; `realpath` of the path itself for `ListDir` and `WatchDir`; the deepest existing ancestor plus the missing components for `Write` and `MakeDir`) starts with a component-wise prefix in `/proc`, `/sys`, `/dev`, `/run/rayito`, `/etc`, `/usr`, `/opt/rayito`, the `rayd` binary path (`/usr/local/bin/rayd`) or the canonical path of the running executable. A listing SHALL never descend into a denied directory, but MAY name it as an entry of its parent.

#### Scenario: reading /etc/passwd
- **WHEN** the SDK calls `files.read("/etc/passwd")`
- **THEN** `AuthenticationException` is raised with `proxy_rejected == False`

#### Scenario: symlinked parent into a denied tree
- **WHEN** `/home/user/link` is a symlink to `/etc` and `Read{path:"/home/user/link/passwd"}` is called
- **THEN** the RPC fails with `PERMISSION_DENIED` before any message

#### Scenario: root listing still names denied directories
- **WHEN** the SDK calls `files.list("/")`
- **THEN** the result contains an entry named `etc` and `files.list("/proc")` raises `AuthenticationException`

### Requirement: Filesystem operations run under the requesting user's identity
Every filesystem syscall issued for a request SHALL run on a blocking thread whose filesystem uid and gid are those of the resolved user (`setfsuid`/`setfsgid`, restored when the call returns) when `rayd` runs as root; when `rayd` is not root it SHALL use its own identity and log one warning at boot. The username SHALL be resolved as in `process-lifecycle` (request `User.username` → `/run` payload `user` → `"user"`; `"root"` refused with `PERMISSION_DENIED` unless `RAYITO_ALLOW_ROOT=1`; unknown → `INVALID_ARGUMENT`). `EACCES`/`EPERM` SHALL map to `PERMISSION_DENIED` and `rayd` SHALL never retry as root.

#### Scenario: written file owned by the user
- **WHEN** the SDK calls `files.write("/home/user/m3/big.bin", data)` and then `commands.run("stat -c %U:%G /home/user/m3/big.bin")`
- **THEN** the output is `user:user`

#### Scenario: root-only directory
- **WHEN** the SDK calls `files.list("/root")` as the default user
- **THEN** `AuthenticationException` is raised because uid 1000 gets `EACCES`

#### Scenario: root refused by policy
- **WHEN** the image does not set `RAYITO_ALLOW_ROOT=1` and the SDK calls `files.write("/root/x", b"x", user="root")`
- **THEN** the RPC fails with `PERMISSION_DENIED` and the SDK raises `AuthenticationException`

### Requirement: Read streams 256 KiB chunks and never follows the final symlink
`Read` SHALL open the file with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`, accept only regular files, and answer with a server-stream of `ReadResponse{chunk}` where every chunk except the last is exactly 256 KiB and the last is the remainder; an empty file SHALL produce zero messages and `OK`. A directory, a symlink, a FIFO or a device SHALL fail with `INVALID_ARGUMENT` before any message; a missing path SHALL fail with `NOT_FOUND`; a read error after the first chunk SHALL end the stream with `INTERNAL`. At most four chunks SHALL be buffered ahead of the client.

#### Scenario: eight megabytes round trip
- **WHEN** the SDK writes 8 000 000 random bytes and reads them back with `files.read(path, format="bytes")` using the default deadline
- **THEN** the sha256 of the data read equals the sha256 of the data written and the read completes within 10 s

#### Scenario: chunk sizes
- **WHEN** a 600 KiB file is read through `Read`
- **THEN** the stream delivers chunks of 262 144, 262 144 and 90 112 bytes and then ends

#### Scenario: symlink refused
- **WHEN** `/home/user/m3/link` is a symlink to `big.bin` and `Read{path:"/home/user/m3/link"}` is called
- **THEN** the RPC fails with `INVALID_ARGUMENT`

### Requirement: Write is a multi-file stream with per-file atomic commits
`Write` SHALL accept a client-stream where a message with `path` starts a new file (committing the previous one) and messages without `path` append to the current file; `user` and `mode` SHALL only be accepted on a message with `path`; the first message without `path` and a stream with zero files SHALL fail with `INVALID_ARGUMENT`; a `chunk` larger than 1 MiB SHALL fail with `INVALID_ARGUMENT`; `mode` above `0o7777` SHALL fail with `INVALID_ARGUMENT` and default to `0o644`. Before creating a file's temporary, after canonicalisation and the deny list, `rayd` SHALL read the free space of the destination directory (`statvfs`, `f_bavail * f_frsize`) and refuse the file with `RESOURCE_EXHAUSTED` and details `disk_reserve` when fewer than 256 MiB are free; `ENOSPC` during a write or commit SHALL map to `RESOURCE_EXHAUSTED` with details `disk_full`. Each file SHALL be written to a temp file named `.rayito-tmp-<random>` in the destination directory (missing parents created with mode `0o755`), then `fsync`, `fchmod(mode)`, `fchown` to the user, `rename` over the final name and `fsync` of the directory. A failure or a cancelled stream SHALL unlink the temp file and leave the destination untouched; files already committed in the same stream SHALL stay committed. Before answering any error `rayd` SHALL keep draining the client's messages for at most 1 MiB or 2 s so the error reaches the client as a gRPC status. The response SHALL list one `EntryInfo` per file in request order. The SDK SHALL raise `DiskFullException` (a `SandboxException`) for both details. Error messages SHALL never include the path or the free-space figure.

#### Scenario: fifty files in one call
- **WHEN** the SDK calls `files.write_files([WriteEntry(f"/home/user/m3/many/f{i:02}.txt", ...) for i in range(50)])`
- **THEN** exactly one `Write` stream is opened, the response has 50 entries whose names are `f00.txt` … `f49.txt` in order, and `ls /home/user/m3/many | wc -l` prints `50`

#### Scenario: mode honoured
- **WHEN** the SDK calls `files.write(path, b"s", mode=0o600)`
- **THEN** `files.get_info(path).mode == 0o600`, `permissions == "-rw-------"` and `stat -c %a` prints `600`

#### Scenario: oversized chunk
- **WHEN** a `WriteRequest` carries a 1 MiB + 1 byte `chunk`
- **THEN** the RPC fails with `INVALID_ARGUMENT` and no `.rayito-tmp-*` file remains in the directory

#### Scenario: destination is a symlink
- **WHEN** the destination path is a symlink to another file and a `Write` completes
- **THEN** the destination becomes a regular file with the new content and the former target is unchanged

#### Scenario: denied destination through the proxy
- **WHEN** the SDK calls `files.write("/usr/local/bin/rayd", b"x")` against a real MicroVM
- **THEN** `AuthenticationException` is raised (the client receives `PERMISSION_DENIED`, not `CANCELLED`)

#### Scenario: disk reserve refused before any temporary
- **WHEN** an integration test's fake filesystem reports 10 MiB free and a `Write` stream begins a second file after a first one committed
- **THEN** the RPC fails with `RESOURCE_EXHAUSTED` and details `disk_reserve`, the first file stays committed, no `.rayito-tmp-*` was created for the second, and the SDK raises `DiskFullException`

#### Scenario: reserve not hit on AWS
- **WHEN** the e2e writes a 1 MiB file on a fresh MicroVM and reads `get_metrics()`
- **THEN** the write succeeds and `disk_total_bytes - disk_used_bytes` exceeds 256 MiB

### Requirement: Stat reports the entry without following symlinks
`Stat` SHALL `lstat` the path and answer `EntryInfo{name, type, path, size, mode, permissions, owner, group, modified_time_unix_ms, symlink_target?}` with `permissions` in the 10-character `ls -l` form, `owner`/`group` as names (numeric ids as strings when unknown), `type` `FILE_TYPE_SYMLINK` with `symlink_target` for symlinks and `FILE_TYPE_UNSPECIFIED` for other kinds. A missing path or a non-directory component SHALL fail with `NOT_FOUND`. The SDK SHALL expose `files.get_info(path) -> EntryInfo` and `files.exists(path) -> bool` (`False` only on `FileNotFoundException`).

#### Scenario: exists on a missing path
- **WHEN** the SDK calls `files.exists("/home/user/m3/missing")`
- **THEN** it returns `False` and `files.get_info("/home/user/m3/missing")` raises `FileNotFoundException`

#### Scenario: symlink info
- **WHEN** `ln -s big.bin /home/user/m3/link` was run and the SDK calls `files.get_info("/home/user/m3/link")`
- **THEN** `type is FileType.SYMLINK` and `symlink_target == "big.bin"`

### Requirement: ListDir walks with depth semantics, sorted, without following symlinks
`ListDir` SHALL treat `depth` `0` as `1` and include an entry iff its depth relative to the root is `<= depth`; entries SHALL be returned in depth-first pre-order with the entries of each directory sorted by name bytes; symlinked directories SHALL be listed as symlinks and never descended; denied subtrees SHALL be skipped; a root that is not a directory SHALL fail with `INVALID_ARGUMENT`, a missing root with `NOT_FOUND`; more than 10 000 entries SHALL fail with `RESOURCE_EXHAUSTED`. The SDK SHALL expose `files.list(path, depth=1)`.

#### Scenario: depth two after write_files
- **WHEN** the SDK calls `files.list("/home/user/m3", depth=2)` after the fifty-file write
- **THEN** the entry `many` (`type is FileType.DIR`) is immediately followed by `many/f00.txt` … `many/f49.txt` in byte order, and `files.list("/home/user/m3")` contains `many` but none of its children

#### Scenario: listing a file
- **WHEN** the SDK calls `files.list("/home/user/m3/big.bin")`
- **THEN** `InvalidArgumentException` is raised

#### Scenario: entry cap
- **WHEN** a listing would exceed the configured entry cap
- **THEN** the RPC fails with `RESOURCE_EXHAUSTED` and no partial listing is returned

### Requirement: MakeDir creates parents and distinguishes the existing kinds
`MakeDir` SHALL create the directory and any missing parents with mode `0o755` and return the new directory's `EntryInfo`; if the final component already is a directory it SHALL fail with `ALREADY_EXISTS`; if it exists as anything else, or a parent component is not a directory, it SHALL fail with `INVALID_ARGUMENT`. The SDK SHALL expose `files.make_dir(path) -> bool`, `True` when created and `False` on `ALREADY_EXISTS`.

#### Scenario: make_dir twice
- **WHEN** the SDK calls `files.make_dir("/home/user/m3/dir")` twice
- **THEN** the first call returns `True` and the second returns `False`

#### Scenario: make_dir on a file
- **WHEN** the SDK calls `files.make_dir("/home/user/m3/big.bin")`
- **THEN** `InvalidArgumentException` is raised

### Requirement: Move uses rename semantics
`Move` SHALL `lstat` the source (`NOT_FOUND` if missing), then `rename(2)` it to the destination, answering the destination's `EntryInfo`; a missing destination parent SHALL fail with `NOT_FOUND`; a destination that conflicts (`EEXIST`, `ENOTEMPTY`, `EISDIR`, `ENOTDIR`) or a cross-device rename SHALL fail with `FAILED_PRECONDITION`. The SDK SHALL expose `files.rename(old_path, new_path) -> EntryInfo`.

#### Scenario: rename a file
- **WHEN** the SDK calls `files.rename("/home/user/m3/hola.txt", "/home/user/m3/dir/hola.txt")`
- **THEN** the returned `path` is the destination, the source no longer exists and the destination reads the same content

#### Scenario: rename onto a non-empty directory
- **WHEN** the destination is a non-empty directory
- **THEN** the SDK raises `InvalidArgumentException`

### Requirement: Remove honours the recursive flag and symlinks
`Remove` SHALL `lstat` the path (`NOT_FOUND` if missing) and unlink files, symlinks and other non-directories; a directory with `recursive=false` SHALL be removed with `rmdir` and fail with `FAILED_PRECONDITION` when not empty; a directory with `recursive=true` SHALL be removed recursively without following symlinks. The SDK SHALL expose `files.remove(path, recursive=True)`.

#### Scenario: non-empty directory without recursive
- **WHEN** the SDK calls `files.remove("/home/user/m3/many", recursive=False)`
- **THEN** `InvalidArgumentException` is raised and the directory still exists

#### Scenario: recursive removal and missing path
- **WHEN** the SDK calls `files.remove("/home/user/m3/many")` and then again
- **THEN** the first call removes the tree and the second raises `FileNotFoundException`

#### Scenario: removing a symlink to a directory
- **WHEN** the SDK calls `files.remove("/home/user/m3/link")` where `link` points to `big.bin`
- **THEN** only the link is removed and `files.exists("/home/user/m3/big.bin")` stays `True`

### Requirement: WatchDir emits WatchStarted only after the watch is installed
`WatchDir` SHALL validate the path first and fail with `NOT_FOUND` (missing), `INVALID_ARGUMENT` (not a directory) or `PERMISSION_DENIED` as a gRPC status before any message; it SHALL install an inotify watch through `notify` 8.x without debouncing and without following symlinks while walking a recursive watch, and only then send `WatchStarted` as the first message. Events that occur after installation and before the client reads `WatchStarted` SHALL not be lost. The SDK's `files.watch_dir(...)` SHALL block until `WatchStarted` and return a `WatchHandle`.

#### Scenario: missing directory
- **WHEN** the SDK calls `files.watch_dir("/home/user/m3/nope")`
- **THEN** `FileNotFoundException` is raised and no `WatchHandle` is created

#### Scenario: file created right after installation
- **WHEN** the integration test installs a watch, creates a file before polling the stream, then reads the stream
- **THEN** the first message is `WatchStarted` and a `CREATE` event for that file follows

### Requirement: Watch events carry relative names and the E2B event types
Each `FilesystemEvent` SHALL carry `name` relative to the watched directory (`sub/n.txt` in recursive mode, never absolute) and `type` mapped as create → `CREATE`, data modification → `WRITE`, metadata change → `CHMOD`, deletion → `REMOVE`, rename → `RENAME` (one event per affected name, `from` before `to`); close-write events SHALL not be surfaced; events whose name starts with `.rayito-tmp-` SHALL be dropped, so a `Write` RPC into a watched directory surfaces as a single `RENAME` of the destination name. When `include_entry` is true, `entry` SHALL be filled for `CREATE`, `WRITE` and `CHMOD` if the `lstat` at emission time succeeds.

#### Scenario: shell create, write, remove
- **WHEN** `files.watch_dir("/home/user/m3/watch")` is active and `commands.run` executes `echo hola > a.txt; sleep 0.3; echo mas >> a.txt; sleep 0.3; rm a.txt` inside that directory
- **THEN** within 5 s `get_new_events()` has yielded `("a.txt", CREATE)`, then at least one `("a.txt", WRITE)`, then `("a.txt", REMOVE)` in that relative order, with no name containing `/` or starting with `.rayito-tmp-`

#### Scenario: atomic write and chmod with entries
- **WHEN** a watch with `include_entry=True` is active and the SDK calls `files.write(".../watch/w.txt", b"w")` and then `commands.run("chmod 600 .../watch/w.txt")`
- **THEN** a `("w.txt", RENAME)` event arrives, no `.rayito-tmp-` name appears, and a `("w.txt", CHMOD)` event arrives with `entry.mode == 0o600`

#### Scenario: recursive subdirectory
- **WHEN** a watch with `recursive=True` is active and `mkdir sub && echo x > sub/n.txt` runs inside the directory
- **THEN** events `("sub", CREATE)` and `("sub/n.txt", CREATE)` arrive within 5 s

### Requirement: Watch streams are bounded, kept alive and released on drop
Each watch SHALL queue at most 1024 raw events; when the queue is full `rayd` SHALL stop enqueuing, deliver the buffered events and end the stream with `RESOURCE_EXHAUSTED`. An inotify queue overflow SHALL end the stream the same way; removal of the watched directory SHALL end it with `NOT_FOUND`. A silent watch stream SHALL emit `KeepAlive` every 50 s. Dropping the client stream SHALL remove the inotify watch immediately. At most 64 watches SHALL be live per sandbox; the next `WatchDir` SHALL fail with `RESOURCE_EXHAUSTED`. `WatchDir`, `Read` and `Write` SHALL fail with `UNAVAILABLE` and details `suspending` or `terminating` while the session phase is `Suspending` or `Terminating`. On the first `/suspend` of a cycle every live `WatchDir` and `Read` stream SHALL end with the gRPC status `UNAVAILABLE` and details `suspending` (the inotify watch is removed with the stream), and an in-flight `Write` SHALL be aborted with the same status, its temporary file removed and the destination untouched, draining at most 200 ms of the remaining body.

#### Scenario: stalled client overflows the queue
- **WHEN** the integration test configures a queue capacity of 4, produces more events than that without reading, then reads
- **THEN** the buffered events arrive and the stream ends with `RESOURCE_EXHAUSTED`

#### Scenario: watch closed by a suspend
- **WHEN** a `WatchDir` stream is open and `/suspend` is posted
- **THEN** the stream ends with `UNAVAILABLE` and details `suspending` within 2 s and the live-watch count drops to zero

#### Scenario: write aborted by a suspend
- **WHEN** a `Write` client-stream is mid-body when `/suspend` is posted
- **THEN** the RPC fails with `UNAVAILABLE suspending`, no `.rayito-tmp-*` file remains in the destination directory and the destination file does not exist

### Requirement: SDK WatchHandle contract
`WatchHandle` SHALL consume the stream in a background daemon thread (`asyncio.Task` for `AsyncWatchHandle`), call `on_event(event)` for each event when given (an exception inside the callback is logged and does not stop the watch), otherwise collect events for `get_new_events()`, ignore `keepalive`, and on stream end store the terminal exception (`None` after `stop()`) and call `on_exit(exc)` when given. When the stream ends with `UNAVAILABLE suspending`, a connection reset, `GOAWAY` or EOF while not stopped, the handle SHALL run the sandbox's reconnection poll and, on success, re-issue `WatchDir` with the same `path`, `recursive`, `include_entry`, `user` and the remaining deadline, wait for `WatchStarted`, keep the same event state and continue without calling `on_exit` (`is_running` stays `True`; events raised while suspended are not replayed); when the poll fails the terminal exception is stored as before. `get_new_events()` SHALL return the undelivered events or raise the stored exception once; `stop()` SHALL cancel the RPC, wait for the consumer (≤ 5 s) and be idempotent; `is_running` SHALL be `False` afterwards; `Sandbox.close()` SHALL stop every live handle. A `timeout > 0` SHALL become the gRPC deadline and surface as `TimeoutException`; `timeout` `0` or `None` SHALL mean no deadline.

#### Scenario: stop is clean
- **WHEN** the SDK calls `h.stop()` on a running handle
- **THEN** `h.is_running` is `False` and `h.get_new_events()` returns `[]` without raising

#### Scenario: watch survives a pause
- **WHEN** a `WatchHandle` on `/home/user` is live across `pause()` and `resume()` on real AWS and a file is written afterwards
- **THEN** within 10 s `get_new_events()` contains an event for that file name and `is_running` is `True`

#### Scenario: watch re-issue observed by the fake
- **WHEN** a unit test suspends and resumes the fake `rayd` with a live handle
- **THEN** the fake received two `WatchDir` requests with identical `path`, `recursive`, `include_entry` and `user`, and `on_exit` was not called

### Requirement: SDK filesystem surface, deadlines and channels
The SDK SHALL expose `sbx.files` with `read(path, format="text"|"bytes"|"stream")`, `write(path, data: str | bytes | IO, mode=None) -> EntryInfo`, `write_files(files: Sequence[WriteEntry]) -> list[EntryInfo]`, `list(path, depth=1)`, `exists(path)`, `get_info(path)`, `remove(path, recursive=True)`, `rename(old_path, new_path)`, `make_dir(path) -> bool` and `watch_dir(...)`, every method accepting `user` and `request_timeout`, with an identical async surface on `AsyncSandbox.files`. `read` SHALL call `Stat` first and refuse directories and symlinks client-side, then open `Read` with a deadline of `60 s + 1 s per 1 000 000 bytes` of the file size; `write`/`write_files` SHALL materialise the data as bytes, send 1 MiB chunks with `path`/`user`/`mode` only on each file's first message, and use a deadline of `60 s + 1 s per 1 000 000 bytes` of the total; an explicit `request_timeout` SHALL replace both. `format="text"` SHALL decode strict UTF-8 and raise `InvalidArgumentException` on invalid data. Unary RPCs, `Write` and the foreground `Read` SHALL use the unary channel; `WatchDir` SHALL use the stream channel; a sandbox SHALL never open more than two channels. A proxy 403 SHALL be retried once by re-minting the JWE: for `Write` by re-creating the request iterator, for `Read`/`WatchDir` only before the first message.

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

### Requirement: Filesystem logging hygiene
`rayd` SHALL log only `rpc`, counts (`files`, `bytes`, `chunks`, `entries`, `events`, `live_watches`), `depth`, `recursive`, durations, errno names and outcomes for filesystem events, and SHALL never log a path, an entry name, a symlink target, file contents or a chunk.

#### Scenario: denied read log
- **WHEN** `Read` is refused by the deny list
- **THEN** the log line contains `rpc="Read"` and `outcome="permission_denied"` and no part of the path, while the gRPC message returned to the client is a fixed sentence without the path

### Requirement: Image provides the acceptance tooling
The image SHALL fail its build if `sha256sum`, `stat`, `rm`, `mv`, `mkdir`, `chmod`, `touch` or `ln` are missing from the child `PATH`, SHALL keep `rayd` running as root and SHALL NOT set `RAYITO_ALLOW_ROOT`.

#### Scenario: identity check from the shell
- **WHEN** the e2e runs `commands.run("stat -c %U:%G /home/user/m3/big.bin")`
- **THEN** the command exits 0 and prints `user:user`

