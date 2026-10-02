//! Generated gRPC contract for `rayito.v1`. The `.proto` files are the source
//! of truth; nothing in this crate is written by hand.

pub mod v1 {
    #![allow(clippy::all, clippy::pedantic, clippy::nursery)]
    tonic::include_proto!("rayito.v1");
}

/// Vendored OTLP metrics wire types (m15-rayd-otlp, ADR-021; Apache-2.0,
/// see `/NOTICE` and `vendor/opentelemetry/proto/common/v1/common.proto`'s
/// header comment). `vendor/**/*.proto` lives outside the `buf` module
/// (`buf.yaml` only covers `proto/`) so `buf lint`/`buf breaking` never see
/// it; `rayito-proto/build.rs`'s `VENDOR_GLOB` still compiles it with the
/// same `protox` + `tonic-prost-build` pipeline as `rayito.v1`. Nothing in
/// the Python or TypeScript SDKs ever touches these types: only `rayd`'s
/// `adapters::otlp_codec` encodes an `ExportMetricsServiceRequest` from
/// them before sending it to CloudWatch's OTLP endpoint.
///
/// The module nesting below mirrors each `.proto`'s dotted `package`
/// exactly (`opentelemetry.proto.common.v1` -> `opentelemetry::proto::common::v1`):
/// prost's generated code cross-references a sibling package with a
/// relative `super::super::...` path assuming that exact nesting, the same
/// shape upstream `opentelemetry-proto` generated crates use.
pub mod otlp {
    #![allow(clippy::all, clippy::pedantic, clippy::nursery)]
    pub mod opentelemetry {
        pub mod proto {
            pub mod common {
                pub mod v1 {
                    tonic::include_proto!("opentelemetry.proto.common.v1");
                }
            }
            pub mod resource {
                pub mod v1 {
                    tonic::include_proto!("opentelemetry.proto.resource.v1");
                }
            }
            pub mod metrics {
                pub mod v1 {
                    tonic::include_proto!("opentelemetry.proto.metrics.v1");
                }
            }
            pub mod collector {
                pub mod metrics {
                    pub mod v1 {
                        tonic::include_proto!("opentelemetry.proto.collector.metrics.v1");
                    }
                }
            }
        }
    }

    pub use opentelemetry::proto::collector::metrics::v1 as collector_metrics;
    pub use opentelemetry::proto::common::v1 as common;
    pub use opentelemetry::proto::metrics::v1 as metrics;
    pub use opentelemetry::proto::resource::v1 as resource;
}

/// Serialized `FileDescriptorSet` of every `rayito.v1` proto, for gRPC
/// reflection in debug builds.
#[cfg(feature = "reflection")]
pub const FILE_DESCRIPTOR_SET: &[u8] = tonic::include_file_descriptor_set!("rayito_descriptor");
