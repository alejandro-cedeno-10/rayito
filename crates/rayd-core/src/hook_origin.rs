//! Who is calling a lifecycle hook, as far as the guest can tell: the uid
//! that owns the caller's TCP socket. Hooks and proxied client traffic both
//! arrive from `127.0.0.1` (`AWS_API_NOTES.md` §8), so neither the source
//! address nor a header tells them apart, but the socket owner does: the
//! platform's agent inside the guest owns its sockets as uids 991-994
//! (Q48), root is `rayd` itself, and every sandbox process runs at a uid in
//! `SANDBOX_UID_FIRST..=SANDBOX_UID_LAST` (the same range the IMDS block
//! and the egress routes cover, `network::route_plan::SANDBOX_UID_RANGE`).
//!
//! Only a positive match is acted on: a socket that cannot be found (the
//! caller already gone, `/proc` not mounted, a test harness without a
//! connection) is `Unknown` and the hook behaves exactly as before. What
//! `Sandbox` changes is decided by `SandboxSession::run_from` (a `/run`
//! from the sandbox is refused without claiming the boot's one `/run`:
//! the first step of C-01, which closes RAYD-08 — a template's
//! `start_cmd` is already running when the genuine `/run` arrives).
//!
//! The `/proc/net/tcp{,6}` text is parsed here; the `rayd` adapter only
//! reads the two files (`SocketOwners`).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use crate::process::identity::{MAX_UNPRIVILEGED_ID, MIN_UNPRIVILEGED_ID};

/// First uid of the sandbox range: the image's `user` and every account a
/// request may run as (`process::identity`).
pub const SANDBOX_UID_FIRST: u32 = MIN_UNPRIVILEGED_ID;
/// Last uid of the sandbox range, the end of `SANDBOX_UID_RANGE`.
pub const SANDBOX_UID_LAST: u32 = MAX_UNPRIVILEGED_ID;

/// Hex digits of an IPv4 address and of an IPv6 address in
/// `/proc/net/tcp{,6}` (`%08X` per 32-bit word, `net/ipv4/tcp_ipv4.c`,
/// `net/ipv6/tcp_ipv6.c`).
const IPV4_HEX_DIGITS: usize = 8;
const IPV6_HEX_DIGITS: usize = 32;
const HEX_DIGITS_PER_WORD: usize = 8;
/// Whitespace-separated column of the owning uid in a socket row
/// (`sl local_address rem_address st tx_queue:rx_queue tr:tm->when
/// retrnsmt uid ...`).
const UID_COLUMN: usize = 7;
const LOCAL_ADDRESS_COLUMN: usize = 1;
const REMOTE_ADDRESS_COLUMN: usize = 2;

/// What the socket owner says about a hook's caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookOrigin {
    /// A uid in the sandbox range owns the caller's socket.
    Sandbox,
    /// Root or a platform uid owns it.
    Platform,
    /// The socket could not be found: no decision is taken on it.
    Unknown,
}

impl HookOrigin {
    /// `agent_uid` is the uid `rayd` itself runs as: a socket it owns says
    /// nothing about the sandbox. In the image `rayd` is root, so this only
    /// matters where it runs unprivileged (`make dev-run`, whose sandbox
    /// processes share its uid and whose hooks come from the developer's
    /// own shell, `make dev-hooks`).
    #[must_use]
    pub fn from_socket_owner(uid: Option<u32>, agent_uid: u32) -> Self {
        match uid {
            Some(uid) if uid == agent_uid => Self::Platform,
            Some(uid) if (SANDBOX_UID_FIRST..=SANDBOX_UID_LAST).contains(&uid) => Self::Sandbox,
            Some(_) => Self::Platform,
            None => Self::Unknown,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sandbox => "sandbox",
            Self::Platform => "platform",
            Self::Unknown => "unknown",
        }
    }
}

/// The uid owning the caller's socket of one hook connection. The adapter
/// reads `/proc/net/tcp` and `/proc/net/tcp6` (`rayd` is root, so every
/// row is visible) and hands the text to `socket_owner`.
pub trait SocketOwners: Send + Sync {
    /// `peer` is the caller's address as the hooks listener saw it, `local`
    /// the listener's own end of that connection.
    fn owner_uid(&self, peer: SocketAddr, local: SocketAddr) -> Option<u32>;
}

/// No socket is ever found: every hook is `Unknown` (tests and hosts that
/// never serve hooks over a real connection).
#[derive(Debug, Default, Clone, Copy)]
pub struct NoSocketOwners;

impl SocketOwners for NoSocketOwners {
    fn owner_uid(&self, _peer: SocketAddr, _local: SocketAddr) -> Option<u32> {
        None
    }
}

/// The caller's own row in `/proc/net/tcp{,6}` text: its local end is the
/// hook's `peer` and its remote end the listener's `local`. Malformed rows
/// (and the header line) are skipped.
#[must_use]
pub fn socket_owner(proc_net_tcp: &str, peer: SocketAddr, local: SocketAddr) -> Option<u32> {
    proc_net_tcp.lines().find_map(|line| {
        let columns: Vec<&str> = line.split_whitespace().collect();
        let row_local = parse_socket(columns.get(LOCAL_ADDRESS_COLUMN)?)?;
        let row_remote = parse_socket(columns.get(REMOTE_ADDRESS_COLUMN)?)?;
        if row_local != peer || row_remote != local {
            return None;
        }
        columns.get(UID_COLUMN)?.parse().ok()
    })
}

