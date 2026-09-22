//! Processes spawned inside the sandbox: how a `Start` request becomes a
//! spawn plan (identity, environment, cwd, limits), how output is sequenced
//! and retained for `Connect(from_seq)` within the sandbox-wide output
//! budget, when a server timeout kills, and what the registry of live and
//! recently ended processes guarantees. Pure data and rules: the tokio
//! pumps and `fork`/`exec` live in the `rayd` adapters.

pub mod budget;
pub mod cwd;
pub mod env;
pub mod error;
pub mod events;
pub mod identity;
pub mod limits;
pub mod ports;
pub mod registry;
pub mod ring;
pub mod spec;
pub mod timeout;

use std::fmt;

pub use budget::{OUTPUT_BUDGET_HIGH_WATER, OutputBudget, SANDBOX_OUTPUT_BUDGET_BYTES};
pub use error::{CwdRejection, ProcessError};
pub use events::{EndStatus, OutputEvent, OutputStream, ProcessEnd, ProcessEvent, StreamFailure};
pub use identity::{ProcessIdentity, UserPolicy, resolve_username};
pub use limits::{CPU_LIMIT_KILL_GRACE_SECONDS, ResourceLimits, StdinMode};
pub use ports::{
    LookupError, ProcessSpawner, SignalError, SpawnError, SpawnedChild, SubscriberSlot, UserLookup,
};
pub use registry::{
    Attachment, ProcessKind, ProcessRegistry, ProcessSummary, RegistryLimits, SubscriberId,
};
pub use ring::OutputRing;
pub use spec::{ProcessConfigInfo, SpawnInput, SpawnSpec, plan_spawn, sandbox_limits};
pub use timeout::{EndReason, TimeoutPlan, TimeoutStep, WaitOutcome, end_from_wait};

/// Highest POSIX signal number `SendSignal` accepts (Linux real-time range).
pub const MAX_SIGNAL: i32 = 64;

/// Kernel process id of a spawned child, which is also its process-group id
/// because every child is spawned as a group leader.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Pid(pub u32);

impl fmt::Display for Pid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// `SendSignal` accepts any real signal number; `0` (existence probe) and
/// out-of-range values are rejected before touching the process group.
pub fn validate_signal(signal: i32) -> Result<i32, ProcessError> {
    if (1..=MAX_SIGNAL).contains(&signal) {
        Ok(signal)
    } else {
        Err(ProcessError::InvalidSignal(signal))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signals_between_one_and_sixty_four_are_valid() {
        assert_eq!(validate_signal(1), Ok(1));
        assert_eq!(validate_signal(9), Ok(9));
        assert_eq!(validate_signal(64), Ok(64));
    }

    #[test]
    fn zero_negative_and_large_signals_are_rejected() {
        assert_eq!(validate_signal(0), Err(ProcessError::InvalidSignal(0)));
        assert_eq!(validate_signal(-1), Err(ProcessError::InvalidSignal(-1)));
        assert_eq!(validate_signal(65), Err(ProcessError::InvalidSignal(65)));
    }

    #[test]
    fn pid_displays_as_its_number() {
        assert_eq!(Pid(4242).to_string(), "4242");
    }
}
