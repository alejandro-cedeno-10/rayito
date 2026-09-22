## 1. [proto] Contract (comment-only, already applied by the spec agent)

- [x] 1.1 `process.proto`: `StartRequest.timeout_ms` comment states the monotonic clock advances during suspend (M0 Q19); `EndEvent.status` comment lists `output_truncated`
- [x] 1.2 `buf lint` and `buf build` clean; `python scripts/gen_python.py` regenerated `clients/python/src/rayito/v1`; `import rayito.v1.process_pb2_grpc` works
- [x] 1.3 `cargo build -p rayito-proto` regenerates the Rust types (no diff expected beyond doc comments)

## 2. [e2e] Acceptance test first (red)

- [x] 2.1 Create `clients/python/tests/e2e/test_m2_processes.py` with the 20 assertions of `design.md` "Acceptance test list" (marker `e2e`, `sandbox` fixture, `urllib.request` for `get_host`, M1's `wait_for_terminal_state` helper reused)
- [x] 2.2 Add an `AsyncSandbox` parity block (assertion 19) run with `asyncio.run` inside the same file
- [x] 2.3 Run `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> uv run pytest tests/e2e/test_m2_processes.py -m e2e -x` against the M1 image and confirm it fails at assertion 1 with `InvalidArgumentException` (`UNIMPLEMENTED`) (superseded: the SDK cannot pin `imageVersion` and the M2 binary was already published as 2.0 when the acceptance run started; the red run happened against 2.0 instead and exposed two real platform facts, see 7.1)

## 3. [rayd] Domain in `rayd-core`

- [x] 3.1 `Cargo.toml` workspace: add `process`, `io-util` to `tokio` features; `resource` to `nix` features; `sync` to `tokio-stream` features; `rayd` depends on `nix`, `futures`, `tokio-stream`
- [x] 3.2 `rayd-core/src/process/{mod,error,events,identity,env,cwd,limits,spec,ring,timeout,registry,ports}.rs` per design D2 (types, `ProcessError` with `thiserror`, `Pid` newtype)
- [x] 3.3 `identity.rs`: `resolve_username` (request → payload `user` → `"user"`) and `UserPolicy::authorize` (root only with `allow_root`); unit tests
- [x] 3.4 `env.rs`: `build_child_env` (PATH/HOME/USER/LOGNAME → sandbox envs → request envs, sorted output); tests for order, override and no inheritance
- [x] 3.5 `cwd.rs`: `resolve_cwd` precedence and `validate_cwd` (absolute, no NUL); tests
- [x] 3.6 `spec.rs`: `plan_spawn(input, defaults, policy, lookup)` with a `FakeUserLookup` test double; tests for empty cmd, unknown user, root refused/allowed, stdin mode, tag passthrough
- [x] 3.7 `ring.rs`: `OutputRing` 1 MiB by payload bytes, `seq` from 1, whole-event eviction, `replay_from` returning `OutOfRange{oldest, next}`; tests incl. `from_seq == next_seq` accepted
- [x] 3.8 `timeout.rs`: `TimeoutPlan{term_at, kill_at}` and `end_from_wait(WaitOutcome, EndReason) -> ProcessEnd` producing the five `EndStatus` shapes of design D1; tests for exited, signaled, timeout-after-exit-0, timeout-after-SIGKILL
- [x] 3.9 `registry.rs`: `ProcessRegistry` with `max_live=256`, `max_subscribers_per_pid=8`, `retention=30 s`, `register/push_output/mark_ended/reap_expired/replay/live/subscribe`; tests with a fake `Clock` for cap, subscriber cap, retention expiry, `live()` excluding ended
- [x] 3.10 `ports.rs`: `UserLookup`, `ProcessSpawner`/`SpawnedChild` traits (sync fns, no tokio types)
- [x] 3.11 `session.rs`: `spawn_defaults()` and `accepts_new_streams()` (`Running | Resumed` only); tests
- [x] 3.12 `rayd-core/src/metrics.rs`: `parse_proc_stat`, `parse_meminfo`, `cpu_used_pct`, `MetricsSnapshot`, `MetricsProbe` trait; fixture tests (real `/proc/stat` and `/proc/meminfo` excerpts, zero-delta case)
- [x] 3.13 `cargo test -p rayd-core` green on Windows host; `cargo clippy -p rayd-core --all-targets -- -D warnings` clean

## 4. [rayd] Adapters and gRPC in `rayd`

- [x] 4.1 `adapters/process_spawner.rs` (`cfg(unix)`): `NixUserLookup` (`User::from_name` + `getgrouplist`), `TokioProcessSpawner{identity_switch}` with `env_clear`, `current_dir`, `process_group(0)`, stdio per `StdinMode`, single `pre_exec` doing `setrlimit ×3 → setgroups → setgid → setuid` (design D3); `UnsupportedSpawner` for `cfg(not(unix))`
- [x] 4.2 `IdentitySwitch::{Enforce, KeepCurrent}` chosen in `main.rs` from `geteuid()`, one `warn!` in `KeepCurrent`; rlimits clamped to parent hard limits in `KeepCurrent`
- [x] 4.3 `adapters/procfs_metrics.rs` (`cfg(unix)`): `ProcfsMetricsProbe` (two `/proc/stat` samples 100 ms apart, `/proc/meminfo`, `statvfs("/")`, `available_parallelism`); `UnsupportedProbe` otherwise
- [x] 4.4 `process/subscriber.rs`: bounded `mpsc(64)` subscriber, `send_timeout(30 s)` stall detection, `SubscriberStream` that appends the `output_truncated` `EndEvent` when detached, replay slice delivered before live events without gaps
- [x] 4.5 `process/manager.rs`: `ProcessManager::start` (phase gate → cap → `plan_spawn` → cwd `is_dir` check → spawn → register → pumps/stdin writer/timeout/waiter tasks), `connect`, `send_input`, `close_stdin` (idempotent), `send_signal` (`killpg`, `1..=64`), `list`, reaper task every 5 s using the `Clock` port
- [x] 4.6 Output pumps: 32 KiB reads on stdout and stderr, shared `seq`, ring push, fan-out; `EndEvent` emitted only after both EOFs and `wait()`; `EndReason` set by timeout/`SendSignal`
- [x] 4.7 Timeout task: `sleep_until(term_at)` → `SIGTERM`, `sleep_until(kill_at)` → `SIGKILL` if not reaped; `timeout_ms == 0` → no task
- [x] 4.8 `grpc/keepalive.rs`: `KeepAliveStream` emitting `ProcessEvent{keepalive}` after a configurable idle interval (default 30 s)
- [x] 4.9 `grpc/process.rs`: `ProcessGrpc` implementing all six RPCs with proto ↔ domain conversion and the status table of design D11 (`INVALID_ARGUMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `FAILED_PRECONDITION`, `RESOURCE_EXHAUSTED`, `OUT_OF_RANGE`, `UNAVAILABLE`, `INTERNAL`); error messages never echo `cmd`/`args`/`envs`
- [x] 4.10 `grpc/health.rs`: `Metrics` via `MetricsProbe` (`UNAVAILABLE` when unsupported); `grpc/mod.rs`: `router(session, process_manager, metrics_probe)`; remove `PendingProcessService`
- [x] 4.11 `main.rs`: build `UserPolicy` from `RAYITO_ALLOW_ROOT=1`, spawner, probe, manager; wire into the router; `logging.rs` allowlist doc extended (`pid`, `status`, `exit_code`, `signal`, `seq`, counts, `duration_ms`, `errno`, `identity_switch`)
- [x] 4.12 Adapt `crates/rayd/tests/m1_hello.rs` to the new `router` signature (unsupported spawner on Windows, real one on unix); M1 assertions unchanged except `List` now answering `OK` with an empty list once the token is installed
- [x] 4.13 `crates/rayd/tests/m2_process.rs` (`#![cfg(unix)]`): the integration cases of design D16 (echo/exit/stderr/stdin/timeout/signal/connect replay/out-of-range/retention/list/caps/truncation/keepalive/env/cwd/metrics; root refusal `#[ignore]` unless root); keepalive, stall and retention intervals injected via constructor parameters
- [x] 4.14 `cargo test --workspace` green on Windows (unix tests compiled out) and on Linux; `cargo fmt --all --check`; `cargo clippy --workspace --all-targets -- -D warnings` clean (Windows host green 2026-09-15: 97 core + 8 rayd + 12 M1 tests, fmt and clippy clean, `cargo-zigbuild clippy --target aarch64-unknown-linux-musl --all-targets` clean; the `cfg(unix)` suite executed for real on 2026-09-15 inside `rust:1.98.1-slim-bookworm` under Docker Desktop, x86_64, as root: `cargo test --workspace` green, `m2_process` 25/25 three times in a row; first execution exposed two test-only defects, fixed: `cat` exited before the second `CloseStdin` so the pid was already `NOT_FOUND`, now `cat; sleep 1`; Debian `dash` has no `ulimit -u`, now `RLIMIT_NPROC` is read from `/proc/self/limits`)
- [x] 4.15 `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd` produces a statically linked `rayd`
- [x] 4.16 Review fix: `SubscriberSlot` port (`rayd-core::process::ports`), `ProcessRegistry<S: Clone + SubscriberSlot>` prunes closed slots in `attach` before the cap; `SubscriberSink::is_open = !Sender::is_closed()`; registry unit test with a fake sink that flips closed; integration case `dropped_streams_free_their_subscriber_slots_on_a_silent_process` (8 × connect+drop on `sleep 30`, then connect succeeds)
- [x] 4.17 Review fix: `RegistryLimits.max_retained` (default 256); `mark_ended` evicts the oldest ended entries by `ended_at` beyond the cap; unit tests (300 ended → 256 kept, evicted pid → `NotFound`, live entries untouched); integration case `retained_entries_are_capped_before_retention_expires` (`max_retained = 2`); design D8/D18/risks and the `process-lifecycle` retention requirement updated

## 5. [sdk] Python client

- [x] 5.1 `_models.py`: `CommandResult`, `ProcessInfo`, `SandboxMetrics`; export from `rayito/__init__.py` together with `CommandHandle`/`AsyncCommandHandle`
- [x] 5.2 `_process_base.py`: constants, `build_start_request`, `stream_deadline`, `validate_pid`/`validate_from_seq`, `OutputAccumulator` (incremental UTF-8, callbacks, `last_seq`), `outcome_from_end`, `process_info_from_proto`, `metrics_from_proto`, `stream_failure_exception`
- [x] 5.3 `_transport.py`: `FAILED_PRECONDITION → InvalidArgumentException`, `OUT_OF_RANGE → NotFoundException`, `translate_stream_error("output_truncated") → SandboxException`, `is_stream_reset`
- [x] 5.4 `sandbox_sync/commands.py`: `Commands` (`run` overloads, `list`, `kill`, `send_stdin`, `close_stdin`, `connect`) and `CommandHandle` (`pid`, `last_seq`, `stdout`, `stderr`, `exit_code`, `error`, `wait`, `kill`, `disconnect`, `send_stdin`, `close_stdin`, `__iter__` skipping keepalives)
- [x] 5.5 `sandbox_sync/main.py`: `commands` property, `get_metrics()`, lazy `_stream_channel` (max two channels, both closed in `close()`), `_open_stream` with one proxy-403 re-mint before the first message, `_stream_failure` (Health probe 5 s → `get_microvm` mapping); remove the `commands` `NotImplementedError` stub
- [x] 5.6 `sandbox_async/commands.py` and `sandbox_async/main.py`: same surface over `grpc.aio` (`AsyncCommands`, `AsyncCommandHandle`, `__aiter__`, `await wait()`), same channel and error rules
- [x] 5.7 `tests/unit/fake_process.py`: `FakeProcessService` (scripted `echo`/`exit`/`sleep`/`cat`/`err`/`big`/unknown, `timeout_ms`, `SendSignal`, per-pid ring, `Connect(from_seq)`, token check on every RPC); register it in the `fake_rayd` fixture
- [x] 5.8 `tests/unit/test_process_base.py`: request shape, deadline math, decoder split at a chunk boundary, `outcome_from_end` matrix, `stream_failure_exception` matrix
- [x] 5.9 `tests/unit/test_commands_sync.py` and `test_commands_async.py`: foreground/background parity, `CommandExitException` fields, `TimeoutException`, `kill` True/False, stdin round-trip and `InvalidArgumentException` without stdin, `connect` replay and `NotFoundException`, `list` mapping with `kind`, `output_truncated → SandboxException`, 403-on-open re-mint (Stubber expects one extra `create_microvm_auth_token`), at most two channels, `get_metrics` mapping, `disconnect` keeps the process listed
- [x] 5.10 `uv run pytest tests/unit`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` all clean; `pyproject.toml` version bumped to `0.0.2`

## 6. [image] Dockerfile and publish

- [x] 6.1 `image/Dockerfile`: `ln -sf /usr/bin/python3.12 /usr/local/bin/python3`; build-time `RUN` sanity check for `/bin/bash`, `whoami`, `id`, `sleep`, `head`, `tr`, `env`, `python3`; header comment updated (M2); no `RAYITO_ALLOW_ROOT`
- [x] 6.2 `make image-zip` equivalent by hand (`cargo zigbuild` + `python scripts/image_zip.py image image/rayito-image.zip`) and `scripts/publish_image.py` → new `rayito-base` version `SUCCESSFUL` + `ACTIVE`; record `snapshotBuild` sizes in the change notes (2026-09-15: version `2.0` from `rayd-3ee429d6a78e.zip` (3 426 247 B) in 123.8 s, memory 581 709 824 B / code install 435 101 696 B / disk 23 502 848 B; version `3.0` from `rayd-59ab4c9ba9cd.zip` (3 429 831 B, adds the rejected-body drain) in 113.4 s, memory 579 080 192 B / code install 433 504 256 B / disk 22 970 368 B; `chipsetGeneration` 3 both)
- [x] 6.3 Local loop: `cargo run -p rayd` under WSL2 + `python scripts/hooks-sim.py --skip-terminate` + a manual `Start` with `grpcurl` or the SDK against `127.0.0.1:8080` with `TransportSettings(local)`; `echo hola` and `whoami` work before publishing (replaced by the in-process `cfg(unix)` suite `crates/rayd/tests/m2_process.rs` run under Docker, 4.14, and by the real-AWS run of 7.1; no WSL2 on this box)

## 7. [e2e] Green and close

- [x] 7.1 `RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> make test-e2e` (or the `uv run pytest tests/e2e -m e2e -v -s` equivalent): `test_m1_hello.py` and `test_m2_processes.py` pass; paste timings (echo p95, 3 MB transfer, 30-command total, timeout latency) into `MILESTONES.md` M2 "Estado de aceptación" (2026-09-15: first run on 2.0 failed twice for real — anonymous `List` came back `CANCELLED` through the proxy (fixed: the token layer drains the rejected body, 3.0) and `ulimit -n` is capped at 1024 by the platform (test relaxed, documented); rerun on 3.0: 2 passed in 71.45 s; `test_m1_hello.py` adapted so the authenticated `List` returns an empty list and `FilesystemService.Stat` keeps proving `UNIMPLEMENTED`)
- [x] 7.2 Record the new §16 measurements in `AWS_API_NOTES.md` (proxy pings on an idle stream channel; server-stream `Start` latency) (Q17 extended with the M2 server-stream numbers, Q29 early trailers-only response → `CANCELLED`, Q30 `RLIMIT_NOFILE` hard cap, Q31 idle channel/stream survived the 71 s test with 30 s pings; §7 and §9 updated)
- [x] 7.3 Docs: `MILESTONES.md` M2 snippet (`TimeoutException`, `get_metrics()`), M6 bullet (`get_metrics()` moved to M2); `ARCHITECTURE.md` `ProcessService` row (stall rule, `output_truncated`); `SECURITY.md` T7 marked implemented in M2 (plus the `NOFILE` clamp note in MILESTONES/SECURITY and the rejected-body drain in ARCHITECTURE "Auth interna")
- [x] 7.4 `openspec validate m2-processes-lifecycle --strict` passes; acceptance agent runs `openspec archive m2-processes-lifecycle --yes`
