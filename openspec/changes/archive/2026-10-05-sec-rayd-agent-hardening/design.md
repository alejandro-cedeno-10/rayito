## Context

`rayd` runs as root and lowers only its filesystem identity per call
(`setfsuid`), which removes `CAP_DAC_*`/`CAP_FOWNER` but none of its other
capabilities. Much of what it touches on behalf of the operator is writable
by uid 1000. Hooks reach it over loopback from inside the VM. The five
findings share one root cause: a check made on one representation (a path
string, an encoding list, a launcher's assumptions, a doc sentence, a
once-per-boot claim) that the later action does not honour.

## Decisions

### D1. Resolve once, act on descriptors (RAYD-01)

The domain keeps `canonicalize` + deny check on the string (it is portable
and testable against the fake). The adapter never resolves that string
again by name: `dir_walk::open_dir_beneath("/", parent, ..)` opens every
component with `O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC` relative to
the previous descriptor; `ENOTDIR`/`ELOOP` are classified with an `lstat`
of the entry (`Symlink` vs `NotADirectory`). Because the canonical string
has no symlink in any component, a symlink found during the walk can only
be a swap after the check, so it is refused (`FsIoError::Redirected` →
`FilesystemError::Denied`), never followed. The final component keeps each
operation's own semantics (`fstatat(AT_SYMLINK_NOFOLLOW)`, `openat` with
`O_NOFOLLOW`, `unlinkat`, `renameat`, `mkdirat`).

- `openat2(RESOLVE_NO_SYMLINKS)` was considered: it needs Linux 5.6 and a
  fallback walk anyway, so only the walk is implemented (one code path).
- The walk starts at `/` and the canonical string never contains `..`, so
  the opened directory is the one the string names; a re-check of
  `readlink(/proc/self/fd/N)` against the deny list would add a `/proc`
  dependency for no case the walk misses (a rename of an ancestor cannot
  move a uid-1000 directory into a denied tree it cannot write).
- Defence in depth: the walked directory is `fstatfs`ed and `proc`,
  `sysfs` and `devpts` are refused whatever path led there.
- Temp files: `openat(dirfd, ".rayito-tmp-<12 hex>", O_CREAT|O_EXCL|O_NOFOLLOW)`
  with `OsRandomSource`, bounded retries; commit is `renameat` inside the
  same descriptor, drop is `unlinkat` of the sink's own name.
- Recursive removal is iterative (no stack recursion on a deep tree), one
  `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` descriptor per level; a child that is
  not a directory (or became a symlink) is unlinked as it is.
- Metadata reads open the entry `O_PATH|O_NOFOLLOW` (no FUSE open, no read
  permission for the open), skip anything but regular files and
  directories, and read the set through `/proc/self/fd/<n>`, which names
  that inode (`flistxattr` refuses an `O_PATH` descriptor).
- The mountpoint walk both mount features share (`mountpoint`, used by
  `fuse_device` and `efs_mount`) now uses the same module (no
  duplication); its error mapping is unchanged.
- The fake enforces the same contract (a symlink in a component the port
  does not follow is `Redirected`) and can swap a directory for a symlink
  right after the next `canonicalize`, which is how the domain test
  reproduces the race.

### D2. Gateway paths are an allowlist (RAYD-02)

