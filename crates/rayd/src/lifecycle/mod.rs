//! Runtime side of suspend/resume (design D6, D7): the broadcast that
//! closes every open client stream before the checkpoint, the stream
//! wrapper that turns it into the right close form per RPC, the sleep that
//! measures its deadline on the running clock, the watchdog that recovers
//! a `/suspend` nobody checkpointed, and the shared reaper tick.

pub mod reaper;
pub mod running_sleep;
pub mod suspend;
pub mod suspend_watchdog;

pub use reaper::{DEFAULT_REAPER_INTERVAL, Reaper, spawn_reaper};
pub use running_sleep::running_sleep;
pub use suspend::{SuspendAware, SuspendClose, SuspendSignal, SuspendWatch, SuspendableStream};
pub use suspend_watchdog::{WatchdogOutcome, suspend_watchdog};
