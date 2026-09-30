## 1. SetTimeout: the timeout rule only in the domain

- [x] 1.1 `crates/rayd-core/src/sandbox_timeout/machine.rs`: table test,
  `set_timeout(Duration::ZERO)` answers `Unmanaged`, `Expired` (terminating)
  or `InvalidTimeout` (active, grace, pause-mode expired) and never moves
  the deadline or counts an extension.
- [x] 1.2 `crates/rayd/src/grpc/lifecycle.rs`: adapter test (0 ms on an
  unmanaged sandbox is `FAILED_PRECONDITION lifecycle_unmanaged`; on an
  active one `INVALID_ARGUMENT` with the 999 ms message), then remove the
  `timeout_ms == 0` check.
- [x] 1.3 `crates/rayd/tests/m9_timeout.rs`: `a_zero_timeout_follows_the_phase_rules`.

## 2. Spanish agent messages with frozen wire tokens

- [x] 2.1 `crates/rayd-core/src/wire_tokens.rs` with the golden tests
  (token bytes, messages that carry a token, transfer reason tokens).
- [x] 2.2 Translate every `#[error]` of `rayd-core`, the rejection
  `Display`s, the process/PTY in-stream messages and the metrics, `/run`
  payload and lifecycle rule texts; tokens through `wire_tokens`.
- [x] 2.3 `crates/rayd/src/grpc`: status literals in Spanish; the
  adapters take `suspending`/`terminating`/`sandbox_timeout` from
  `wire_tokens` (no `SUSPENDING_CODE`/`SUSPENDING_MESSAGE` copies); the
  adapter reasons that reach a client (`KernelNotReady`, `Internal`,
  `SpawnError`, `SignalError`) in Spanish.
- [x] 2.4 DRY: `From<RegistryError> for TransferError` (test: same text by
  both paths), `ProcessEnd::suspending`, `not_a_pty_message`,
  `out_of_range_message`.
- [x] 2.5 Update every unit and integration assertion that compares text.
- [x] 2.6 `CHANGELOG` (`crates/rayd`), OpenSpec deltas, `openspec validate
  v040-rayd --strict`.
- [x] 2.7 Gates: `cargo test --workspace` (as uid 1500 in the VM),
  `cargo clippy --workspace --all-targets -D warnings`, `cargo fmt
  --check`, `cargo deny check`.

## 3. Final review (0.4.0 gate)

- [x] 3.1 `PersistenceError::stream_code` returns `wire_tokens::SUSPENDING`
  instead of a second `"suspending"` literal.
- [x] 3.2 `transfer/manager.rs`: `snapshot` (lookup) and `cancel` map
  registry errors with `TransferError::from`, the single mapping `admit`
  already uses (both registry calls only return `Unknown` on reachable
  paths, so the wire answer does not change).
- [x] 3.3 Spec: the synthetic `ExecutionError.value` texts ("the kernel
  sidecar exited", "execution exceeded <n> ms"...) are execution data, not
  status messages; they stay in English in 0.4.0 and the requirement says
  so (deferred).

Follow-up, report-only and outside 0.4.0 (not a task of this change): generic Spanish texts
repeated across parallel error enums and adapters (`usuario desconocido`,
`permiso denegado`, `falló la búsqueda del usuario: {0}`, `el hijo no tiene
pid`, `el pid no cabe en pid_t`, `las operaciones de ficheros no se admiten
en esta plataforma`) → shared message helpers or `From` conversions between
port and domain errors.
