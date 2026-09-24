//! Runtime side of suspend/resume (design D6, D7): the broadcast that
//! closes every open client stream before the checkpoint, the stream
//! wrapper that turns it into the right close form per RPC, the sleep that
//! measures its deadline on the running clock, the watchdog that recovers
//! a `/suspend` nobody checkpointed, the shared reaper tick, the 5 s
//! metrics sampler that only reads procfs while the gate is open, and the
//! logical deadline's watcher thread and exit sequence (ADR-011).

pub mod exit_terminator;
pub mod metrics_sampler;
pub mod reaper;
pub mod running_sleep;
pub mod suspend;
pub mod suspend_watchdog;
pub mod timeout_watcher;

pub use exit_terminator::{ExitParts, ExitReason, ExitTerminator, ForceExit};
pub use metrics_sampler::spawn_metrics_sampler;
pub use reaper::{DEFAULT_REAPER_INTERVAL, Reaper, spawn_reaper};
pub use running_sleep::running_sleep;
pub use suspend::{
    StreamCloseReason, SuspendAware, SuspendClose, SuspendSignal, SuspendWatch, SuspendableStream,
    close_status,
};
pub use suspend_watchdog::{WatchdogOutcome, suspend_watchdog};
pub use timeout_watcher::{StreamCloser, TimeoutWatcher, spawn_timeout_watcher};
