//! What the PTY domain needs from the operating system: something that opens
//! a pseudo-terminal pair, starts a `SpawnSpec` with the slave as its
//! controlling terminal and signals the resulting session. Reading, writing,
//! resizing and reaping are runtime I/O and live with the adapter.

use super::PtySize;
use crate::process::{Pid, SignalError, SpawnError, SpawnSpec};

pub trait PtyChild: Send {
    fn pid(&self) -> Pid;
}

pub trait PtyBackend: Send + Sync {
    type Pty: PtyChild;

    /// `openpty` with `size`, then the spec's program as a session leader
    /// whose controlling terminal is the slave; the master stays with the
    /// caller.
    fn open(&self, spec: &SpawnSpec, size: PtySize) -> Result<Self::Pty, SpawnError>;

    /// `killpg(pid, signal)`: the shell is its own session and group leader,
    /// so `pid` names the group.
    fn signal_group(&self, pid: Pid, signal: i32) -> Result<(), SignalError>;
}
