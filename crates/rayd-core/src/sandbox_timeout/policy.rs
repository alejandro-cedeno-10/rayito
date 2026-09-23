//! What the `runHookPayload` lifecycle block asks for once validated, and
//! the two ways `SetTimeout` moves the deadline.

use std::time::Duration;

/// What happens at the deadline: the agent ends the VM, or the sandbox
/// expires and waits to be suspended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeoutAction {
    Kill,
    Pause,
}

impl TimeoutAction {
    /// The payload spelling; anything else is not an action.
    #[must_use]
    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "kill" => Some(Self::Kill),
            "pause" => Some(Self::Pause),
            _ => None,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Kill => "kill",
            Self::Pause => "pause",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeoutPolicy {
    pub on_timeout: TimeoutAction,
    /// E2B's `lifecycle.auto_resume`: only meaningful with `Pause`.
    pub auto_resume: bool,
}

/// A validated lifecycle block: the logical timeout, the platform cap
/// (`maximumDurationInSeconds`) and the policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LifecycleSpec {
    pub timeout: Duration,
    pub cap: Duration,
    pub policy: TimeoutPolicy,
}

/// `Exact` may shorten the deadline (`set_timeout`); `AtLeast` never does
/// (`connect(timeout=)`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeoutMode {
    Exact,
    AtLeast,
}

impl TimeoutMode {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Exact => "exact",
            Self::AtLeast => "at_least",
        }
    }
}
