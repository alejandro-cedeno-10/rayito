//! What code execution needs from the outside: something that launches the
//! sidecar process and delivers its events and its exit, a link to write
//! request lines into it, the readiness bit `Health` reads, and a random
//! source for identifiers. Plain data and `std` futures, no runtime types.

use std::future::Future;
use std::pin::Pin;

use thiserror::Error;

use super::protocol::SidecarEvent;
use crate::process::{Pid, SpawnError, SpawnSpec};

pub type EventFuture = Pin<Box<dyn Future<Output = ()> + Send>>;
/// Called by the adapter's reader for every decoded stdout line, awaited
/// before the next line is read: a slow consumer stops the read, the pipe
/// fills and the sidecar's writer blocks (design D6).
pub type SidecarEventSink = Box<dyn FnMut(SidecarEvent) -> EventFuture + Send>;
/// Called once with the process exit code (`None` when killed by a signal).
pub type SidecarExitSink = Box<dyn FnOnce(Option<i32>) + Send>;

pub trait KernelSidecar: Send + Sync {
    fn launch(
        &self,
        spec: &SpawnSpec,
        events: SidecarEventSink,
        exited: SidecarExitSink,
    ) -> Result<Box<dyn SidecarLink>, SpawnError>;
}

pub trait SidecarLink: Send + Sync {
    fn pid(&self) -> Pid;
    /// Enqueues one encoded request line (without newline) for the writer.
    fn send(&self, line: &str) -> Result<(), SidecarIoError>;
    /// `SIGKILL` to the sidecar's process group; the exit sink still fires.
    fn kill(&self);
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum SidecarIoError {
    #[error("sidecar stdin is closed")]
    Closed,
    #[error("sidecar request queue is full")]
    QueueFull,
}

/// Read by `HealthGrpc` and the hooks.
pub trait KernelStatus: Send + Sync {
    fn kernel_ready(&self) -> bool;

    /// Whether the last `/resume` probe found a kernel that did not answer
    /// (restarted since, its variables gone); recomputed at every resume.
    fn kernel_state_lost(&self) -> bool;
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("random source failed: {0}")]
pub struct RandomError(pub String);

pub trait RandomSource: Send + Sync {
    fn fill(&self, buf: &mut [u8]) -> Result<(), RandomError>;
}
