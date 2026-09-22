## Why

M2 gives Rayito running processes but no way to move files in or out of the
sandbox, inspect the filesystem, or watch it for changes, which every real
workload needs (upload a dataset, read back generated output, live reload).
`FilesystemService` is still `PendingFilesystemService` answering
`UNIMPLEMENTED` and `Sandbox.files` raises `NotImplementedError`. M3 is also
where the filesystem's security posture must land, because it lives on the
I/O path and cannot be retrofitted (`openspec/project.md` hard rule 6): a
sandbox's own code (uid 1000) must not be able to use the root-running agent
as a confused deputy to read `/etc`, `/proc`, the agent binary or another
user's home.

## What Changes

Milestone goal, from `MILESTONES.md` ("M3 — Filesystem"), decided in full by
`design.md`:

- `rayd`: `FilesystemService` fully implemented over a `filesystem` domain in
  `rayd-core` (request-path model, deny list, atomic-write session state
  machine, listing walker with depth semantics, watch event translation) and
  two new ports (`FileSystem`, `Watcher`, plus `NameResolver` for owner and
  group names) with `cfg(unix)` adapters in `rayd` (`StdFileSystem` over
  blocking `std::fs` with a per-thread filesystem identity, `NotifyWatcher`
  over `notify` 8.x inotify).
  - `Read`: server-stream of 256 KiB chunks, `O_NOFOLLOW` on the final
    component, regular files only.
  - `Write`: client-stream, N files per stream, chunks ≤ 1 MiB, temp file in
    the destination directory + `fsync` + `rename`, parents created, mode
    honoured (default `0644`), ownership set to the requesting user, temp
    unlinked on any failure or cancellation.
  - `Stat` (`lstat`, symlinks reported with their target), `ListDir` (depth
    semantics, sorted, never follows symlinks while walking, 10 000-entry
    cap), `MakeDir` (parents created; `ALREADY_EXISTS` / `INVALID_ARGUMENT`),
    `Move` (`rename(2)`), `Remove` (`recursive` flag).
  - `WatchDir`: `notify` 8.x without debounce; `WatchStarted` only after the
    inotify watch is installed; `NOT_FOUND`/`INVALID_ARGUMENT` as gRPC status
    before any message; names relative to the watched directory;
    `include_entry`; `KeepAlive` every 50 s of silence; bounded queue (1024)
    that ends the stream with `RESOURCE_EXHAUSTED` on overflow; the watch is
    removed as soon as the client drops the stream; at most 64 live watches.
  - Structural security: every path is normalised, `..` refused, the
    canonical parent is checked against the deny list (`/proc`, `/sys`,
    `/dev`, `/run/rayito`, `/etc`, `/usr`, `/opt/rayito`, the `rayd` binary)
    → `PERMISSION_DENIED`; every blocking I/O call runs under the requesting
    user's filesystem identity (`setfsuid`/`setfsgid`, per thread) so the
    kernel, not a lexical check, enforces `EACCES`; root only with
    `RAYITO_ALLOW_ROOT=1` as in M2.
- Python SDK: `sbx.files` with `read(format="text"|"bytes"|"stream")`,
  `write(path, data: str | bytes | IO)`, `write_files([...])` in one stream,
  `list(depth)`, `exists`, `get_info`, `remove(recursive=True)`, `rename`,
  `make_dir -> bool`, `watch_dir(...) -> WatchHandle` (`get_new_events()`,
  `stop()`, `on_event`, `on_exit`, background thread/task); models
  `EntryInfo`, `FileType`, `FilesystemEvent`, `FilesystemEventType`,
  `WriteEntry`; per-request deadline `60 s + 1 s per MB`. Sync and async
  trees keep the same surface over `_filesystem_base.py`.
- `.proto`: **no changes**. `filesystem.proto` and `common.proto` already
  carry everything; the design fixes how each field is used.
- Image: build-time sanity check extended with the tools the e2e uses
  (`sha256sum`, `stat`, `rm`, `mv`, `mkdir`, `chmod`, `touch`); new
  `rayito-base` version with the M3 `rayd`.
- Tests: `rayd-core` host tests for every domain rule, a `cfg(unix)`
  in-process integration suite `crates/rayd/tests/m3_filesystem.rs`, an
  in-process `FakeFilesystemService` for the SDK unit tests, the real-AWS
  `tests/e2e/test_m3_filesystem.py` and the nightly
  `tests/e2e/test_m3_bench.py` (marker `bench`, 50 MB write + read ≤ 120 s).

Full acceptance criteria: `design.md` "Acceptance test list".

## Capabilities

### New Capabilities
- `filesystem`: `FilesystemService` (Read/Write/Stat/ListDir/MakeDir/Move/
  Remove/WatchDir) with its path policy, filesystem identity, atomic write,
  listing, watch and keepalive rules, and the Python client's `.files`
  surface including the `WatchHandle` contract.

### Modified Capabilities
- (none — `process-lifecycle` is untouched; M3 reuses its `UserLookup`,
  `UserPolicy` and `IdentitySwitch` without changing their contract)

## Impact

- `proto/rayito/v1/*.proto`: unchanged; no regeneration needed.
- `crates/rayd-core`: new `filesystem` module (domain + `FileSystem`,
  `Watcher`, `NameResolver` ports); `session::accepts_new_streams` reused.
- `crates/rayd`: `adapters/std_filesystem.rs`, `adapters/notify_watcher.rs`,
  `adapters/fs_identity.rs`, `adapters/name_resolver.rs` (all `cfg(unix)`
  with `Unsupported*` stand-ins), `filesystem/{manager,read,write,watch}.rs`,
  `grpc/filesystem.rs`; `grpc::router` gains a `FilesystemManager` argument;
  `PendingFilesystemService` removed; `main.rs` wiring; `logging.rs`
  allowlist.
- `Cargo.toml`: `notify` (8.x, `default-features = false`) and `tempfile`
  become `cfg(unix)` dependencies of `rayd`; `nix` already has `user` and
  `fs`.
- `crates/rayd/tests/m1_hello.rs`, `m2_process.rs`,
  `clients/python/tests/e2e/test_m1_hello.py`: adapt to the new `router`
  signature and move the "pending service answers `UNIMPLEMENTED`" probe
  from `FilesystemService.Stat` to `PtyService.Resize`.
- `clients/python`: `_models.py`, `_filesystem_base.py`, `_transport.py`
  (no table changes; `filesystem=True` already exists), `sandbox_sync/
  {main,filesystem}.py`, `sandbox_async/{main,filesystem}.py`,
  `__init__.py`, unit fake `fake_filesystem.py`, e2e `test_m3_filesystem.py`
  and `test_m3_bench.py`; `Makefile` gains `test-bench`; version `0.0.3`.
- `image/Dockerfile`: sanity-check list; one more published version
  (+$0.037 of snapshot storage per week).
- Docs: `ARCHITECTURE.md` `FilesystemService` row and adapter names,
  `MILESTONES.md` M3 snippet, `SECURITY.md` new row for the filesystem
  confused-deputy threat, `AWS_API_NOTES.md` §16 measurements (upload
  throughput through the proxy, client-stream + early error behaviour).
- Governed by `AWS_API_NOTES.md`: bandwidth 4 MB/s per direction at 2 GB
  (Q27), idle policy counts only bytes crossing the endpoint (Q15), early
  trailers-only answers race the client's DATA frames (Q29). M3 adds no
  control-plane call and no new AWS parameter.
