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
- [x] 1.4 `fuse_device` reuses `dir_walk`.

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

- [x] 5.1 `rayd_core::hook_origin` (parser, `HookOrigin`, `SocketOwners`)
  and `SandboxSession::run_from` with tests.
- [x] 5.2 `HookPeer` connect info, `ProcNetSocketOwners`, `/run` wiring,
  integration test over a real connection.

## 6. Docs and verification

- [x] 6.1 `SECURITY.md` T2/T6/T11/T17, `ARCHITECTURE.md`, site pages,
  changelogs.
- [x] 6.2 Gates: Rust (VM), Python, TypeScript, docs, OpenSpec.
- [ ] 6.3 Real-AWS acceptance (`rayito-base` and `rayito-base-caps` with
  this `rayd`): `create()` succeeds and no `sandbox_origin` appears in the
  `/run` log line; filesystem e2e (`m3`, transfer) green; a template with a
  `start_cmd` that posts `/run` at boot still gets the operator's token;
  an S3 mount reaches `mounted`; a gateway route forwards an allowed
  request and refuses `..;`.
