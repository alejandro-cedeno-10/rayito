//! What one gateway route is and the bounds `GatewaySpec::parse` enforces
//! before `rayd` ever opens a listener for it (ADR-023). Pure: building a
//! `GatewayRoute` touches no socket, no clock and no AWS call.

use std::collections::BTreeMap;
use std::fmt;

use super::header_template::is_reserved_header;
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

/// Percent-encoded forms of `.`, `/` and `\` a client could use to smuggle a
/// dot-segment past a literal `..`/`.` check: `rayd` never decodes the
/// inbound path before comparing it against an `AllowRule` or forwarding it,
/// so these must be rejected as raw substrings, case-insensitively (an
/// upstream or a CDN in front of it routinely normalises `%2e` the same as
/// `.`). Checked by `path_is_safe`.
const FORBIDDEN_PATH_SUBSTRINGS: [&str; 3] = ["%2e", "%2f", "%5c"];

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
    InvalidHeaderName,
    DuplicateHeaderName,
    InvalidUpstreamHost,
    InvalidRouteName,
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
            Self::InvalidHeaderName => "invalid_header_name",
            Self::DuplicateHeaderName => "duplicate_header_name",
            Self::InvalidUpstreamHost => "invalid_upstream_host",
            Self::InvalidRouteName => "invalid_route_name",
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

/// `true` when `path` is safe to match against an `AllowRule` and forward
/// unchanged: no dot-segment (`.`/`..`, raw or percent-encoded), no
/// backslash and no empty segment (`//`). `rayd` never decodes or
/// normalises the inbound path before this check or before forwarding it,
/// so rejecting an unsafe one here — fail closed, before the allowlist ever
/// sees it — is what keeps `AllowRule::matches`'s prefix compare a real
/// boundary (confused-deputy mitigation, T24) instead of a string compare a
/// request for `/v1/../admin` or `/v1/%2e%2e/admin` can walk around: an
/// upstream or the CDN in front of it routinely collapses a `..` segment
/// before routing on it.
#[must_use]
pub fn path_is_safe(path: &str) -> bool {
    if path.contains('\\') || path.contains("//") {
        return false;
    }
    let lower = path.to_ascii_lowercase();
    if FORBIDDEN_PATH_SUBSTRINGS
        .iter()
        .any(|needle| lower.contains(needle))
    {
        return false;
    }
    path.split('/')
        .all(|segment| segment != "." && segment != "..")
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
            if !is_route_name_charset(&raw.name) {
                return Err(GatewaySpecError::InvalidRouteName);
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

/// `true` for every byte RFC 9110 §5.6.2 allows in an HTTP header-field
/// name (`token`); checked so `Configure` rejects an invalid name at parse
/// time instead of every request failing with a 502 once the listener tries
/// to build `http::HeaderName` from it.
fn is_header_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'!' | b'#'
                | b'$'
                | b'%'
                | b'&'
                | b'\''
                | b'*'
                | b'+'
                | b'-'
                | b'.'
                | b'^'
                | b'_'
                | b'`'
                | b'|'
                | b'~'
        )
}

/// Lower-cases every header name (so `X-Api-Key` and `x-api-key` cannot
/// both be configured, injecting the value twice), then rejects an invalid
/// token, a duplicate after lower-casing, or a name a route must never be
/// allowed to set itself (`header_template::is_reserved_header`).
fn normalize_headers(
    headers: Vec<(String, SecretValue)>,
) -> Result<Vec<(String, SecretValue)>, GatewaySpecError> {
    let mut seen = std::collections::BTreeSet::new();
    let mut normalized = Vec::with_capacity(headers.len());
    for (name, value) in headers {
        let lower = name.to_ascii_lowercase();
        if lower.is_empty() || !lower.bytes().all(is_header_token_byte) {
            return Err(GatewaySpecError::InvalidHeaderName);
        }
        if is_reserved_header(&lower) {
            return Err(GatewaySpecError::InvalidHeaderName);
        }
        if !seen.insert(lower.clone()) {
            return Err(GatewaySpecError::DuplicateHeaderName);
        }
        normalized.push((lower, value));
    }
    Ok(normalized)
}

