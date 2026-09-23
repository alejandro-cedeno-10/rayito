# Tasks — m9-server-timeout

The order is: contract, then domain, then agent, then image, then the Q63
measurement, then the SDKs, then docs, then acceptance. Tick a box only when
its gate is green, and paste the evidence (command, counts, real output) in
the task note.

Tracked files get placeholders only (`123456789012`,
`amzn-s3-demo-bucket`, `<tu-perfil>`, `microvm-<id>`, `rayito-base`).
Every design reference ("D4", "D6"…) points to `design.md`.

## 0. [prepare] Ground truth and baseline

- [x] 0.1 Re-read `AWS_API_NOTES.md` §1, §2, §5, §7, §15 and §16 Q10, Q15,
      Q40, Q41 and Q58. Confirm that every control-plane parameter the
      design uses is already listed there, so no new AWS parameter is
      needed (hard rule 1).
  - Hecho (2026-09-24, cierre): §2 lista `imageIdentifier`, `imageVersion`, `runHookPayload`, `maximumDurationInSeconds`, `idlePolicy` (`autoResumeEnabled`, `maxIdleDurationSeconds`) y `logging`; §5 `suspend-microvm`/`resume-microvm`; §6 `stateReason`, `startedAt` y `terminatedAt`. Es todo lo que usan el diseño, la sonda de 6.1 y los e2e de 10.x: ningún parámetro de AWS nuevo (regla 1). Q63–Q65 son medidas.
- [x] 0.2 Export the Windows toolchain (`RUSTUP_HOME`, `CARGO_HOME`, `PATH`
      with `/d/…` entries, the `zigcc`/`zigar` host wrappers,
      `CFLAGS_x86_64_pc_windows_gnu`) and a private `CARGO_TARGET_DIR`.
      Record `cargo --version`, `uv --version` and `pnpm --version`.
  - No aplica (2026-09-24): M9 no se implementó ni se aceptó en la máquina Windows sino en macOS arm64 con una VM Lima aarch64 para Rust (la línea base es 0.3). Versiones del entorno real: `cargo 1.98.1` y `rustc 1.98.1` (VM Lima), `uv 0.12.18`, `pnpm 9.15.4`, Node 22.23.2.
- [x] 0.3 Record the baseline counts: `cargo test --workspace --locked`,
      `cd clients/python && uv run pytest tests/unit`,
      `cd clients/typescript && pnpm test`.
  - registrado aquí (2026-09-23), sobre el árbol de trabajo con los seis cambios M9 a medio implementar (la línea base previa a M9 ya no se puede medir sin tocar ficheros ajenos): `cargo test --workspace --locked` en la VM Lima aarch64 con los binarios como uid 1500 → rayd-core 483, rayd lib 166, bin 5, m1 14, m2 25, m3 30, m4 19 (+1 ignored), m5_pty 15, m5_suspend_resume 7, m6_hooks 7, m6_imds 3, m6_limits 5, m7_poly 9, m9_deno 4, m9_egress 1, m9_metrics_history 6, m9_network 4, m9_timeout 11, m9_transfer 21, s3_store 0 (+1 ignored), todo verde; `uv run --with pytest-timeout pytest tests/unit --timeout 60` → 2015 passed, 1 failed (`test_packaging.py::test_e2b_all_matches_the_proposal_list`, de `m9-e2b-v2-surface` en curso); `pnpm test` → 38 ficheros, 797 passed.

## 1. [proto] Contract (applied by the Contract agent, not by implementers)

