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

/// Framing and routing headers the listener derives itself (`host` from
/// `GatewayRoute::upstream`, `content-length`/`trailer` from the streamed
/// body): a route that injected one of these could desynchronise the
/// forwarded request or point it at another host.
pub const FRAMING_HEADERS: [&str; 3] = ["host", "content-length", "trailer"];

/// `true` when `lower_name` is a header no route may inject: a hop-by-hop
/// header or one of `FRAMING_HEADERS`. `GatewaySpec::parse` rejects such a
/// route (`invalid_header_name`) before any listener opens.
#[must_use]
pub fn is_reserved_header(lower_name: &str) -> bool {
    FRAMING_HEADERS.contains(&lower_name) || HOP_BY_HOP_HEADERS.contains(&lower_name)
}

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

/// Vaulted values shorter than this are matched in response headers by
/// name only: a value this short could occur inside an innocuous header
/// (`content-type`, `content-encoding`) and dropping that would break the
/// response, while the API keys a route vaults are tens of characters.
pub const MIN_REFLECTED_VALUE_LEN: usize = 8;

/// `true` when a header of the upstream's response must not reach the
/// sandbox: a hop-by-hop header, one this route injects (an upstream that
/// echoes the credential back under its own name), or any header whose
/// value contains one of the route's vaulted values (an echo under another
/// name). The response body is streamed unchanged, so an upstream that
/// echoes request headers in its body still leaks them: that is why
/// `allow` must never list such an endpoint (T24).
#[must_use]
pub fn must_drop_from_response(route: &GatewayRoute, lower_name: &str, value: &[u8]) -> bool {
    HOP_BY_HOP_HEADERS.contains(&lower_name)
        || stripped_header_names(route).contains(lower_name)
        || reflects_a_vaulted_value(route, value)
}

fn reflects_a_vaulted_value(route: &GatewayRoute, value: &[u8]) -> bool {
    route
        .header_names()
        .filter_map(|name| route.header_value(name))
        .map(|secret| secret.expose().as_bytes())
        .filter(|secret| secret.len() >= MIN_REFLECTED_VALUE_LEN)
        .any(|secret| value.windows(secret.len()).any(|window| window == secret))
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

    const LONG_KEY: &str = "sk-test-0123456789abcdef";

    fn route_with(value: &str) -> GatewayRoute {
        GatewaySpec::parse(vec![RawRoute {
            name: "a".to_owned(),
            upstream: "https://example.com".to_owned(),
            headers: vec![("x-api-key".to_owned(), SecretValue::new(value.to_owned()))],
            allow: vec![("GET".to_owned(), "/x".to_owned())],
            rate_per_minute: 60,
        }])
        .unwrap()
        .routes()[0]
            .clone()
    }

    #[test]
    fn a_response_echoing_the_injected_header_by_name_is_dropped() {
        let route = route_with(LONG_KEY);
        assert!(must_drop_from_response(&route, "x-api-key", b"anything"));
        assert!(must_drop_from_response(&route, "connection", b"close"));
    }

    #[test]
    fn a_response_header_carrying_a_vaulted_value_is_dropped_under_any_name() {
        let route = route_with(LONG_KEY);
        let echoed = format!("received: Bearer {LONG_KEY}; ok");
        assert!(must_drop_from_response(&route, "x-echo", echoed.as_bytes()));
        assert!(!must_drop_from_response(
            &route,
            "content-type",
            b"application/json"
        ));
    }

    #[test]
    fn a_short_vaulted_value_is_matched_by_name_only() {
        let route = route_with("json");
        assert!(!must_drop_from_response(
            &route,
            "content-type",
            b"application/json"
        ));
        assert!(must_drop_from_response(&route, "x-api-key", b"json"));
    }

    #[test]
    fn matching_is_case_insensitive_through_the_lower_cased_contract() {
        // The caller is responsible for lower-casing before calling
        // `must_drop`; this test documents that `stripped_header_names`
        // itself always returns lower-case names regardless of the route's
        // own header-name casing.
        let names = stripped_header_names(&route());
        assert!(
            names
                .iter()
                .all(|name| name.chars().all(|c| !c.is_ascii_uppercase()))
        );
    }
}
