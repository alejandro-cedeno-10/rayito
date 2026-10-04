//! `rayd_core::hook_peer::PeerSocketTable` over the kernel's TCP socket
//! tables: who owns the client end of a hook connection (C-01). Both
//! tables are read, because an IPv4 peer of the hooks listener can come
//! from a dual-stack socket listed only in `tcp6`.

use std::fs;
use std::net::SocketAddr;

use rayd_core::hook_peer::{PeerSocket, PeerSocketTable, find_in_proc_net};

/// The IPv4 and IPv6 TCP socket tables of `rayd`'s network namespace,
/// which the sandbox shares (`SECURITY.md` T2).
const PROC_NET_TCP_TABLES: [&str; 2] = ["/proc/net/tcp", "/proc/net/tcp6"];

#[derive(Debug, Default, Clone, Copy)]
pub struct ProcNetPeers;

impl PeerSocketTable for ProcNetPeers {
    fn find(&self, peer: SocketAddr) -> Option<PeerSocket> {
        PROC_NET_TCP_TABLES.iter().find_map(|path| {
            let table = fs::read_to_string(path).ok()?;
            find_in_proc_net(&table, peer)
        })
    }
}

/// `rayd`'s effective uid, the one `hook_peer::classify_peer` never takes
/// for a sandbox uid.
#[cfg(unix)]
#[must_use]
pub fn agent_uid() -> u32 {
    nix::unistd::geteuid().as_raw()
}

/// Without `/proc` there is no socket table either, so nothing is ever
/// classified as a sandbox uid; root is the conservative stand-in.
#[cfg(not(unix))]
#[must_use]
pub fn agent_uid() -> u32 {
    0
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use std::net::TcpListener as StdListener;
    use std::net::TcpStream as StdStream;

    use super::*;

    /// The real table finds this test's own client socket, owned by the
    /// uid the test runs as: the lookup the hooks listener does, end to
    /// end against the kernel.
    #[test]
    fn finds_this_process_s_own_client_socket() {
        let listener = StdListener::bind("127.0.0.1:0").unwrap();
        let client = StdStream::connect(listener.local_addr().unwrap()).unwrap();
        let (_server, peer) = listener.accept().unwrap();
        assert_eq!(peer, client.local_addr().unwrap());
        let socket = ProcNetPeers.find(peer).expect("the client end is listed");
        assert_eq!(socket.uid, agent_uid());
        assert!(socket.established);
    }

    #[test]
    fn a_port_nobody_uses_is_not_found() {
        let listener = StdListener::bind("127.0.0.1:0").unwrap();
        let unused = listener.local_addr().unwrap();
        drop(listener);
        assert_eq!(ProcNetPeers.find(unused), None);
    }
}
