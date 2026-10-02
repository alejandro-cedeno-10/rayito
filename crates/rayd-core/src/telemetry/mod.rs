//! Domain of rayd-otlp (M15, ADR-021): rayd's own OTLP/HTTP metrics
//! exporter, pushing the 7 gauges the 5 s `MetricsHistory` sampler already
//! computes to `CloudWatch`'s OTLP endpoint (research §6, options B1/B1').
//! Opt-in only, through `ConfigureSandbox`'s `telemetry_export` section: no
//! `telemetry=` means no section, no gauge sampling for export and no
//! outbound connection (ADR-014 rule 4).
//!
//! `config` validates the section's desired state (`TelemetryConfig`) with
//! no I/O; `model` is the closed vocabulary of what can ever be exported
//! (`GaugeKind`, `AttrKey`, `MetricPoint`, `ResourceAttrs`) so a path, a
//! command or an env value can never reach an attribute by construction;
//! `batcher` is the bounded queue with jittered backoff that decouples the
//! sampler from the network and the two ports (`TelemetrySink`,
//! `OtlpEncoder`) that keep this crate free of `opentelemetry-proto` and
//! any HTTP client. The adapters (`otlp_codec`, `cloudwatch_otlp_sink`) and
//! the `/suspend`/`/resume` wiring live in the `rayd` crate.

pub mod batcher;
pub mod config;
pub mod model;

pub use batcher::{
    BASE_BACKOFF, BatchPlan, Batcher, BatcherStats, MAX_BACKOFF, OtlpEncoder, QUEUE_CAPACITY,
    SinkError, TelemetrySink, jittered_backoff, plan_suspend_flush,
};
pub use config::{
    MAX_INTERVAL, MIN_INTERVAL, TelemetryAuth, TelemetryConfig, TelemetryConfigError,
};
pub use model::{
    ALL_GAUGES, AttrKey, GaugeKind, MetricPoint, NameStyle, ResourceAttrs, points_from_sample,
};
