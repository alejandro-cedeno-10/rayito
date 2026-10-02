//! Pure domain of `m15-events-webhooks` (ADR-020): the event model
//! (`event`), its MAC (`mac`) and the stdout wire format plus the port its
//! adapter implements (`emit`). No boto3, grpc, tokio or nix here — the
//! queue, the key's `Zeroizing` storage and the actual stdout write all
//! live in `rayd::features::lifecycle_events` and
//! `rayd::adapters::stdout_event_sink`.

pub mod emit;
pub mod event;
pub mod mac;

use std::time::Duration;

pub use emit::{LIFECYCLE_EVENT_TOKEN, LifecycleEventSink, SinkFlush, format_event_line};
pub use event::{EventKind, KillReason, LifecycleEvent};
pub use mac::{MAC_LEN, compute_mac};

/// `LifecycleEventsParticipant`'s `/suspend` share
/// (`rayd::lifecycle::participants::LifecycleParticipant::demand`): the
/// flush is one bounded channel drain plus a handful of non-blocking stdout
/// writes, never a network call, so §7.4's "at most 1 s" ceiling is already
/// generous.
pub const SUSPEND_SHARE_MAX: Duration = Duration::from_secs(1);

/// `LifecycleEventsParticipant`'s name in `SuspendShares` — must match the
/// `ParticipantDemand.name` the feature's slot registers.
pub const PARTICIPANT_NAME: &str = "lifecycle_events";

/// How many formatted lines the sink's channel holds before a new event is
/// dropped rather than blocking the caller (§7.4: "non-blocking with a
/// bounded queue"). Sized for a burst of every hook firing back to back
/// (created, paused, resumed, killed) plus headroom, not for sustained
/// high-rate emission — `rayd` emits at most one event per lifecycle
/// transition, never per request.
pub const EVENT_QUEUE_CAPACITY: usize = 64;

/// Domain separator the SDK's own key derivation uses
/// (`_lifecycle_events/_keys.py`, `configure/keys.ts`):
/// `k_sbx = HMAC-SHA256(stack_key, DOMAIN_SEPARATOR + sandbox_id)`. `rayd`
/// never computes this derivation itself (it only ever sees `k_sbx`), but
/// the constant is mirrored here so the Rust test vectors
/// (`testdata/lifecycle-events/mac-vectors.json`) document the exact string
/// the wire format is built on.
pub const DOMAIN_SEPARATOR: &str = "rayito.events.v1|";