/// `ADDRESS:PORT` in hex. The kernel prints each 32-bit word of the
/// address as the host-order value of its network-order bytes, so the
/// bytes come back with `to_ne_bytes`; the port is plain hex.
fn parse_socket(text: &str) -> Option<SocketAddr> {
    let (address, port) = text.split_once(':')?;
    let port = u16::from_str_radix(port, 16).ok()?;
    let ip = match address.len() {
        IPV4_HEX_DIGITS => IpAddr::V4(Ipv4Addr::from(parse_word(address)?)),
        IPV6_HEX_DIGITS => {
            let mut bytes = [0u8; 16];
            for (index, chunk) in bytes.as_chunks_mut::<4>().0.iter_mut().enumerate() {
                let start = index * HEX_DIGITS_PER_WORD;
                let word = address.get(start..start + HEX_DIGITS_PER_WORD)?;
                *chunk = parse_word(word)?;
            }
            IpAddr::V6(Ipv6Addr::from(bytes))
        }
        _ => return None,
    };
    Some(SocketAddr::new(ip, port))
}

fn parse_word(hex: &str) -> Option<[u8; 4]> {
    u32::from_str_radix(hex, 16).ok().map(u32::to_ne_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A `/proc/net/tcp` capture from a little-endian guest: the hooks
    /// listener on `0.0.0.0:9000` (`2328`), one connection from a uid-1000
    /// process on port 40000 (`9C40`) and one from a platform uid on port
    /// 40001.
    const PROC_NET_TCP: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:2328 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1 0000000000000000 100 0 0 10 0
   1: 0100007F:2328 0100007F:9C40 01 00000000:00000000 00:00000000 00000000     0        0 2 1 0000000000000000 20 4 30 10 -1
   2: 0100007F:9C40 0100007F:2328 01 00000000:00000000 00:00000000 00000000  1000        0 3 1 0000000000000000 20 4 30 10 -1
   3: 0100007F:9C41 0100007F:2328 01 00000000:00000000 00:00000000 00000000   993        0 4 1 0000000000000000 20 4 30 10 -1
";

    fn loopback(port: u16) -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port)
    }

    #[cfg(target_endian = "little")]
    #[test]
    fn the_callers_own_row_names_its_owner() {
        let listener = loopback(9000);
        assert_eq!(
            socket_owner(PROC_NET_TCP, loopback(40000), listener),
            Some(1000)
        );
        assert_eq!(
            socket_owner(PROC_NET_TCP, loopback(40001), listener),
            Some(993)
        );
        assert_eq!(socket_owner(PROC_NET_TCP, loopback(40002), listener), None);
        assert_eq!(
            socket_owner(PROC_NET_TCP, loopback(40000), loopback(8080)),
            None,
            "the remote end must be the hooks listener"
        );
    }

    #[cfg(target_endian = "little")]
    #[test]
    fn ipv6_rows_and_mapped_addresses_parse() {
        let mapped: SocketAddr = "[::ffff:127.0.0.1]:40000".parse().unwrap();
        let listener: SocketAddr = "[::ffff:127.0.0.1]:9000".parse().unwrap();
        let text = "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0000000000000000FFFF00000100007F:9C40 0000000000000000FFFF00000100007F:2328 01 00000000:00000000 00:00000000 00000000  1000        0 5 1
";
        assert_eq!(socket_owner(text, mapped, listener), Some(1000));
        let v6_loopback: SocketAddr = "[::1]:40000".parse().unwrap();
        assert_eq!(
            parse_socket("00000000000000000000000001000000:9C40"),
            Some(v6_loopback)
        );
    }

    #[test]
    fn malformed_rows_are_skipped() {
        let text = "garbage\n   0: zz:2328 00000000:0000 0A x x x notauid\n";
        assert_eq!(socket_owner(text, loopback(1), loopback(2)), None);
        assert_eq!(parse_socket("0100007F"), None);
        assert_eq!(parse_socket("0100:2328"), None);
    }

    /// `rayd` as root, the image.
    const ROOT: u32 = 0;

    #[test]
    fn only_a_sandbox_uid_is_a_sandbox_origin() {
        assert_eq!(
            HookOrigin::from_socket_owner(Some(1000), ROOT),
            HookOrigin::Sandbox
        );
        assert_eq!(
            HookOrigin::from_socket_owner(Some(SANDBOX_UID_LAST), ROOT),
            HookOrigin::Sandbox
        );
        for platform in [0, 991, 993, 994, 999, SANDBOX_UID_LAST + 1] {
            assert_eq!(
                HookOrigin::from_socket_owner(Some(platform), ROOT),
                HookOrigin::Platform,
                "{platform}"
            );
        }
        assert_eq!(
            HookOrigin::from_socket_owner(None, ROOT),
            HookOrigin::Unknown
        );
        assert_eq!(NoSocketOwners.owner_uid(loopback(1), loopback(2)), None);
    }

    /// An unprivileged `rayd` (the dev loop) and the shell that drives its
    /// hooks share a uid: that caller is not the sandbox.
    #[test]
    fn a_socket_of_the_agents_own_uid_is_never_a_sandbox_origin() {
        assert_eq!(
            HookOrigin::from_socket_owner(Some(1000), 1000),
            HookOrigin::Platform
        );
        assert_eq!(
            HookOrigin::from_socket_owner(Some(1001), 1000),
            HookOrigin::Sandbox
        );
    }

    #[test]
    fn the_sandbox_range_is_the_one_the_routes_use() {
        assert_eq!(
            crate::network::route_plan::SANDBOX_UID_RANGE,
            format!("{SANDBOX_UID_FIRST}-{SANDBOX_UID_LAST}")
        );
    }
}
