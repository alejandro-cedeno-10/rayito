//! What the deadline needs from the outside to end the sandbox: something
//! that winds the workload and the agent down, and a last resort for when
//! that never completes.

/// Why the agent ends itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminationReason {
    SandboxTimeout,
}

impl TerminationReason {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SandboxTimeout => "sandbox_timeout",
        }
    }
}

pub trait SelfTerminator: Send + Sync {
    /// Starts the graceful end of the workload and of the agent; never blocks.
    fn begin(&self, reason: TerminationReason);
    /// Last resort after `force_budget`: ends the agent process now.
    fn force(&self, reason: TerminationReason);
}
