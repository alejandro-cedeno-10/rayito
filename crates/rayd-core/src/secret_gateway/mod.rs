//! Domain of the secrets-gateway feature (M15, ADR-023): a loopback HTTP
//! listener per route that injects a vaulted header value into requests
//! the sandbox makes to one fixed upstream, after checking a method/path
//! allowlist and a rate limit (T24). Nothing here opens a socket, spawns a
//! task or calls Secrets Manager — `rayd::secret_gateway` (adapters) and
//! `rayito._secret_gateway`/`secret-gateway` (SDKs) do that; this module
//! only decides what a route is, whether one request against it may be
//! forwarded, and which header names a listener must strip versus inject.
//!
//! The one value this domain ever holds that matters for confidentiality
//! is a vaulted header's bytes (`vault::SecretValue`): `Zeroizing`, no
//! `Debug`/`Display`/`serde`, exposed only at the single point a header is
//! actually built.

pub mod decision;
pub mod header_template;
pub mod route;
pub mod vault;

pub use decision::{Decision, GatewayErrorClass, TokenBucket, evaluate};
pub use route::{GatewayRoute, GatewaySpec, GatewaySpecError, RawRoute, path_is_safe};
pub use vault::{HeaderInjectionError, SecretValue};
