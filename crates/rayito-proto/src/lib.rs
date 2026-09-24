//! Generated gRPC contract for `rayito.v1`. The `.proto` files are the source
//! of truth; nothing in this crate is written by hand.

pub mod v1 {
    #![allow(clippy::all, clippy::pedantic, clippy::nursery)]
    tonic::include_proto!("rayito.v1");
}

/// Serialized `FileDescriptorSet` of every `rayito.v1` proto, for gRPC
/// reflection in debug builds.
#[cfg(feature = "reflection")]
pub const FILE_DESCRIPTOR_SET: &[u8] = tonic::include_file_descriptor_set!("rayito_descriptor");
