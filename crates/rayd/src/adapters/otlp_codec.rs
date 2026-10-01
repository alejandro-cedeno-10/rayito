//! `rayd_core::telemetry::OtlpEncoder` over the vendored OTLP proto types
//! (`rayito_proto::otlp`, m15-rayd-otlp ADR-021): builds one
//! `ExportMetricsServiceRequest` carrying one `ResourceMetrics` (this
//! sandbox) with one `ScopeMetrics` (this `rayd` build) and serializes it
//! with `prost`. Compression (`Content-Encoding: gzip`) is the sink's job,
//! not this one: encoding is pure protobuf shaping, nothing about the
//! transport.

use std::time::SystemTime;

use prost::Message;
use rayd_core::telemetry::{ALL_GAUGES, AttrKey, MetricPoint, NameStyle, OtlpEncoder, ResourceAttrs};
use rayito_proto::otlp::collector_metrics::ExportMetricsServiceRequest;
use rayito_proto::otlp::common::{AnyValue, InstrumentationScope, KeyValue, any_value};
use rayito_proto::otlp::metrics::{Gauge, Metric, NumberDataPoint, ResourceMetrics, ScopeMetrics, metric, number_data_point};
use rayito_proto::otlp::resource::Resource;

/// `InstrumentationScope.name`: identifies the exporter, not the gauges
/// (those are `Metric.name`, `GaugeKind::metric_name`).
pub const SCOPE_NAME: &str = "rayd";
/// `InstrumentationScope.version`: `rayd`'s own crate version, so a batch
/// is traceable to the agent build that sent it without adding a resource
/// attribute for it.
pub const SCOPE_VERSION: &str = env!("CARGO_PKG_VERSION");
/// The fixed OTLP resource attribute key for the service name (not one of
/// `rayd`'s own 4 `AttrKey`s: OTLP convention, not a rayito-specific key).
const SERVICE_NAME_KEY: &str = "service.name";

#[derive(Debug, Clone, Copy, Default)]
pub struct ProstOtlpEncoder;

impl OtlpEncoder for ProstOtlpEncoder {
    fn encode(
        &self,
        resource: &ResourceAttrs,
        service_name: &str,
        names: NameStyle,
        points: &[MetricPoint],
    ) -> Vec<u8> {
        let request = ExportMetricsServiceRequest {
            resource_metrics: vec![ResourceMetrics {
                resource: Some(Resource {
                    attributes: resource_attributes(resource, service_name),
                }),
                scope_metrics: vec![ScopeMetrics {
                    scope: Some(InstrumentationScope {
                        name: SCOPE_NAME.to_owned(),
                        version: SCOPE_VERSION.to_owned(),
                    }),
                    metrics: group_into_metrics(points, names),
                }],
            }],
        };
        let mut buffer = Vec::with_capacity(request.encoded_len());
        // `Vec<u8>` as `prost::bytes::BufMut` never runs out of remaining
        // capacity (it grows), so `encode` cannot actually fail here; the
        // crate's lints deny `unwrap`/`expect`/`panic`, so the `Result` is
        // deliberately discarded rather than asserted on.
        let _ = request.encode(&mut buffer);
        buffer
    }
}

fn resource_attributes(resource: &ResourceAttrs, service_name: &str) -> Vec<KeyValue> {
    vec![
        string_attr(SERVICE_NAME_KEY, service_name),
        string_attr(AttrKey::SandboxId.name(), &resource.sandbox_id),
        string_attr(AttrKey::ImageArn.name(), &resource.image_arn),
        string_attr(AttrKey::ImageVersion.name(), &resource.image_version),
        int_attr(
            AttrKey::ImageMemoryMib.name(),
            i64::from(resource.image_memory_mib),
        ),
    ]
}

fn string_attr(key: &str, value: &str) -> KeyValue {
    KeyValue {
        key: key.to_owned(),
        value: Some(AnyValue {
            value: Some(any_value::Value::StringValue(value.to_owned())),
        }),
    }
}

fn int_attr(key: &str, value: i64) -> KeyValue {
    KeyValue {
        key: key.to_owned(),
        value: Some(AnyValue {
            value: Some(any_value::Value::IntValue(value)),
        }),
    }
}

