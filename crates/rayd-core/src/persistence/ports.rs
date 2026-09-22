//! What persistence needs from the outside (design D7): an archiver that
//! walks and unpacks the user's home on a blocking thread under the user's
//! filesystem identity, an object store reached with the execution role,
//! and the two channel ends that couple them (`PartSource` on the way up,
//! `ArchiveSink`/`Read` on the way down). Native `async fn` in traits,
//! wired as generics in `rayd`, never `dyn`; no executor type crosses.

use std::future::Future;
use std::io::{Read, Write};
use std::sync::Arc;

use bytes::Bytes;
use thiserror::Error;

use super::keys::BucketName;
use super::plan::ArchivePlan;
use super::progress::Counters;

/// Adapter failures on the home side; errno names at most, never a path.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ArchiveError {
    /// The sink or source closed under the archiver: the client went away
    /// or `/suspend` cut the stream.
    #[error("cancelled")]
    Cancelled,
    #[error("no space left on device")]
    DiskFull,
    /// A restore entry the rules of D4 refuse (absolute path, `..`, a
    /// parent that escapes the home).
    #[error("entry refused")]
    EntryRefused,
    #[error("permission denied")]
    PermissionDenied,
    #[error("archiving is not supported on this platform")]
    Unsupported,
    #[error("{operation} failed: {errno}")]
    Io {
        operation: &'static str,
        errno: String,
    },
}

/// What the pre-walk and the archive report.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ArchiveSummary {
    pub files: u64,
    pub bytes_read: u64,
    pub archive_bytes: u64,
    pub sha256: String,
    pub skipped: u64,
}

/// What the unpack reports.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExtractSummary {
    pub files: u64,
    pub bytes_written: u64,
    pub archive_bytes: u64,
    pub sha256: String,
    pub skipped: u64,
}

/// Where the compressed archive goes, part by part; `finish` marks a
/// complete archive (a sink dropped without it means the producer died).
pub trait ArchiveSink: Write + Send {
    fn finish(self: Box<Self>) -> std::io::Result<()>;
}

/// Walks and unpacks the user's home. Every call runs on the caller's
/// blocking thread under the user's filesystem identity, so an entry the
/// user cannot read is skipped, never read as root.
pub trait HomeArchiver: Send + Sync {
    /// The pre-walk: counts what `archive` would include right now.
    fn count(&self, plan: &ArchivePlan) -> Result<ArchiveSummary, ArchiveError>;

    /// tar + gzip of the home into `sink`, sha256 over the compressed bytes.
    fn archive(
        &self,
        plan: &ArchivePlan,
        sink: Box<dyn ArchiveSink>,
        counters: Arc<Counters>,
    ) -> Result<ArchiveSummary, ArchiveError>;

    /// Unpacks a gzip'd tar read from `source` into the home.
    fn extract(
        &self,
        plan: &ArchivePlan,
        source: Box<dyn Read + Send>,
        counters: Arc<Counters>,
    ) -> Result<ExtractSummary, ArchiveError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoreErrorKind {
    NotFound,
    NoCredentials,
    CredentialsRejected,
    AccessDenied,
    WrongRegion,
    /// The part source or the body stopped before the end.
    Interrupted,
    Other,
}

/// A store failure classified by the adapter; the S3 error code and the
/// request id are for the log line, never for the client.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{kind:?}")]
pub struct StoreError {
    pub kind: StoreErrorKind,
    pub s3_error_code: Option<String>,
    pub request_id: Option<String>,
}

impl StoreError {
    #[must_use]
    pub fn new(kind: StoreErrorKind) -> Self {
        Self {
            kind,
            s3_error_code: None,
            request_id: None,
        }
    }
}

/// A downloaded object, pulled chunk by chunk.
pub trait ObjectBody: Send {
    fn content_length(&self) -> Option<u64>;
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>, StoreError>> + Send;
}

/// The parts of one multipart upload, in order; `Ok(None)` ends the
/// archive, an error means the producer stopped before the end.
pub trait PartSource: Send + 'static {
    fn next_part(&mut self) -> impl Future<Output = Result<Option<Bytes>, StoreError>> + Send;
}

/// The receiving end of the restore download: chunks in, then `finish`
/// (a sink dropped without it tells the reader the download died).
pub trait ChunkSink: Send {
    fn push(&mut self, chunk: Bytes) -> impl Future<Output = Result<(), SinkClosed>> + Send;
    fn finish(self) -> impl Future<Output = ()> + Send;
}

/// The reader side went away (the extract thread stopped).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SinkClosed;

/// Runs one blocking job off the async executor and resolves with its
/// result; `Err` means the job never returned (panic or runtime shutdown).
pub trait BlockingRunner: Send + Sync {
    fn run<T, F>(&self, job: F) -> impl Future<Output = Result<T, JoinFailure>> + Send
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JoinFailure;

/// The bucket one operation addresses and, when the request named it, its
/// region (else the adapter's default from `AWS_REGION`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreTarget {
    pub bucket: BucketName,
    pub region: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PutSummary {
    pub parts: u32,
    pub bytes: u64,
}

pub trait ObjectStore: Send + Sync + 'static {
    type Body: ObjectBody;

    /// Resolves the credentials without touching the bucket, so a sandbox
    /// without an execution role fails in one IMDS round trip.
    fn probe_credentials(&self) -> impl Future<Output = Result<(), StoreError>> + Send;

    fn get(
        &self,
        target: &StoreTarget,
        key: &str,
    ) -> impl Future<Output = Result<Self::Body, StoreError>> + Send;

    fn put(
        &self,
        target: &StoreTarget,
        key: &str,
        body: Bytes,
        content_type: &str,
    ) -> impl Future<Output = Result<(), StoreError>> + Send;

    /// Uploads `parts` sequentially; aborts the multipart upload on any
    /// error, on an interrupted source and when the future is dropped.
    fn put_multipart<P: PartSource>(
        &self,
        target: &StoreTarget,
        key: &str,
        content_type: &str,
        parts: P,
    ) -> impl Future<Output = Result<PutSummary, StoreError>> + Send;
}
