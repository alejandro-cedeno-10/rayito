//! Application service behind `FilesystemService`: resolves the requesting
//! identity with the process rules, runs each RPC's `FilesystemOps` call on
//! the blocking pool, and owns the knobs (listing cap, watch queue, watch
//! cap, error drain) the integration tests shrink.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use rayd_core::filesystem::{
    DenyList, Entry, FileSystem, FilesystemError, FilesystemOps, FsIdentity, ListingLimits,
    NameResolver, Watcher, resolve_identity,
};
use rayd_core::process::{UserLookup, UserPolicy};
use rayd_core::session::SandboxSession;

use crate::adapters::{PlatformFileSystem, PlatformNameResolver, PlatformWatcher, SpawnPlatform};

pub const DEFAULT_WATCH_QUEUE_CAPACITY: usize = 1024;
pub const DEFAULT_MAX_WATCHES: usize = 64;
/// Bounds of the drain that lets an early `Write` error reach the client
/// as a status instead of a stream reset (`AWS_API_NOTES.md` Q29).
pub const WRITE_ERROR_DRAIN_BYTES: usize = 1 << 20;
pub const WRITE_ERROR_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FilesystemSettings {
    pub list_limits: ListingLimits,
    pub watch_queue_capacity: usize,
    pub max_watches: usize,
    pub write_error_drain_bytes: usize,
    pub write_error_drain_timeout: Duration,
}

impl Default for FilesystemSettings {
    fn default() -> Self {
        Self {
            list_limits: ListingLimits::default(),
            watch_queue_capacity: DEFAULT_WATCH_QUEUE_CAPACITY,
            max_watches: DEFAULT_MAX_WATCHES,
            write_error_drain_bytes: WRITE_ERROR_DRAIN_BYTES,
            write_error_drain_timeout: WRITE_ERROR_DRAIN_TIMEOUT,
        }
    }
}

/// The four ports a manager runs on; `main` and the tests pick them.
pub struct FilesystemPlatform {
    pub fs: Arc<dyn FileSystem>,
    pub watcher: Arc<dyn Watcher>,
    pub names: Arc<dyn NameResolver>,
    pub lookup: Arc<dyn UserLookup>,
}

pub struct FilesystemManager {
    session: Arc<SandboxSession>,
    fs: Arc<dyn FileSystem>,
    watcher: Arc<dyn Watcher>,
    names: Arc<dyn NameResolver>,
    lookup: Arc<dyn UserLookup>,
    policy: UserPolicy,
    deny: Arc<DenyList>,
    settings: FilesystemSettings,
    live_watches: Arc<AtomicUsize>,
}

impl FilesystemManager {
    pub fn new(
        session: Arc<SandboxSession>,
        platform: FilesystemPlatform,
        policy: UserPolicy,
        deny: DenyList,
        settings: FilesystemSettings,
    ) -> Arc<Self> {
        Arc::new(Self {
            session,
            fs: platform.fs,
            watcher: platform.watcher,
            names: platform.names,
            lookup: platform.lookup,
            policy,
            deny: Arc::new(deny),
            settings,
            live_watches: Arc::new(AtomicUsize::new(0)),
        })
    }

    /// Request `User.username` → `/run` payload default → `user`; root only
    /// with the image's opt-in, exactly like process spawning.
    pub fn identity(&self, user: Option<&str>) -> Result<FsIdentity, FilesystemError> {
        let defaults = self.session.spawn_defaults();
        resolve_identity(
            user,
            defaults.user.as_deref(),
            self.policy,
            self.lookup.as_ref(),
        )
    }

    pub async fn stat(&self, user: Option<String>, path: String) -> Result<Entry, FilesystemError> {
        let id = self.identity(user.as_deref())?;
        self.blocking(move |ops| ops.stat(&id, &path)).await
    }

