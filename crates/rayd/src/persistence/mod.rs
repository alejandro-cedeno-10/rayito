//! Persistence application layer of `rayd` (ADR-009): the manager that
//! runs `prepare_*` before the first message (so its failure is a gRPC
//! status) and the rest of the flow on a task feeding an event channel,
//! the tokio blocking runner, the part/chunk channels, and the progress
//! sampler tick. Every rule lives in `rayd_core::persistence`.

pub mod channels;
#[cfg(test)]
pub mod fakes;
pub mod manager;

pub use channels::{
    CHUNK_CHANNEL_CAPACITY, ChannelParts, ChannelSink, ChunkReader, PART_CHANNEL_CAPACITY,
    PartWriter, chunk_channel, part_channel,
};
pub use manager::{
    CheckpointItem, CheckpointStream, DEFAULT_PROGRESS_INTERVAL, PersistenceBackend,
    PersistenceManager, PersistenceSettings, PlatformPersistenceManager, RestoreItem,
    RestoreStream, TokioRunner, UnavailablePersistence, iso8601_utc, platform_persistence_manager,
};
