//! What the process domain needs from the operating system: a user database,
//! something that can `fork`/`exec` a `SpawnSpec` and signal its group, and
//! a way to ask a subscriber slot whether its client is still there.
//! Synchronous functions with plain data, no runtime types.

use thiserror::Error;

use super::Pid;
use super::identity::ProcessIdentity;
use super::spec::SpawnSpec;

pub trait UserLookup: Send + Sync {
    fn lookup(&self, username: &str) -> Result<ProcessIdentity, LookupError>;
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum LookupError {
    #[error("unknown user")]
    UnknownUser,
    #[error("{0}")]
    Failed(String),
}

/// Adapter failures carry the errno *name* (`ENOENT`), never the command.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SpawnError {
    /// `exec` refused the program (`ENOENT`, `EACCES`): the client's mistake.
    #[error("cannot execute: {0}")]
    CannotExecute(String),
    #[error("spawn failed: {0}")]
    Failed(String),
    #[error("process spawning is not supported on this platform")]
    Unsupported,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SignalError {
    #[error("no such process group")]
    NoSuchProcess,
    #[error("signal failed: {0}")]
    Failed(String),
}

pub trait SpawnedChild: Send {
    fn pid(&self) -> Pid;
}

/// A subscriber sink the registry stores and counts against the per-pid
/// cap. A slot whose receiver is gone (client cancelled, connection reset)
/// must answer `false` so the registry can reclaim it before the next
/// `Connect`, without waiting for the process to write again.
pub trait SubscriberSlot {
    fn is_open(&self) -> bool;
}

pub trait ProcessSpawner: Send + Sync {
    type Child: SpawnedChild;

    fn spawn(&self, spec: &SpawnSpec) -> Result<Self::Child, SpawnError>;

    /// `killpg(pid, signal)`: the child is its own group leader, so `pid`
    /// names the group.
    fn signal_group(&self, pid: Pid, signal: i32) -> Result<(), SignalError>;
}
