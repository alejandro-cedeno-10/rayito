//! Presigned S3 transfers (ADR-010), the runtime half: the manager that
//! accepts imports and exports and runs their tasks over the `SignedHttp`
//! port, the read-after-upload barrier every service consults, and the
//! snapshot stream behind `WatchTransfer`. Every rule lives in
//! `rayd_core::transfer`; everything that needs tokio lives here.

mod barrier;
mod export;
mod import;
mod manager;
mod watch;

pub use barrier::TransferBarrier;
pub use manager::{
    GATE_POLL_INTERVAL, TransferBackend, TransferManager, TransferSettings, UnavailableTransfers,
};
pub use watch::{TransferWatch, watch_snapshots};
