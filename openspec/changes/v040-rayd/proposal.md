## Why

Two items of the M9 architecture review
(`docs/research/2026-09-m9-architecture-review.md`) in `rayd`, planned for
0.4.0 as deliberate, documented behaviour changes:

- **The `SetTimeout` rule lived in the adapter.** `grpc/lifecycle.rs`
  refused `timeout_ms == 0` before calling the domain, which already
  refuses anything under 1 s and judges the phase first. With
  `timeout_ms = 0` an unmanaged sandbox got `INVALID_ARGUMENT "timeout_ms
  must be positive"` instead of `FAILED_PRECONDITION lifecycle_unmanaged`,
  and the same error carried two texts (0 ms and 1–999 ms).
- **Agent messages mixed Spanish and English.** The domain errors of
  `rayd-core`, which become gRPC status messages (and the reason of a
  refused `/run`), were Spanish in the M9 transfer and network modules and
  English everywhere else, while user-facing strings must be Spanish
  (`CONTRIBUTING.md` §4). `RegistryError::Full`/`Unknown` also repeated the
  text of `TransferError`. The review recommended English; the maintainer
  chose Spanish, and this change records that decision.

## What Changes

- `crates/rayd/src/grpc/lifecycle.rs`: the `timeout_ms == 0` check is gone;
  the adapter parses the `mode` enum and maps the domain's errors with
  `status_for`, unchanged. `Duration::ZERO` reaches `session.set_timeout`
  and the domain answers by phase: unmanaged `FAILED_PRECONDITION
  lifecycle_unmanaged`, terminating `FAILED_PRECONDITION sandbox_timeout`
  (the deadline gate admits `SetTimeout` in that phase, so the domain gives
  the same status and message the gate would), and `ACTIVE`,
  `RESUME_GRACE` or pause-mode `EXPIRED` `INVALID_ARGUMENT` with the one
  message of every timeout below 1 s.
- `rayd_core::wire_tokens` (new): the frozen wire tokens as `pub const`,
  used by the `#[error]` attributes, by `HookPhase`, `EndStatus`,
  `FailureReason::token` and by the adapters (`SANDBOX_TIMEOUT_CODE` now
  points there; the adapter copies `SUSPENDING_CODE` and
  `SUSPENDING_MESSAGE` are gone). A golden test pins each value.
- Every `#[error]` of `rayd-core`, the `Display` of the rejection enums
  (`PathRejection`, `CwdRejection`, `BucketRejection`,
  `KeyPrefixRejection`, `ExcludeRejection`, `ManifestRejection`), the
  in-stream `StreamError` messages of process and PTY ends, the status
  literals of `crates/rayd/src/grpc` and the reason strings the adapters
  feed into those errors are Spanish; identifiers stay as they are. The
  new `InvalidTimeout` text is exactly `el timeout debe ser de al menos
  1 s`.
- DRY: `impl From<RegistryError> for TransferError`, and `RegistryError`
  takes its `Full`/`Unknown` text from it; the suspending end of a process
  or PTY stream is one domain constructor (`ProcessEnd::suspending`) shared
  by both adapters; the "not a PTY" and "from_seq out of range" texts are
  one function each, shared by the process, PTY and code errors.

Not done here: `proto/**` (its comments quote tokens that stay valid), the
kernel sidecar's own messages (`CodeError::SidecarRejected`), tracing and
log lines, the CLI, `docs/site/**` (no page quotes a changed message) and
the other report-only rows of the review. Repeated generic texts across
parallel enums of different modules (`usuario desconocido`, `esta imagen
no permite ejecutar como root`, "no se admite en esta plataforma") are left
as they were: each module keeps its own error type. The plan listed
`reset` (`grpc/client_abort.rs`) as a token; it is a fixture of that
module's test, not a message `rayd` sends, so it is not in `wire_tokens`.

## Migración

- Los SDK (Python y TypeScript, 0.3.x y 0.4.0) no cambian: validan un
  timeout >= 1 s antes del RPC y clasifican por código gRPC y por los
  tokens congelados, que no se tocan.
- Un cliente gRPC propio que envíe `timeout_ms=0` a un sandbox sin plazo
  recibe ahora `FAILED_PRECONDITION lifecycle_unmanaged` en lugar de
  `INVALID_ARGUMENT`; a uno terminando, `FAILED_PRECONDITION
  sandbox_timeout`.
- Quien compare por texto los mensajes de status de `rayd` los recibe
  ahora en español: usa el código gRPC o los tokens documentados en
  `rayd_core::wire_tokens`.
- Los cambios llegan a los usuarios cuando se republican las imágenes
  (`rayito-base`, `-caps`, `-poly`).

## Impact

- Affected specs: `sandbox-timeout` (SetTimeout errors),
  `filesystem-persistence` and `pty` (requirements that quote changed
  messages), new `agent-wire-messages`. The requirements "SDK error mapping
  for the deadline" and "fails closed" are not touched (they belong to
  `v040-python`).
- Affected code: `crates/rayd-core/src/**` (errors, `wire_tokens`,
  `process::events`), `crates/rayd/src/grpc/**`, `crates/rayd/src/code`,
  `crates/rayd/src/hooks`, `crates/rayd/src/lifecycle/suspend.rs`,
  `crates/rayd/src/transfer/manager.rs`, the launchers in
  `crates/rayd/src/adapters`, and the tests that compare message text.
- No `.proto`, SDK or E2B-compatible surface change.
