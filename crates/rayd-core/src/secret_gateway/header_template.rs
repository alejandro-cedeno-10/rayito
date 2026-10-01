//! Which inbound header names a route's listener must drop before
//! forwarding, and which it injects from the vault (ADR-023, T24). Pure:
//! it works over header *names* only (`&str`), never a transport header
//! type, so the adapter (`rayd::secret_gateway::listener`) is the only
//! place that touches `http::HeaderMap`.

use std::collections::BTreeSet;

use super::route::GatewayRoute;

/// Hop-by-hop headers RFC 9110 §7.6.1 says a proxy must not forward
/// unchanged; the listener always strips these in addition to whatever
/// `stripped_header_names` returns.
pub const HOP_BY_HOP_HEADERS: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
];

/// Lower-cased header names the listener must strip from the inbound
/// request before it reaches the upstream: exactly the names the route
/// injects, so the sandbox can never spoof its own gateway's credential by
/// setting the header itself, nor read a previous value back from an echo
/// endpoint.
#[must_use]
pub fn stripped_header_names(route: &GatewayRoute) -> BTreeSet<String> {
    route.header_names().map(str::to_ascii_lowercase).collect()
}

/// `true` when `name` (already lower-cased by the caller) must never reach
/// the upstream unchanged: a hop-by-hop header, the inbound `host` (the
/// listener sets its own from `route.upstream()`), or one this route
/// injects itself.
#[must_use]
pub fn must_drop(route: &GatewayRoute, lower_name: &str) -> bool {
    lower_name == "host"
        || HOP_BY_HOP_HEADERS.contains(&lower_name)
        || stripped_header_names(route).contains(lower_name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret_gateway::route::{GatewaySpec, RawRoute};
    use crate::secret_gateway::vault::SecretValue;

    fn route() -> GatewayRoute {
        GatewaySpec::parse(vec![RawRoute {
            name: "a".to_owned(),
            upstream: "https://example.com".to_owned(),
            headers: vec![("x-api-key".to_owned(), SecretValue::new("k".to_owned()))],
            allow: vec![("GET".to_owned(), "/x".to_owned())],
            rate_per_minute: 60,
        }])
        .unwrap()
        .routes()[0]
            .clone()
    }

    #[test]
    fn the_injected_header_name_is_in_the_strip_set() {
        let names = stripped_header_names(&route());
        assert!(names.contains("x-api-key"));
        assert_eq!(names.len(), 1);
    }

    #[test]
    fn must_drop_covers_host_hop_by_hop_and_injected_names() {
        let route = route();
        assert!(must_drop(&route, "host"));
        assert!(must_drop(&route, "connection"));
        assert!(must_drop(&route, "x-api-key"));
        assert!(!must_drop(&route, "content-type"));
    }

    #[test]
    fn matching_is_case_insensitive_through_the_lower_cased_contract() {
        // The caller is responsible for lower-casing before calling
        // `must_drop`; this test documents that `stripped_header_names`
        // itself always returns lower-case names regardless of the route's
        // own header-name casing.
        let names = stripped_header_names(&route());
        assert!(names.iter().all(|name| name.chars().all(|c| !c.is_ascii_uppercase())));
    }
}
