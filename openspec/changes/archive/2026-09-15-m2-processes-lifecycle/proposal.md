## Why

M1 shipped the toolchain, the `.proto` contract, the image pipeline and a
"hello rayd" that only answers `Health`. Rayito still cannot run a process,
stream its output, feed it input, kill it, or reach a port it opens, which is
the whole point of a sandbox. M2 is also where the security defaults of the
spawn path must land, because they live on that path and cannot be safely
retrofitted (moved here from M6 per `SPEC.md` §5 and `openspec/project.md`
hard rule 6).

## What Changes

Milestone goal, from `MILESTONES.md` ("M2 — Procesos, ciclo de vida, red y
seguridad estructural"), plus the scope the orchestrator pulled forward:

- `rayd`: `ProcessService` fully implemented (`Start`, `Connect(pid,
  from_seq)`, `SendInput`, `CloseStdin`, `SendSignal`, `List`) over a domain
  in `rayd-core` (registry, output ring with `seq`, timeout state machine,
  user/env/cwd policy) and a `tokio::process` adapter in `rayd`.
- Structural security on the spawn path: default user `user` (uid 1000),
  root only when `username == "root"` **and** the image sets
  `RAYITO_ALLOW_ROOT=1`; child environment built from scratch (`PATH`,
  `HOME`, `USER`, `LOGNAME` + sandbox `envs` + request `envs`, last wins);
  `setrlimit` (`RLIMIT_NPROC` 512, `RLIMIT_NOFILE` 4096, `RLIMIT_CORE` 0);
  process groups + `killpg`; server-enforced `timeout_ms` (SIGTERM, SIGKILL
  5 s later); 32 KiB output chunks; bounded per-subscriber channels (64
  events) with `output_truncated` when a client stops consuming; per-pid
  1 MiB ring buffer for `Connect(from_seq)`; 30 s retention of terminal
  events; `KeepAlive` every 30 s of stream silence; max 256 live
  processes/PTYs; `Health` stays the only anonymous RPC.
- `HealthService.Metrics` implemented from procfs (`cpu_used_pct`,
  memory, disk, `cpu_count`, timestamp). This pulls `get_metrics()` forward
  from M6 (`MILESTONES.md` M6 bullet) because the orchestrator's M2
  acceptance requires `get_metrics().cpu_count >= 1`.
- Python SDK: `sbx.commands.run()` (foreground and background,
  `on_stdout`/`on_stderr`, `stdin`, `user`, `cwd`, `envs`, `timeout` 60 s
  default enforced by the server, `CommandExitException` on non-zero exit,
  `TimeoutException` on server timeout), `commands.list/kill/send_stdin/
  close_stdin/connect`, `CommandHandle` (`pid`, `wait`, `kill`,
  `disconnect`, `send_stdin`, `close_stdin`, iteration, incremental UTF-8
  decoding), `sbx.get_metrics()`, a second gRPC channel for long streams
  (max two per sandbox), proxy-403 re-mint on stream open, and the
  stream-failure classification (`UNAVAILABLE`/reset → `Health` probe →
  `SandboxNotFoundException` / `SandboxStateException` / `SandboxException`).
  Sync and async trees keep the same surface.
- `.proto`: **no field changes**. Two comment-only corrections in
  `process.proto` (already applied, `buf lint` and `buf build` clean, Python
  regenerated): `StartRequest.timeout_ms` no longer claims that
  `CLOCK_MONOTONIC` stops during suspend (M0 Q19 measured that it advances),
  and `EndEvent.status` documents the `"output_truncated"` value.
- Image: `python3` resolvable (symlink to `python3.12`), a build-time sanity
  check that the tools the acceptance test needs exist; `rayd` keeps running
  as root; `RAYITO_ALLOW_ROOT` is not set.
- Docs: `MILESTONES.md` M2 acceptance snippet aligned with the e2e (server
  timeout raises `TimeoutException`; `get_metrics()` in M2).

Full acceptance criteria: `design.md` "Acceptance test list" (real AWS,
`clients/python/tests/e2e/test_m2_processes.py`).

## Capabilities

### New Capabilities
- `process-lifecycle`: `ProcessService` (Start/Connect/SendInput/CloseStdin/
  SendSignal/List) with the structural spawn security defaults, output
  streaming/ring/retention/keepalive rules, `HealthService.Metrics`, and the
  Python client surface for commands (`commands.*`, `CommandHandle`,
  `CommandResult`, `ProcessInfo`), `get_metrics()`, the second gRPC channel
  and the stream error contract.

### Modified Capabilities
- (none — M2 is additive on top of the M1 hexagonal skeleton and hello-rayd
  path; no existing spec in `openspec/specs/`)

## Impact

- `proto/rayito/v1/process.proto`: comment-only edits (done).
- `crates/rayd-core`: new `process` module (domain + `ProcessSpawner` port)
  and `metrics` module (domain + `MetricsProbe` port); `session` exposes the
  spawn defaults it already parses from `runHookPayload`.
- `crates/rayd`: `adapters/` (`TokioProcessSpawner`, `ProcfsMetricsProbe`,
  `cfg(unix)`), `process/` (application service: pumps, timeout, stdin,
  subscribers, retention), `grpc/process.rs`, `grpc/keepalive.rs`, real
  `Metrics`, `main.rs` wiring; `PendingProcessService` removed.
- `Cargo.toml`: `tokio` gains `process` + `io-util`; `nix` gains
  `resource`; `tokio-stream` gains `sync`; `rayd` depends on `nix`,
  `futures`, `tokio-stream`.
- `clients/python`: `_models.py`, `_process_base.py`, `_transport.py`,
  `sandbox_sync/{main,commands}.py`, `sandbox_async/{main,commands}.py`,
  `exceptions.py` (no new classes), unit fake for `ProcessService`, e2e
  `test_m2_processes.py`.
- `image/Dockerfile`: `python3` symlink + sanity check; new image version
  published by `image-publish` (one more week of snapshot storage, $0.037).
- Governed by `AWS_API_NOTES.md` for everything touching the proxy
  (connection cap, idle semantics, token expiry on connect) — no invented
  parameters; M2 adds no new control-plane call.