Rejecting `%` outright would break legitimate encoded names (`a%20b`), so
the path is decoded exactly once per segment and the decoded form is what
the dot-segment and separator checks see. Raw bytes are limited to RFC 3986
`pchar` minus `;`; decoded bytes may not be controls, `/`, `\`, `%` or `;`;
the decoded segment must be valid UTF-8 (which refuses overlong encodings
such as `%c0%ae`). The forwarded path is still the raw one, unchanged.
`allow` paths must pass the same rule (the wildcard `*` is a sub-delim, so
`/v1/*` passes), refused as `invalid_allow_path` by `rayd` and as
`InvalidArgumentException`/`InvalidArgumentError` by the SDKs, all three
reading `testdata/secret-gateway/request-paths.json`.

### D3. One exec posture for every launcher (RAYD-04)

`exec_posture` owns `seal_descriptors_from(first, ceiling)` (close_range
with `CLOSE_RANGE_CLOEXEC`, `fcntl` fallback) and
`reset_signal_dispositions`, used by `PreExecPlan` and by `ExecPosture`
(`as_user(uid, gid)`, `inheriting_up_to(slot)`). The probe is built by
`guest_command`: `env_clear()`, `PATH=DEFAULT_PATH`, null stdio, posture in
`pre_exec`, binary `/usr/bin/stat` (in `mountpoint`, so the EFS volume
probe gets it too). `mount-s3` keeps its `dup2` onto
`MOUNT_FD_SLOT` and seals above it.

### D4. Egress `Host` rewrite: document, do not refuse (RAYD-06)

Refusing a `CONNECT`/SOCKS5 tunnel to an allowed name on port 80 would
break `ws://` and every client that carries plain HTTP through SOCKS5: a
product decision, not taken here. The decision code is unchanged; T17, the
network page and `NAME_RULE_PORTS` now say the rewrite covers only the
absolute form and that tunnels on 80 and 443 share the shared-IP residual.

### D5. `/run` origin by socket owner (RAYD-08)

After the merge of `sec-sandbox-isolation` there is one socket-owner
lookup: its `guard_peers` middleware classifies every hook call
(`rayd_core::hook_peer::classify_peer` over `ProcNetPeers`, the
`/proc/net/tcp{,6}` row whose local end is the caller's address) and now
inserts the `PeerOrigin` it found as a request extension. The `/run`
handler passes it to `SandboxSession::run_from(origin, ..)`, which refuses
`PeerOrigin::Sandbox` (uid 1000-65535) before the once-per-boot claim,
counting an anomaly. `Platform` and `Unverified` behave as before, so a
failed lookup never blocks the genuine `/run`. This change's own parser,
port and connect-info type were dropped in the merge (one mechanism, not
two).

- Freezing `start_cmd` (`SIGSTOP` of its group before `/ready`, `SIGCONT`
  after `Installed`) was considered: a process that left the group with
  `setsid` escapes it, freezing every uid ≥ 1000 would also freeze the
  kernel sidecar the build's `/validate` needs, and it changes the template
  build path, which needs real-AWS validation. Not done; listed as residual.
- Only `/run` is guarded: refusing a session-changing `/suspend`/`/resume`
  is out of scope (hook-defense), and `/terminate`/`/validate` stay C-01.
- Risk accepted for acceptance: the platform's `/run` must arrive from a
  socket owned by a uid < 1000 (Q48 measured the agent's sockets at
  991-994). The acceptance run checks `hook_audit`/`sandbox_origin` never
  appears on a genuine create.

### D6. One connection cap for both listeners (second pass)

A second review pass found that neither listener bounded its connections:
tonic's `concurrency_limit` is per connection, `axum::serve` accepts
without limit and builds hyper without a timer (so the 30 s head timeout
never fires), and tonic retries a failed `accept` with `continue`. Sandbox
processes reach both ports on loopback, and `rayd` cannot raise its 1024
descriptors.

- `adapters::CappedListener<A: Accept>` holds a `Semaphore`; `accept`
  first takes an owned permit, then accepts, and the `CappedStream` carries
  the permit until it is dropped: the proxy's pattern (`network::proxy`),
  moved to the listener side. Acquiring before `accept` (instead of
  accepting and refusing) keeps an over-cap connection in the kernel
  backlog without a descriptor. `into_incoming()` is the `Stream` tonic
  serves; it never yields an error, so tonic's `continue` is never reached.
- `AcceptFailure::classify` (in `rayd-core`, by `ErrorKind`): reset,
  aborted, refused, interrupted and would-block retry at once; everything
  else (`EMFILE`, `ENFILE`, `ENOBUFS` have no kind of their own) waits
  `ACCEPT_BACKOFF`, logged once per streak.
- Hooks: `hooks::serve` replaces `axum::serve` with a small loop over
  `CappedListener` and `hyper::server::conn::http1` with `TokioTimer`,
  `header_read_timeout(HOOKS_HEADER_READ_TIMEOUT)` and `keep_alive(false)`,
  graceful shutdown per connection on cancel. Closing after each response
  is what makes the head deadline safe: hyper starts it whenever it waits
  for a head, including on an idle kept-alive connection, and a deadline
  there could race a platform request on a reused socket. The router and
  the handlers do not change; the peer is inserted as `ConnectInfo`.
- Sizes: 256 gRPC (the SDK opens ≤ 2 channels per `Sandbox`), 32 hooks
  (one hook at a time from the platform), so with the proxy's 2 × 128 the
  listeners stay under 3/4 of 1024 (a unit test). Head deadline 10 s, the
  shortest declared hook timeout. Backoff 100 ms.
- Residual, documented in T7: a sandbox process can fill its own VM's
  slots (the same boundary as a fork bomb; a forged `/terminate` already
  ends the VM), and a body trickled after a complete head keeps its slot.
  gRPC has no handshake deadline; its cap bounds the descriptors.

### D7. Kernel pids are admitted against the process table (second pass)

The sidecar is a uid-1000 process and other uid-1000 processes can open its
`/proc/<pid>/fd/1` (no Yama), so a forged `ready` or reply line could name
any pid for `rayd`'s root `killpg`. A start-time check alone would not
help: the attacker names a live pid.

- `rayd_core::code::kernel_process`: `KernelProcess::admit(pid,
  sidecar_pid, kernel_facts, sidecar_facts)` requires pid ≥ 2, the kernel's
  `ppid == sidecar_pid`, `pgrp == pid` (kernels start with
  `start_new_session`) and `uid ==` the sidecar's real uid (read from the
  table, so the dev loop, where the sidecar shares `rayd`'s uid, still
  works). `may_signal(now)` allows the signal when the same process is
  there (start ticks, uid, still leader) or when no process has the pid
  (Linux does not reuse a pid while its group exists, so the number names
  the kernel's leftover group or nothing); a recycled pid is never
  signalled.
- Port `KernelProcesses { admit, signal }`, adapter `ProcfsProcessTable`
  (the orphan reaper's `/proc` table: `stat` fields 4, 5, 22 and the real
  uid from `status`, with `stat` read again after `status` so a pid recycled
  between the reads never mixes two processes). It replaces the
  `KernelSignaller` closure; every registration site (`ready`, rotation,
  create, restart, restart after resume) goes through
  `SidecarSupervisor::admit_kernel`, and `ContextEntry.kernel` stores the
  value object instead of a bare `u32`.
- `signal_process_group` refuses 0, 1 and `rayd`'s own pgid whatever the
  caller (`may_signal_group`).
- The integration tests' fake sidecar reports made-up pids, so their
  harness uses a recording `KernelProcesses` (m4) or the real table, which
  rejects them (m5, common).

### D8. The identity gate has a ceiling (second pass)

`MAX_UNPRIVILEGED_ID = 65_535` next to the floor; `is_unprivileged` checks
both ids against `MIN..=MAX`. `SANDBOX_UID_RANGE` stays the `&str` the `ip`
commands and the probe compare against; tests pin it to the two constants
(`network::route_plan`'s `SANDBOX_UID_MIN`/`SANDBOX_UID_MAX`, which the
peer check uses, are now the identity constants themselves).

## Out of scope

- `WatchDir` installs its inotify watch by path after validating the root
  by descriptor (names only, under the user's identity).
- The home archiver of persistence walks by path inside the home without
  following symlinks or crossing filesystems.
- Per-uid authentication of `/suspend`, `/resume`, `/terminate`,
  `/validate` (C-01, restated as the highest-impact standing gap by the
  second review pass; no new action).
- A handshake deadline on the gRPC listener and a body-read deadline on
  the hooks listener.
