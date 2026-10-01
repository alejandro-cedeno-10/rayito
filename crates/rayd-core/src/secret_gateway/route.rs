//! What one gateway route is and the bounds `GatewaySpec::parse` enforces
//! before `rayd` ever opens a listener for it (ADR-023). Pure: building a
//! `GatewayRoute` touches no socket, no clock and no AWS call.

use std::collections::BTreeMap;
use std::fmt;

use super::vault::SecretValue;

/// 1-64 lowercase `[a-z0-9-]`, the same shape `rayito/v1/secret_gateway.proto`
/// documents for `SecretGatewayRoute.name`; it keys `SecretGatewayStatus`
/// and the SDK's `sbx.gateways[name]`, so it must stay short and stable.
pub const MAX_ROUTE_NAME_LEN: usize = 64;

/// One `SecretGatewayConfig` configures at most this many routes: each
/// opens its own TCP listener, and an unbounded count would let a single
/// `Configure` call exhaust the guest's ephemeral port range.
pub const MAX_ROUTES_PER_GATEWAY: usize = 8;

/// A route's `headers` map, bounded so a single route cannot smuggle an
/// unbounded number of vaulted values through one `Configure` call.
pub const MAX_HEADERS_PER_ROUTE: usize = 16;

/// A route's `allow` list, bounded for the same reason.
pub const MAX_ALLOW_RULES_PER_ROUTE: usize = 32;

/// `rate_per_minute: 0` in the wire message asks for this value: 10
/// requests/second, a conservative ceiling for an interactive agent
/// calling one upstream API (documented in
/// `docs/site/docs/funciones-opcionales/pasarela-de-secretos.md`, "Coste y
/// activación").
pub const DEFAULT_RATE_PER_MINUTE: u32 = 600;

/// Below this, the token bucket would starve a single well-behaved client
/// of even one request in its first minute.
pub const MIN_RATE_PER_MINUTE: u32 = 1;

/// Above this (100 requests/second sustained) a route stops meaningfully
/// rate-limiting anything a loopback client could not already do unbounded.
pub const MAX_RATE_PER_MINUTE: u32 = 6_000;

/// The literal suffix that turns an exact path into a prefix match
/// (`"/v1/*"` allows `"/v1/messages"`, `"/v1/models"`, ...). Chosen over a
/// general glob so `AllowRule::matches` stays a prefix compare, no regex
/// engine in the domain.
pub const WILDCARD_SUFFIX: &str = "/*";

/// Why a route, or the gateway spec it belongs to, was rejected before any
/// listener opened. Mirrors the closed, lowercase-snake `error_class`
/// strings `secret_gateway.proto` documents.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GatewaySpecError {
    EmptyName,
    NameTooLong,
    TooManyRoutes,
    DuplicateRouteName,
    UpstreamNotHttps,
    UpstreamHasPathOrQuery,
    TooManyHeaders,
    InvalidHeaderValue,
    TooManyAllowRules,
    EmptyAllowList,
    InvalidMethod,
    EmptyPath,
    RateOutOfRange,
}

impl GatewaySpecError {
    /// The lowercase snake string `secret_gateway.proto` says `error_class`
    /// carries; never an AWS message, host or path.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::EmptyName => "empty_name",
            Self::NameTooLong => "name_too_long",
            Self::TooManyRoutes => "too_many_routes",
            Self::DuplicateRouteName => "duplicate_route_name",
            Self::UpstreamNotHttps => "upstream_not_https",
            Self::UpstreamHasPathOrQuery => "upstream_has_path_or_query",
            Self::TooManyHeaders => "too_many_headers",
            Self::InvalidHeaderValue => "invalid_header_value",
            Self::TooManyAllowRules => "too_many_allow_rules",
            Self::EmptyAllowList => "empty_allow_list",
            Self::InvalidMethod => "invalid_method",
            Self::EmptyPath => "empty_path",
            Self::RateOutOfRange => "rate_out_of_range",
        }
    }
}

