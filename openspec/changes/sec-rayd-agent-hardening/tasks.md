## 1. Filesystem (RAYD-01)

- [x] 1.1 Domain test: a component swapped for a symlink after
  `canonicalize` is `Denied` for stat, read, export, list, watch, remove,
  rename, mkdir, write and import (fake swap hook, port contract).
- [x] 1.2 `FsIoError::Redirected` → `FilesystemError::Denied`; listings skip
  a subdirectory that became a symlink.
- [x] 1.3 Adapter test reproducing the race on a real filesystem, then
  `adapters::dir_walk` and the descriptor-based `StdFileSystem` (walk,
  `*at` calls, `O_EXCL` temp files, iterative removal, `O_PATH` metadata,
  `fstatfs` refusal of `proc`/`sysfs`/`devpts`).
- [x] 1.4 `mountpoint` (`fuse_device`, `efs_mount`) reuses `dir_walk`.

## 2. Secrets gateway (RAYD-02)

- [x] 2.1 Shared vectors: `..;`, `;jsessionid`, `%252e`, `%u002e`, overlong
  UTF-8, controls, non-ASCII; safe encoded names.
- [x] 2.2 `path_is_safe` as an allowlist; `invalid_allow_path` for `allow`.
- [x] 2.3 Python and TypeScript `allow` path validation over the same
  vectors; proto comment and generated TS.

## 3. Launchers (RAYD-04)

- [x] 3.1 Tests: the probe sets only `PATH` on a cleared environment; a guest
  helper sees no inherited descriptor; a slot posture seals only above it.
- [x] 3.2 `adapters::exec_posture` shared by `PreExecPlan`, the probe and
  `mount-s3`.

## 4. Egress (RAYD-06)

- [x] 4.1 Policy test pinning the name decision on 80 and 443 whatever the
  request form; `NAME_RULE_PORTS` doc, T17 and the network page.

## 5. Hooks (RAYD-08)

- [x] 5.1 Socket-owner lookup and `SandboxSession::run_from` (after the
  merge of `sec-sandbox-isolation`, on its `rayd_core::hook_peer`:
  `PeerOrigin`, `PeerSocketTable`, `ProcNetPeers`; this change's own
  parser was dropped)
  and `SandboxSession::run_from` with tests.
- [x] 5.2 `guard_peers` hands the origin to the `/run` handler
  (`PeerOrigin` request extension); integration test over a real
  connection through `hooks::serve`.

## 6. Listeners (second pass)

- [x] 6.1 Tests: N+1 idle hook connections, the last waits and `/terminate`
  is served once one closes; a half-sent head loses its slot at the
  deadline; one response per connection; `EMFILE` backs off instead of
  spinning (paused clock); the incoming stream keeps the cap and `nodelay`.
- [x] 6.2 `rayd_core::listeners`, `adapters::CappedListener`,
  `hooks::serve`; `main` serves both listeners through the cap.

## 7. Kernel pids (second pass)

- [x] 7.1 Tests: forged `ready` pids 0, 1, a root non-child and a missing
  pid are never signalled; a recycled pid is never signalled; only a
  kernel of the running sidecar is admitted; the live `/proc` table admits
  a real group-leader child and rejects the rest; `killpg` refuses 0, 1
  and its own group.
- [x] 7.2 `KernelProcess`/`KernelProcesses`, `ProcfsProcessTable` adapter,
  `SidecarSupervisor::admit_kernel` at every registration site,
  `ContextEntry.kernel`, `may_signal_group`.

## 8. Identity ceiling (second pass)

- [x] 8.1 Test: uid or gid 65536 refused with `PrivilegedAccount`, 65535
  accepted; the range string equals the constants.
- [x] 8.2 `MAX_UNPRIVILEGED_ID`; `SANDBOX_UID_LAST` derives from it.

## 9. Docs and verification

- [x] 9.1 `SECURITY.md` T1/T2/T6/T7/T11/T12/T17, `ARCHITECTURE.md`, site
  pages, changelogs.
- [x] 9.2 Gates: Rust (VM), Python, TypeScript, docs, OpenSpec.
- [ ] 9.3 Real-AWS acceptance (`rayito-base` and `rayito-base-caps` with
  this `rayd`): `create()` succeeds and no `sandbox_origin` appears in the
  `/run` log line; filesystem e2e (`m3`, transfer) green; a template with a
  `start_cmd` that posts `/run` at boot still gets the operator's token;
  an S3 mount reaches `mounted`; a gateway route forwards an allowed
  request and refuses `..;`; every hook of a create/pause/resume/kill cycle
  answers through the new listener (no connection reset at the platform);
  killing the sidecar kills its kernels (`kernels_killed` ≥ 1, no
  `kernel_pid_rejected` on a genuine boot).
  Partly verified in the 0.7.0 acceptance (2026-10-04, us-east-1):
  `create()`, `m3` and transfer e2e green on both images; S3 mount e2e
  green; the gateway forwards the allowed paths and refuses every encoded
  or `;`/`..` variant with 403; create/pause/resume/kill hooks answer and a
  genuine cycle adds no anomaly; after killing the sidecar the kernel is
  back in 0.2 s with no `kernel_pid_rejected`. Still to measure: the
  `start_cmd` template that posts `/run` at boot, `kernels_killed` ≥ 1 in
  the runtime log, and the absence of `sandbox_origin` in the genuine
  `/run` log line.