    pub async fn list_dir(
        &self,
        user: Option<String>,
        path: String,
        depth: u32,
    ) -> Result<Vec<Entry>, FilesystemError> {
        let id = self.identity(user.as_deref())?;
        let limits = self.settings.list_limits;
        self.blocking(move |ops| ops.list_dir(&id, &path, depth, limits))
            .await
    }

    pub async fn make_dir(
        &self,
        user: Option<String>,
        path: String,
    ) -> Result<Entry, FilesystemError> {
        let id = self.identity(user.as_deref())?;
        self.blocking(move |ops| ops.make_dir(&id, &path)).await
    }

    pub async fn rename(
        &self,
        user: Option<String>,
        source: String,
        destination: String,
    ) -> Result<Entry, FilesystemError> {
        let id = self.identity(user.as_deref())?;
        self.blocking(move |ops| ops.rename(&id, &source, &destination))
            .await
    }

    pub async fn remove(
        &self,
        user: Option<String>,
        path: String,
        recursive: bool,
    ) -> Result<(), FilesystemError> {
        let id = self.identity(user.as_deref())?;
        self.blocking(move |ops| ops.remove(&id, &path, recursive))
            .await
    }

    #[must_use]
    pub fn live_watches(&self) -> usize {
        self.live_watches.load(Ordering::SeqCst)
    }

    #[must_use]
    pub fn settings(&self) -> FilesystemSettings {
        self.settings
    }

    pub(super) fn stream_gate(&self) -> Result<(), FilesystemError> {
        self.session
            .stream_gate()
            .map_err(|phase| FilesystemError::NotAcceptingStreams { phase })
    }

    pub(super) fn filesystem(&self) -> Arc<dyn FileSystem> {
        self.fs.clone()
    }

    pub(super) fn watcher(&self) -> Arc<dyn Watcher> {
        self.watcher.clone()
    }

    pub(super) fn names(&self) -> Arc<dyn NameResolver> {
        self.names.clone()
    }

    pub(super) fn deny(&self) -> Arc<DenyList> {
        self.deny.clone()
    }

    pub(super) fn live_watch_counter(&self) -> Arc<AtomicUsize> {
        self.live_watches.clone()
    }

    /// One `FilesystemOps` call on the blocking pool; the adapter enters the
    /// identity inside each port method, so nothing here touches uids.
    pub(super) async fn blocking<T, F>(&self, task: F) -> Result<T, FilesystemError>
    where
        T: Send + 'static,
        F: FnOnce(&FilesystemOps<'_>) -> Result<T, FilesystemError> + Send + 'static,
    {
        let fs = self.fs.clone();
        let deny = self.deny.clone();
        let names = self.names.clone();
        tokio::task::spawn_blocking(move || {
            let ops = FilesystemOps::new(fs.as_ref(), &deny, names.as_ref());
            task(&ops)
        })
        .await
        .unwrap_or_else(|error| Err(join_error(&error)))
    }
}

pub(super) fn join_error(error: &tokio::task::JoinError) -> FilesystemError {
    FilesystemError::Io {
        operation: "blocking task",
        errno: if error.is_panic() {
            "panicked".to_owned()
        } else {
            "cancelled".to_owned()
        },
    }
}

/// Builds the manager for the host `rayd` runs on, sharing the user lookup
/// and the identity switch of the process platform.
pub fn platform_filesystem_manager(
    session: Arc<SandboxSession>,
    platform: &SpawnPlatform,
    policy: UserPolicy,
    deny: DenyList,
    settings: FilesystemSettings,
) -> Arc<FilesystemManager> {
    FilesystemManager::new(
        session,
        FilesystemPlatform {
            fs: Arc::new(PlatformFileSystem::new(platform.identity_switch)),
            watcher: Arc::new(PlatformWatcher::new(platform.identity_switch)),
            names: Arc::new(PlatformNameResolver::default()),
            lookup: platform.lookup.clone(),
        },
        policy,
        deny,
        settings,
    )
}
