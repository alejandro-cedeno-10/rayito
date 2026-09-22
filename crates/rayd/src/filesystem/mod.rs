//! Filesystem application layer of `rayd`: the manager that runs every
//! RPC's blocking work on the pool under the requesting identity, the read
//! pipeline, the multi-file write driver and the watch pump. Everything
//! that needs tokio lives here; every rule lives in `rayd_core::filesystem`.

pub mod manager;
pub mod read;
pub mod watch;
pub mod write;

pub use manager::{
    DEFAULT_MAX_WATCHES, DEFAULT_WATCH_QUEUE_CAPACITY, FilesystemManager, FilesystemPlatform,
    FilesystemSettings, WRITE_ERROR_DRAIN_BYTES, WRITE_ERROR_DRAIN_TIMEOUT,
    platform_filesystem_manager,
};
pub use read::{READ_PIPELINE_DEPTH, ReadStream};
pub use watch::{WATCH_OUTPUT_CAPACITY, WatchItem, WatchStream};
pub use write::{WriteFailure, WriteMessageWithChunk};