/// One `(method, path)` the route forwards; everything else gets
/// `GatewayErrorClass::NotAllowed` before a byte leaves the loopback
/// listener.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AllowRule {
    method: String,
    path: String,
}

impl AllowRule {
    fn parse(method: &str, path: &str) -> Result<Self, GatewaySpecError> {
        if method.is_empty() || !method.bytes().all(|b| b.is_ascii_uppercase()) {
            return Err(GatewaySpecError::InvalidMethod);
        }
        if path.is_empty() || !path.starts_with('/') {
            return Err(GatewaySpecError::EmptyPath);
        }
        Ok(Self {
            method: method.to_owned(),
            path: path.to_owned(),
        })
    }

    /// Exact match, or a prefix match when this rule's path ends in
    /// `WILDCARD_SUFFIX` (`"/v1/*"` matches `"/v1/messages"` but not
    /// `"/v1"` itself — the slash before the wildcard is part of the
    /// prefix it compares).
    #[must_use]
    pub fn matches(&self, method: &str, path: &str) -> bool {
        if self.method != method {
            return false;
        }
        match self.path.strip_suffix(WILDCARD_SUFFIX) {
            Some(prefix) => path.starts_with(prefix) && path[prefix.len()..].starts_with('/'),
            None => self.path == path,
        }
    }
}

/// A validated, ready-to-serve route: `GatewaySpec::parse` is the only way
/// to build one.
#[derive(Clone, PartialEq, Eq)]
pub struct GatewayRoute {
    name: String,
    upstream: String,
    headers: BTreeMap<String, SecretValue>,
    allow: Vec<AllowRule>,
    rate_per_minute: u32,
}

impl fmt::Debug for GatewayRoute {
    /// Hand-written so a derive can never start printing `headers`' values:
    /// only the header *names* and the route's other, non-secret shape.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GatewayRoute")
            .field("name", &self.name)
            .field("upstream", &self.upstream)
            .field("header_names", &self.headers.keys().collect::<Vec<_>>())
            .field("allow", &self.allow)
            .field("rate_per_minute", &self.rate_per_minute)
            .finish()
    }
}

impl GatewayRoute {
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    #[must_use]
    pub fn upstream(&self) -> &str {
        &self.upstream
    }

    #[must_use]
    pub fn rate_per_minute(&self) -> u32 {
        self.rate_per_minute
    }

    /// Lower-cased header names this route injects — also exactly the
    /// names its listener must strip from the inbound request
    /// (`header_template::stripped_header_names`).
    pub fn header_names(&self) -> impl Iterator<Item = &str> {
        self.headers.keys().map(String::as_str)
    }

    #[must_use]
    pub fn header_value(&self, name: &str) -> Option<&SecretValue> {
        self.headers.get(name)
    }

    #[must_use]
    pub fn allows(&self, method: &str, path: &str) -> bool {
        self.allow.iter().any(|rule| rule.matches(method, path))
    }
}

/// One `SecretGatewayConfig`'s routes, each already validated against the
/// bounds above. Building one makes no AWS call and opens no socket: it is
/// the input `features::secret_gateway`'s adapter turns into real
/// listeners.
#[derive(Debug, PartialEq, Eq)]
pub struct GatewaySpec {
    routes: Vec<GatewayRoute>,
}

impl GatewaySpec {
    #[must_use]
    pub fn routes(&self) -> &[GatewayRoute] {
        &self.routes
    }

    /// Consumes the spec for the adapter that starts one listener per
    /// route (`rayd::secret_gateway::GatewayRuntime::apply`), which owns
    /// each `GatewayRoute` for as long as its listener runs.
    #[must_use]
    pub fn into_routes(self) -> Vec<GatewayRoute> {
        self.routes
    }

