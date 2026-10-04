//! Bounds of `rayd`'s own two listeners, the gRPC port (8080) and the
//! lifecycle hooks (9000). Both bind `0.0.0.0` inside the sandbox's network
//! namespace, so any sandbox process reaches them on loopback, and `rayd`
//! cannot raise its `RLIMIT_NOFILE` above the inherited 1024
//! (`AWS_API_NOTES.md` §9, Q30). Without a cap, idle or half-sent
//! connections could hold every descriptor `rayd` has, after which it could
//! no longer spawn a process, open a file or accept the platform's own
//! `/suspend` and `/terminate` (`SECURITY.md` T7).
//!
//! Each listener holds a fixed number of connection slots; a connection
//! beyond them waits in the kernel's accept backlog until one closes. The
//! hooks listener also drops a connection whose request head does not
//! arrive in time, and closes every connection after its one response, so
//! an idle socket never keeps a slot. An accept that fails for lack of
//! resources backs off before the next attempt instead of retrying at once.

use std::io::ErrorKind;
use std::time::Duration;

use crate::network::LOCAL_PROXY_MAX_CONNECTIONS;

/// `rayd`'s inherited `RLIMIT_NOFILE` hard limit, which it cannot raise
/// (no `CAP_SYS_RESOURCE`, `AWS_API_NOTES.md` §9 and Q30, measured
/// 2026-09-15).
pub const AGENT_NOFILE_LIMIT: usize = 1024;

/// Concurrent connections the gRPC listener serves. The SDK opens at most
/// two HTTP/2 channels per `Sandbox` (`SECURITY.md` T7), so this leaves
/// room for a hundred clients while keeping, together with the hooks and
/// the local proxy, under half of `AGENT_NOFILE_LIMIT`.
pub const GRPC_MAX_CONNECTIONS: usize = 256;

/// Concurrent connections the hooks listener serves. The platform sends
/// one hook at a time (`AWS_API_NOTES.md` §8); the rest is headroom for a
/// `/suspend` that overlaps a late `/run` or a retried `/ready`.
pub const HOOKS_MAX_CONNECTIONS: usize = 32;

/// How long a hook connection may take to send its whole request head.
/// The platform sends each hook from `127.0.0.1`, inside the guest
/// (`AWS_API_NOTES.md` §8), so a genuine head takes milliseconds; this is
/// the shortest timeout the image declares for a hook (`/terminate`,
/// 10 s), past which the platform has given up on that call anyway, and a
/// third of hyper's 30 s default.
pub const HOOKS_HEADER_READ_TIMEOUT: Duration = Duration::from_secs(10);

/// Pause after an accept failed for lack of resources (`EMFILE`,
/// `ENFILE`, `ENOBUFS`, `ENOMEM`): long enough that a persistent failure
/// costs no CPU, short enough that a slot freed meanwhile is used almost
/// at once.
pub const ACCEPT_BACKOFF: Duration = Duration::from_millis(100);

/// The two kinds of failed `accept`, which decide whether the next attempt
/// waits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcceptFailure {
    /// The connection itself went away before it was accepted (reset,
    /// aborted, refused) or the call was interrupted: the listener is fine
    /// and the next connection can be accepted at once.
    Connection,
    /// Anything else, chiefly running out of descriptors or memory: the
    /// same call would fail again right away, so it waits `ACCEPT_BACKOFF`.
    Resources,
}

impl AcceptFailure {
    /// The kind of a failed `accept` from its error kind. `EMFILE`,
    /// `ENFILE` and `ENOBUFS` have no `ErrorKind` of their own, so every
    /// kind that is not a per-connection one backs off.
    #[must_use]
    pub fn classify(kind: ErrorKind) -> Self {
        match kind {
            ErrorKind::ConnectionAborted
            | ErrorKind::ConnectionReset
            | ErrorKind::ConnectionRefused
            | ErrorKind::Interrupted
            | ErrorKind::WouldBlock => Self::Connection,
            _ => Self::Resources,
        }
    }
}

/// Descriptors the agent's listeners can hold at most: one per gRPC or
/// hook connection and two per proxied connection (client and upstream).
#[must_use]
pub const fn listener_descriptor_ceiling() -> usize {
    GRPC_MAX_CONNECTIONS + HOOKS_MAX_CONNECTIONS + 2 * LOCAL_PROXY_MAX_CONNECTIONS
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `EMFILE` on Linux and macOS (`errno(3)`).
    const EMFILE: i32 = 24;

    #[test]
    fn the_listeners_leave_most_descriptors_to_processes_and_files() {
        assert!(listener_descriptor_ceiling() <= AGENT_NOFILE_LIMIT * 3 / 4);
    }

    #[test]
    fn a_vanished_connection_is_retried_at_once() {
        for kind in [
            ErrorKind::ConnectionAborted,
            ErrorKind::ConnectionReset,
            ErrorKind::ConnectionRefused,
            ErrorKind::Interrupted,
            ErrorKind::WouldBlock,
        ] {
            assert_eq!(AcceptFailure::classify(kind), AcceptFailure::Connection);
        }
    }

    #[test]
    fn running_out_of_resources_backs_off() {
        for kind in [
            ErrorKind::OutOfMemory,
            ErrorKind::Other,
            ErrorKind::PermissionDenied,
            std::io::Error::from_raw_os_error(EMFILE).kind(),
        ] {
            assert_eq!(AcceptFailure::classify(kind), AcceptFailure::Resources);
        }
    }
}
