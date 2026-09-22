## Context

State after M2 (accepted 2026-09-15 against real AWS, image `rayito-base`
3.0): `rayd` serves `HealthService` and `ProcessService` behind the
`AccessTokenLayer`; `FilesystemService`, `PtyService` and `CodeService` are
`Pending*` stubs answering `UNIMPLEMENTED`. `rayd` runs as root (capped
capabilities, no `CAP_SYS_RESOURCE`) and spawns children as `user` (uid 1000)
through `TokioProcessSpawner` with `IdentitySwitch::{Enforce, KeepCurrent}`,
`NixUserLookup` and `UserPolicy` (`RAYITO_ALLOW_ROOT=1`). The Python SDK has
`Sandbox`/`AsyncSandbox` with `commands`, two gRPC channels (unary + long
streams), `_call_unary` (one proxy-403 re-mint), `_open_stream` (same for the
first stream message), `translate_rpc_error(filesystem=...)` already mapping
`NOT_FOUND` to `FileNotFoundException`, and an in-process fake `rayd` for
unit tests. `Sandbox.files` raises `NotImplementedError`.

Measured facts that shape this design (`AWS_API_NOTES.md` §7, §15, §16):

- Endpoint bandwidth at 2 GB is **4 MB/s per direction** (Q27: 4.54 MB/s
  measured downloading 16 MiB; M2: 3 MB of stdout in 0.75 s). Uploading
  through the proxy has not been measured yet: M3 measures it.
- A stream with no bytes does not count as endpoint traffic (Q15); a watch
  stream therefore needs `KeepAlive` messages to keep the VM out of its idle
  policy, and a silent stream survived > 180 s without any cut (M0) and 30 s
  with pings (Q31).
- An early trailers-only answer written before the request body is consumed
  can reach the client as `RST_STREAM(CANCEL)` → grpcio `CANCELLED` (Q29).
  `Write` is the first client-streaming RPC and must drain before rejecting.
- inotify state survives a snapshot (§15); the suspend/resume checklists for
  streams are M5 and out of scope here, as in M2.
- Inside the VM `rayd` is root with capped capabilities; `setfsuid`/
  `setfsgid` need no capability beyond what root already has (M0 Q20 lists
  only `sys_admin`, `net_admin` and `sys_ptrace` as missing, and M2's
  `setuid` in `pre_exec` works).

Constraints: `openspec/project.md` hard rules (no invented AWS parameters,
`.proto` is the source of truth, hexagonal boundaries, ARM64 musl, security
defaults ship now, `clippy` pedantic, no `unwrap` outside tests, identifiers
in English, no inline comments in bodies, never log paths, names, file
contents or tokens).

## Goals / Non-Goals

**Goals:**

- `FilesystemService` complete and correct on real AWS with its final
  security posture (path policy, filesystem identity, atomic writes, bounded
  memory per RPC and per watch).
- Python SDK `.files` with E2B naming, sync and async identical, per-request
  deadline `60 s + 1 s/MB`.
- Every decision below is closed so implementers never guess; every rule has
  a host-side unit test, a `cfg(unix)` integration test, an SDK unit test
  against the fake, or an e2e assertion.

**Non-Goals:**

- Suspend/resume behaviour for `Write` and `WatchDir` (aborting an in-flight
  `Write` on `/suspend`, re-emitting `WatchDir` after `resume_generation`
  changes): M5. M3 only guarantees that a dropped or cancelled `Write`
  leaves no temp file, which is what M5's abort will rely on.
- Debounced or coalesced watch events (E2B parity is raw events).
- Recursive `Read` of directories, archive upload/download, glob/search,
  `chmod`/`chown` RPCs, symlink creation (not in the proto).
- Following symlinks on the final component of `Read`/`Write` (explicitly
  refused; `Stat` reports the link).
- Cross-device `Move` (the VM has one root filesystem).
- Persistence across sessions (S3/EFS), TypeScript client (M6).
- Any change to `create-microvm-image`/`run-microvm` parameters.

## Decisions

### D1. Proto usage: no edits

`filesystem.proto` and `common.proto` already carry every field M3 needs.
Wire mapping used by both sides:

| SDK call | RPC | Request | Response / notes |
|---|---|---|---|
| `files.read(path, format=...)` | `Stat` then `Read` (server-stream) | `StatRequest{path, user?}`, `ReadRequest{path, user?}` | `ReadResponse{chunk}` × N, each ≤ 256 KiB, last may be shorter; empty file → zero messages then OK |
| `files.write(path, data, mode?)` | `Write` (client-stream) | one file: first `WriteRequest{path, user?, mode?, chunk}` then `WriteRequest{chunk}` × N | `WriteResponse{entries:[EntryInfo]}` |
| `files.write_files([...])` | `Write` | file *i*: `WriteRequest{path_i, user?, mode_i?, chunk}` then chunks; a message **with** `path` closes the previous file | `entries` in request order |
| `files.get_info(path)` / `files.exists(path)` | `Stat` | `StatRequest{path, user?}` | `StatResponse{entry}`; `NOT_FOUND` → `exists() == False` |
| `files.list(path, depth)` | `ListDir` | `ListDirRequest{path, depth, user?}` | `ListDirResponse{entries}` |
| `files.make_dir(path)` | `MakeDir` | `MakeDirRequest{path, user?}` | `MakeDirResponse{entry}`; `ALREADY_EXISTS` → `False` |
| `files.rename(old, new)` | `Move` | `MoveRequest{source, destination, user?}` | `MoveResponse{entry}` of the destination |
| `files.remove(path, recursive)` | `Remove` | `RemoveRequest{path, recursive, user?}` | `RemoveResponse{}` |
| `files.watch_dir(path, ...)` | `WatchDir` (server-stream) | `WatchDirRequest{path, recursive, user?, include_entry}` | `WatchStarted` first, then `FilesystemEvent` / `KeepAlive` |

Field semantics fixed here (the proto comments stay as they are):

- `WriteRequest.chunk` larger than 1 MiB → `INVALID_ARGUMENT` from `rayd`
  ("chunk exceeds 1 MiB"). tonic's default 4 MiB decode limit stays as the
  outer guard; a conforming client never reaches it, so the proto's "> 4 MiB
  → `INVALID_ARGUMENT`" is realised by the 1 MiB rule.
- `WriteRequest.mode`: file mode bits, validated `<= 0o7777`, default
  `0o644`; applied with `fchmod` after creation so the umask does not
  interfere.
- `ListDirRequest.depth`: `0` means `1`; an entry at relative depth *d*
  (1 = direct child) is included iff `d <= depth`.
- `EntryInfo.path` is the normalised request path for `Stat`/`MakeDir`/
  `Move`/`Write`, and `<normalised root>/<relative>` for `ListDir` entries;
  never the canonical (symlink-resolved) path. `name` is the final component
  (`/` for the root). `permissions` is the 10-character `ls -l` form
  (`-rw-r--r--`, `drwxr-xr-x`, `lrwxrwxrwx`). `mode` is `st_mode & 0o7777`.
  `owner`/`group` are names resolved from the image's user database with the
  numeric id as a string when unknown. `type` is `FILE_TYPE_UNSPECIFIED` for
  anything that is not a regular file, directory or symlink (FIFO, socket,
  device). `symlink_target` is set only for symlinks (`readlink`, verbatim).
  `modified_time_unix_ms` from `st_mtime`.
- `FilesystemEvent.name` is relative to the watched directory (`a.txt`,
  `sub/b.txt` in recursive mode), `/`-separated, never absolute.
  `FilesystemEvent.entry` is filled only when `include_entry` is true and
  the type is `CREATE`, `WRITE` or `CHMOD`, and only if the `lstat` at
  emission time succeeds.
- `WatchDirResponse.keepalive` every **50 s** of silence (this stream only;
  process streams keep 30 s). `common.proto` says "30 s by default"; the
  WatchDir value is documented in `ARCHITECTURE.md`, no proto edit.
- `User` on every request: `username` resolved like M2 (`request` →
  `/run` payload `user` → `"user"`; `"root"` only with `RAYITO_ALLOW_ROOT=1`
  → otherwise `PERMISSION_DENIED`; unknown → `INVALID_ARGUMENT`).

Alternatives considered: adding `StreamError` to `WatchDirResponse` and
`ReadResponse` for in-stream errors (rejected: a trailing gRPC status is
standard for server streams, the SDK already handles it, and it avoids a
proto change and regeneration in this milestone); a `WatchOverflow` message
(same reason).

### D2. Path model and deny list (`rayd-core::filesystem::path`)

All path rules are pure string logic over `/`-separated Unix paths (never
`std::path::Path`, whose semantics differ on the Windows host where the
domain tests run).

`RequestPath::parse(raw: &str, home: &str) -> Result<RequestPath,
PathRejection>`:

1. empty → `Empty`; contains NUL → `ContainsNul`.
2. relative → joined to `home` (E2B behaviour: `files.read("data.csv")`
   reads `/home/user/data.csv`).
3. normalised lexically: repeated `/` collapsed, `.` components dropped,
   trailing `/` removed (except the root `/`).
4. any `..` component → `ParentReference` (refused; lexical `..` is unsafe
   with symlinks and E2B does not need it).

`RequestPath` exposes `as_str()`, `parent()` (`None` for `/`), `name()`
(`"/"` for the root) and `join(relative)`.