    /// Parses and validates every route, in order, stopping at the first
    /// error (nothing in `raw_routes` is ever partially applied). Header
    /// values are given pre-resolved (`SecretValue`): this module never
    /// talks to Secrets Manager, the SDK does, before the `Configure` call
    /// it sends.
    ///
    /// # Errors
    /// The first `GatewaySpecError` any route in `raw_routes` fails on (see
    /// its variants for what each one checks).
    pub fn parse(raw_routes: Vec<RawRoute>) -> Result<Self, GatewaySpecError> {
        if raw_routes.len() > MAX_ROUTES_PER_GATEWAY {
            return Err(GatewaySpecError::TooManyRoutes);
        }
        let mut routes = Vec::with_capacity(raw_routes.len());
        let mut seen_names = std::collections::BTreeSet::new();
        for raw in raw_routes {
            if raw.name.is_empty() {
                return Err(GatewaySpecError::EmptyName);
            }
            if raw.name.len() > MAX_ROUTE_NAME_LEN {
                return Err(GatewaySpecError::NameTooLong);
            }
            if !seen_names.insert(raw.name.clone()) {
                return Err(GatewaySpecError::DuplicateRouteName);
            }
            routes.push(parse_route(raw)?);
        }
        Ok(Self { routes })
    }
}

/// One route's input, exactly the shape `grpc/configure.rs` reads off the
/// wire message before `GatewaySpec::parse` validates it.
pub struct RawRoute {
    pub name: String,
    pub upstream: String,
    pub headers: Vec<(String, SecretValue)>,
    pub allow: Vec<(String, String)>,
    pub rate_per_minute: u32,
}

