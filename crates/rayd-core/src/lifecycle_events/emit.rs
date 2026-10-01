//! The wire format of one event line and the port its adapter implements
//! (M15, `m15-events-webhooks`). `rayd::adapters::stdout_event_sink` is the
//! only implementation; this module stays free of any actual I/O so the
//! line format itself is unit-tested without touching stdout.

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;

/// The literal token the `CloudWatch` Logs subscription filter
/// (`infra/events-webhooks.yaml`, `AWS_API_NOTES.md` §25) and the forwarder
/// Lambda both match on. Versioned so a future wire change can coexist with
/// this one during a rollout.
pub const LIFECYCLE_EVENT_TOKEN: &str = "rayito.event.v1";

/// `rayito.event.v1 <b64url(event_json)> <b64url(mac)>`, one line, no
/// trailing fields. Base64url without padding, matching every other
/// unpadded token `rayd` already emits (`wire_tokens.rs`).
#[must_use]
pub fn format_event_line(event_json: &[u8], mac: &[u8]) -> String {
    format!(
        "{LIFECYCLE_EVENT_TOKEN} {} {}",
        URL_SAFE_NO_PAD.encode(event_json),
        URL_SAFE_NO_PAD.encode(mac),
    )
}

/// Where a formatted line goes once it is built. The only real adapter is
/// `rayd::adapters::stdout_event_sink::StdoutEventSink`, which queues it
/// (non-blocking, bounded: `EVENT_QUEUE_CAPACITY`) for a background task to
/// write as one atomic line to the process's own stdout (picked up by the
/// image's `CloudWatch` Logs agent); tests use a `Vec<String>`-backed fake
/// instead. Returns `false` when the line was dropped instead of queued
/// (queue full), which the caller folds into `LifecycleEventsStatus.dropped`
/// — never an error, per §7.4 ("never turns a hook into an error").
pub trait LifecycleEventSink: Send + Sync {
    fn emit_line(&self, line: &str) -> bool;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_as_three_space_separated_tokens() {
        let line = format_event_line(b"{}", &[0u8; 32]);
        let parts: Vec<&str> = line.split(' ').collect();
        assert_eq!(parts.len(), 3);
        assert_eq!(parts[0], LIFECYCLE_EVENT_TOKEN);
    }

    #[test]
    fn matches_the_shared_vector_files_expected_line() {
        let raw = include_str!("../../../../testdata/lifecycle-events/mac-vectors.json");
        let parsed: serde_json::Value = serde_json::from_str(raw).unwrap();
        for case in parsed["event_mac"].as_array().unwrap() {
            let payload = case["event_json"].as_str().unwrap().as_bytes();
            let mac_hex = case["expected_mac_hex"].as_str().unwrap();
            let mac = hex_decode(mac_hex);
            let expected_line = case["expected_line"].as_str().unwrap();
            assert_eq!(format_event_line(payload, &mac), expected_line);
        }
    }

    #[test]
    fn encoding_never_emits_padding_characters() {
        let line = format_event_line(b"x", &[1u8; 32]);
        assert!(!line.contains('='));
    }

    fn hex_decode(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }
}