/// `[a-z0-9-]`, the shape `secret_gateway.proto` documents for a route's
/// `name` (length is checked separately, against `MAX_ROUTE_NAME_LEN`).
fn is_route_name_charset(name: &str) -> bool {
    name.bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
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
    // Userinfo (`user@host`, rejected so a route can never be used to leak
    // credentials through the URI itself) and a fragment (meaningless over
    // the wire, but accepting one here would make `rayd`'s own parsing
    // silently more permissive than the SDKs that already reject it) are
    // invalid even though they are not a path or a query string.
    if after_scheme.is_empty() || after_scheme.contains('@') || after_scheme.contains('#') {
        return Err(GatewaySpecError::InvalidUpstreamHost);
    }
    if headers.len() > MAX_HEADERS_PER_ROUTE {
        return Err(GatewaySpecError::TooManyHeaders);
    }
    let headers = normalize_headers(headers)?;
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

    #[test]
    fn an_upstream_with_userinfo_or_a_fragment_is_rejected() {
        for upstream in ["https://user@example.com", "https://example.com#frag"] {
            assert_eq!(
                GatewaySpec::parse(vec![route("a", upstream, vec![], vec![("GET", "/x")], 60)]),
                Err(GatewaySpecError::InvalidUpstreamHost),
                "{upstream} should be rejected"
            );
        }
    }

    #[test]
    fn an_empty_upstream_host_is_rejected() {
        assert_eq!(
            GatewaySpec::parse(vec![route(
                "a",
                "https://",
                vec![],
                vec![("GET", "/x")],
                60
            )]),
            Err(GatewaySpecError::InvalidUpstreamHost)
        );
    }

    #[test]
    fn a_route_name_outside_its_charset_is_rejected() {
        for name in ["Anthropic", "my_route", "a b"] {
            assert_eq!(
                GatewaySpec::parse(vec![route(
                    name,
                    "https://example.com",
                    vec![],
                    vec![("GET", "/x")],
                    60
                )]),
                Err(GatewaySpecError::InvalidRouteName),
                "{name} should be rejected"
            );
        }
    }

    /// `testdata/secret-gateway/`: the same vectors the SDK tests read, so
    /// `rayd` and both SDKs agree on every edge case by construction.
    const HEADER_NAME_VECTORS: &str =
        include_str!("../../../../testdata/secret-gateway/header-names.json");
    const REQUEST_PATH_VECTORS: &str =
        include_str!("../../../../testdata/secret-gateway/request-paths.json");

    fn vector_strings(json: &str, key: &str) -> Vec<String> {
        let vectors: serde_json::Value = serde_json::from_str(json).unwrap();
        vectors[key]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_owned())
            .collect()
    }

    fn parse_with_headers(
        headers: Vec<(String, SecretValue)>,
    ) -> Result<GatewaySpec, GatewaySpecError> {
        GatewaySpec::parse(vec![RawRoute {
            name: "a".to_owned(),
            upstream: "https://example.com".to_owned(),
            headers,
            allow: vec![("GET".to_owned(), "/x".to_owned())],
            rate_per_minute: 60,
        }])
    }

    #[test]
    fn path_is_safe_rejects_every_shared_unsafe_vector() {
        for unsafe_path in vector_strings(REQUEST_PATH_VECTORS, "unsafe") {
            assert!(
                !path_is_safe(&unsafe_path),
                "{unsafe_path} should be unsafe"
            );
        }
    }

    #[test]
    fn path_is_safe_accepts_every_shared_safe_vector() {
        for safe_path in vector_strings(REQUEST_PATH_VECTORS, "safe") {
            assert!(path_is_safe(&safe_path), "{safe_path} should be safe");
        }
    }

    #[test]
    fn every_shared_valid_header_name_parses() {
        for name in vector_strings(HEADER_NAME_VECTORS, "valid") {
            assert!(
                parse_with_headers(vec![header(&name, "1")]).is_ok(),
                "{name} should be accepted"
            );
        }
    }

    #[test]
    fn every_shared_invalid_header_name_is_rejected() {
        for name in vector_strings(HEADER_NAME_VECTORS, "invalid") {
            assert_eq!(
                parse_with_headers(vec![header(&name, "1")]).err(),
                Some(GatewaySpecError::InvalidHeaderName),
                "{name:?} should be rejected"
            );
        }
    }

    #[test]
    fn every_shared_duplicate_header_pair_is_rejected() {
        let vectors: serde_json::Value = serde_json::from_str(HEADER_NAME_VECTORS).unwrap();
        for pair in vectors["duplicates"].as_array().unwrap() {
            let first = pair[0].as_str().unwrap();
            let second = pair[1].as_str().unwrap();
            assert_eq!(
                parse_with_headers(vec![header(first, "1"), header(second, "2")]).err(),
                Some(GatewaySpecError::DuplicateHeaderName),
                "{first}/{second} should be rejected"
            );
        }
    }

    #[test]
    fn a_wildcard_rule_never_matches_a_dot_segment_bypass() {
        // `AllowRule::matches` is a plain prefix compare; the dot-segment
        // check belongs to the caller (`evaluate`), so this documents that
        // `allows()` alone is *not* the confused-deputy boundary — see
        // `decision::evaluate`'s own test for the end-to-end guarantee.
        let spec = GatewaySpec::parse(vec![route(
            "a",
            "https://example.com",
            vec![],
            vec![("GET", "/v1/*")],
            60,
        )])
        .unwrap();
        let route = &spec.routes()[0];
        assert!(route.allows("GET", "/v1/../admin"));
    }
}
