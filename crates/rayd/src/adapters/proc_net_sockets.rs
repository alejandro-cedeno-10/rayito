//! The `SocketOwners` port over `/proc/net/tcp` and `/proc/net/tcp6`:
//! `rayd` (root) sees every row of its network namespace, which the guest
//! shares with the sandbox and the platform agent, and the domain
//! (`rayd_core::hook_origin::socket_owner`) finds the caller's own row.
//! Nothing here is logged; a file that cannot be read is no owner.

use std::net::SocketAddr;

use rayd_core::hook_origin::{SocketOwners, socket_owner};

/// IPv4 sockets, and IPv6 ones (an IPv4-mapped peer of a dual-stack
/// listener included), as `proc(5)` lists them.
const PROC_NET_TCP: &str = "/proc/net/tcp";
const PROC_NET_TCP6: &str = "/proc/net/tcp6";

#[derive(Debug, Default, Clone, Copy)]
pub struct ProcNetSocketOwners;

impl SocketOwners for ProcNetSocketOwners {
    fn owner_uid(&self, peer: SocketAddr, local: SocketAddr) -> Option<u32> {
        let table = if peer.is_ipv4() {
            PROC_NET_TCP
        } else {
            PROC_NET_TCP6
        };
        let text = std::fs::read_to_string(table).ok()?;
        socket_owner(&text, peer, local)
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use std::net::{TcpListener, TcpStream};

    use super::*;

    /// A real loopback connection: the client socket's row names this
    /// test's own uid, looked up from the accepted end exactly as the
    /// hooks listener does.
    #[test]
    fn the_owner_of_a_loopback_client_is_its_process_uid() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let _client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (accepted, peer) = listener.accept().unwrap();
        let local = accepted.local_addr().unwrap();
        assert_eq!(
            ProcNetSocketOwners.owner_uid(peer, local),
            Some(nix::unistd::getuid().as_raw())
        );
        assert_eq!(ProcNetSocketOwners.owner_uid(local, peer), None);
    }
}