`DenyList::new(extra: Vec<String>)` holds `DEFAULT_DENIED_PREFIXES =
["/proc", "/sys", "/dev", "/run/rayito", "/etc", "/usr", "/opt/rayito"]`
plus `RAYD_BINARY = "/usr/local/bin/rayd"` and, at boot, the canonical
`std::env::current_exe()` (so a dev build under `target/` is covered too).
`check(canonical: &str) -> Result<(), FilesystemError::Denied>` matches by
whole components (`/etc` and `/etc/x` denied, `/etcetera` allowed).

Where the check applies: on the **canonical** path, never on the request
string. For `Read`, `Write`, `Stat`, `MakeDir`, `Move` (source and
destination), `Remove`: `canonical = realpath(parent) + "/" + name`. For
`ListDir` and `WatchDir`: `canonical = realpath(path)` (a symlink to a
directory as the root is followed on purpose: the user asked to look inside
it; the canonical target is what gets checked). `realpath` failing with
`ENOENT` → `NotFound`, `ENOTDIR` → `NotFound`, `EACCES` → `PermissionDenied`
(the realpath runs under the requesting user's identity, D3). For `Write`
and `MakeDir` the parent may not exist yet: the deepest existing ancestor is
canonicalised, the missing components are appended lexically and the deny
check runs on that result before anything is created. A listing or a walk
never descends into a denied directory (the check is repeated on each
subdirectory's canonical path before `read_dir`), but a listing of `/` still
names `etc`, `usr`, `proc` as entries: names are not secret, contents are.

Final component: `Read` opens with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC` and
`ELOOP` maps to `INVALID_ARGUMENT` ("path is a symlink"). `Write` never opens
the destination: it creates a temp file with `O_CREAT | O_EXCL` in the
canonical parent and `rename(2)`s over the final name, which replaces a
symlink with the new file without following it. `Stat` uses `lstat`.
`MakeDir` uses `mkdir` (does not follow). `Remove` uses `unlink`, `rmdir` or
a symlink-safe recursive removal (`std::fs::remove_dir_all`, which does not
follow symlinks and removes a symlink root as a link). `Move` uses `rename`
(does not follow either name).

Alternatives considered: `openat`-walking every component with `O_PATH |
O_NOFOLLOW` to close the TOCTOU between `realpath` and the operation
(rejected for M3: the filesystem identity of D3 already makes a swapped
symlink harmless, because the operation runs with uid 1000's rights; a
race can only reach what uid 1000 could reach anyway, and the deny list is
defence in depth on top of that).

### D3. Filesystem identity: `setfsuid`/`setfsgid` per blocking call

`rayd` is root. A lexical deny list alone cannot stop uid 1000 from using
`Read` to open `/home/other/secret` or `Write` to overwrite a root-owned
file outside the list. Decision: every filesystem syscall issued on behalf
of a request runs on a `tokio::task::spawn_blocking` thread whose
**filesystem identity** is the requesting user's, set with
`nix::unistd::setfsuid(uid)` and `setfsgid(gid)` (Linux-only, per-thread,
plain syscalls under musl, never broadcast; `CAP_SETUID` is available to
root in the VM) and restored to the previous values by an RAII guard
(`FsIdentityGuard`) when the closure returns, so the blocking pool never
leaks an identity to the next task (metrics probe, other users).
Supplementary groups are **not** switched (`setgroups` is process-wide
under musl's `__synccall`); the kernel therefore checks `fsuid`, `fsgid` and
root's empty supplementary set, which for `user` (uid 1000, gid 1000,
groups `[1000]`) is equivalent. Documented as a known limitation for users
with extra groups.

`IdentitySwitch` from M2 is reused: `Enforce` (root) switches, `KeepCurrent`
(dev box, CI as non-root) runs everything as the current user and skips
`fchown`. The identity comes from `NixUserLookup` (M2) through
`resolve_username` + `UserPolicy::authorize` + `authorize_identity`.

Consequences fixed here:

- `EACCES`/`EPERM` from any call → `PERMISSION_DENIED`. `rayd` never falls
  back to root to "make it work".
- Files and directories created by `Write` and `MakeDir` are owned by the
  requesting user because they are created under that identity; `Write`
  additionally calls `fchown(uid, gid)` on the temp file before the rename
  (`Enforce` only) so a setgid parent directory cannot change the group,
  which is the "chown to user" of `MILESTONES.md`.
- `realpath`, `lstat`, `read_dir`, `open`, `mkdir`, `rename`, `unlink`,
  `rmdir` and the inotify installation all run under the guard. The reads
  after `open` (pumping chunks) and the writes after the temp file is open do
  not need the identity and run as plain blocking calls on the same file
  descriptor.

Alternative considered: a per-request helper process running as the user
(rejected: fork per RPC, and stream pumping across processes).

### D4. `rayd-core` domain: `filesystem` module

New module `crates/rayd-core/src/filesystem/` (no `tokio`, `tonic`, `nix`,
`notify`; `std` only):

```text
mod.rs        pub use of the types below; constants:
              READ_CHUNK_BYTES = 262_144, MAX_WRITE_CHUNK_BYTES = 1_048_576,
              DEFAULT_FILE_MODE = 0o644, DEFAULT_DIR_MODE = 0o755, MODE_MASK = 0o7777,
              TEMP_PREFIX = ".rayito-tmp-", MAX_LIST_ENTRIES = 10_000
path.rs       RequestPath, PathRejection{Empty, ContainsNul, ParentReference}, DenyList, DEFAULT_DENIED_PREFIXES, RAYD_BINARY
entry.rs      EntryKind{File, Directory, Symlink, Other}, RawEntry{name, kind, size, mode, uid, gid, modified_ms, symlink_target}
              Entry{name, kind, path, size, mode, permissions, owner, group, modified_ms, symlink_target}
              permissions_string(kind, mode) -> String, NameCache<'a>(&'a dyn NameResolver + two HashMaps), build_entry(raw, path, names: &mut NameCache) -> Entry
identity.rs   FsIdentity{uid, gid, home} (From<&ProcessIdentity>)
listing.rs    effective_depth(u32) -> u32, ListingLimits{max_entries}, walk_listing(fs, id, deny, root: &RequestPath, canonical_root, depth, limits, names) -> Result<Vec<Entry>, FilesystemError>
              (DFS pre-order, entries of one directory sorted by name bytes, never descends into symlinks or denied directories, stops with TooManyEntries)
write.rs      WriteMessage{path: Option<String>, user: Option<String>, mode: Option<u32>, chunk_len: usize}
              WriteSession: accept(msg) -> Result<Vec<WriteStep>, FilesystemError>, finish() -> Result<Option<WriteStep>, FilesystemError>
              WriteStep{Begin{path, user, mode}, Append{len}, Commit}
              rules: first message without path → MissingPath; chunk_len > MAX → ChunkTooLarge; mode > MODE_MASK → InvalidMode; stream with zero files → NoFiles;
              a message with path emits [Commit (if a file is open), Begin]; user or mode on a message without path → UserWithoutPath / InvalidArgument
events.rs     WatchEventKind{Create, Write, Remove, Rename, Chmod}, WatchEvent{name, kind}
              RawWatchKind{Create, DataModified, MetadataModified, RenameFrom, RenameTo, RenameBoth, Removed, RootGone, QueueOverflow, Other}
              RawWatchEvent{kind, paths: Vec<String>}
              WatchTranslator{canonical_root}: translate(raw) -> Translation{Events(Vec<WatchEvent>) | End(WatchEnd::RootGone | WatchEnd::Overflow) | Ignore}
              rules: relative_name strips canonical_root + "/" (outside root → dropped); names whose last component starts with TEMP_PREFIX → dropped;
              Create→Create, DataModified→Write, MetadataModified→Chmod, Removed→Remove, RenameFrom/RenameTo→Rename (one per path), RenameBoth→two Rename (from, to), Other→Ignore
ops.rs        FilesystemOps<'a, F: FileSystem>{fs, deny, names}: stat, list_dir, make_dir, rename, remove, prepare_read, write_target, prepare_watch (pure orchestration over the port)
ports.rs      trait FileSystem, trait WriteSink, trait Watcher, trait WatchSubscription, trait NameResolver, FsIoError, WatchError (below)
error.rs      FilesystemError (thiserror): InvalidPath(PathRejection), Denied, NotFound, AlreadyExists, NotADirectory, IsADirectory, NotARegularFile, IsSymlink,
              NotEmpty, DestinationConflict, CrossDevice, PermissionDenied, ChunkTooLarge{max}, InvalidMode(u32), MissingPath, UserWithoutPath, NoFiles,
              TooManyEntries{max}, TooManyWatches{max}, WatchLimitReached, WatchOverflow, WatchRootGone, RootNotAllowed, UnknownUser, UserLookupFailed(String),
              NotAcceptingStreams{phase}, Unsupported, Io{operation: &'static str, errno: String}
              (Display strings never contain a path, a name or file bytes)
```

Ports (sync functions, plain data, `std::io::Read` allowed):

```rust
pub trait FileSystem: Send + Sync {
    fn canonicalize(&self, id: &FsIdentity, path: &str) -> Result<String, FsIoError>;
    fn lstat(&self, id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError>;
    fn read_dir(&self, id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError>;   // lstat of each child, unsorted
    fn open_read(&self, id: &FsIdentity, path: &str) -> Result<Box<dyn std::io::Read + Send>, FsIoError>; // O_NOFOLLOW, regular files only
    fn begin_write(&self, id: &FsIdentity, dir: &str, mode: u32) -> Result<Box<dyn WriteSink>, FsIoError>; // create missing parents + temp file
    fn make_dir(&self, id: &FsIdentity, path: &str, mode: u32) -> Result<(), FsIoError>;      // mkdir -p semantics; AlreadyExists only when the final component exists
    fn rename(&self, id: &FsIdentity, from: &str, to: &str) -> Result<(), FsIoError>;
    fn remove(&self, id: &FsIdentity, path: &str, kind: EntryKind, recursive: bool) -> Result<(), FsIoError>;
}
pub trait WriteSink: Send {
    fn write_chunk(&mut self, bytes: &[u8]) -> Result<(), FsIoError>;
    fn commit(self: Box<Self>, final_name: &str, id: &FsIdentity) -> Result<RawEntry, FsIoError>; // fsync, fchmod, fchown, rename, fsync(dir), lstat
}   // Drop without commit unlinks the temp file
pub trait Watcher: Send + Sync {
    fn watch(&self, id: &FsIdentity, canonical_root: &str, recursive: bool,
             sink: Box<dyn Fn(RawWatchEvent) + Send + Sync>) -> Result<Box<dyn WatchSubscription>, WatchError>;
}
pub trait WatchSubscription: Send {}   // Drop removes the watch
pub trait NameResolver: Send + Sync {
    fn user_name(&self, uid: u32) -> Option<String>;
    fn group_name(&self, gid: u32) -> Option<String>;
}
pub enum FsIoError { NotFound, PermissionDenied, AlreadyExists, NotADirectory, IsADirectory, NotEmpty, NotARegularFile, IsSymlink, CrossDevice, NoSpace, Unsupported, Other { errno: String } }
pub enum WatchError { NotFound, NotADirectory, PermissionDenied, LimitReached, Unsupported, Other(String) }
```

`FilesystemOps` is the pure orchestration over the port, called from inside
one `spawn_blocking` per RPC. Each method starts with
`RequestPath::parse(raw, identity.home)` and the deny check of D2, so the
host tests exercise the whole rule set against an in-memory
`FakeFileSystem` (a tree of `RawEntry` + bytes + symlink targets, with a
`canonicalize` that resolves the fake links and a per-path permission flag
that makes `lstat`/`open_read` answer `PermissionDenied`).

`NameCache` wraps a `NameResolver` with two `HashMap`s so a 10 000-entry
listing costs at most one lookup per distinct uid/gid.

### D5. Read: 256 KiB chunks through a 4-deep pipeline

`FilesystemManager::read(request)`: `spawn_blocking` → `FilesystemOps::
prepare_read` (identity guard inside the adapter) returns the opened reader
or a `FilesystemError` that becomes the gRPC status before any message
(`NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_ARGUMENT` for directories,
symlinks, FIFOs, devices and bad paths). Then one `spawn_blocking` reader
task loops `read` into a fresh 256 KiB `BytesMut`, sends each non-empty read
as `ReadResponse{chunk}` into a `tokio::sync::mpsc::channel(4)` with
`blocking_send`, and stops at EOF (channel closed → stream ends `OK`) or on
a read error (`Err(Status::internal("read failed: <errno name>"))` as the
trailing status). A client that stops reading blocks the reader through
HTTP/2 flow control and the 4-slot channel (≤ 1 MiB buffered per stream);
a client that cancels drops the receiver and the reader exits on the next
`blocking_send` error. The reader fills each buffer with `read_exact`-style
looping until 256 KiB or EOF, so a file of 600 KiB yields 256 KiB, 256 KiB,
88 KiB. Empty file → zero messages, `OK`.

No `KeepAlive` on `Read`: a read either moves bytes or ends.

### D6. Write: one stream, N files, each atomic

`FilesystemManager::write(stream)` consumes `Streaming<WriteRequest>` and
feeds each message to the core `WriteSession`:

- `Begin{path, user, mode}`: resolve the user (`user` may only travel on a
  message that carries `path`; each file may name a different user),
  `spawn_blocking` → `FilesystemOps::write_target` (parse, canonical parent
  per D2, deny check, `begin_write`). The adapter creates missing parents
  with `mkdir` 0o755 one component at a time from the deepest existing
  ancestor (owned by the requesting user because they are created under
  its identity), then `tempfile::Builder::new().prefix(TEMP_PREFIX)
  .tempfile_in(parent)` (`O_CREAT | O_EXCL`, random suffix).
- `Append{len}`: `spawn_blocking` → `write_all` of the chunk on the sink
  (no identity needed). The first chunk travels in the `Begin` message and is
  written right after the sink exists; an empty first chunk creates an empty
  file.
- `Commit`: `spawn_blocking` → `sync_all` (fsync of the file), `fchmod(mode)`,
  `fchown(uid, gid)` (`Enforce` only), `persist` (rename over the final
  name, replacing a regular file or a symlink, refusing a directory with
  `EISDIR` → `IsADirectory`), `fsync` of the parent directory, `lstat` of the
  final path → `RawEntry` → `Entry` appended to `entries`.
- End of stream: `finish()` commits the open file; zero files → `NoFiles` →
  `INVALID_ARGUMENT`.

Errors: any failure returns the status of D10 for the whole RPC. Files
already committed stay committed (per-file atomicity, not per-call; the SDK
documents it); the open temp file is unlinked by the sink's `Drop`, which
also runs when the client cancels the stream or the connection resets
(tonic drops the `Streaming` and the manager's future). Before answering
any error, the manager keeps consuming and discarding the client's remaining
messages for at most 1 MiB or 2 s (`WRITE_ERROR_DRAIN_BYTES`,
`WRITE_ERROR_DRAIN_TIMEOUT`), the same mitigation as the token layer, so the
proxy delivers the trailers instead of `RST_STREAM(CANCEL)` (Q29).

The temp file name starts with `.rayito-tmp-`; the watch translator drops
events whose name starts with it (D8), so a `Write` into a watched
directory surfaces as a single `RENAME` of the destination name (the
`MOVED_TO`), never as a burst of `WRITE`s on the temp name. This is the
honest consequence of atomic writes and is documented in the SDK docstring.

Memory bound per `Write` stream: one 1 MiB chunk in flight plus tonic's
buffering. No `KeepAlive` (client-stream; the client is the one sending).

### D7. Stat, ListDir, MakeDir, Move, Remove

Each is one `spawn_blocking` around the corresponding `FilesystemOps` call.

- `Stat`: `lstat` → `Entry`. Missing path or a non-directory component →
  `NOT_FOUND`; `EACCES` → `PERMISSION_DENIED`.
- `ListDir`: `effective_depth`, canonical root (must be a directory else
  `INVALID_ARGUMENT`), `walk_listing` (D4). More than 10 000 entries →
  `RESOURCE_EXHAUSTED` ("listing exceeds 10000 entries; reduce depth").
  Children whose `lstat` fails mid-walk (race with the sandbox) are skipped.
- `MakeDir`: parents created (0o755) then the final `mkdir` 0o755. Final
  component already a directory → `ALREADY_EXISTS`; already a file or a
  symlink → `INVALID_ARGUMENT` ("exists and is not a directory"); returns
  the `Entry` of the new directory. An existing parent component that is a
  file → `INVALID_ARGUMENT` (`ENOTDIR`).
- `Move`: `lstat(source)` first (`NOT_FOUND` names the source), then
  `rename(source, destination)`. Destination parent missing → `NOT_FOUND`;
  `EEXIST`/`ENOTEMPTY`/`EISDIR`/`ENOTDIR` on the destination →
  `FAILED_PRECONDITION` ("destination conflicts with an existing entry");
  `EXDEV` → `FAILED_PRECONDITION` ("cross-device move"); returns the
  destination `Entry`. `rename(2)` replacing an existing regular file with a
  regular file is allowed (atomic replace, like `mv`).
- `Remove`: `lstat` decides the kind. File/symlink/other → `unlink`.
  Directory with `recursive=false` → `rmdir` (`ENOTEMPTY` →
  `FAILED_PRECONDITION` "directory not empty; use recursive"). Directory with
  `recursive=true` → symlink-safe recursive removal. Missing → `NOT_FOUND`.
  Removing the sandbox home itself is allowed (it is the user's).

### D8. WatchDir with `notify` 8.x

`NotifyWatcher` (`cfg(unix)`) implements the `Watcher` port with
`notify::RecommendedWatcher` (inotify on Linux), built per subscription with
`Config::default().with_follow_symlinks(false)` and no debouncer
(`notify-debouncer-*` is not a dependency: "sin debounce"). notify's own
recursion is never used: notify's event-loop thread is what calls
`inotify_add_watch`, so a `RecursiveMode::Recursive` watch would walk and
watch the tree with the agent's rights (root), including subtrees uid 1000
cannot read. Instead:

- `FilesystemOps::prepare_watch(id, path, recursive)` walks the tree
  itself, under the identity, breadth-first with `read_dir`: a directory
  entry (never a symlink) that is not denied and whose `read_dir` succeeds
  is watched and descended; unreadable, denied and symlinked directories are
  skipped; more than `MAX_WATCH_DIRECTORIES` (8192, the VM's
  `max_user_watches`) → `WatchLimitReached`.
- `Watcher::watch(id, root, subdirectories, sink)` enters the identity
  guard **before** `RecommendedWatcher::new`, so notify's thread inherits
  the user's fsuid/fsgid (Linux threads copy the creating thread's
  credentials) and every `inotify_add_watch` of the subscription runs as
  the user; then one `RecursiveMode::NonRecursive` watch per directory. A
  subdirectory that vanished or became unreadable since the walk is
  skipped; the root must succeed.
- A directory that appears inside a recursive watch (`Create(Folder)` →
  `RawWatchKind::DirectoryCreated`, or a `RenameTo`) is followed by the
  **pump**, not by notify: `FilesystemOps::is_watchable_directory` (deny
  list, `lstat` is a directory, `read_dir` under the identity) and then
  `WatchSubscription::add_directory(id, dir)` (a `Mutex<RecommendedWatcher>`
  behind the port). `LimitReached` there ends the stream like at install.
- `WatchTranslator` owns the `DenyList` and drops events whose canonical
  path is denied. Because each followed directory has its own
  non-recursive watch, notify also reports `IN_DELETE_SELF`/`IN_MOVE_SELF`
  for it; the translator drops the duplicate: an untracked `Name(From)`
  (`MovedSelf`) that does not name the root is ignored, and the second
  `Removed` of a followed directory (after the parent's `IN_DELETE`) is
  ignored unless a create of the same name came in between.

`ENOSPC` (`max_user_watches`, 8192 by default on the VM) →
`WatchError::LimitReached` → `RESOURCE_EXHAUSTED`. Dropping the returned
subscription drops the watcher, which stops its thread and removes every
inotify watch it installed.

Event translation (adapter → core `RawWatchKind`): `Create(Folder)` →
`DirectoryCreated`, other `Create(_)` → `Create`;
`Modify(Data(_))` and `Modify(Any)` → `DataModified`;
`Modify(Metadata(_))` → `MetadataModified`; `Modify(Name(From))` with a
rename tracker → `RenameFrom`, without one (`IN_MOVE_SELF`) → `MovedSelf`;
`Modify(Name(To))` → `RenameTo`; `Modify(Name(Both))` →
`RenameBoth` (paths `[from, to]`); `Modify(Name(Any))` → `RenameTo`;
`Remove(_)` → `Removed`; `Access(_)` → `Other` (ignored: `IN_CLOSE_WRITE`
would duplicate every `WRITE`, and E2B's fsnotify-based `envd` does not
surface it either); an event flagged `Rescan` (inotify queue overflow) →
`QueueOverflow`; a `Remove`/rename whose path equals the canonical root →
`RootGone`.

`FilesystemManager::watch_dir(request)`:

1. Phase gate (`session.accepts_new_streams()` → `UNAVAILABLE` while
   suspending/terminating), live-watch cap (`MAX_WATCHES = 64`, counted with
   an `AtomicUsize` decremented in the stream's `Drop`) →
   `RESOURCE_EXHAUSTED`.
2. `spawn_blocking` → `FilesystemOps::prepare_watch` (parse, canonicalize,
   deny, `lstat` is-directory, `read_dir` under the identity, the recursive
   walk above) → `NOT_FOUND` / `INVALID_ARGUMENT` / `PERMISSION_DENIED` /
   `RESOURCE_EXHAUSTED` as gRPC status before any message.
3. Create `tokio::sync::mpsc::channel::<RawWatchEvent>(WATCH_QUEUE_CAPACITY
   = 1024)` and an `Arc<AtomicBool>` overflow flag **before** installing the
   watch, so every event after installation is captured.
4. `spawn_blocking` → `Watcher::watch(id, root, subdirectories, sink)` with
   a sink closure that, on the
   notify thread: returns immediately if the overflow flag is set; otherwise
   `try_send(raw)`; on `Full` sets the flag and drops the event (the notify
   thread is never blocked, so a stalled client cannot stall inotify).
5. Only when `watch()` returned `Ok` does the stream start, and its first
   item is `WatchStarted`. The response stream is a hand-written `Stream`
   (`WatchStream`) owning, in this field order, the receiver, the
   translator, the optional `include_entry` resolver, the subscription and
   the manager's live-watch counter guard; field order matters because Rust
   drops fields in declaration order: the receiver closes first, then the
   watcher stops.
6. Each `RawWatchEvent` goes through `WatchTranslator::translate`: `Events`
   → one `FilesystemEvent` per item (`entry` filled for `CREATE`/`WRITE`/
   `CHMOD` when `include_entry`, via a `spawn_blocking` `lstat` under the
   user's identity, omitted if it fails); `End(RootGone)` → trailing
   `Err(Status::not_found("watched directory removed"))`; `End(Overflow)` →
   trailing `Err(Status::resource_exhausted("watch queue overflowed; re-open
   the watch"))`; `Ignore` → nothing. When the channel is empty and the
   overflow flag is set, the stream ends with the same `RESOURCE_EXHAUSTED`
   (buffered events are delivered first).
7. The stream is wrapped in `KeepAliveStream` with
   `WATCH_KEEPALIVE_INTERVAL = 50 s` (`StreamSettings.watch_keepalive_interval`,
   overridable to 200 ms in tests). A watch stream held open therefore keeps
   the VM out of its idle policy (bytes cross the endpoint every 50 s, Q15).

Ordering guarantee: events of one watch are delivered in inotify order;
`RenameBoth` yields the `from` event before the `to` event.

Alternatives considered: `blocking_send` in the sink (rejected: blocks the
notify thread, and notify's own `Drop` joins that thread); an unbounded
channel (rejected: unbounded memory under a stalled client); ending the
stream immediately on overflow (rejected: the buffered 1024 events are still
valid and cheap to deliver).

### D9. `rayd` adapter and application modules

```text
crates/rayd/src/
  adapters/fs_identity.rs        FsIdentityGuard::enter(switch: IdentitySwitch, id: &FsIdentity) -> Guard (cfg(unix): setfsgid then setfsuid, restore in Drop; not(unix): no-op)
  adapters/std_filesystem.rs     StdFileSystem{identity_switch} (cfg(unix)): the FileSystem port over std::fs + nix; every method wraps itself in FsIdentityGuard;
                                 open_read = OpenOptions::custom_flags(O_NOFOLLOW | O_CLOEXEC) + fstat is-regular; begin_write = parents + tempfile; TempWriteSink{NamedTempFile, mode, identity_switch} implements WriteSink
                                 UnsupportedFileSystem (cfg(not(unix))): every op → FsIoError::Unsupported
  adapters/notify_watcher.rs     NotifyWatcher{identity_switch} (cfg(unix)): Watcher port over notify 8.x, NotifySubscription(RecommendedWatcher) implements WatchSubscription; raw_kind(notify::EventKind) -> RawWatchKind
                                 UnsupportedWatcher otherwise
  adapters/name_resolver.rs      NixNameResolver (User::from_uid / Group::from_gid) (cfg(unix)); NumericNameResolver otherwise (always None)
  adapters/mod.rs                pub use PlatformFileSystem, PlatformWatcher, PlatformNameResolver aliases (like PlatformSpawner)
  filesystem/mod.rs
  filesystem/manager.rs          FilesystemManager<F: FileSystem, W: Watcher>{session, fs: Arc<F>, watcher: Arc<W>, names: Arc<dyn NameResolver>, lookup: Arc<dyn UserLookup>, policy: UserPolicy, deny: DenyList, settings: FilesystemSettings, live_watches: AtomicUsize}
                                 FilesystemSettings{list_limits: ListingLimits, watch_queue_capacity: 1024, max_watches: 64, write_error_drain_bytes: 1 MiB, write_error_drain_timeout: 2 s}
                                 identity(user: Option<&str>) -> Result<FsIdentity, FilesystemError>   // resolve_username + policy + lookup (M2 rules)
                                 stat / list_dir / make_dir / rename / remove -> spawn_blocking(FilesystemOps)
                                 read(input) -> Result<ReadStream, FilesystemError>              // D5
                                 write(messages: impl Stream<Item = Result<WriteMessageWithChunk, Status>>) -> Result<Vec<Entry>, WriteFailure>   // D6
                                 watch_dir(input) -> Result<WatchStream, FilesystemError>        // D8
                                 live_watches() -> usize
  filesystem/read.rs             ReadStream (ReceiverStream<Result<Bytes, FilesystemError>>), reader task
  filesystem/write.rs            WriteMessageWithChunk{message: WriteMessage, chunk: Bytes}, drain_after_error
  filesystem/watch.rs            WatchStream (hand-written Stream, field order per D8), WatchEnd mapping
  grpc/filesystem.rs             FilesystemGrpc: proto <-> domain conversion (EntryInfo, FilesystemEvent, FileType, FilesystemEventType), status table D10, KeepAliveStream on WatchDir
  grpc/mod.rs                    router(session, processes, files: Arc<PlatformFilesystemManager>, metrics), StreamSettings{keepalive_interval, watch_keepalive_interval}; PendingFilesystemService removed
  main.rs                        builds DenyList (defaults + current_exe), StdFileSystem, NotifyWatcher, NixNameResolver, FilesystemManager (same UserPolicy and IdentitySwitch as processes); wires into the router
```

`Cargo.toml`: workspace dependency `notify = { version = "=8.<latest>",
default-features = false }` (the exact patch is the latest 8.x on crates.io
when the task runs, recorded in `Cargo.lock`; `default-features = false`
drops the macOS backends and `crossbeam-channel`, leaving `inotify`, `mio`,
`walkdir`, `filetime`, `libc`, `notify-types`, all of which cross-compile to
`aarch64-unknown-linux-musl` with `cargo zigbuild`); `tempfile` (already in
the workspace list) and `notify` become `[target.'cfg(unix)'.dependencies]`
of `rayd`. `nix` keeps its current features (`user` provides `setfsuid`,
`setfsgid`, `User::from_uid`, `Group::from_gid`; `std` provides `fchown`,
`set_permissions`, `sync_all`).

Logging allowlist additions (`logging.rs` doc): `rpc`, `files`, `bytes`,
`chunks`, `entries`, `depth`, `recursive`, `events`, `live_watches`,
`duration_ms`, `errno`, `outcome`. Never paths, names, contents, modes with
the path, or `symlink_target`.

### D10. gRPC status mapping (server side)

| Domain error | gRPC status |
|---|---|
| `InvalidPath(*)`, `IsADirectory`, `NotARegularFile`, `IsSymlink` (final component of `Read`), `ChunkTooLarge`, `InvalidMode`, `MissingPath`, `UserWithoutPath`, `NoFiles`, `NotADirectory` (`ListDir`/`WatchDir` root, `MakeDir` on a file, `ENOTDIR` component), `UnknownUser` | `INVALID_ARGUMENT` |
| `Denied`, `PermissionDenied` (`EACCES`/`EPERM`), `RootNotAllowed` | `PERMISSION_DENIED` |
| `NotFound` | `NOT_FOUND` |
| `AlreadyExists` (`MakeDir`) | `ALREADY_EXISTS` |
| `NotEmpty`, `DestinationConflict`, `CrossDevice` | `FAILED_PRECONDITION` |
| `TooManyEntries`, `TooManyWatches`, `WatchLimitReached`, `WatchOverflow`, `NoSpace` | `RESOURCE_EXHAUSTED` |
| `WatchRootGone` (trailing) | `NOT_FOUND` |
| `NotAcceptingStreams` | `UNAVAILABLE` |
| `Unsupported` | `UNIMPLEMENTED` |
| `Io{operation, errno}`, `UserLookupFailed` | `INTERNAL` (message: `"<operation> failed: <errno name>"`) |
| missing/invalid `x-access-token` | `UNAUTHENTICATED` (layer, unchanged) |

Server streams (`Read`, `WatchDir`): statuses before the first message are
exactly these; after the first message the only possible terminal statuses
are `INTERNAL` (read error), `NOT_FOUND` (watched root removed) and
`RESOURCE_EXHAUSTED` (watch overflow), delivered as the stream's trailing
error. Error messages never echo a path.

### D11. SDK modules, models and signatures

New/changed modules in `clients/python/src/rayito/`:

- `_models.py` adds:
  - `class FileType(str, Enum)`: `FILE = "file"`, `DIR = "dir"`, `SYMLINK = "symlink"`.
  - `EntryInfo(name: str, type: FileType | None, path: str, size: int, mode: int, permissions: str, owner: str, group: str, modified_time: datetime, symlink_target: str | None = None)` (frozen dataclass; `type is None` for `FILE_TYPE_UNSPECIFIED`).
  - `class FilesystemEventType(str, Enum)`: `CREATE = "create"`, `WRITE = "write"`, `REMOVE = "remove"`, `RENAME = "rename"`, `CHMOD = "chmod"`.
  - `FilesystemEvent(name: str, type: FilesystemEventType, entry: EntryInfo | None = None)`.
  - `WriteEntry(path: str, data: str | bytes | IO[bytes] | IO[str], mode: int | None = None)`.
- `_filesystem_base.py` (pure, shared):
  - `FILE_REQUEST_BASE_SECONDS = 60.0`, `FILE_REQUEST_SECONDS_PER_MB = 1.0`, `MB = 1_000_000`, `WRITE_CHUNK_BYTES = 1_048_576`, `READ_CHUNK_BYTES = 262_144`, `WATCH_STOP_JOIN_SECONDS = 5.0`.
  - `file_request_deadline(total_bytes: int, request_timeout: float | None) -> float` = `request_timeout` if not `None` else `60 + total_bytes / MB`.
  - `coerce_data(data) -> bytes`: `str` → UTF-8; `bytes`/`bytearray`/`memoryview` → bytes; file-like → `.read()` (text mode results encoded UTF-8); anything else → `InvalidArgumentException`. Data is always materialised in memory (E2B does the same) so the size, the deadline and the proxy-403 retry are all well defined.
  - `validate_path(path) -> str` (non-empty `str`, no NUL), `validate_mode(mode) -> int | None` (`0 <= mode <= 0o7777`), `validate_depth(depth) -> int` (`>= 0`; `0` sent as is, server treats it as 1).
  - `build_write_requests(entries: Sequence[tuple[str, bytes, int | None]], user: str | None) -> Iterator[WriteRequest]`: per entry the first message carries `path`, `user` (if any) and `mode` (if any) with the first chunk (possibly empty), then 1 MiB chunks; the generator is re-creatable, which `_call_unary`'s retry needs.
  - `entry_info_from_proto`, `filesystem_event_from_proto`, `file_type_from_proto`.
  - `decode_text(data: bytes) -> str`: strict UTF-8; `UnicodeDecodeError` → `InvalidArgumentException("file is not valid UTF-8; use format='bytes'")`.
  - `WatchState`: the deque of undelivered events (only filled when `on_event is None`), the stored terminal exception, `stopped` flag, `feed(response) -> FilesystemEvent | None` (ignores `keepalive`, raises on an unexpected `started`), `record_end(exc: Exception | None)`, `drain() -> list[FilesystemEvent]` (raises the stored exception once, then returns empty lists).
  - `watch_failure(exc: grpc.RpcError, *, stopped: bool) -> Exception | None`: `CANCELLED` after `stop()` → `None`; `DEADLINE_EXCEEDED` → `TimeoutException`; anything else → `translate_rpc_error(exc, filesystem=True)` (resets are classified by the sandbox's `_stream_failure` as in M2).
- `sandbox_sync/filesystem.py`:

```python
class Filesystem:
    def __init__(self, sandbox: Sandbox) -> None: ...
    @overload
    def read(self, path: str, *, format: Literal["text"] = "text", user: str | None = None,
             request_timeout: float | None = None) -> str: ...
    @overload
    def read(self, path: str, *, format: Literal["bytes"], user=None, request_timeout=None) -> bytes: ...
    @overload
    def read(self, path: str, *, format: Literal["stream"], user=None, request_timeout=None) -> Iterator[bytes]: ...
    def write(self, path: str, data: str | bytes | IO, *, user: str | None = None,
              mode: int | None = None, request_timeout: float | None = None) -> EntryInfo
    def write_files(self, files: Sequence[WriteEntry], *, user: str | None = None,
                    request_timeout: float | None = None) -> list[EntryInfo]
    def list(self, path: str, *, depth: int = 1, user=None, request_timeout=None) -> list[EntryInfo]
    def exists(self, path: str, *, user=None, request_timeout=None) -> bool
    def get_info(self, path: str, *, user=None, request_timeout=None) -> EntryInfo
    def remove(self, path: str, *, recursive: bool = True, user=None, request_timeout=None) -> None
    def rename(self, old_path: str, new_path: str, *, user=None, request_timeout=None) -> EntryInfo
    def make_dir(self, path: str, *, user=None, request_timeout=None) -> bool
    def watch_dir(self, path: str, *, on_event: Callable[[FilesystemEvent], None] | None = None,
                  on_exit: Callable[[Exception], None] | None = None, recursive: bool = False,
                  include_entry: bool = False, user: str | None = None,
                  timeout: float | None = 0, request_timeout: float | None = None) -> WatchHandle

class WatchHandle:
    path: str                     # property, the path as requested
    is_running: bool              # property, False after stop() or after the stream ended
    def get_new_events(self) -> list[FilesystemEvent]   # drains; raises the stored terminal error once
    def stop(self) -> None        # cancels the stream, joins the thread (<= 5 s), idempotent
    def __enter__ / __exit__      # exit = stop()
```

- `sandbox_async/filesystem.py`: `AsyncFilesystem` and `AsyncWatchHandle`
  with the same names as coroutines (`await files.read(...)`, `format=
  "stream"` returns an `AsyncIterator[bytes]`, `await handle.get_new_events()`,
  `await handle.stop()`, `async with`), built on `grpc.aio` and an
  `asyncio.Task` consumer.
- `sandbox_sync/main.py` / `sandbox_async/main.py`: `files` property
  returning the `Filesystem` created in `__init__`; `_files` stub on the
  unary channel and `_files_stream` on the lazily opened stream channel;
  `_process_stub(stream=)` generalised to `_stub(service, *, stream)` so
  `_open_stream` serves `WatchDir` too; the `files` `NotImplementedError`
  stub disappears (`pty` and `run_code` keep theirs).
- `__init__.py` exports `EntryInfo`, `FileType`, `FilesystemEvent`,
  `FilesystemEventType`, `WriteEntry`, `WatchHandle`, `AsyncWatchHandle`.

Semantics fixed here:

- Channels: `Stat`, `ListDir`, `MakeDir`, `Move`, `Remove`, `Write` and the
  foreground `Read` stream use the **unary** channel (like foreground
  `run`); `WatchDir` uses the **stream** channel. Never a channel per call.
- Deadlines: unary RPCs use `request_timeout` or the sandbox's
  `request_timeout` (60 s). `read` does `Stat` first (unary deadline), then
  `Read` with `file_request_deadline(entry.size, request_timeout)`; a
  directory or symlink is refused client-side after the `Stat`
  (`InvalidArgumentException`) without opening the stream. `write` and
  `write_files` use `file_request_deadline(total_bytes, request_timeout)`.
  `watch_dir`: `timeout` `0`/`None` → no gRPC deadline (unlimited,
  `ARCHITECTURE.md` vocabulary); `timeout > 0` → gRPC deadline of that many
  seconds, after which the handle records `TimeoutException`.
- `read(format="text")` decodes strictly; `"bytes"` returns `bytes`;
  `"stream"` returns an iterator of chunks as received (≤ 256 KiB each),
  translating a trailing error into the M2 stream-failure classification.
- `exists`: `Stat` → `True`; `FileNotFoundException` → `False`; any other
  exception propagates.
- `make_dir`: `True` when created; a `grpc.RpcError` with `ALREADY_EXISTS`
  → `False`; everything else → `translate_rpc_error(filesystem=True)`.
- `remove(path)` is recursive by default (E2B parity); `recursive=False` on
  a non-empty directory raises `InvalidArgumentException`
  (`FAILED_PRECONDITION`).
- `write` returns the single `EntryInfo`; `write_files` returns the list in
  request order; an empty `files` list raises `InvalidArgumentException`
  client-side (no RPC).
- Proxy 403: unary calls and `Write` go through `_call_unary` (the request
  iterator is a factory so the retry re-sends from the start); `Read` and
  `WatchDir` go through `_open_stream` (one retry before the first message,
  which for `WatchDir` is `WatchStarted` and for `Read` the first chunk or
  the stream's end).
- `watch_dir` blocks until `WatchStarted` (a first message that is not
  `started` raises `SandboxException`), then hands the iterator to a daemon
  thread (`rayito-watch-<n>`) / an `asyncio.Task` that calls `on_event` for
  each `FilesystemEvent` (exceptions raised by `on_event` are logged and do
  not stop the watch), stores events in the deque when `on_event is None`,
  ignores `keepalive`, and on the stream's end records the terminal
  exception (`None` for a clean end after `stop()`) and calls `on_exit(exc)`
  if given and `exc is not None`. `get_new_events()` returns the drained
  list or raises the recorded exception once. `stop()` cancels the gRPC call,
  waits for the consumer (≤ 5 s), and is idempotent; `Sandbox.close()` stops
  every live handle.
- Error mapping already in place: `NOT_FOUND` → `FileNotFoundException`,
  `INVALID_ARGUMENT`/`FAILED_PRECONDITION` → `InvalidArgumentException`,
  `PERMISSION_DENIED` → `AuthenticationException(proxy_rejected=False)`,
  `RESOURCE_EXHAUSTED` → `RateLimitException`, `DEADLINE_EXCEEDED` →
  `TimeoutException`, `UNIMPLEMENTED` → `InvalidArgumentException`. No table
  changes; `Filesystem` always passes `filesystem=True`.

### D12. Tests

**`rayd-core` (host, Windows and Linux)** — `cargo test -p rayd-core`:
`RequestPath` (absolute, relative → home, `//` and `.` normalisation,
trailing slash, `..` refused, NUL, empty, `name`/`parent`/`join`);
`DenyList` (component prefix semantics, `/etcetera` allowed, the binary,
`current_exe` extra entry); `permissions_string` for the three kinds and
`Other`; `build_entry` with a fake `NameResolver` (names, numeric fallback,
cache hits); `effective_depth`; `walk_listing` against `FakeFileSystem`
(DFS pre-order, byte-sorted names, depth 1/2/3, symlinked directory not
descended but listed as `Symlink` with target, denied subtree skipped,
`TooManyEntries` with `max_entries = 3`, `lstat` race skipped);
`WriteSession` (first message without path, path switch commits, empty first
chunk, chunk cap, mode default/validation, user without path, zero files);
`WatchTranslator` (relative names, recursive nested names, outside-root
dropped, temp-prefix dropped, `RenameBoth` → two events in order, root gone,
overflow, `Other` ignored); `FilesystemOps` end to end on the fake (stat,
list, make_dir twice → `AlreadyExists`, make_dir on file → `NotADirectory`,
rename conflicts, remove non-empty → `NotEmpty`, read of directory/symlink,
write target creates parents, denied canonical parent through a fake
symlink, permission flag → `PermissionDenied`, identity resolution rules).

**`rayd` integration, `cfg(unix)`** — `crates/rayd/tests/m3_filesystem.rs`
(`#![cfg(unix)]`, in-process router on `127.0.0.1:0`, `/run` installed via
the hooks router, `IdentitySwitch::KeepCurrent` when not root, a `tempfile`
directory as the playground, `watch_keepalive_interval` 200 ms,
`watch_queue_capacity` 4 and `max_entries` 5 injected through
`FilesystemSettings`/`StreamSettings`): `Read` of a 600 KiB file arrives as
256/256/88 KiB chunks; empty file → zero messages; missing → `NOT_FOUND`;
directory → `INVALID_ARGUMENT`; symlink → `INVALID_ARGUMENT`; `/etc/passwd`
→ `PERMISSION_DENIED`; `<tmp>/link -> /etc` then `<tmp>/link/passwd` →
`PERMISSION_DENIED`; `Write` single file with `mode 0o600` (content, mode,
no `.rayito-tmp-*` left, `EntryInfo` fields); two files in one stream (order
of `entries`); chunk of 1 MiB + 1 → `INVALID_ARGUMENT` and no temp left;
first message without path → `INVALID_ARGUMENT`; empty stream →
`INVALID_ARGUMENT`; destination is a symlink → replaced by a regular file and
the target untouched; destination is a directory → `INVALID_ARGUMENT`;
parents created; denied destination → `PERMISSION_DENIED`; client cancels
mid-stream → temp gone within 1 s; `Stat` of file/dir/symlink (target) and
`NOT_FOUND`; `ListDir` depth 0/1/2 order and contents, symlinked directory
not descended, on a file → `INVALID_ARGUMENT`, 6 entries with `max_entries
5` → `RESOURCE_EXHAUSTED`; `MakeDir` → entry, again → `ALREADY_EXISTS`, on a
file → `INVALID_ARGUMENT`, parents created; `Move` file, onto an existing
directory → `FAILED_PRECONDITION`, missing → `NOT_FOUND`; `Remove` file,
non-empty non-recursive → `FAILED_PRECONDITION`, recursive → gone, missing →
`NOT_FOUND`, symlink-to-dir removes only the link; `WatchDir`: `WatchStarted`
first and a file created right after installation (before the first poll)
is reported; create/write/remove/rename/chmod with relative names; recursive
subdirectory names `sub/x`; `include_entry` on `CREATE`; a `Write` RPC into
the watched dir yields exactly one `RENAME` of the final name and nothing
with the temp prefix; missing → `NOT_FOUND` before any message; file →
`INVALID_ARGUMENT`; dropping the client stream brings `live_watches()` back
to 0 within 1 s; 65th watch → `RESOURCE_EXHAUSTED` (with `max_watches`
overridden to 2, the 3rd); keepalive at 200 ms on a silent watch; overflow
with capacity 4 → buffered events then `RESOURCE_EXHAUSTED`; removing the
watched root → `NOT_FOUND` trailing; `Read`/`Write`/`WatchDir` while
`/suspend` was acknowledged → `UNAVAILABLE`; `user: "root"` →
`PERMISSION_DENIED` without `RAYITO_ALLOW_ROOT`; identity (`#[ignore]`
unless `geteuid() == 0`, run in the Docker loop as in M2): a root-only
readable file → `PERMISSION_DENIED`, a written file owned by uid 1000, and
two concurrent RPCs as `user` and as `root` (allowed via `UserPolicy` in the
test) each see their own permissions (the `setfsuid` per-thread guarantee).
`m1_hello.rs` and `m2_process.rs` adapt to the new `router` signature; the
"pending service" probe moves to `PtyService.Resize` → `UNIMPLEMENTED`.
`cargo clippy --workspace --all-targets -- -D warnings` clean on both hosts.

**Python unit** — `clients/python/tests/unit/`: `fake_filesystem.py` adds
`FakeFilesystemService` to the in-process fake `rayd` (registered by the
`fake_rayd` fixture, exposed as `RaydEndpoint.filesystem`; every RPC checks
`x-access-token`): an in-memory tree (`dict[str, bytes | Directory |
Symlink]`) seeded with `/home/user`, `/tmp` and `/etc/passwd`, deny list
`/etc` and `/usr` → `PERMISSION_DENIED`, `Read` in 256 KiB chunks, `Write`
state machine with the 1 MiB rule and `ALREADY_EXISTS`/`FAILED_PRECONDITION`
semantics, `ListDir` depth walk, `WatchDir` that emits `WatchStarted`, one
`keepalive`, then whatever the test pushes with `push_event(path, event)`
and `end_watch(path, code)`. Tests (`test_filesystem_base.py`,
`test_files_sync.py`, `test_files_async.py`): deadline math (`60 + 8 MB →
68 s`, explicit `request_timeout` wins), `coerce_data` for `str`/`bytes`/
`BytesIO`/`StringIO`/invalid, request chunking (3 MiB → messages of 1 MiB,
1 MiB, 1 MiB with `path` only on the first; two entries → `path` on the
first message of each; empty data → one message with an empty chunk),
`entry_info_from_proto` incl. `UNSPECIFIED → None` and `symlink_target`,
`filesystem_event_from_proto`, `decode_text` strict, `WatchState` (deque
only without callback, keepalive ignored, terminal error raised once),
`read` text/bytes/stream, `read` of a directory → `InvalidArgumentException`
before any `Read` call (the fake counts `Read` calls), `write` returns
`EntryInfo`, `write_files` order, `list(depth=2)`, `exists` True/False,
`get_info`, `remove` recursive and `recursive=False` on a non-empty dir,
`rename`, `make_dir` True/False, `/etc/passwd` → `AuthenticationException`
with `proxy_rejected False`, `watch_dir` blocks until `WatchStarted`, events
via `get_new_events()` and via `on_event`, `stop()` idempotent and no
exception after a clean stop, terminal `NOT_FOUND` → `FileNotFoundException`
from `get_new_events()` and `on_exit` called, `timeout=1` → `TimeoutException`,
403-on-open re-mint for `Write` (Stubber expects one extra
`create_microvm_auth_token`; the fake asserts the whole file arrived once),
`WatchDir` uses the second channel and `Stat` the first (the fake records
client ports), `Sandbox.close()` stops live handles, async parity for every
case. `uv run pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy
src` all clean.

**e2e (real AWS)** — `clients/python/tests/e2e/test_m3_filesystem.py` and
`test_m3_bench.py`; see "Acceptance test list".

### D13. Image

`image/Dockerfile`: no new packages. Extend the build-time sanity `RUN` with
`sha256sum`, `stat`, `rm`, `mv`, `mkdir`, `chmod`, `touch`, `ln` (all from
the `coreutils` already present, but the build must fail here rather than
in AWS if a future base image drops one). Header comment updated (M3).
`rayd` stays `CMD` as root; `RAYITO_ALLOW_ROOT` stays unset. Publish with
`image-publish` (new version, three-state gate). No `create-microvm-image`
parameter changes; inotify needs nothing from the image (`fs.inotify.*`
sysctls are the kernel's defaults, not configurable from the Dockerfile).

### D14. Performance budgets (checked in e2e/integration, logged, asserted where marked)

| Budget | Value | Where |
|---|---|---|
| `Read` 8 MB through the proxy | ≥ 3 MB/s, total ≤ 10 s | e2e (asserted ≤ 10 s, MB/s logged) |
| `Write` 8 MB through the proxy | ≥ 3 MB/s, total ≤ 10 s | e2e (asserted ≤ 10 s, MB/s logged; first upload measurement, recorded in `AWS_API_NOTES.md` §16) |
| `write_files` 50 × 1 KB in one stream | ≤ 5 s | e2e (asserted) |
| `list(depth=2)` of 51 entries | ≤ 1 s | e2e (asserted) |
| `Stat` round trip | ≤ 0.5 s p95 over 30 calls | e2e (asserted total ≤ 20 s, p95 logged) |
| `watch_dir` → `WatchStarted` | ≤ 1 s | e2e (asserted) |
| event latency (create → client) | ≤ 2 s | e2e (asserted with a 5 s poll budget) |
| in-VM `Read` pipeline | ≥ 100 MB/s on a 64 MiB file | integration (logged) |
| in-VM `Write` 50 small files (fsync each) | ≤ 2 s | integration (logged) |
| memory per `Read` stream | ≤ 4 × 256 KiB | by construction (D5) |
| memory per `Write` stream | ≤ 1 MiB chunk in flight | by construction (D6) |
| memory per watch | ≤ 1024 queued raw events | by construction (D8) |
| 50 MB write + read (nightly `bench`) | ≤ 120 s, MB/s logged (theory: ≥ 25 s at 4 MB/s) | `test_m3_bench.py` (asserted) |

### D15. Docs alignment

`ARCHITECTURE.md`: `FilesystemService` row gains the deny list, the
filesystem identity, the 10 000-entry cap, the 1024-event watch queue with
`RESOURCE_EXHAUSTED` on overflow, the 64-watch cap and the 50 s
`KeepAlive`; the adapters list renames `TokioFileSystem` to `StdFileSystem`
(blocking `std::fs` under `spawn_blocking` with a per-thread filesystem
identity; `tokio::fs` cannot pin a thread identity across awaits) and adds
`FsIdentityGuard`, `NixNameResolver`; the domain table's `filesystem` and
`watch` rows list the D4 types. `MILESTONES.md` M3: acceptance snippet
aligned with the e2e (`write` returns `EntryInfo`, `read(format="bytes")`,
the `RENAME`-on-atomic-write note). `SECURITY.md`: new row T11 "sandbox code
uses the root agent as a confused deputy through `FilesystemService`" →
canonical-path deny list + per-thread `setfsuid`/`setfsgid` + `O_NOFOLLOW`
+ no symlink following in walks and watches (M3 ✔); T9 unchanged.
`AWS_API_NOTES.md` §16: new rows for upload throughput through the proxy and
for client-stream early-error delivery (does the drain of D6 avoid Q29's
`CANCELLED`?), both measured by the e2e.

## Risks / Trade-offs

- [`setfsuid` under musl] musl wraps `setfsuid`/`setfsgid` as plain
  syscalls (per-thread), unlike `setuid` which it broadcasts; if a future
  musl broadcast them, every thread of `rayd` would drop to uid 1000 →
  Mitigation: the integration test run as root asserts that a second
  concurrent RPC as a different identity still sees its own permissions,
  and `FsIdentityGuard::enter` verifies with a `setfsuid(-1)` read-back that
  the switch applied to the calling thread only.
- [TOCTOU between `realpath` and the operation] A symlink swapped by the
  sandbox between the check and the syscall → Mitigation: the operation runs
  with uid 1000's rights (D3), so the race can only reach what the sandbox
  already could; the deny list is defence in depth. Documented; `openat`
  walking is a possible M6 hardening.
- [Atomic write vs watch semantics] `files.write` into a watched directory
  produces `RENAME`, not `CREATE`+`WRITE` → Mitigation: documented in the
  SDK and asserted in the e2e; shell writes (the acceptance case) produce the
  E2B sequence.
- [`Stat` before `Read`] Every `read()` costs two RPCs → Mitigation: needed
  for the size-based deadline and the client-side directory/symlink check;
  the connection budget assertion (30 sequential `exists`) shows the RPS
  headroom; `format="stream"` users who want a single RPC can call the stub
  directly (not a public promise).
- [Early error on `Write` through the proxy] Q29 says a trailers-only
  answer can be turned into `CANCELLED` → Mitigation: the drain of D6 (≤ 1
  MiB / 2 s) before answering; the e2e asserts a denied-path `write` raises
  `AuthenticationException`, not `SandboxException("cancelled")`, and the
  result is recorded in §16.
- [inotify limits on a 2 GB VM] `max_user_watches` 8192 and
  `max_user_instances` 128 by default → Mitigation: 64-watch cap per
  sandbox, `ENOSPC` → `RESOURCE_EXHAUSTED` with a clear message, recursive
  watches of huge trees are the user's choice.
- [Kernel inotify queue overflow] 16384 events per instance; our 1024
  channel is smaller and fills first under a stalled client → Mitigation:
  the overflow ends the stream deterministically; the client re-opens.
- [`notify` 8.x on musl ARM64] Never built here before → Mitigation: pinned
  with `default-features = false`; the ARM64 CI job builds it; if the crate
  does not cross-compile, the fallback is the `inotify` crate directly
  behind the same `Watcher` port (no design change).
- [Listing cap] 10 000 entries may be too small for `depth=5` on a big tree
  → Mitigation: a clear `RESOURCE_EXHAUSTED` message telling the caller to
  reduce depth; E2B has the same shape of limit through message size.
- [Materialising `IO` data in memory] `write(path, big_file_object)` loads
  the whole file → Mitigation: same as E2B; the 50 MB bench proves it is
  fine at the sizes a 2 GB sandbox makes sense for.
- [Per-file, not per-call, atomicity of `write_files`] → Mitigation:
  documented; a failure lists nothing and the caller can retry the whole
  batch (each file is replaced atomically, so retries are safe).

## Migration Plan

1. Merge; CI green (`buf lint`, `buf breaking`, `cargo fmt/clippy/test`,
   `pytest tests/unit`, `ruff`, `mypy`, ARM64 build with `notify`).
2. `make image-zip` + `image-publish` → new `rayito-base` version;
   three-state gate.
3. `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> make test-e2e` → `test_m1_hello.py`,
   `test_m2_processes.py`, `test_m3_filesystem.py` green; `make test-bench`
   once for the 50 MB numbers.
4. Rollback: `update-microvm-image-version --status INACTIVE` on the new
   version; the SDK stays backwards compatible with the M2 agent for
   `Health`, lifecycle and `commands` (`files.*` would answer
   `UNIMPLEMENTED` → `InvalidArgumentException`).
5. Acceptance agent archives the change.

## Open Questions

None blocking. To measure during e2e and record in `AWS_API_NOTES.md` §16:
upload throughput of an 8 MB and a 50 MB `Write` through the proxy (first
client-stream measurement); whether the D6 drain avoids Q29's `CANCELLED`
on an early `PERMISSION_DENIED`; `WatchStarted` latency and create → event
latency through the proxy.

## Acceptance test list

`clients/python/tests/e2e/test_m3_filesystem.py`, marker `e2e`, one
`sandbox` fixture (`maximumDurationInSeconds=900`, no `idlePolicy`,
`ingress=["ALL_INGRESS"]`), one test function of numbered blocks as in M2,
`BASE = "/home/user/m3"` created by `files.make_dir(BASE)`:

1. `payload = os.urandom(8_000_000)`; `info = files.write(f"{BASE}/big.bin", payload)` with the default deadline: `info.size == 8_000_000`, `info.type is FileType.FILE`, `info.path == f"{BASE}/big.bin"`, `info.name == "big.bin"`, `info.owner == "user"`, `info.group == "user"`, `info.permissions == "-rw-r--r--"`, `info.mode == 0o644`; elapsed ≤ 10 s, MB/s logged.
2. `data = files.read(f"{BASE}/big.bin", format="bytes")`: `hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()`; elapsed ≤ 10 s, MB/s logged. `chunks = list(files.read(..., format="stream"))`: `b"".join(chunks) == payload`, `all(len(c) <= 262_144 for c in chunks)`, `len(chunks) >= 30`. `files.write(f"{BASE}/hola.txt", "hola ñ\n")`; `files.read(f"{BASE}/hola.txt") == "hola ñ\n"`; `files.read(f"{BASE}/big.bin")` (text) raises `InvalidArgumentException` (not UTF-8).
3. `entries = files.write_files([WriteEntry(f"{BASE}/many/f{i:02}.txt", f"file {i}\n".encode()) for i in range(50)])` in one call: `len(entries) == 50`, `[e.name for e in entries] == [f"f{i:02}.txt" for i in range(50)]`, every `size == len(...)`; elapsed ≤ 5 s. `commands.run(f"ls {BASE}/many | wc -l").stdout.strip() == "50"`.
4. `listing = files.list(BASE, depth=2)`: names at depth 1 are `{"big.bin", "hola.txt", "many"}`, `many` has `type is FileType.DIR`, the 50 files appear right after `many` with `path == f"{BASE}/many/f{i:02}.txt"` in byte order; `files.list(BASE)` (depth 1) has no `many/` children; `files.list(BASE, depth=0) == files.list(BASE, depth=1)`; elapsed of the depth-2 call ≤ 1 s. `files.list(f"{BASE}/big.bin")` raises `InvalidArgumentException`; `files.list(f"{BASE}/nope")` raises `FileNotFoundException`.
5. `files.make_dir(f"{BASE}/dir") is True`; `files.make_dir(f"{BASE}/dir") is False`; `files.make_dir(f"{BASE}/big.bin")` raises `InvalidArgumentException`; `files.make_dir(f"{BASE}/a/b/c") is True` and `files.get_info(f"{BASE}/a/b").type is FileType.DIR`.
6. `files.exists(f"{BASE}/missing") is False`; `files.exists(f"{BASE}/big.bin") is True`; `files.get_info(f"{BASE}/missing")` raises `FileNotFoundException`; `commands.run(f"ln -s big.bin {BASE}/link")`; `link = files.get_info(f"{BASE}/link")`: `link.type is FileType.SYMLINK`, `link.symlink_target == "big.bin"`; `files.read(f"{BASE}/link", format="bytes")` raises `InvalidArgumentException` (`O_NOFOLLOW`, refused client-side after the `Stat`).
7. `moved = files.rename(f"{BASE}/hola.txt", f"{BASE}/dir/hola.txt")`: `moved.path == f"{BASE}/dir/hola.txt"`, `files.exists(f"{BASE}/hola.txt") is False`, `files.read(f"{BASE}/dir/hola.txt") == "hola ñ\n"`; `files.rename(f"{BASE}/missing", f"{BASE}/x")` raises `FileNotFoundException`; `files.rename(f"{BASE}/dir/hola.txt", f"{BASE}/many")` raises `InvalidArgumentException` (destination is a non-empty directory).
8. `files.remove(f"{BASE}/dir/hola.txt")`; `files.exists(...) is False`; `files.remove(f"{BASE}/many", recursive=False)` raises `InvalidArgumentException`; `files.remove(f"{BASE}/many")` (recursive default); `files.exists(f"{BASE}/many") is False`; `files.remove(f"{BASE}/many")` raises `FileNotFoundException`; `files.remove(f"{BASE}/link")` then `files.exists(f"{BASE}/big.bin") is True` (only the link went).
9. Path policy: `files.read("/etc/passwd")` raises `AuthenticationException` with `proxy_rejected is False`; `files.write("/usr/local/bin/rayd", b"x")` raises `AuthenticationException` (not `SandboxException`; also proves the D6 drain against Q29); `files.list("/proc")` raises `AuthenticationException`; `files.list("/")` succeeds and contains `etc`; `files.read(f"{BASE}/../../etc/passwd")` raises `InvalidArgumentException` (`..` refused); `files.write("m3/rel.txt", "r")` then `files.exists("/home/user/m3/rel.txt") is True` (relative to home).
10. Identity: `commands.run(f"stat -c %U:%G {BASE}/big.bin").stdout.strip() == "user:user"`; `files.list("/root")` raises `AuthenticationException` (root's home is `0700` on al2023, so uid 1000 gets `EACCES`); `files.write("/root/x", b"x", user="root")` raises `AuthenticationException` (root refused by policy).
11. Mode: `files.write(f"{BASE}/secret.txt", b"s", mode=0o600)`; `info = files.get_info(...)`: `info.mode == 0o600`, `info.permissions == "-rw-------"`; `commands.run(f"stat -c %a {BASE}/secret.txt").stdout.strip() == "600"`.
12. `watch_dir`: `files.make_dir(f"{BASE}/watch")`; `started = perf_counter(); h = files.watch_dir(f"{BASE}/watch")`; `WatchStarted` latency ≤ 1 s (logged); `commands.run(f"echo hola > {BASE}/watch/a.txt; sleep 0.3; echo mas >> {BASE}/watch/a.txt; sleep 0.3; rm {BASE}/watch/a.txt")`; poll `h.get_new_events()` every 0.2 s for ≤ 5 s until a `REMOVE` for `a.txt` arrives; the collected `[(e.name, e.type)]` contains `("a.txt", CREATE)`, then at least one `("a.txt", WRITE)`, then `("a.txt", REMOVE)` in that relative order, and no name contains `/` or starts with `.rayito-tmp-`; `h.stop()`; `h.is_running is False`; `h.get_new_events() == []` (no exception after a clean stop).
13. `watch_dir` with `on_event` and `include_entry=True` on `f"{BASE}/watch"` collecting into a list; `files.write(f"{BASE}/watch/w.txt", b"w")`; within 5 s the list contains an event `name == "w.txt"` with `type is FilesystemEventType.RENAME` (atomic write surfaces as a rename) and no `.rayito-tmp-` names; `commands.run(f"chmod 600 {BASE}/watch/w.txt")` → a `CHMOD` event with `entry is not None and entry.mode == 0o600` within 5 s; `h.stop()`.
14. `files.watch_dir(f"{BASE}/nope")` raises `FileNotFoundException`; `files.watch_dir(f"{BASE}/big.bin")` raises `InvalidArgumentException`; `h = files.watch_dir(f"{BASE}/watch", timeout=2)` → within 4 s `h.get_new_events()` raises `TimeoutException` and `h.is_running is False`.
15. Recursive: `h = files.watch_dir(f"{BASE}/watch", recursive=True)`; `commands.run(f"mkdir {BASE}/watch/sub && echo x > {BASE}/watch/sub/n.txt")`; within 5 s events include `("sub", CREATE)` and `("sub/n.txt", CREATE)`; `h.stop()`.
16. Connection budget: 30 × `files.exists(f"{BASE}/big.bin")` sequential, all `True`, no `RateLimitException`, total ≤ 20 s (p95 logged); `commands.list() == []`.
17. Async parity: `asyncio.run(...)` with `AsyncSandbox.connect(sbx.sandbox_id, access_token=sbx.access_token)`: `await files.write(f"{BASE}/async.txt", "async")` then `await files.read(f"{BASE}/async.txt") == "async"`; `(await files.list(BASE))` contains `async.txt`; `h = await files.watch_dir(f"{BASE}/watch")`; `await sbx.commands.run(f"touch {BASE}/watch/z.txt")`; poll `await h.get_new_events()` ≤ 5 s until `("z.txt", CREATE)`; `await h.stop()`; `await files.make_dir(f"{BASE}/adir") is True`; `await files.exists(f"{BASE}/nope") is False`.
18. Teardown: `sbx.kill() is True`; `get_info().state in TERMINAL_STATES` within 30 s (M1's poll helper).

`clients/python/tests/e2e/test_m3_bench.py`, marker `bench` only (the e2e
conftest skips `bench` too when `RAYITO_E2E` is unset; `make test-bench` runs
`uv run pytest tests/e2e -m bench -v -s`), one `sandbox`: `payload =
os.urandom(50_000_000)`; `files.write(f"{BASE}/bench.bin", payload)` timed;
`files.read(..., format="bytes")` timed; sha256 equal; total ≤ 120 s
(asserted); write MB/s, read MB/s and total logged and pasted into
`MILESTONES.md` M3 "Estado de aceptación" and `AWS_API_NOTES.md` §16.

Cost: one image version (+$0.037) and one MicroVM of ≈ 3 min for the e2e
plus ≈ 2 min for the bench (< $0.02).
