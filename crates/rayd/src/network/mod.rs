//! The guest egress policy of ADR-012 on the adapter side: the manager
//! that installs, swaps, verifies and recovers the uid-scoped routes, the
//! local forward proxy `rayd` serves as root on `127.0.0.1`, the chain to
//! the operator's SOCKS5 upstream and the proxy's decision counters. The
//! rules themselves live in `rayd_core::network`.

pub mod manager;
pub mod proxy;
pub mod stats;
pub mod upstream;

#[cfg(test)]
pub(crate) mod fake_kernel;
#[cfg(test)]
mod tests;

pub use manager::NetworkManager;
pub use proxy::{Dial, LocalProxy, ProxyPolicy, ProxySeams, Resolve};
