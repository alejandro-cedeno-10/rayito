//! What presigned transfers (ADR-010) ask of the filesystem, on the pool
//! and under the requesting identity like every RPC: an import
//! destination resolved like a `Write` target, its free space, the same
//! temp-file sink (`fsync` + `fchmod` + `fchown` + `rename`, metadata on
//! the descriptor before the rename), the export source opened once and
//! measured by `fstat`, and the canonical target the read-after-upload
//! barrier matches.

use bytes::Bytes;
use rayd_core::filesystem::{
    Entry, ExportSource, FileMetadata, FilesystemError, FsIdentity, ImportDestination, NameCache,
    WriteSink, build_entry, metadata_error,
};

use super::manager::{FilesystemManager, join_error};

impl FilesystemManager {
    /// Resolved again right before the temp file is created, so a symlink
    /// swapped in while the ticket waited is seen.
    pub async fn import_destination(
        &self,
        identity: &FsIdentity,
        path: &str,
    ) -> Result<ImportDestination, FilesystemError> {
        let (id, path) = (identity.clone(), path.to_owned());
        self.blocking(move |ops| ops.import_destination(&id, &path))
            .await
    }

    pub async fn import_free_bytes(
        &self,
        identity: &FsIdentity,
        dir: &str,
    ) -> Result<u64, FilesystemError> {
        let (id, dir) = (identity.clone(), dir.to_owned());
        self.blocking(move |ops| ops.free_bytes(&id, &dir)).await
    }

    pub async fn begin_import(
        &self,
        identity: &FsIdentity,
        destination: &ImportDestination,
        mode: u32,
    ) -> Result<ImportSink, FilesystemError> {
        let (id, target) = (identity.clone(), destination.clone());
        let sink = self
            .blocking(move |ops| ops.begin_import(&id, &target, mode))
            .await?;
        Ok(ImportSink {
            sink: Some(sink),
            destination: destination.clone(),
            identity: identity.clone(),
            names: self.names(),
        })
    }

    pub async fn export_source(
        &self,
        identity: &FsIdentity,
        path: &str,
    ) -> Result<ExportSource, FilesystemError> {
        let (id, path) = (identity.clone(), path.to_owned());
        self.blocking(move |ops| ops.export_source(&id, &path))
            .await
    }

    /// The normalised request path and the would-be canonical target of
    /// `path`, or `None` when it cannot be resolved or is denied (the RPC
    /// itself then reports why).
    pub async fn barrier_target(&self, user: Option<&str>, path: &str) -> Option<(String, String)> {
        let id = self.identity(user).ok()?;
        let path = path.to_owned();
        self.blocking(move |ops| Ok(ops.barrier_target(&id, &path)))
            .await
            .ok()
            .flatten()
            .map(|(request, canonical)| (request.as_str().to_owned(), canonical))
    }
}

/// The temp file of one import. Dropping it before `commit` removes the
/// temp file (the sink's own `Drop`).
pub struct ImportSink {
    sink: Option<Box<dyn WriteSink>>,
    destination: ImportDestination,
    identity: FsIdentity,
    names: std::sync::Arc<dyn rayd_core::filesystem::NameResolver>,
}

impl ImportSink {
    /// The descriptor is already open, so the write needs no identity; a
    /// failure drops the sink and its temp file.
    pub async fn write(&mut self, bytes: Bytes) -> Result<(), FilesystemError> {
        let mut sink = self.sink.take().ok_or(FilesystemError::MissingPath)?;
        let written = tokio::task::spawn_blocking(move || {
            sink.write_chunk(&bytes)
                .map(|()| sink)
                .map_err(|error| FilesystemError::from_io("write", error))
        })
        .await
        .unwrap_or_else(|error| Err(join_error(&error)))?;
        self.sink = Some(written);
        Ok(())
    }

    /// Metadata on the descriptor, then the commit of the `Write` path; the
    /// entry reports exactly the set the file carries.
    pub async fn commit(mut self, metadata: FileMetadata) -> Result<Entry, FilesystemError> {
        let mut sink = self.sink.take().ok_or(FilesystemError::MissingPath)?;
        let (destination, identity, names) = (
            self.destination.clone(),
            self.identity.clone(),
            self.names.clone(),
        );
        tokio::task::spawn_blocking(move || {
            if !metadata.is_empty() {
                sink.set_metadata(&metadata).map_err(metadata_error)?;
            }
            let raw = sink
                .commit(&destination.name, &identity)
                .map_err(|error| FilesystemError::from_io("commit", error))?;
            let mut cache = NameCache::new(names.as_ref());
            let mut entry = build_entry(raw, &destination.path, &mut cache);
            entry.metadata = metadata;
            Ok(entry)
        })
        .await
        .unwrap_or_else(|error| Err(join_error(&error)))
    }
}
