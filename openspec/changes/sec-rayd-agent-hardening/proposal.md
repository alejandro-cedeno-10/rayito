## Why

A security review of the `rayd` agent (the open-source agent that runs as
root, PID 1, inside every sandbox) confirmed five findings where code
running in the sandbox, or a third-party `start_cmd`, could make `rayd`
act outside what its documentation promises. None leaks a credential in
the default configuration, but each weakens a stated guarantee:

- **RAYD-01 (low)**: the filesystem deny list was checked on a resolved
  path string that the adapter then resolved again by name, so a component
  swapped for a symlink between the check and the use was followed (only
  the last component had `O_NOFOLLOW`).
- **RAYD-02 (low)**: the secrets gateway refused a short list of known-bad
  encodings, so path parameters (`;`) and double encoding could carry an
  allowed prefix past an upstream that normalises them.
- **RAYD-04 (low)**: the S3 readiness probe, launched as uid 1000, inherited
  `rayd`'s whole environment and skipped the descriptor seal and signal
  reset every other launcher applies.
- **RAYD-06 (info)**: `SECURITY.md` T17 described the egress proxy's `Host`
  rewrite without saying it only covers absolute-form requests.
- **RAYD-08 (low)**: with templates, `start_cmd` runs before the platform's
  `/run`, which is accepted once per boot, so it could claim `/run` first.

A second review pass confirmed three more (and restated C-01 as info):

- **Listeners (low)**: neither the gRPC nor the hooks listener capped its
  connections, the hooks server never applied a request-head timeout, and
  tonic retried a failed `accept` without pause, so sandbox processes could
  hold `rayd`'s 1024 descriptors and keep the platform's hooks out.
- **Kernel pids (low)**: `rayd` killed process groups by pids the sidecar
  reported, and sandbox processes can write into the sidecar's pipe, so a
  forged line could make root signal any group (its own included).
- **Identity ceiling (low)**: the identity gate had no upper bound while the
  IMDS and egress `uidrange` rules stop at 65535.

## What Changes

- Filesystem adapter: every operation but `realpath` walks the parent of
  the canonical path one component at a time with `O_PATH | O_NOFOLLOW`
  (`adapters::dir_walk`, shared with the mountpoint walk of
  `mountpoint`, used by both mount features) and acts with `*at` calls on that descriptor; a symlink in
  the walk or a directory on `proc`/`sysfs`/`devpts` answers the new port
  error `FsIoError::Redirected`, which the domain maps to `Denied`. Writes
  create their temp file with `O_EXCL` inside the walked directory and
  commit with `renameat`; recursive removal empties each level by
  descriptor; metadata is read through the entry's own `O_PATH` descriptor.
- Secrets gateway: `path_is_safe` becomes an allowlist (RFC 3986 path
  characters except `;`, decode once, no control byte, no decoded `/`,
  `\`, `%` or `;`, no `.`/`..` segment, no empty segment but the last). An
  `allow` path that fails the same rule is refused at `Configure`
  (`invalid_allow_path`) and by both SDKs before any RPC; the shared
  vectors in `testdata/secret-gateway/request-paths.json` grow accordingly.
- Launchers: `adapters::exec_posture` holds the descriptor seal and the
  signal reset (moved out of `PreExecPlan`) plus `ExecPosture`, a fixed
  identity drop for internal launchers. The `stat` probe uses an absolute
  binary, an environment with only `PATH` and that posture; `mount-s3`
  seals every descriptor above its FUSE slot.
- Egress: no behaviour change; `NAME_RULE_PORTS`, T17 and the network page
  state that the `Host` rewrite only covers the absolute form, with a
  policy test pinning the decision for both ports.
- Hooks: `rayd` reads the uid owning the caller's socket
  (`/proc/net/tcp{,6}`, `rayd_core::hook_origin`, `HookPeer` connect info)
  and refuses a `/run` from a sandbox uid (1000-65535) without claiming the
  boot's `/run` (`RunOutcome::SandboxOrigin`, 200 `sandbox_origin`, one
  more `hook_anomalies`).
- Listeners: `adapters::CappedListener` (semaphore permit per connection,
  backoff on resource failures) in front of both listeners; the hooks
  listener is served by `hooks::serve` (hyper HTTP/1.1 with a 10 s head
  deadline and one request per connection) instead of `axum::serve`.
  Bounds in `rayd_core::listeners`.
- Kernel pids: port `KernelProcesses` (admit a reported pid against the
  process table, signal only while the pid still names that kernel),
  adapter `ProcfsProcessTable`; `ContextEntry.kernel` holds a
  `KernelProcess`; `signal_process_group` refuses groups 0, 1 and its own.
- Identity: `MAX_UNPRIVILEGED_ID` (65535) bounds uid and gid in
  `authorize_identity`.
- Docs: `SECURITY.md` T1, T2, T6, T7, T11, T12, T17, `ARCHITECTURE.md` (hook origin,
  ADR-006, ADR-022, ADR-023), the site's security, network, gateway, S3
  mounts and templates pages, and the three component changelogs.

## Impact

- Rust: `rayd-core` (`filesystem`, `secret_gateway::route`,
  `network::policy`, new `hook_origin`, `session`), `rayd` (adapters
  `std_filesystem`, `mountpoint`, `mount_s3`, `process_spawner`, new
  `dir_walk`, `exec_posture`, `proc_net_sockets`, `capped_listener`,
  `procfs_process_table`, `sidecar_process`; `hooks` with `hooks::serve`;
  `code` supervisor and manager; `main`), `rayd-core` (`listeners`,
  `code::kernel_process`, `process::identity`). Workspace `nix` gains the
  `dir` feature and `hyper` the `server` feature (same crates, no new
  dependency, `Cargo.lock` unchanged).
- SDKs: Python and TypeScript `SecretGateway` validate `allow` paths;
  generated TS comments follow the proto comment change.
- Runtime behaviour changes need real-AWS acceptance before archiving
  (filesystem RPCs, gateway, S3 probe, `/run` origin, the hooks listener
  closing each connection after its response, kernel kill on sidecar exit).
