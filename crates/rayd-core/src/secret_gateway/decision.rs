//! `GatewayDecision`: whether one inbound request against one
//! `GatewayRoute` may be forwarded, combining the method/path allowlist
//! with a token-bucket rate limit (ADR-023). Pure: it reads a clock value
//! the caller already sampled and returns the bucket's next state rather
//! than mutating anything, so the adapter (`rayd::secret_gateway::listener`)
//! owns the only `Mutex` and the only real clock read.

use super::route::GatewayRoute;

/// Why a request was refused, closed and lowercase-snake
/// (`secret_gateway.proto`'s `error_class`); never an upstream message,
/// host or path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GatewayErrorClass {
    NotAllowed,
    RateLimited,
    UpstreamUnreachable,
    UpstreamTimeout,
    UpstreamError,
}

impl GatewayErrorClass {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NotAllowed => "not_allowed",
            Self::RateLimited => "rate_limited",
            Self::UpstreamUnreachable => "upstream_unreachable",
            Self::UpstreamTimeout => "upstream_timeout",
            Self::UpstreamError => "upstream_error",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Allow,
    Deny(GatewayErrorClass),
}

/// A token bucket sized in whole requests, refilled continuously from
/// `rate_per_minute`. `tokens` is kept as whole milli-tokens
/// (`TOKEN_SCALE`) so the state is an integer and two evaluations of the
/// same inputs are bit-for-bit identical — no floating-point drift between
/// a unit test and the running gateway.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenBucket {
    milli_tokens: u64,
    last_refill_ms: u64,
}

/// `milli_tokens` units per whole token, so refilling a fraction of a
/// token over a short interval still advances the bucket instead of
/// truncating to zero every tick.
const TOKEN_SCALE: u64 = 1_000;
const MS_PER_MINUTE: u64 = 60_000;

impl TokenBucket {
    /// A full bucket at `now_ms`: the first request after `Configure` is
    /// never rate-limited by an empty starting bucket.
    #[must_use]
    pub fn full(route: &GatewayRoute, now_ms: u64) -> Self {
        Self {
            milli_tokens: u64::from(route.rate_per_minute()) * TOKEN_SCALE,
            last_refill_ms: now_ms,
        }
    }

    fn refilled(self, route: &GatewayRoute, now_ms: u64) -> Self {
        let elapsed_ms = now_ms.saturating_sub(self.last_refill_ms);
        let capacity = u64::from(route.rate_per_minute()) * TOKEN_SCALE;
        let added = elapsed_ms.saturating_mul(capacity) / MS_PER_MINUTE;
        Self {
            milli_tokens: (self.milli_tokens + added).min(capacity),
            last_refill_ms: now_ms,
        }
    }

    /// Refills to `now_ms`, then spends one whole token if there is one.
    /// Returns the bucket's next state either way: the caller stores it
    /// back regardless of the decision, so a denied request still advances
    /// the refill clock.
    #[must_use]
    fn try_consume(self, route: &GatewayRoute, now_ms: u64) -> (Self, bool) {
        let refilled = self.refilled(route, now_ms);
        if refilled.milli_tokens >= TOKEN_SCALE {
            (
                Self {
                    milli_tokens: refilled.milli_tokens - TOKEN_SCALE,
                    ..refilled
                },
                true,
            )
        } else {
            (refilled, false)
        }
    }
}

/// `route.allows()` first (free, no bucket mutation for a request that was
/// never going anywhere), then the rate limit. Returns the bucket's next
/// state so the adapter can store it back under its own lock.
#[must_use]
pub fn evaluate(
    route: &GatewayRoute,
    method: &str,
    path: &str,
    bucket: TokenBucket,
    now_ms: u64,
) -> (TokenBucket, Decision) {
    if !route.allows(method, path) {
        return (bucket, Decision::Deny(GatewayErrorClass::NotAllowed));
    }
    let (next, allowed) = bucket.try_consume(route, now_ms);
    if allowed {
        (next, Decision::Allow)
    } else {
        (next, Decision::Deny(GatewayErrorClass::RateLimited))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret_gateway::vault::SecretValue;

    fn route(rate_per_minute: u32) -> GatewayRoute {
        use crate::secret_gateway::route::{GatewaySpec, RawRoute};
        let spec = GatewaySpec::parse(vec![RawRoute {
            name: "a".to_owned(),
            upstream: "https://example.com".to_owned(),
            headers: vec![("x-api-key".to_owned(), SecretValue::new("k".to_owned()))],
            allow: vec![("POST".to_owned(), "/v1/messages".to_owned())],
            rate_per_minute,
        }])
        .unwrap();
        spec.routes()[0].clone()
    }

    #[test]
    fn a_disallowed_method_is_denied_without_touching_the_bucket() {
        let route = route(60);
        let bucket = TokenBucket::full(&route, 0);
        let (next, decision) = evaluate(&route, "GET", "/v1/messages", bucket, 0);
        assert_eq!(decision, Decision::Deny(GatewayErrorClass::NotAllowed));
        assert_eq!(next, bucket);
    }

    #[test]
    fn a_full_bucket_allows_the_first_request() {
        let route = route(60);
        let bucket = TokenBucket::full(&route, 0);
        let (_, decision) = evaluate(&route, "POST", "/v1/messages", bucket, 0);
        assert_eq!(decision, Decision::Allow);
    }

    #[test]
    fn a_bucket_of_one_denies_the_second_immediate_request() {
        let route = route(60); // 1 token/second
        let bucket = TokenBucket {
            milli_tokens: TOKEN_SCALE,
            last_refill_ms: 0,
        };
        let (next, first) = evaluate(&route, "POST", "/v1/messages", bucket, 0);
        assert_eq!(first, Decision::Allow);
        let (_, second) = evaluate(&route, "POST", "/v1/messages", next, 0);
        assert_eq!(second, Decision::Deny(GatewayErrorClass::RateLimited));
    }

    #[test]
    fn the_bucket_refills_over_time_and_allows_again() {
        let route = route(60); // 1 token/second
        let bucket = TokenBucket {
            milli_tokens: 0,
            last_refill_ms: 0,
        };
        let (_, denied) = evaluate(&route, "POST", "/v1/messages", bucket, 500);
        assert_eq!(denied, Decision::Deny(GatewayErrorClass::RateLimited));
        let (_, allowed) = evaluate(&route, "POST", "/v1/messages", bucket, 1_000);
        assert_eq!(allowed, Decision::Allow);
    }

    #[test]
    fn refilling_never_exceeds_capacity() {
        let route = route(60);
        let bucket = TokenBucket::full(&route, 0);
        let refilled = bucket.refilled(&route, 1_000_000);
        assert_eq!(
            refilled.milli_tokens,
            u64::from(route.rate_per_minute()) * TOKEN_SCALE
        );
    }

    #[test]
    fn error_classes_are_closed_lowercase_snake_strings() {
        assert_eq!(GatewayErrorClass::NotAllowed.as_str(), "not_allowed");
        assert_eq!(GatewayErrorClass::RateLimited.as_str(), "rate_limited");
    }
}
