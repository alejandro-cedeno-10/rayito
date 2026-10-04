//! The event model itself (M15, `m15-events-webhooks`, ADR-020). An event
//! never carries metadata or environment values — only the identity and
//! timing a forwarder/deliverer needs to route and dedupe it
//! (`AWS_API_NOTES.md` §25).

use serde::{Deserialize, Serialize};

/// What happened. `created` fires once, from the first `ConfigureSandbox`
/// call that carries a `sandbox_key` (`features::lifecycle_events`);
/// `paused`/`resumed` bracket a suspend/resume cycle; `killed` is the
/// terminal event, with `KillReason` saying how `rayd` learned about it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    Created,
    Paused,
    Resumed,
    Killed,
}

/// Why a `killed` event was raised. `Request` is a normal `/terminate`;
/// `Timeout` and `Unknown` are never produced by `rayd` itself — they are
/// synthesized by the reconciler Lambda (`infra/lambdas/events_webhooks`)
/// for a sandbox that stopped calling hooks without one, which is why they
/// exist in the wire enum at all even though this crate never constructs
/// them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KillReason {
    Request,
    Timeout,
    Unknown,
}

/// One lifecycle event, serialized to exactly this JSON shape (field order
/// does not matter to a JSON parser, but the MAC is computed over the exact
/// serialized bytes `serde_json` produces — see `mod::LIFECYCLE_EVENT_SCHEMA_VERSION`
/// and `mac::compute_mac`). `kill_reason` is only present for `Killed`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LifecycleEvent {
    pub event_id: String,
    pub sandbox_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kill_reason: Option<KillReason>,
    pub kind: EventKind,
    /// Bumped on every `resumed` (0 at `created`); lets a consumer order
    /// events within one sandbox without trusting wall-clock alone.
    pub generation: u64,
    pub occurred_at_ms: u64,
    pub image_arn: String,
    pub image_version: String,
}

impl LifecycleEvent {
    /// The exact bytes the MAC in `rayito.event.v1 <event> <mac>` covers
    /// (`mac::compute_mac`); there is no canonicalization step, so the two
    /// sides of a comparison must serialize with the same library version
    /// `rayd` ships (`serde_json`, pinned in `Cargo.toml`). Every field is
    /// a primitive, a `String` or a closed enum, none of which
    /// `serde_json` can fail to encode — `unwrap_or_default` never panics
    /// here, it only avoids `expect` (denied project-wide) for a branch
    /// this type can't actually reach.
    #[must_use]
    pub fn to_canonical_json(&self) -> Vec<u8> {
        serde_json::to_vec(self).unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn killed_carries_its_reason_created_does_not() {
        let created = LifecycleEvent {
            event_id: "evt-1".to_owned(),
            sandbox_id: "sbx-1".to_owned(),
            kill_reason: None,
            kind: EventKind::Created,
            generation: 0,
            occurred_at_ms: 1,
            image_arn: "arn:test".to_owned(),
            image_version: "1".to_owned(),
        };
        let json = String::from_utf8(created.to_canonical_json()).unwrap();
        assert!(!json.contains("kill_reason"));

        let killed = LifecycleEvent {
            kill_reason: Some(KillReason::Request),
            kind: EventKind::Killed,
            ..created
        };
        let json = String::from_utf8(killed.to_canonical_json()).unwrap();
        assert!(json.contains("\"kill_reason\":\"request\""));
    }

    #[test]
    fn serialization_never_includes_metadata_or_env_keys() {
        // Structural guarantee: the type itself has no field that could
        // carry a metadata map or an environment variable, so there is no
        // string to grep for here — the test documents the invariant by
        // asserting the full set of keys this type can ever emit.
        let event = LifecycleEvent {
            event_id: "evt-1".to_owned(),
            sandbox_id: "sbx-1".to_owned(),
            kill_reason: Some(KillReason::Unknown),
            kind: EventKind::Killed,
            generation: 3,
            occurred_at_ms: 42,
            image_arn: "arn:test".to_owned(),
            image_version: "9".to_owned(),
        };
        let value: serde_json::Value = serde_json::from_slice(&event.to_canonical_json()).unwrap();
        let mut keys: Vec<&str> = value
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            [
                "event_id",
                "generation",
                "image_arn",
                "image_version",
                "kill_reason",
                "kind",
                "occurred_at_ms",
                "sandbox_id",
            ]
        );
    }
}
