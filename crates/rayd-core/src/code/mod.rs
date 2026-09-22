//! Code execution: what a context and an execution are, the JSON-lines
//! protocol `rayd` speaks with the kernel sidecar, how execution events are
//! sequenced and rewritten under a server timeout, how they are retained
//! for `Reattach`, when the sidecar counts as ready, and the pure parts of
//! the `/ready`, `/validate`, `/run`, `/suspend` and `/resume` hooks. The
//! supervisor, the recorder tasks, the tokio streams and the process
//! adapter live in the `rayd` crate.

pub mod context;
pub mod error;
pub mod execution;
pub mod executions;
pub mod hooks;
pub mod language;
pub mod ports;
pub mod protocol;
pub mod readiness;
pub mod ring;
pub mod timeout;

use std::time::Duration;

pub use context::{
    ContextEntry, ContextId, ContextInfo, ContextPlan, ContextRegistry, ContextState,
    CreateContextInput, DEFAULT_CONTEXT_ID, plan_context, validate_envs,
};
pub use error::CodeError;
pub use execution::{
    ExecuteOutput, ExecutionErrorInfo, ExecutionId, ExecutionTracker, ResultBundle, SyntheticError,
};
pub use executions::{
    Attachment as ExecutionAttachment, ExecutionLimits, ExecutionRegistry,
    SubscriberId as ExecutionSubscriberId,
};
pub use hooks::{
    ProbeOutcome, SidecarConfig, VALIDATE_CELL, ValidateDecision, ValidationOutcome,
    ValidationState, probe_outcome, restart_after_resume, run_rotation_request, sidecar_spawn_spec,
    validate_hook_decision,
};
pub use language::{AvailableLanguages, ExecuteTarget, Language};
pub use ports::{
    EventFuture, KernelSidecar, KernelStatus, RandomError, RandomSource, SidecarEventSink,
    SidecarExitSink, SidecarIoError, SidecarLink,
};
pub use protocol::{
    ProtocolError, ReplyError, ReplyPayload, SIDECAR_PROTOCOL_VERSION, SidecarErrorCode,
    SidecarEvent, SidecarOp, SidecarRequest, decode_event, encode_request,
};
pub use readiness::{ReadyDecision, SidecarState, ready_hook_decision, restart_delay};
pub use ring::{EXECUTE_RING_CAPACITY_BYTES, ExecuteRing, event_cost};
pub use timeout::{TimeoutSchedule, plan_timeout};

/// Live contexts per sandbox, the default one included (the `/resume` probe
/// budget and what 2 GB tolerates with the warm-up).
pub const MAX_CONTEXTS: usize = 8;
/// `ExecuteRequest.code` above this is refused before reaching the sidecar.
pub const MAX_CODE_BYTES: usize = 1024 * 1024;
/// A sandbox never lives longer (ADR-007), so larger timeouts are clamped.
pub const MAX_EXECUTE_TIMEOUT_MS: u64 = 28_800_000;
/// `KeepAlive` cadence on a silent `Execute` stream.
pub const EXECUTE_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(5);
/// Between the interrupt at the deadline and the context restart.
pub const INTERRUPT_GRACE: Duration = Duration::from_secs(5);
/// Sidecar events buffered ahead of a slow `Execute` client.
pub const EXECUTE_QUEUE_CAPACITY: usize = 256;
/// How long a full execution queue blocks the sidecar reader before the
/// stream is truncated.
pub const STALL_TIMEOUT: Duration = Duration::from_secs(30);
/// How long an execution waits for a `Starting`/`Restarting` context.
pub const CONTEXT_READY_TIMEOUT: Duration = Duration::from_secs(30);
/// `/ready` answers 200 regardless this long after boot.
pub const READY_ESCAPE: Duration = Duration::from_secs(300);
/// A launched sidecar that has not said `ready` by then is killed.
pub const SIDECAR_READY_TIMEOUT: Duration = Duration::from_secs(180);
/// Longest stdout line accepted from the sidecar (an 8 MiB mime payload
/// escaped as JSON fits).
pub const MAX_SIDECAR_LINE_BYTES: usize = 16 * 1024 * 1024;