- [x] 1.1 Confirm the Contract agent landed D2 verbatim:
  - `proto/rayito/v1/lifecycle.proto`
  - `HealthResponse.lifecycle = 12`
  - the comment edits in `common.proto`, `process.proto` and `pty.proto`
  - `PROTO_FILES` += `"lifecycle"` in `crates/rayito-proto/build.rs`
  - verificado aquí (2026-09-23): el bloque ```proto de D2 extraído de `design.md` y `proto/rayito/v1/lifecycle.proto` son idénticos (`diff` sin líneas en blanco → sin salida); `health.proto` importa `rayito/v1/lifecycle.proto` y declara `LifecycleState lifecycle = 12;` con el comentario de D2; `common.proto` (`StreamError`) y `process.proto` (`EndEvent.status`, con la frase «"sandbox_timeout" cierra el stream al vencer el plazo lógico (ADR-011); error.code = "sandbox_timeout"») llevan `sandbox_timeout`; `pty.proto` sin cambios desde 0.2.0; `PROTO_FILES: [&str; 8]` en `crates/rayito-proto/build.rs` incluye `"lifecycle"`.
- [x] 1.2 Check `buf lint` is green and `buf breaking --against` the main
      branch (FILE) is green.
  - verificado aquí (2026-09-23): `buf lint` (buf 1.73.0) → sin salida, exit 0, con `buf.yaml` sin cambios desde 0.2.0. `buf breaking proto --against ".git#ref=origin/main,subdir=proto"` (`origin/main` = `dde681c`, 0.2.0) → sin salida, exit 0 (regla FILE de `buf.yaml`).
- [x] 1.3 Check the generated code exists and compiles:
      `clients/python/src/rayito/v1/lifecycle_pb2*.py`,
      `clients/typescript/src/gen/rayito/v1/lifecycle_pb.ts`, and the
      `cargo build -p rayito-proto` output.
  - verificado aquí (2026-09-23): `clients/python/src/rayito/v1/lifecycle_pb2{.py,.pyi,_grpc.py}` y `clients/typescript/src/gen/rayito/v1/lifecycle_pb.ts` existen, y `buf generate` con los plugins remotos de `buf.gen.yaml` hacia un directorio temporal no da ninguna diferencia (`diff -rq`) con el gencode comprometido. `rayito-proto` compila dentro de `cargo clippy --workspace --all-targets --locked -- -D warnings` (limpio) y de `cargo test --workspace --locked` en la VM Lima aarch64.

## 2. [rayd] Domain (`rayd-core`, Windows-testable)

- [x] 2.1 Add the new keys of D10 to `limits.json` and a `GROUPS` entry to
      `scripts/gen_limits.py`. Run `python scripts/gen_limits.py`, then
      `--check`.
      Verified here: `python scripts/gen_limits.py --check` exits 0.
- [x] 2.2 Create `crates/rayd-core/src/sandbox_timeout/`
      (`mod.rs`, `policy.rs`, `machine.rs`, `ports.rs`) with the constants,
      the `TimeoutSettings`, the state machine, `admits`, `view` and the
      `SelfTerminator` port of D4. Re-export it from `lib.rs` and add it to
      the crate doc.
- [x] 2.3 Write the unit tests of D11 for `sandbox_timeout`, including
      `constants_match_limits_json`. Run each one against a stub first, to
      see it fail, then make it pass.
      Verified here (red then green, mutations applied on a throwaway copy
      of the tree and restored): dropping the 5-minute floor, letting
      AT_LEAST shorten, lifting the cap check, ignoring `frozen`, an
      endless suspend hold, no `CAP_MARGIN`, admitting every RPC past the
      deadline and ignoring the thaw turned `cargo test -p rayd-core`
      red (17 failed, 465 passed, every targeted test among them); the
      restored tree is green (482 passed).
- [x] 2.4 `run_payload.rs`: parse and validate the `lifecycle` block (D3),
      add `RunPayloadError::InvalidLifecycle`, update the module doc, and
      add `lifecycle_is_optional_and_validated`.
- [x] 2.5 `session.rs` / `health.rs`: `SessionSettings.timeout`,
      `Mutex<SandboxTimeout>`, installation at `/run`, and the session
      methods of D5 (`lifecycle`, `set_timeout`, `tick_timeout`,
      `timeout_resumed`, `admits_rpc`, `timeout_managed`).
      `HealthSnapshot.lifecycle`. Add the two session tests of D11.
      `lifecycle.rs` stays untouched (`git diff --stat` shows no change
      there).
      Verified here: `git diff --stat dde681c -- crates/rayd-core/src/lifecycle.rs`
      prints nothing. Skipping `install` at `/run` turns
      `run_installs_the_lifecycle_and_health_reports_it` red; dropping the
      `auto_resume` rule turns `lifecycle_is_optional_and_validated` red.
- [x] 2.6 Gate: `cargo test -p rayd-core --locked`, and
      `cargo clippy -p rayd-core --all-targets -- -D warnings`.
      Verified here (devbox, 2026-09-23): `cargo test -p rayd-core --locked`
      482 passed, 0 failed; the workspace clippy (3.14) exits 0.

## 3. [rayd] Adapters, wiring and integration tests

- [x] 3.1 `lifecycle/suspend.rs`:
  - add `StreamCloseReason`, `SuspendSignal::close_all(reason)` and
    `SuspendWatch::close_reason()`
  - close closures take the reason
  - replace `suspending_status()` with `close_status(reason)`
  - update every call site in `grpc/{process,pty,filesystem,code,
    persistence}.rs` to the D6 close-form table
- [x] 3.2 `grpc/reject.rs`: extract the bounded body drain from
      `access_token.rs`.
- [x] 3.3 `grpc/timeout_gate.rs`: add `SandboxTimeoutGateLayer` inside the
      access-token layer and update the `GrpcRouter` alias.
- [x] 3.4 `grpc/lifecycle.rs`: add `LifecycleGrpc` (validation, error
      mapping of D6, `watcher.wake()`) and register it in `grpc/mod.rs`.
      `Services.timeout`.
- [x] 3.5 `grpc/health.rs`: map `HealthSnapshot.lifecycle` into
      `HealthResponse.lifecycle`, always set.
- [x] 3.6 `lifecycle/timeout_watcher.rs`: `spawn_timeout_watcher`,
      `TimeoutWatcher { wake, frozen }` and `StreamCloser`, dispatching
      `Expire`, `ReExpire`, `Gate` and `Terminate` (D6).
- [x] 3.7 `process/manager.rs`: add `signal_all(signal) -> usize` over the
      shared registry (covers PTYs).
- [x] 3.8 Code manager and sidecar adapter:
  - `code/{manager,supervisor}.rs`: `stop_for_exit(signal)`, the
    `stopping` flag, and `KernelKiller` → `KernelSignaller`
  - `rayd-core` `code/ports.rs`: `SidecarLink::terminate`
  - `adapters/sidecar_process.rs`: implement `terminate` (SIGTERM to the
    group)
- [x] 3.9 `lifecycle/exit_terminator.rs`: `ExitTerminator` (the graceful
      sequence and `force`) and `ExitReason`.
- [x] 3.10 `hooks/mod.rs`: `HookServices.timeout`, `wake()` after an
      installed `/run`, and `timeout_resumed(watcher.frozen(now))` + `wake()`
      after a changed `/resume`. The `resume recorded` log line gains
      `lifecycle_phase`. `/suspend` still answers 200 always.
      Verified here: `auto_resume_after_a_freeze_applies_the_five_minute_minimum`
      (new in `m9_timeout.rs`) is the only test that needs the `/resume`
      hook's `timeout_resumed`: without it the watcher alone opens the grace
      after the jump. Removing the call turns it red.
- [x] 3.11 `main.rs`: build the watcher and terminator, pass them to
      `Services`/`HookServices`, return `anyhow::Result<ExitCode>` (124 for
      a sandbox timeout), and add `reason` to `rayd stopped`.
- [x] 3.12 Update the `crates/rayd/tests/common/mod.rs` builders: the
      watcher, and a `JumpClock` (a `SystemClock` plus an atomic offset).
- [x] 3.13 `crates/rayd/tests/m9_timeout.rs`: every integration test of
      D11 (`cfg(unix)`, shrunk `TimeoutSettings`), including the
      log-capture assertion of the logging requirement.
      Verified here: 11 tests, all green (6 consecutive runs, 3 of them with
      8 CPU burners on 8 vCPU). `forged_suspend_holds_the_deadline_only_once`
      uses its own 2 s hold and bounds the action below two holds: with
      the shared 800 ms hold it failed once under two parallel release
      builds. Red then green: bypassing the gate, `UNAVAILABLE` for the
      timeout close, no SIGTERM in the exit sequence, no `timeout_resumed`
      in `/resume` and a supervisor that ignores `stopping` turned 5
      `m9_timeout` tests and 3 `rayd --lib` tests
      (`only_health_and_set_timeout_pass_an_expired_sandbox`,
      `close_all_ends_streams_with_sandbox_timeout_and_keeps_the_generation`,
      `stop_for_exit_signals_kernels_and_the_sidecar_and_never_relaunches`)
      red. `Health.lifecycle: None` turned
      `health_reports_the_lifecycle_after_run_and_unmanaged_without_it` red.
- [x] 3.14 Gate: `cargo fmt --all --check`,
      `cargo clippy --workspace --all-targets -- -D warnings`,
      `cargo test --workspace --locked`, and
      `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd`.
      Record the binary size delta.
      Verified here (devbox, Graviton, Amazon Linux 2023, non-root user):
      - `cargo fmt --all --check`: no diff.
      - `cargo clippy --workspace --all-targets --locked -- -D warnings`:
        exit 0.
      - `cargo test --workspace --locked --no-fail-fast`: every target green
        except 3 tests that also fail on the pre-M9 commit `dde681c` in the
        same environment, so they are not caused by this change:
        `adapters::tar_archiver::unix::tests::restore_merges_directories_and_overwrites_files`
        (`permission denied` on unpack as a non-root user),
        `m5_pty::echo_round_trip_size_tty_uid_and_env` (the host's login
        profile drops `LC_ALL`) and
        `m5_pty::a_stalled_second_subscriber_is_truncated_alone` (3 MB
        through the PTY misses its 30 s read budget on this host).
      - `cargo zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd`:
        13 737 264 bytes, against 12 528 432 bytes for `dde681c`, i.e.
        +1 208 832 bytes (+9.6 %) for the whole M9 tree (this change plus
        the sibling M9 changes in the same working tree).

## 4. [sidecar] Kernel sidecar

- [ ] 4.1 No code change. `kernel-sidecar/src/rayito_kernel_sidecar/__main__.py`
      already stops on SIGTERM (`server.stop`). Confirm it with
      `kill_mode_signals_the_workload_and_exits` (the sidecar exited within
      the SIGTERM grace), and note it in the task note.
      Sin marcar a propósito (2026-09-23, reintento de specs-docs):
      `crates/rayd/tests/m9_timeout.rs::kill_mode_signals_the_workload_and_exits`
      usa el sidecar falso de los tests (`m9_timeout.rs`, línea 10), así que
      prueba la secuencia `SIGTERM` → salida de `rayd` pero no que el
      `server.stop` del sidecar real termine dentro de la gracia. Falta
      confirmarlo con el sidecar real (Linux con `kernel-sidecar` instalado o
      el e2e 10.1 contra una imagen M9, *pendiente de aceptación en AWS*).
  - Diferido con razón escrita (2026-09-24, siguiente ciclo): no bloquea M9. La secuencia de D4 manda `SIGKILL` 5 s después del `SIGTERM` sea cual sea la respuesta del sidecar, y la e2e con el sidecar real (10.1, test 1, `rayito-base` 23.0 y 25.0) termina la VM con `Container Stopped with Exit Code: 124` 17,4–17,7 s después del plazo. Confirmar que el `server.stop` del sidecar sale dentro de la gracia exige leer el log de `rayd` de CloudWatch de una VM en modo `kill`; queda en `MILESTONES.md` (M9, diferidos).

## 5. [infra] Image and account

- [x] 5.1 Record the currently published pre-M9 `rayito-base` version as
      the value of `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION`, in the local
      environment only, never in a tracked file.
      Hecho (2026-09-23, accept-prep): leída con `list-microvm-images`
      (`latestActiveImageVersion` antes de publicar) y pasada al orquestador
      sólo como variable de entorno; no se escribe aquí.
- [x] 5.2 Build `rayd` for `aarch64-unknown-linux-musl`, build the image zip
      (`python scripts/image_zip.py`), and publish a new `rayito-base`
      version with `scripts/publish_image.py --base-image-version <pinned>`.
      Wait for the three-state gate and record the version and the
      `snapshotBuild` sizes.
      Hecho (2026-09-23): `cargo auditable zigbuild --release --locked
      --target aarch64-unknown-linux-musl -p rayd` en la VM Lima ARM64 (árbol
      M9 con el fix de logging de apagado de `supervisor.rs`), 13 756 408 B;
      `image_zip.py` → 13 874 844 B; `publish_image.py --base-image-version 1`
      → `rayito-base` **21.0** `UPDATED`/`SUCCESSFUL`/`ACTIVE` en 216,6 s,
      `snapshotBuild` 927 043 584 / 1 360 146 432 / 39 071 744 B (memoria /
      code install / disco).
- [x] 5.3 Confirm that no IAM change is needed: the caller policy of
      `spike/m0/iam.yaml` already grants `lambda:SuspendMicrovm`, and there
      is no execution role.
      Confirmado (2026-09-23): `CallerPolicy` (sid `ImagesAndMicrovms`) lista
      `lambda:SuspendMicrovm`, `ResumeMicrovm` y `TerminateMicrovm`; el
      sondeo de 6.1 lanza sin `executionRoleArn`. El único cambio del stack
      `rayito-m0-iam` de hoy es el de `m9-file-transfer` 0.5.

## 6. [e2e] Q63 before the SDK mapping (decides D6's fallback)

- [x] 6.1 Probe with a scratch script in the session scratchpad, never in
      the repo:
  1. build the payload JSON with a
     `lifecycle{timeout_s:30,cap_s:900,on_timeout:"kill",auto_resume:false}`
     block
  2. launch with boto3 `run_microvm` (the parameters of `AWS_API_NOTES.md`
     §2 only)
  3. poll `get-microvm` every 1 s
  4. record `stateReason`, `terminatedAt − (startedAt + 30 s)` and whether
     the VM ever returns to `RUNNING` after the exit
  - Medido (2026-09-23, `rayito-base` 21.0, dos pasadas, script en el
    scratchpad con `build_run_hook_payload` + boto3 `run_microvm`
    (`imageIdentifier`, `imageVersion`, `runHookPayload`,
    `maximumDurationInSeconds=900`, `logging.disabled`), sin execution
    role, `get-microvm` cada 1 s): `PENDING` → `RUNNING` (2,4 s) →
    `TERMINATING` → `TERMINATED`; `stateReason` **"Container Stopped with
    Exit Code: 124"** en las dos; `terminatedAt − startedAt` = 42,3 s y
    45,5 s, es decir `terminatedAt − (startedAt + 30 s)` = **12,3 s y
    15,5 s** (el retraso de ≈ 15 s de Q58 entre la salida de `rayd` y
    `TERMINATED`); nunca vuelve a `RUNNING`. Cero MicroVMs vivos al final.
- [x] 6.2 Decide:
  - If the result is `TERMINATED` within 45 s with "Container Stopped with
    Exit Code: 124", keep 124.
  - Otherwise, set `TIMEOUT_EXIT_CODE` to 0 in `rayd-core` and
    `limits.json`, regenerate the limits, delete the `timed_out`/`timedOut`
    helpers and their tests from the plan below, and republish (5.2).
  - Write down the outcome.
  - Resultado: se **mantiene 124**. La plataforma propaga el código de
    salida de `rayd` (`Container Stopped with Exit Code: 124`) y la VM
    termina sola; el tiempo total queda en el borde de los 45 s (42,3 s y
    45,5 s desde `startedAt`) sólo por los ≈ 15 s de Q58 de la plataforma,
    no por `rayd`. Sin republicar.

## 7. [python] SDK (sync and async identical)

- [x] 7.1 `_lifecycle_base.py` (new, pure): `LifecycleBlock`,
      `LifecyclePlan`, `resolve_lifecycle`, `lifecycle_from_proto`,
      `validate_set_timeout_seconds`, `connect_extension`,
      `beyond_cap_error`, `older_agent_error`, `pause_trigger_delay` (D7,
      D8).
      Verificado aquí (2026-09-23, macOS arm64, venv de `clients/python`):
      `test_lifecycle_base.py` 22/22 (los 11 casos de D11 más la tabla de
      `translate_set_timeout_error` y `validate_set_timeout_seconds`).
      Además de los nombres de D7/D8 lleva `TimeoutRequest` (EXACT vs
      AT_LEAST), `lifecycle_from_state`, `deadline_may_have_moved` y
      `suspended_set_timeout_error`, espejo de los helpers de `lifecycle.ts`.
- [x] 7.2 Models and payload:
  - `_models.py`: `SandboxLifecycle`, `SandboxHealth.lifecycle`,
    `SandboxInfo.lifecycle`, and the logical `expires_at`,
    `platform_expires_at` and `timed_out`
  - `exceptions.py`: `LifecycleUnsupportedException`
  - `__init__.py`: the exports
  - `_payload.py`: the `lifecycle` block
  Verificado aquí: `SandboxInfo.expires_at` sigue el plazo lógico sólo con
  un lifecycle gestionado; `platform_expires_at` y `timed_out` existen;
  `LifecycleUnsupportedException(InvalidArgumentException)` y
  `SandboxLifecycle` se exportan desde `rayito`. Mutaciones sobre una copia
  aislada del paquete (en el scratchpad de la sesión): quitar el bloque del
  payload → 5 fallos (`test_payload`/`test_lifecycle_*`); `expires_at` →
  plataforma → 4 fallos.
- [x] 7.3 `_sandbox_base.py`: `build_launch_plan(max_lifetime,
      on_timeout, default_max_lifetime)`, `LaunchPlan.lifecycle_requested`,
      `health_from_proto` with `lifecycle`, and the timed-out wording in
      `terminal_state_error`.
      Verificado aquí: `build_launch_plan(..., max_lifetime, on_timeout,
      default_max_lifetime)`, `LaunchPlan.lifecycle_requested`,
      `health_from_proto` con `lifecycle` y «alcanzó su timeout» en
      `terminal_state_error`; `test_sandbox_base.py` verde.
- [x] 7.4 Error mapping in `_transport.py`, `_process_base.py` and
      `_pty_base.py`: `is_sandbox_timeout`, the D8 table, and
      `sandbox_timeout` terminal status → `TimeoutException`.
      Verificado aquí: anular `is_sandbox_timeout` en `_transport.py` → 2
      fallos; anular la rama `sandbox_timeout` de `outcome_from_end` no
      tumbaba nada porque el fake manda también `error.code`, así que se
      añadió `test_process_base.py::test_sandbox_timeout_end_is_a_timeout_and_terminal`
      (status sin `error`, y `consumed_end` terminal): con la mutación 1
      fallo, sin ella 51/51. La PTY usa la misma tabla vía `consumed_end`.
- [x] 7.5 `sandbox_sync/main.py`:
  - `create(max_lifetime, on_timeout, _default_max_lifetime)`
  - the older-agent gate in `_open`
  - `set_timeout` (instance and class variant)
  - `connect` as a `class_method_variant` with an instance form and
    `timeout`
  - `resume()` reopen
  - `get_info()` refresh
  - the deadline trigger (`threading.Timer`) and `_suspend_for_deadline()`
  - `LaunchOptions` gains the two kwargs
  Verificado aquí: `test_lifecycle_sync.py` 17/17. Mutaciones: sin la puerta
  de agente anterior → 1 fallo + 1 error; trigger sin armar → 1; `set_timeout`
  con AT_LEAST → 2; `get_info()` sin releer `Health` → 1. Firma del
  `connect` de instancia: `connect(self, *, timeout: int | None = None,
  request_timeout: float | None = None) -> Sandbox`.
  Fix de revisión (2026-09-23): `get_info()` sólo relee `Health` con
  `RUNNING` y lifecycle gestionado; la condición queda escrita en D8/D10 y
  en `specs/sandbox-timeout` (sin plazo gestionado no hay nada que refrescar
  y rige el «sin RPC extra» de `sandbox-metadata`/`sandbox-observability`),
  cubierta por `test_instance_get_info_carries_metadata_without_extra_rpc` y
  `test_get_info_expires_at_is_the_logical_deadline`. TS `getInfo()` debe
  adoptar la misma condición (área typescript). Además, D7 sin test:
  `test_ending_the_handle_cancels_the_pause_trigger[close|kill|exit]` y su
  gemelo async; rojo con `self._deadline_trigger.cancel()` → `pass` en una
  copia (6 fallos), verde en el árbol (6/6).
- [x] 7.6 `sandbox_async/main.py`: the same surface over the same helpers
      (`loop.call_later` trigger).
      Verificado aquí: `test_lifecycle_async.py` 19/19; se añadieron los dos
      casos que faltaban frente al síncrono
      (`test_async_create_records_the_lifecycle_kwargs_for_reincarnate`,
      `test_async_kill_mode_never_arms_the_pause_trigger`). Mutaciones: sin
      la puerta de agente → 1 fallo + 1 error; sin `connect_extension` → 3.
- [x] 7.7 `sandbox_sync/pool.py` and `sandbox_async/pool.py`:
      `reject_launch_kwargs_with_pool` includes `max_lifetime` and
      `on_timeout`.
      Verificado aquí: quitar `max_lifetime`/`on_timeout` de
      `LaunchKwargDefaults` (lo que lee `reject_launch_kwargs_with_pool`)
      → 4 fallos (`test_pool_rejects_lifecycle_kwargs` sync y async).
- [x] 7.8 E2B shim:
  - `e2b/_compat.py`: `map_lifecycle`, `E2B_DEFAULT_MAX_LIFETIME_SECONDS`,
    and `map_create_kwargs(lifecycle, max_lifetime)`
  - `e2b/_sync.py` and `e2b/_async.py`: the `lifecycle` and `max_lifetime`
    kwargs, `set_timeout` (instance and class), positional
    `connect(sandbox_id, timeout)`, `beta_create(auto_pause=)`, the
    `LifecycleUnsupportedException` → `UnimplementedError` re-raise, and the
    reasons updated
  - `e2b/__init__.py`: the docstring
  Verificado aquí (2026-09-23, macOS arm64, `clients/python`, reintento): `map_lifecycle`, `E2B_DEFAULT_MAX_LIFETIME_SECONDS` (3600) y `map_create_kwargs(lifecycle, max_lifetime)` en `_compat.py`; `set_timeout` de instancia y de clase (EXACT), `connect(sandbox_id, timeout)` AT_LEAST, `beta_create(auto_pause=)`, `LifecycleUnsupportedException` → `UnimplementedError` con `__cause__`, y el docstring de `e2b/__init__.py`.
  Mutaciones: `set_timeout` que lanza → 1 fallo; `auto_pause` ignorado → 3 fallos; `_default_max_lifetime` fijo en 28800 → 14 fallos.
- [x] 7.9 Unit tests of D11:
  - `tests/unit/fake_lifecycle.py`, registered in `tests/unit/conftest.py`,
    and the `lifecycle` script in the Health fake
  - `test_lifecycle_base.py`, `test_lifecycle_sync.py`,
    `test_lifecycle_async.py`
  - the e2b additions, and the removal of the `set_timeout`-unimplemented
    test
  - the async parity lists
  Parte nativa: `fake_lifecycle.py` registrado en `conftest.py`, el guion
  `lifecycle` del fake de Health, `test_lifecycle_base/sync/async.py` y la
  paridad `test_async_lifecycle_surface_matches_sync`.
  Parte e2b (reintento del shim): las adiciones e2b de D11 existen (`test_e2b_compat_base.py::test_lifecycle_mapping_table`, `test_e2b_compat_sync.py::test_set_timeout_maps_to_native_exact`, `test_class_set_timeout`, `test_connect_timeout_is_at_least`, `test_beta_create_auto_pause_is_lifecycle_pause` y sus gemelos async) y el test de `set_timeout` no implementado ya no está. Rojo→verde por las mutaciones de 7.8.
- [x] 7.10 Gate: `cd clients/python && uv run pytest tests/unit && uv run
      ruff check . && uv run ruff format --check . && uv run mypy src
      tests`.
      Gate de Python (2026-09-23, macOS arm64, reintento de specs-docs): `uv run --with pytest-timeout pytest tests/unit --timeout 60` da 2090 passed en 321 s, exit 0; `ruff check .` limpio; `ruff format --check .` 198 ficheros (el `README.md` ya formateado); `mypy src tests` sin errores en 196 ficheros.
      Corridas anteriores (superadas):
      - `uv run --with pytest-timeout pytest tests/unit --timeout 60`:
        2015 passed, 1 failed: `test_packaging.py::test_e2b_all_matches_the_proposal_list`
        (el `__all__` de `rayito/e2b/__init__.py`, del shim en curso).
      - `uv run mypy src tests`: sin errores en 196 ficheros.
      - `uv run ruff format --check .`: 198 ficheros formateados.
      - `uv run ruff check .`: limpio.
      La parte nativa está verde (los antiguos rojos `test_code_base.py::test_result_from_proto_maps_every_mime_field`,
      `mypy tests/unit/test_transport.py:143` y `cli/test_sandbox.py::test_info_prints_metadata_and_no_metadata_skips_the_probe`
      pasan). Queda abierta hasta que el shim cierre 7.8 y la parte e2b de 7.9.
  Reintento del shim: Sin marcar. Última corrida (2026-09-23, macOS arm64, reintento): `uv run --with pytest-timeout pytest tests/unit --timeout 60` 2058 passed, 2 failed; los dos fallos son nativos y ajenos al shim: `test_sandbox_sync.py::test_is_running_request_timeout_bounds_the_health_deadline` y su gemelo async (el plazo de `Health` llega como `0.501 <= 0.5` con la máquina cargada; la revisión los vio fallar también aislados; los corrigió después el fixer de python y la corrida completa de hoy da 2090 passed). `ruff check .` limpio, `ruff format --check .` 198 ficheros, `mypy src tests` sin errores en 196 ficheros. Los ficheros del shim (`test_e2b_compat_{base,sync,async}.py`, `test_e2b_v2_{base,sync,async,exports}.py`, `test_packaging.py`) dan 530 passed.

## 8. [ts] SDK (native camelCase mirror)

- [x] 8.1 `src/sandbox/lifecycle.ts` (new, pure), mirroring 7.1 in
      milliseconds.
      Verified here: `tests/unit/lifecycle.test.ts` 15/15 green.
      Also added `optionalSetTimeoutMs`, `setTimeoutRequest` and
      `suspendedSetTimeoutError` (the class-variant refusal).
- [x] 8.2 `models.ts` (`SandboxLifecycle`, `SandboxHealth.lifecycle`,
      `SandboxInfo.lifecycle`, `expiresAt`/`platformExpiresAt`/`timedOut`),
      `errors.ts` (`LifecycleUnsupportedError`), the `index.ts` exports,
      `payload.ts` (the lifecycle block, plus a cross-SDK golden case), and
      `limits.ts` (regenerated).
      Verified here: `models.ts` also has `withLifecycle`. `index.ts` exports
      `LifecycleUnsupportedError`, `SandboxLifecycle`, `OnTimeout`,
      `SandboxSetTimeoutOptions` and `InstanceConnectOptions`.
      `python scripts/gen_limits.py --check` exits 0. `models` and
      `payload` tests are green.
- [x] 8.3 `sandbox/launch.ts`, `sandbox/core.ts` (the `LifecycleService`
      client) and `sandbox/readiness.ts` (`healthFromProto`).
      Verified here: `core.clients.lifecycle`. `core.lifecycle` and
      `recordLifecycle` are recorded on every `Health`. `launch` and
      `readiness` tests are green.
- [x] 8.4 `sandbox/sandbox.ts`:
  - `timeoutMs` moves to `SandboxConnectOptions`; `maxLifetimeMs`,
    `onTimeout` and `SandboxSetTimeoutOptions` are added
  - static and instance `setTimeout`, static and instance `connect`
  - `resume()` reopen, `getInfo()` refresh
  - `#open(requireLifecycle)`
  - the unref'd trigger, cleared on `close`/`kill`/dispose
  Verified here:
  - The trigger lives in `SandboxCore`, backed by a new
    `sandbox/deadline-trigger.ts` (unref'd). `core.close()` cancels it,
    and `kill()` and `[Symbol.asyncDispose]` both go through it.
  - `LaunchOptions` records `maxLifetimeMs`/`onTimeout`, so
    `reincarnate()` reuses them.
  - `POOL_REJECTED_OPTIONS` gains both.
  Hallazgo de la review (2026-09-23, `Sandbox.getInfo(id)` de TS sin `lifecycle`):
  el `rayito/e2b` estático (`getInfo`/`getFullInfo`) usa ahora el nuevo
  `Sandbox.probedInfo` interno (un `Health` anónimo sólo sobre `RUNNING`),
  así que su `endAt` es el plazo lógico como en el shim Python; test en
  `e2b-shim.test.ts`, rojo antes (`endAt` = tope de plataforma) y verde
  después. El `Sandbox.getInfo(id)` nativo sigue sin sondear, porque
  `m9-sandbox-observability` (spec y D-TS) lo exige; unificarlo pide
  cambiar esa spec.
- [x] 8.5 `transport/errors.ts`, `sandbox/commands.ts` and `sandbox/pty.ts`:
      `isSandboxTimeout` and the `sandbox_timeout` terminal status →
      `TimeoutError`.
      Verified here: `pty.ts` needed no change, because
      `endEventFromPtyExited` feeds the same `outcomeFromEnd` table.
      `persistence.ts` maps `sandbox_timeout` for Checkpoint/Restore.
- [x] 8.6 Tests: the fakes `tests/unit/fake/lifecycle.ts` and the
      `fake/health.ts` lifecycle script; `tests/unit/lifecycle.test.ts`,
      and the additions of D11 in `sandbox`, `commands`, `pty`,
      `transport-errors`, `payload` and `launch`. Update `gen.test.ts` if it
      lists the generated files.
      Verified here:
      - The `Sandbox` additions live in a new
        `tests/unit/sandbox-timeout.test.ts` (17 tests), next to the egress
        additions of `sandbox.test.ts`.
      - `gen.test.ts` gains the `SetTimeout` unary check. `readiness.test.ts`
        is updated for the two new `SandboxHealth` fields.
      - Red then green, on an isolated copy of the package: 17 targeted
        mutations each turned the suite red, and the unmutated copy stayed
        green (17/17 and 215/215).
      - Mutated: the gate, trigger arming, extension, `getInfo`, the pool
        list, close-cancel, reincarnate, the expired-only suspend, the
        RUNNING-only probe, the stream status, the RPC/stream error maps,
        the payload block, the launch cap, `expiresAt`, `healthFromProto`,
        and the Checkpoint/Restore map.
- [x] 8.7 Gate: `cd clients/typescript && pnpm lint && pnpm typecheck &&
      pnpm test && pnpm build`.
      Verificado 2026-09-23: Gate completo del paquete el 2026-09-23 en este árbol: `pnpm lint` (biome, 114 ficheros, 0 errores), `pnpm typecheck` exit 0, `pnpm test` 35 ficheros / 762 passed, `pnpm build` ok y `pnpm pack:check` ok (lista `package/dist/index.{mjs,cjs,d.mts,d.cts}`).
      Reverificado 2026-09-23 (reintento): lint 123 ficheros 0 errores, typecheck exit 0, test 38 ficheros / 797 passed, build y pack:check exit 0.

## 9. [docs] Architecture, spec, security, site

- [x] 9.1 `ARCHITECTURE.md`: ADR-007 superseded line, ADR-011, ADR-009
      consequence (4), and the "Servicios", hooks, "Suspend / resume" and
      hexagonal tables (D1, D13).
      Verificado (2026-09-23, reintento de specs-docs): ADR-007 conserva su
      cuerpo con la primera línea «**Sustituida por ADR-011**
      (`m9-server-timeout`).»; ADR-011 tras ADR-010 (contexto Q58/§15,
      decisión D3/D4/D6/D7, compatibilidad `UNMANAGED`, los siete costes
      honestos, imagen M9 del shim, `reincarnate()`; Q63–Q65 *pendiente de
      aceptación en AWS*); consecuencia (4) de ADR-009 reescrita; fila
      `LifecycleService` y campo 12 en `HealthService`; `/run`, `/suspend` y
      `/resume` en la tabla de hooks; columna `sandbox_timeout` en la tabla de
      cierres; `sandbox_timeout`, `SelfTerminator`, `timeout_watcher`,
      `exit_terminator` y `timeout_gate` en las tablas hexagonales.
- [x] 9.2 `SPEC.md` §1, §3, §4 (delete the `set_timeout` bullet), §5 and §7,
      and `openspec/project.md` hard rule 3 (D13).
      Verificado (2026-09-23, reintento de specs-docs): §1 con el texto de
      D13, §3 fila «Ciclo de vida (M9)», §4 sin el bullet de `set_timeout`,
      §5 «Vida del sandbox» = plazo lógico + tope (ADR-011) y la fila del
      shim sin `set_timeout`, §7 conserva el tope y quita «sin `set_timeout`»;
      la regla 3 de `project.md` ya no dice «no `set_timeout`».
- [x] 9.3 `SECURITY.md` T2: append the D14 paragraph verbatim and keep every
      existing sentence. `scripts/tests/test_security_docs.py` stays
      green.
      Verificado (2026-09-23, reintento de specs-docs): el párrafo de D14 va
      literal al final de la celda de mitigación de T2 (hito «/ M9»), sin
      tocar ninguna frase anterior; `test_security_docs.py` verde dentro de
      `cd clients/python && uv run pytest ../../scripts/tests` → 184 passed.
- [x] 9.4 `AWS_API_NOTES.md` §16: rows Q63 (from 6.1 and e2e 1), Q64 (e2e 7
      and 8) and Q65 (e2e 6), with placeholders only.
  - Hecho (2026-09-24): filas **Q63** (sonda de 6.1 y tests 1, 2 y 12: `Container Stopped with Exit Code: 124`, `terminatedAt − plazo` 17,3–18,8 s), **Q64** (tests 7, 8 y 9: `SUSPENDED` 2,31–2,45 s tras el plazo con el cliente vivo, auto-resume 1,04–1,07 s, 254,3 s con el cliente muerto por la política de idle) y **Q65** (tests 5 y 6: del auto-resume suelto a `TERMINATED` 45,2–46,4 s) en `AWS_API_NOTES.md` §16, sólo con marcadores (`microvm-<id>`).
- [x] 9.5 Site pages:
  - `docs/site/docs/e2b-compat.md`, `concepts.md`, `limits.md`, `cost.md`,
    `persistence.md`, and `api.md` if it lists members
  - `README.md`, `clients/python/README.md`, `clients/typescript/README.md`
  - the `[Unreleased]` entries of the three CHANGELOGs, including the
    breaking shim note (D13)
  Verificado (2026-09-23, reintento de specs-docs): `e2b-compat.md`
  reescrita (fila `timeout` con `max_lifetime`, `set_timeout`,
  `connect(timeout)`, `lifecycle`, `beta_create(auto_pause=True)`,
  `TimeoutException`, aviso «exige una imagen M9», `keep_memory=False` en la
  tabla de no implementados, costes honestos en «Diferencias por cambio»);
  `concepts.md` «Plazo, tope e idle»; fila de vida y del plazo en
  `limits.md`; `cost.md` (huérfano en modo `kill` = plazo + ≈ 15 s, Q58);
  `persistence.md` «`reincarnate()`: más allá de `max_lifetime`»;
  `api.md` gana `SandboxLifecycle`; los snippets del shim de `README.md` y
  `clients/python/README.md` llaman a `set_timeout(600)` de verdad y el de
  Python documenta `max_lifetime`/`on_timeout`; `clients/typescript/README.md`
  sección M9 con `setTimeout`/`onTimeout`/`maxLifetimeMs`; entradas
  `[Unreleased]` de los tres CHANGELOG, con «Changed (shim)» (imagen M9,
  `maximumDurationInSeconds` = `max_lifetime`, `set_timeout` ya no lanza; la
  línea de 0.2.0 que decía lo contrario se deja: era cierta en esa versión).
  `test_m9_docs.py::test_readmes_show_a_working_set_timeout` y
  `test_unreleased_changelogs_name_the_m9_surfaces` rojo → verde;
  `mkdocs build --strict` verde.
- [x] 9.6 `scripts/tests/test_lifecycle_docs.py` with the three tests of
      D11, run once before 9.1–9.3 to see them fail.
      Verificado (2026-09-23, reintento de specs-docs): escrito antes de tocar
      las docs; `uv run --no-project --with pytest python -m pytest
      scripts/tests/test_lifecycle_docs.py` → 3 failed
      (`test_set_timeout_is_no_longer_a_non_goal`,
      `test_adr_007_is_superseded_by_adr_011`, `test_t2_names_the_timeout_exit`);
      tras 9.1–9.3 → 3 passed. `ruff check`/`ruff format --check` limpios.

## 10. [e2e] Acceptance on real AWS (design D12)

- [x] 10.1 `clients/python/tests/e2e/test_m9_server_timeout.py`, tests 1–12
      of D12. Run them with `RAYITO_E2E=1`, `RAYITO_TEMPLATE=rayito-base`
      (M9 version) and `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION`. Paste the
      printed `stateReason`, overruns and suspend or resume timings.
  - Hecho (2026-09-24, aceptación): tests 1–12 **12/12** dentro de la regresión e2e de Python de 2026-09-24 sobre `rayito-base` 23.0, `rayito-base-caps` 12.0 y `rayito-base-poly` 7.0: **62 passed de 62** (server-timeout 12, observability 4, deno 8, egress 14, transfer 14, M4 1, M5 2, M6 2, M7 poly 5) (con `RAYITO_E2E_PRE_M9_TEMPLATE_VERSION` en la versión 20.0 para el test 11); `test_set_timeout_beyond_cap` **3/3** aislado en 23.0; re-corrida de los ficheros afectados en `rayito-base` 25.0 (el `rayd` final) en curso; sus 12 tests de timeout ya pasaron 12/12 en 25.0 (`terminatedAt − plazo` 14,2–18,8 s, pausa 2,45 s, auto-resume 1,07 s, cliente muerto 251,2 s). `stateReason` `Container Stopped with Exit Code: 124`; `terminatedAt − plazo` 17,4 s (cliente muerto), 17,3/26,9 s (`set_timeout` 150/10), 15,3 s (gracia), 23,2 s (shim); `connect(timeout=300)` con ≈ 600 s restantes no acorta (595,6 → 594,6) y con ≈ 30 s alarga a 299,9 s; streams abiertos terminan a los 46,4 s de `startedAt` con plazo 45; pausa al vencer 2,31 s con cliente vivo, auto-resume 1,04 s, 254,3 s con el cliente muerto; sin auto-resume vuelve a `SUSPENDED` en 63,1 s. Filas Q63–Q65.
  - Cierre de M9 (2026-09-24): aceptación final contra AWS real sobre `rayito-base` 25.0, `rayito-base-caps` 14.0 y `rayito-base-poly` 9.0 (republicadas desde el `rayd` final): Python **52/52** de las suites afectadas (timeout, egress, observability, corpus E2B y M4), además de los **62/62** de la regresión completa anterior sobre 23.0/12.0/7.0; TypeScript **24/24** de las suites afectadas (timeout, egress, corpus, observability), además de los **26/26** anteriores sobre 24.0/13.0/8.0. Aceptación de paquete con instalación limpia (Python y TS, fuera del repo): subida y descarga de 50 MB por S3 con sha256 coincidente, y `set_timeout` → el sandbox acaba `TERMINATED`. Sólo marcadores de posición (`microvm-<id>`, sin cuenta, bucket ni ARN). Arreglo tardío del SDK de TypeScript (en `clients/typescript/CHANGELOG.md`, «Fixed»): al abortar el controller de un server-stream ya terminado o abandonado, el SDK pide una lectura más para que `@connectrpc/connect` 2.x suelte el timer (con ref) de su `timeoutMs`; antes el proceso de Node quedaba vivo hasta el deadline del stream (~315 s tras `runCode`, ~65 s tras `commands.run`). El deadline se sigue imponiendo mientras el stream vive; cubierto por los 24/24 de TS de arriba.
- [x] 10.2 `clients/typescript/tests/e2e/timeout.e2e.test.ts` (the native
      mirror of D12). Run it with the same image.
      Written as `clients/typescript/tests/e2e/m9-timeout.e2e.test.ts`
      (M9 e2e naming), with the five D12 TS legs. Not run yet: that is
      the Accept step.
  - Hecho (2026-09-24): `m9-timeout.e2e.test.ts` **5/5** (las cinco patas de D12) dentro de los 26/26 de TS sobre `rayito-base` 24.0.
- [x] 10.3 Confirm no VM is left alive (`list-microvms` for the test image
      shows none non-terminal), and record the approximate cost of the run.
  - Hecho (2026-09-24): cada VM de la suite acaba `TERMINATED` por su plazo o la termina el `cleanup` del test, y el sweeper de sesión de `conftest.py` termina lo que quede; las pasadas siguientes arrancaron sin fallo de pre-flight (≤ límite de VMs vivas). Coste aproximado: ≈ 0,1 USD por pasada (21 min de suite, ≤ 2 VMs de 2 GB a la vez a 0,126 USD/h más ≈ 15 lanzamientos a ≈ 0,0014 USD, §12).

## 11. [gates] Everything local, in one pass

- [x] 11.1 `cargo fmt --all --check`,
      `cargo clippy --workspace --all-targets -- -D warnings`,
      `cargo test --workspace --locked`.
  - Pendiente, paso de cierre de M9 (no se difiere, 2026-09-24): se corre sobre el árbol final cuando terminen los refactors sin cambio de comportamiento que están en curso en `crates/` y `clients/*/src`. Última pasada local completa (2026-09-23): Python 2090 passed, TypeScript 831 passed, sidecar 85 passed, `scripts/tests` 184, `check_hygiene`/`check_pins`/`check_license`/`buf lint` OK; faltan los gates de Rust (`cargo fmt`, `clippy -D warnings`, `cargo test --workspace --locked` en la VM Lima como uid 1500) sobre ese árbol.
  - Hecho (2026-09-24, cierre de M9, árbol final): Rust en la VM Lima (Ubuntu aarch64, `CARGO_TARGET_DIR` propio, `-j 2`): `cargo fmt --all --check` exit 0; `cargo clippy --workspace --all-targets --locked -- -D warnings` limpio; `cargo test --workspace --locked` con los binarios como uid 1500 (`setpriv --reuid=1500 --regid=1500 --clear-groups`): rayd lib 172, bin 5, m1 14, m2 25, m3 30, m4 19 (+1 ignored), m5_pty 15, m5_suspend_resume 7, m6_hooks 7, m6_imds 3, m6_limits 5, m7_poly 9, m9_deno 4, m9_egress 1, m9_metrics_history 6, m9_network 4, m9_timeout 11, m9_transfer 21, s3_store 0 (+1 ignored), rayd-core 498: 856 passed, 0 failed, 2 ignored; `m9_egress` como root en `unshare --net` con `RAYITO_REQUIRE_EGRESS_NETNS=1` → 1 passed; `cargo deny check` → advisories, bans, licenses, sources ok; `cargo auditable zigbuild --release --locked --target aarch64-unknown-linux-musl -p rayd` → `rayd` estático de 13 764 024 B y `check_auditable.py` → `.dep-v0: 257 packages, root rayd 0.2.0`.
- [x] 11.2 `cd clients/python && uv run pytest tests/unit && uv run ruff
      check . && uv run ruff format --check . && uv run mypy src tests`.
      Gate de Python (2026-09-23, macOS arm64, reintento de specs-docs): `uv run --with pytest-timeout pytest tests/unit --timeout 60` da 2090 passed en 321 s, exit 0; `ruff check .` limpio; `ruff format --check .` 198 ficheros (el `README.md` ya formateado); `mypy src tests` sin errores en 196 ficheros.
  - Hecho (2026-09-24, cierre de M9, árbol final): Python (macOS arm64): `uv run --with pytest-timeout pytest tests/unit -q --timeout 60` 2115 passed; `uvx ruff==0.16.7 check` limpio y `format --check` 219 ficheros; `mypy src tests` sin errores en 217 ficheros.
- [x] 11.3 `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm
      test && pnpm build`.
      Gate de TypeScript (2026-09-23, reintento de specs-docs): `pnpm lint` (biome, 123 ficheros, sin cambios), `pnpm typecheck` exit 0, `pnpm test` 38 ficheros / 831 passed, `pnpm build` exit 0 y `pnpm pack:check` exit 0.
  - Hecho (2026-09-24, cierre de M9, árbol final): TypeScript: `pnpm lint` (biome, 139 ficheros), `pnpm typecheck` exit 0, `pnpm test` 40 ficheros / 863 passed, `pnpm build` y `pnpm pack:check` exit 0.
- [x] 11.4 `buf lint`, `python scripts/gen_limits.py --check`,
      `python scripts/check_pins.py`, `python scripts/check_license.py`,
      `python scripts/check_hygiene.py`, and the `scripts/tests` suite.
      Verificado (2026-09-23, reintento de specs-docs): `buf lint` exit 0; `gen_limits.py --check` exit 0; `check_pins.py` OK (8 ficheros); `check_license.py` OK; `check_hygiene.py` OK (939 ficheros); `cd clients/python && uv run pytest ../../scripts/tests` 184 passed; `uvx ruff==0.16.7 check scripts` y `format --check scripts` limpios.
  - Hecho (2026-09-24, cierre de M9, árbol final): `buf lint` exit 0; `check_hygiene.py` OK (939 ficheros); `check_pins.py` OK (8 ficheros); `check_license.py` OK; `gen_limits.py --check` exit 0; `scripts/tests` 186 passed; `mkdocs build --strict` (target `docs` del `Makefile`) sin avisos.
- [x] 11.5 Re-base the MODIFIED blocks of `specs/e2b-compat`,
      `specs/suspend-resume`, `specs/process-lifecycle` and
      `specs/typescript-sdk` on the then-current `openspec/specs` text,
      because sibling changes may have archived edits to the same
      requirements (design D16). Then run
      `openspec validate m9-server-timeout --strict --no-interactive`.
      Verificado (2026-09-23): el bloque `e2b-compat` "E2B features without
      an AWS primitive raise UnimplementedError" se re-basó sobre
      `openspec/specs` más los hermanos anteriores en el orden de archivo
      (deno-kernels, file-transfer): el escenario `set_timeout` se conserva
      reescrito al comportamiento M9 (ya no lanza, `SetTimeout{60000, EXACT}`)
      y se añaden `typescript is forwarded…`, `the shim runs JavaScript and
      TypeScript…` y `native error caught by the E2B name` con su texto
      (re-export nativo, `ts`, `upload_url`/`download_url` mapeados).
      `suspend-resume`, `process-lifecycle` y `typescript-sdk` ya eran
      consistentes. `openspec validate m9-server-timeout --strict
      --no-interactive` → valid; una simulación del archivo secuencial (copia de `openspec/` en un scratchpad, `openspec archive --yes` en el orden deno-kernels → file-transfer → server-timeout → sandbox-observability → egress-policy → e2b-v2-surface) archiva las seis sin error y `openspec validate --all --strict` sobre el resultado solo falla en `m9-handoff` (sin deltas, esperado). Si un hermano archiva
      cambios nuevos antes que éste, repetir el re-base. Re-verificado (2026-09-23): las seis validaciones `openspec validate <cambio> --strict --no-interactive` → valid; el archivo secuencial simulado no pierde ningún escenario del spec principal (comparación de nombres por capability: 0 perdidos; `e2b-compat` 20 → 59) y `openspec validate --specs --strict` sobre el resultado → 36/36.