/// One `Metric` per gauge kind present in `points`, in `ALL_GAUGES` order
/// (a diff of two encoded batches' metric lists is stable); a kind with no
/// sample in this batch contributes no `Metric` at all, never an empty one.
fn group_into_metrics(points: &[MetricPoint], names: NameStyle) -> Vec<Metric> {
    ALL_GAUGES
        .into_iter()
        .filter_map(|kind| {
            let data_points: Vec<NumberDataPoint> = points
                .iter()
                .filter(|point| point.kind == kind)
                .map(|point| NumberDataPoint {
                    attributes: Vec::new(),
                    time_unix_nano: unix_nanos(point.timestamp),
                    value: Some(number_data_point::Value::AsDouble(point.value)),
                })
                .collect();
            if data_points.is_empty() {
                return None;
            }
            Some(Metric {
                name: kind.metric_name(names),
                description: String::new(),
                unit: String::new(),
                data: Some(metric::Data::Gauge(Gauge { data_points })),
            })
        })
        .collect()
}

/// Nanoseconds since the Unix epoch, saturating rather than panicking on a
/// clock set before 1970 or a duration too large for `u64` nanoseconds
/// (the latter is centuries away; the former is only a misconfigured
/// guest clock, never a reason to crash the exporter).
fn unix_nanos(timestamp: SystemTime) -> u64 {
    timestamp
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |elapsed| u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::telemetry::GaugeKind;

    fn resource() -> ResourceAttrs {
        ResourceAttrs {
            sandbox_id: "mvm-test".to_owned(),
            image_arn: "arn:aws:lambda:us-east-1:111111111111:microvm-image/rayito-base".to_owned(),
            image_version: "3".to_owned(),
            image_memory_mib: 2048,
        }
    }

    fn point(kind: GaugeKind, value: f64) -> MetricPoint {
        MetricPoint {
            kind,
            value,
            timestamp: SystemTime::UNIX_EPOCH,
        }
    }

    #[test]
    fn encoding_an_empty_batch_still_carries_the_resource() {
        let bytes = ProstOtlpEncoder.encode(&resource(), "agente", NameStyle::Rayito, &[]);
        let decoded = ExportMetricsServiceRequest::decode(bytes.as_slice()).unwrap_or_default();
        let resource_metrics = &decoded.resource_metrics[0];
        assert!(resource_metrics.resource.is_some());
        assert!(resource_metrics.scope_metrics[0].metrics.is_empty());
    }

    #[test]
    fn each_present_gauge_kind_becomes_its_own_metric() {
        let points = [
            point(GaugeKind::CpuUsedPct, 12.5),
            point(GaugeKind::MemoryUsedBytes, 1024.0),
        ];
        let bytes = ProstOtlpEncoder.encode(&resource(), "agente", NameStyle::Rayito, &points);
        let decoded = ExportMetricsServiceRequest::decode(bytes.as_slice()).unwrap_or_default();
        let metrics = &decoded.resource_metrics[0].scope_metrics[0].metrics;
        assert_eq!(metrics.len(), 2);
        assert_eq!(metrics[0].name, "rayito.sandbox.cpu.used_pct");
        assert_eq!(metrics[1].name, "rayito.sandbox.memory.used_bytes");
    }

    #[test]
    fn the_e2b_name_style_renames_every_metric() {
        let points = [point(GaugeKind::CpuCount, 4.0)];
        let bytes = ProstOtlpEncoder.encode(&resource(), "agente", NameStyle::E2b, &points);
        let decoded = ExportMetricsServiceRequest::decode(bytes.as_slice()).unwrap_or_default();
        let metric = &decoded.resource_metrics[0].scope_metrics[0].metrics[0];
        assert_eq!(metric.name, "e2b.sandbox.cpu.count");
    }

    #[test]
    fn resource_attributes_carry_exactly_the_4_closed_keys_plus_service_name() {
        let bytes = ProstOtlpEncoder.encode(&resource(), "agente", NameStyle::Rayito, &[]);
        let decoded = ExportMetricsServiceRequest::decode(bytes.as_slice()).unwrap_or_default();
        let attrs = &decoded.resource_metrics[0].resource.as_ref().unwrap().attributes;
        let keys: Vec<&str> = attrs.iter().map(|kv| kv.key.as_str()).collect();
        assert_eq!(
            keys,
            vec![
                "service.name",
                "sandbox_id",
                "image_arn",
                "image_version",
                "image_memory_mib",
            ]
        );
    }

    #[test]
    fn a_metric_point_carries_its_value_as_a_gauge_double() {
        let bytes =
            ProstOtlpEncoder.encode(&resource(), "agente", NameStyle::Rayito, &[point(GaugeKind::CpuUsedPct, 42.0)]);
        let decoded = ExportMetricsServiceRequest::decode(bytes.as_slice()).unwrap_or_default();
        let metric = &decoded.resource_metrics[0].scope_metrics[0].metrics[0];
        let Some(metric::Data::Gauge(gauge)) = &metric.data else {
            panic!("expected a gauge");
        };
        assert_eq!(
            gauge.data_points[0].value,
            Some(number_data_point::Value::AsDouble(42.0))
        );
    }
}
