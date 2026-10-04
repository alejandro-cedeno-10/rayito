//! Who opened a hook connection, and what that changes for the hook
//! (`SECURITY.md` T2, C-01). The hooks listener shares the network
//! namespace with the sandbox, so its source address (`127.0.0.1`) says
//! nothing; the kernel's socket table does: the client end of the
//! connection is listed in `/proc/net/tcp` (or `tcp6`, for a dual-stack
//! socket) with the uid that owns it. The platform agent that sends the
//! genuine hooks owns its sockets as uids outside `SANDBOX_UID_RANGE`
//! (991-994, `AWS_API_NOTES.md` Q48), and every sandbox process runs at a
//! uid inside it.
//!
//! The answer only narrows what a hook does, never what it may answer: a
//! refused hook still answers 200 (a non-2xx on a runtime hook terminates
//! the `MicroVM`). Only `/terminate` and `/validate` are refused, the two
//! whose forged effect the operator cannot undo or recover from; a
//! session-changing `/suspend` or `/resume` from a sandbox uid is audited
//! as an anomaly but still honoured, because a refused genuine one would
//! leave a real checkpoint unprepared (`hooks` module docs). Pure: the
//! adapter reads the table and hands the text in.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use crate::hooks::HookCallOutcome;
use crate::lifecycle::Hook;
use crate::network::route_plan::{SANDBOX_UID_MAX, SANDBOX_UID_MIN};

/// `st` value of an established connection in `/proc/net/tcp`
/// (`TCP_ESTABLISHED` = 1, `include/net/tcp_states.h`).
pub const TCP_ESTABLISHED: &str = "01";

/// Outcome a refused hook answers with (always 200).
pub const PEER_REFUSED: &str = "peer_refused";

/// Whitespace-separated columns of a `/proc/net/tcp` row (`proc(5)`):
/// `sl local_address rem_address st tx_queue:rx_queue tr:tm->when
/// retrnsmt uid ...`.
const LOCAL_ADDRESS_COLUMN: usize = 1;
const STATE_COLUMN: usize = 3;
const UID_COLUMN: usize = 7;
/// Hex digits of an IPv4 and an IPv6 address in that table.
const IPV4_HEX_DIGITS: usize = 8;
const IPV6_HEX_DIGITS: usize = 32;
/// Hex digits of each 32-bit word of an IPv6 address there.
const WORD_HEX_DIGITS: usize = 8;

/// The client end of a hook connection, as the kernel lists it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerSocket {
    pub uid: u32,
    /// A genuine caller waits for its answer, so its end is still
    /// established; a closed or reset end can list a uid that no longer
    /// says who opened it.
    pub established: bool,
}

/// The port to the kernel's socket tables.
pub trait PeerSocketTable: Send + Sync {
    /// The socket whose local address is `peer` (the remote end the hooks
    /// listener saw), in this network namespace; `None` when none is
    /// listed or the table cannot be read.
    fn find(&self, peer: SocketAddr) -> Option<PeerSocket>;
}

/// Looks `peer` up in the text of `/proc/net/tcp` or `/proc/net/tcp6`.
/// An IPv4 peer also matches its IPv4-mapped form in `tcp6`, so a sandbox
/// process cannot hide behind a dual-stack socket.
#[must_use]
pub fn find_in_proc_net(table: &str, peer: SocketAddr) -> Option<PeerSocket> {
    table.lines().skip(1).find_map(|row| {
        let columns: Vec<&str> = row.split_whitespace().collect();
        let local = parse_endpoint(columns.get(LOCAL_ADDRESS_COLUMN)?)?;
        if !same_endpoint(local, peer) {
            return None;
        }
        Some(PeerSocket {
            uid: columns.get(UID_COLUMN)?.parse().ok()?,
            established: *columns.get(STATE_COLUMN)? == TCP_ESTABLISHED,
        })
    })
}

/// `ADDR:PORT` in hex. The kernel prints each 32-bit word of the address
/// as the integer it reads from memory, so the word's native-endian bytes
/// are the address bytes in network order on the host that printed it
/// (the one parsing it here).
fn parse_endpoint(text: &str) -> Option<SocketAddr> {
    let (address, port) = text.split_once(':')?;
    let port = u16::from_str_radix(port, 16).ok()?;
    let ip = match address.len() {
        IPV4_HEX_DIGITS => IpAddr::V4(Ipv4Addr::from(parse_word(address)?)),
        IPV6_HEX_DIGITS => {
            let mut octets = [0_u8; 16];
            for (index, word) in octets.as_chunks_mut::<4>().0.iter_mut().enumerate() {
                let start = index * WORD_HEX_DIGITS;
                *word = parse_word(address.get(start..start + WORD_HEX_DIGITS)?)?;
            }
            IpAddr::V6(Ipv6Addr::from(octets))
        }
        _ => return None,
    };
    Some(SocketAddr::new(ip, port))
}

