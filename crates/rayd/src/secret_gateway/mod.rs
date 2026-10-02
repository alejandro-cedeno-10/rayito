//! Adapters of the secrets-gateway feature (M15, ADR-023): the per-route
//! loopback listener (`listener`) and the shared HTTPS client every route
//! forwards through (`upstream`). `features::secret_gateway` wires these
//! into the `ConfigurableFeature` slot `grpc::configure::ConfigureGrpc`
//! dispatches to.

pub mod listener;
pub mod upstream;

pub use listener::{GatewayRuntime, ListenError, RouteStatus};
pub use upstream::{GatewayUpstream, UpstreamInitError};