fn parse_route(raw: RawRoute) -> Result<GatewayRoute, GatewaySpecError> {
    let RawRoute {
        name,
        upstream,
        headers,
        allow,
        rate_per_minute,
    } = raw;
    if !upstream.starts_with("https://") {
        return Err(GatewaySpecError::UpstreamNotHttps);
    }
    let after_scheme = &upstream["https://".len()..];
    if after_scheme.contains('/') || after_scheme.contains('?') {
        return Err(GatewaySpecError::UpstreamHasPathOrQuery);
    }
    if headers.len() > MAX_HEADERS_PER_ROUTE {
        return Err(GatewaySpecError::TooManyHeaders);
    }
    if allow.len() > MAX_ALLOW_RULES_PER_ROUTE {
        return Err(GatewaySpecError::TooManyAllowRules);
    }
    if allow.is_empty() {
        return Err(GatewaySpecError::EmptyAllowList);
    }
    let allow = allow
        .into_iter()
        .map(|(method, path)| AllowRule::parse(&method, &path))
        .collect::<Result<Vec<_>, _>>()?;
    let rate_per_minute = if rate_per_minute == 0 {
        DEFAULT_RATE_PER_MINUTE
    } else {
        rate_per_minute
    };
    if !(MIN_RATE_PER_MINUTE..=MAX_RATE_PER_MINUTE).contains(&rate_per_minute) {
        return Err(GatewaySpecError::RateOutOfRange);
    }
    Ok(GatewayRoute {
        name,
        upstream,
        headers: headers.into_iter().collect(),
        allow,
        rate_per_minute,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header(name: &str, value: &str) -> (String, SecretValue) {
        (name.to_owned(), SecretValue::new(value.to_owned()))
    }

    fn route(
        name: &str,
        upstream: &str,
        headers: Vec<(String, SecretValue)>,
        allow: Vec<(&str, &str)>,
        rate: u32,
    ) -> RawRoute {
        RawRoute {
            name: name.to_owned(),
            upstream: upstream.to_owned(),
            headers,
            allow: allow
                .into_iter()
                .map(|(m, p)| (m.to_owned(), p.to_owned()))
                .collect(),
            rate_per_minute: rate,
        }
    }

    #[test]
    fn a_well_formed_single_route_parses() {
        let spec = GatewaySpec::parse(vec![route(
            "anthropic",
            "https://api.anthropic.com",
            vec![header("x-api-key", "sk-abc")],
            vec![("POST", "/v1/messages")],
            600,
        )])
        .unwrap();
        assert_eq!(spec.routes().len(), 1);
        let route = &spec.routes()[0];
        assert_eq!(route.name(), "anthropic");
        assert_eq!(route.upstream(), "https://api.anthropic.com");
        assert_eq!(route.rate_per_minute(), 600);
        assert!(route.allows("POST", "/v1/messages"));
        assert!(!route.allows("GET", "/v1/messages"));
        assert!(!route.allows("POST", "/v1/other"));
    }

    #[test]
    fn a_zero_rate_takes_the_default() {
        let spec = GatewaySpec::parse(vec![route(
            "a",
            "https://example.com",
            vec![],
            vec![("GET", "/x")],
            0,
        )])
        .unwrap();
        assert_eq!(spec.routes()[0].rate_per_minute(), DEFAULT_RATE_PER_MINUTE);
    }

    #[test]
    fn a_wildcard_path_matches_only_its_own_subtree() {
        let spec = GatewaySpec::parse(vec![route(
            "a",
            "https://example.com",
            vec![],
            vec![("GET", "/v1/*")],
            60,
        )])
        .unwrap();
        let route = &spec.routes()[0];
        assert!(route.allows("GET", "/v1/models"));
        assert!(route.allows("GET", "/v1/models/x"));
        assert!(!route.allows("GET", "/v1"));
        assert!(!route.allows("GET", "/v2/models"));
    }

    #[test]
    fn http_upstream_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route(
                "a",
                "http://example.com",
                vec![],
                vec![("GET", "/x")],
                60
            )]),
            Err(GatewaySpecError::UpstreamNotHttps)
        );
    }

    #[test]
    fn an_upstream_with_a_path_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route(
                "a",
                "https://example.com/v1",
                vec![],
                vec![("GET", "/x")],
                60
            )]),
            Err(GatewaySpecError::UpstreamHasPathOrQuery)
        );
    }

    #[test]
    fn an_empty_allow_list_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route("a", "https://example.com", vec![], vec![], 60)]),
            Err(GatewaySpecError::EmptyAllowList)
        );
    }

    #[test]
    fn duplicate_route_names_are_rejected() {
        let make = || route("a", "https://example.com", vec![], vec![("GET", "/x")], 60);
        assert_eq!(
            GatewaySpec::parse(vec![make(), make()]),
            Err(GatewaySpecError::DuplicateRouteName)
        );
    }

    #[test]
    fn too_many_routes_is_rejected() {
        let routes = (0..=MAX_ROUTES_PER_GATEWAY)
            .map(|i| {
                route(
                    &format!("r{i}"),
                    "https://example.com",
                    vec![],
                    vec![("GET", "/x")],
                    60,
                )
            })
            .collect();
        assert_eq!(
            GatewaySpec::parse(routes),
            Err(GatewaySpecError::TooManyRoutes)
        );
    }

    #[test]
    fn rate_out_of_bounds_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route(
                "a",
                "https://example.com",
                vec![],
                vec![("GET", "/x")],
                MAX_RATE_PER_MINUTE + 1
            )]),
            Err(GatewaySpecError::RateOutOfRange)
        );
    }

    #[test]
    fn a_lowercase_method_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route(
                "a",
                "https://example.com",
                vec![],
                vec![("get", "/x")],
                60
            )]),
            Err(GatewaySpecError::InvalidMethod)
        );
    }

    #[test]
    fn error_classes_are_closed_lowercase_snake_strings() {
        assert_eq!(
            GatewaySpecError::RateOutOfRange.as_str(),
            "rate_out_of_range"
        );
        assert_eq!(
            GatewaySpecError::UpstreamNotHttps.as_str(),
            "upstream_not_https"
        );
    }
}