fn parse_word(hex: &str) -> Option<[u8; 4]> {
    u32::from_str_radix(hex, 16).ok().map(u32::to_ne_bytes)
}

fn same_endpoint(listed: SocketAddr, peer: SocketAddr) -> bool {
    listed.port() == peer.port() && listed.ip().to_canonical() == peer.ip().to_canonical()
}

/// What the socket table says about a hook's caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PeerOrigin {
    /// An established connection owned by a uid outside the sandbox range
    /// (root or the platform agent).
    Platform,
    /// A connection owned by a sandbox uid, in any state.
    Sandbox,
    /// Not listed, or listed in a state whose uid proves nothing.
    Unverified,
}

impl PeerOrigin {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Platform => "platform",
            Self::Sandbox => "sandbox",
            Self::Unverified => "unverified",
        }
    }
}

/// Whether `uid` is one the sandbox's processes run at.
#[must_use]
pub fn is_sandbox_uid(uid: u32) -> bool {
    (SANDBOX_UID_MIN..=SANDBOX_UID_MAX).contains(&uid)
}

/// `agent_uid` is `rayd`'s own effective uid: a connection it owns is
/// never a sandbox's. On the image `rayd` runs as root, so the exception
/// never applies there; where `rayd` runs unprivileged (a developer's
/// machine) the sandbox runs as `rayd`'s own uid and there is no boundary
/// for the check to defend.
#[must_use]
pub fn classify_peer(socket: Option<PeerSocket>, agent_uid: u32) -> PeerOrigin {
    match socket {
        None => PeerOrigin::Unverified,
        Some(socket) if is_sandbox_uid(socket.uid) && socket.uid != agent_uid => {
            PeerOrigin::Sandbox
        }
        Some(socket) if !socket.established => PeerOrigin::Unverified,
        Some(_) => PeerOrigin::Platform,
    }
}

/// What the hooks adapter does with a call before its handler runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PeerAction {
    /// Run the handler; `Anomalous` also counts one anomaly.
    Proceed(HookCallOutcome),
    /// Answer 200 `PEER_REFUSED` without running the handler, audited as
    /// an anomaly.
    Refuse,
}

/// `/terminate` and `/validate` from a sandbox uid are refused. An
/// unverified `/terminate` is honoured (the lookup can miss a genuine
/// call) but counted, so a caller that dodges the lookup still shows in
/// `hook_anomalies`. A `/suspend` or `/resume` from a sandbox uid is
/// honoured and counted. `/ready` and `/run` are always honoured without
/// a count here, and so is a `/validate` that is not refused: their
/// handlers already count every call after the accepted `/run`, and
/// before it nothing is counted (`HookAudit::note_anomaly_after_run`), so
/// a count here would only ever double theirs.
#[must_use]
pub fn peer_action(hook: Hook, origin: PeerOrigin) -> PeerAction {
    match (hook, origin) {
        (Hook::Terminate | Hook::Validate, PeerOrigin::Sandbox) => PeerAction::Refuse,
        (Hook::Terminate, PeerOrigin::Unverified)
        | (Hook::Suspend | Hook::Resume, PeerOrigin::Sandbox) => {
            PeerAction::Proceed(HookCallOutcome::Anomalous)
        }
        _ => PeerAction::Proceed(HookCallOutcome::Nominal),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEADER: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode";
    const HOOKS_PORT: u16 = 9000;
    const PEER_PORT: u16 = 41_234;
    const ROOT: u32 = 0;
    const PLATFORM_AGENT: u32 = 993;
    const SANDBOX_USER: u32 = 1000;
    const FIN_WAIT2: &str = "05";

    /// The address as this host's kernel would print it.
    fn hex_v4(ip: Ipv4Addr) -> String {
        format!("{:08X}", u32::from_ne_bytes(ip.octets()))
    }

    fn hex_v6(ip: Ipv6Addr) -> String {
        use std::fmt::Write as _;
        ip.octets()
            .as_chunks::<4>()
            .0
            .iter()
            .fold(String::new(), |mut hex, word| {
                write!(hex, "{:08X}", u32::from_ne_bytes(*word)).unwrap();
                hex
            })
    }

    fn row(local: &str, port: u16, state: &str, uid: u32) -> String {
        format!(
            "   0: {local}:{port:04X} {}:{HOOKS_PORT:04X} {state} 00000000:00000000 00:00000000 00000000  {uid}        0 4242 1 0000000000000000 20 4 30 10 -1",
            hex_v4(Ipv4Addr::LOCALHOST)
        )
    }

    fn table(rows: &[String]) -> String {
        std::iter::once(HEADER.to_owned())
            .chain(rows.iter().cloned())
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn loopback_peer() -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), PEER_PORT)
    }

    #[test]
    fn finds_the_owner_of_an_ipv4_peer() {
        let text = table(&[
            row(
                &hex_v4(Ipv4Addr::LOCALHOST),
                HOOKS_PORT,
                TCP_ESTABLISHED,
                ROOT,
            ),
            row(
                &hex_v4(Ipv4Addr::LOCALHOST),
                PEER_PORT,
                TCP_ESTABLISHED,
                SANDBOX_USER,
            ),
        ]);
        assert_eq!(
            find_in_proc_net(&text, loopback_peer()),
            Some(PeerSocket {
                uid: SANDBOX_USER,
                established: true
            })
        );
    }

    #[test]
    fn an_ipv4_mapped_dual_stack_socket_still_matches() {
        let mapped = Ipv4Addr::LOCALHOST.to_ipv6_mapped();
        let text = table(&[row(
            &hex_v6(mapped),
            PEER_PORT,
            TCP_ESTABLISHED,
            SANDBOX_USER,
        )]);
        assert_eq!(
            find_in_proc_net(&text, loopback_peer()).map(|socket| socket.uid),
            Some(SANDBOX_USER)
        );
    }

    #[test]
    fn a_closing_socket_is_listed_as_not_established() {
        let text = table(&[row(
            &hex_v4(Ipv4Addr::LOCALHOST),
            PEER_PORT,
            FIN_WAIT2,
            ROOT,
        )]);
        assert_eq!(
            find_in_proc_net(&text, loopback_peer()),
            Some(PeerSocket {
                uid: ROOT,
                established: false
            })
        );
    }

    #[test]
    fn another_port_or_garbage_is_no_match() {
        let text = table(&[
            row(
                &hex_v4(Ipv4Addr::LOCALHOST),
                PEER_PORT + 1,
                TCP_ESTABLISHED,
                SANDBOX_USER,
            ),
            "   1: nothex:zz 00000000:0000 01".to_owned(),
            String::new(),
        ]);
        assert_eq!(find_in_proc_net(&text, loopback_peer()), None);
        assert_eq!(find_in_proc_net("", loopback_peer()), None);
    }

    #[test]
    fn the_header_row_is_never_parsed() {
        let text = row(
            &hex_v4(Ipv4Addr::LOCALHOST),
            PEER_PORT,
            TCP_ESTABLISHED,
            SANDBOX_USER,
        );
        assert_eq!(find_in_proc_net(&text, loopback_peer()), None);
    }

    #[test]
    fn a_sandbox_uid_is_conclusive_in_any_state() {
        for established in [true, false] {
            let socket = PeerSocket {
                uid: SANDBOX_USER,
                established,
            };
            assert_eq!(classify_peer(Some(socket), ROOT), PeerOrigin::Sandbox);
        }
        let top = PeerSocket {
            uid: SANDBOX_UID_MAX,
            established: true,
        };
        assert_eq!(classify_peer(Some(top), ROOT), PeerOrigin::Sandbox);
    }

    #[test]
    fn root_and_the_platform_agent_are_the_platform_only_while_established() {
        for uid in [
            ROOT,
            PLATFORM_AGENT,
            SANDBOX_UID_MIN - 1,
            SANDBOX_UID_MAX + 1,
        ] {
            let open = PeerSocket {
                uid,
                established: true,
            };
            assert_eq!(classify_peer(Some(open), ROOT), PeerOrigin::Platform);
            let closing = PeerSocket {
                uid,
                established: false,
            };
            assert_eq!(classify_peer(Some(closing), ROOT), PeerOrigin::Unverified);
        }
        assert_eq!(classify_peer(None, ROOT), PeerOrigin::Unverified);
    }

    #[test]
    fn rayd_s_own_uid_is_never_a_sandbox_uid() {
        let own = PeerSocket {
            uid: SANDBOX_USER,
            established: true,
        };
        assert_eq!(classify_peer(Some(own), SANDBOX_USER), PeerOrigin::Platform);
    }

    #[test]
    fn only_terminate_and_validate_are_ever_refused() {
        for hook in Hook::ALL {
            let expected = match hook {
                Hook::Terminate | Hook::Validate => PeerAction::Refuse,
                Hook::Suspend | Hook::Resume => PeerAction::Proceed(HookCallOutcome::Anomalous),
                Hook::Ready | Hook::Run => PeerAction::Proceed(HookCallOutcome::Nominal),
            };
            assert_eq!(peer_action(hook, PeerOrigin::Sandbox), expected, "{hook}");
            assert_eq!(
                peer_action(hook, PeerOrigin::Platform),
                PeerAction::Proceed(HookCallOutcome::Nominal),
                "{hook}"
            );
        }
    }

    #[test]
    fn an_unverified_caller_is_counted_only_on_terminate() {
        for hook in Hook::ALL {
            let expected = if hook == Hook::Terminate {
                PeerAction::Proceed(HookCallOutcome::Anomalous)
            } else {
                PeerAction::Proceed(HookCallOutcome::Nominal)
            };
            assert_eq!(
                peer_action(hook, PeerOrigin::Unverified),
                expected,
                "{hook}"
            );
        }
    }
}
