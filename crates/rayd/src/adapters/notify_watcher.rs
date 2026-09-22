//! The `Watcher` port over `notify` 8.x (inotify on Linux) without any
//! debouncer: one `RecommendedWatcher` per subscription, created inside
//! the requesting identity so notify's own thread (the one that calls
//! `inotify_add_watch`) inherits the user's fsuid, and one non-recursive
//! watch per directory the domain walked. Recursion is never delegated to
//! notify: its root-run walk would install watches on trees the user
//! cannot read. Off Unix every watch answers `Unsupported`.

#[cfg(unix)]
pub use unix::{NotifySubscription, NotifyWatcher, raw_kind};
#[cfg(unix)]
pub type PlatformWatcher = unix::NotifyWatcher;

#[cfg(not(unix))]
pub use unsupported::UnsupportedWatcher;
#[cfg(not(unix))]
pub type PlatformWatcher = unsupported::UnsupportedWatcher;

#[cfg(unix)]
mod unix {
    use std::path::Path;
    use std::sync::{Mutex, PoisonError};

    use notify::event::{CreateKind, ModifyKind, RenameMode};
    use notify::{Config, ErrorKind, Event, EventKind, RecommendedWatcher, RecursiveMode};
    use rayd_core::filesystem::{
        FsIdentity, RawWatchEvent, RawWatchKind, WatchError, WatchSubscription, Watcher,
    };

    use crate::adapters::IdentitySwitch;
    use crate::adapters::fs_identity::FsIdentityGuard;
    use crate::adapters::std_filesystem::io_error;

    pub struct NotifyWatcher {
        identity_switch: IdentitySwitch,
    }

    impl NotifyWatcher {
        #[must_use]
        pub fn new(identity_switch: IdentitySwitch) -> Self {
            Self { identity_switch }
        }
    }

    impl Watcher for NotifyWatcher {
        /// The guard is entered before `RecommendedWatcher::new` on purpose:
        /// notify spawns its event-loop thread there, and a Linux thread
        /// inherits the creating thread's fsuid/fsgid, so every
        /// `inotify_add_watch` this subscription ever issues runs as `id`.
        fn watch(
            &self,
            id: &FsIdentity,
            canonical_root: &str,
            subdirectories: &[String],
            sink: Box<dyn Fn(RawWatchEvent) + Send + Sync>,
        ) -> Result<Box<dyn WatchSubscription>, WatchError> {
            let config = Config::default().with_follow_symlinks(false);
            let handler = move |result: notify::Result<Event>| {
                if let Some(raw) = raw_event(result) {
                    sink(raw);
                }
            };
            let _guard = FsIdentityGuard::enter(self.identity_switch, id)
                .map_err(|error| WatchError::Other(error.to_string()))?;
            let mut watcher = <RecommendedWatcher as notify::Watcher>::new(handler, config)
                .map_err(watch_error)?;
            add_directory_watch(&mut watcher, canonical_root)?;
            for dir in subdirectories {
                match add_directory_watch(&mut watcher, dir) {
                    Ok(()) => {}
                    Err(WatchError::NotFound | WatchError::PermissionDenied) => {
                        tracing::debug!("subdirectory skipped after the walk");
                    }
                    Err(error) => return Err(error),
                }
            }
            Ok(Box::new(NotifySubscription {
                watcher: Mutex::new(watcher),
                identity_switch: self.identity_switch,
            }))
        }
    }

    fn add_directory_watch(watcher: &mut RecommendedWatcher, dir: &str) -> Result<(), WatchError> {
        notify::Watcher::watch(watcher, Path::new(dir), RecursiveMode::NonRecursive)
            .map_err(watch_error)
    }

    /// Dropping the watcher stops its thread and removes every inotify
    /// watch it installed.
    pub struct NotifySubscription {
        watcher: Mutex<RecommendedWatcher>,
        identity_switch: IdentitySwitch,
    }

    impl WatchSubscription for NotifySubscription {
        fn add_directory(&self, id: &FsIdentity, canonical_dir: &str) -> Result<(), WatchError> {
            let _guard = FsIdentityGuard::enter(self.identity_switch, id)
                .map_err(|error| WatchError::Other(error.to_string()))?;
            let mut watcher = self.watcher.lock().unwrap_or_else(PoisonError::into_inner);
            add_directory_watch(&mut watcher, canonical_dir)
        }
    }

    /// Kinds the wire does not carry (`IN_OPEN`, `IN_CLOSE_*`, the paired
    /// `Both`) are dropped here so they never take a slot of the bounded
    /// queue.
    fn raw_event(result: notify::Result<Event>) -> Option<RawWatchEvent> {
        match result {
            Ok(event) => {
                let kind = raw_kind(&event);
                (kind != RawWatchKind::Other).then(|| RawWatchEvent {
                    kind,
                    paths: event
                        .paths
                        .iter()
                        .map(|path| path.to_string_lossy().into_owned())
                        .collect(),
                })
            }
            Err(error) if matches!(error.kind, ErrorKind::MaxFilesWatch) => Some(RawWatchEvent {
                kind: RawWatchKind::LimitReached,
                paths: Vec::new(),
            }),
            Err(error) => {
                tracing::debug!(reason = error_label(&error.kind), "watcher error ignored");
                None
            }
        }
    }

    /// The inotify backend emits `From` and `To` before the paired `Both`,
    /// so `Both` is dropped here to keep one event per affected name. A
    /// `From` without a rename cookie is `IN_MOVE_SELF` (the watched
    /// directory itself moved), which notify reports untracked.
    #[must_use]
    pub fn raw_kind(event: &Event) -> RawWatchKind {
        if event.need_rescan() {
            return RawWatchKind::QueueOverflow;
        }
        match event.kind {
            EventKind::Create(CreateKind::Folder) => RawWatchKind::DirectoryCreated,
            EventKind::Create(_) => RawWatchKind::Create,
            EventKind::Modify(ModifyKind::Data(_) | ModifyKind::Any) => RawWatchKind::DataModified,
            EventKind::Modify(ModifyKind::Metadata(_)) => RawWatchKind::MetadataModified,
            EventKind::Modify(ModifyKind::Name(RenameMode::From)) => {
                if event.tracker().is_some() {
                    RawWatchKind::RenameFrom
                } else {
                    RawWatchKind::MovedSelf
                }
            }
            EventKind::Modify(ModifyKind::Name(RenameMode::To | RenameMode::Any)) => {
                RawWatchKind::RenameTo
            }
            EventKind::Remove(_) => RawWatchKind::Removed,
            EventKind::Modify(
                ModifyKind::Name(RenameMode::Both | RenameMode::Other) | ModifyKind::Other,
            )
            | EventKind::Access(_)
            | EventKind::Any
            | EventKind::Other => RawWatchKind::Other,
        }
    }

    /// notify's errors carry the paths they failed on; only the kind is
    /// ever logged or forwarded.
    fn watch_error(error: notify::Error) -> WatchError {
        match error.kind {
            ErrorKind::MaxFilesWatch => WatchError::LimitReached,
            ErrorKind::PathNotFound => WatchError::NotFound,
            ErrorKind::Io(io) => match io_error(&io) {
                rayd_core::filesystem::FsIoError::NotFound => WatchError::NotFound,
                rayd_core::filesystem::FsIoError::PermissionDenied => WatchError::PermissionDenied,
                rayd_core::filesystem::FsIoError::NotADirectory => WatchError::NotADirectory,
                rayd_core::filesystem::FsIoError::NoSpace => WatchError::LimitReached,
                other => WatchError::Other(other.to_string()),
            },
            other => WatchError::Other(error_label(&other).to_owned()),
        }
    }

    fn error_label(kind: &ErrorKind) -> &'static str {
        match kind {
            ErrorKind::Generic(_) => "generic",
            ErrorKind::Io(_) => "io",
            ErrorKind::PathNotFound => "path not found",
            ErrorKind::WatchNotFound => "watch not found",
            ErrorKind::InvalidConfig(_) => "invalid config",
            ErrorKind::MaxFilesWatch => "max files watch",
        }
    }

    #[cfg(test)]
    mod tests {
        use notify::event::{AccessKind, DataChange, Flag, MetadataKind, RemoveKind};

        use super::*;

        fn event(kind: EventKind) -> Event {
            Event::new(kind)
        }

        #[test]
        fn notify_kinds_map_to_raw_kinds() {
            let cases = [
                (EventKind::Create(CreateKind::File), RawWatchKind::Create),
                (EventKind::Create(CreateKind::Any), RawWatchKind::Create),
                (
                    EventKind::Create(CreateKind::Folder),
                    RawWatchKind::DirectoryCreated,
                ),
                (
                    EventKind::Modify(ModifyKind::Data(DataChange::Any)),
                    RawWatchKind::DataModified,
                ),
                (
                    EventKind::Modify(ModifyKind::Any),
                    RawWatchKind::DataModified,
                ),
                (
                    EventKind::Modify(ModifyKind::Metadata(MetadataKind::Any)),
                    RawWatchKind::MetadataModified,
                ),
                (
                    EventKind::Modify(ModifyKind::Name(RenameMode::From)),
                    RawWatchKind::MovedSelf,
                ),
                (
                    EventKind::Modify(ModifyKind::Name(RenameMode::To)),
                    RawWatchKind::RenameTo,
                ),
                (
                    EventKind::Modify(ModifyKind::Name(RenameMode::Any)),
                    RawWatchKind::RenameTo,
                ),
                (
                    EventKind::Modify(ModifyKind::Name(RenameMode::Both)),
                    RawWatchKind::Other,
                ),
                (EventKind::Remove(RemoveKind::Folder), RawWatchKind::Removed),
                (EventKind::Access(AccessKind::Any), RawWatchKind::Other),
                (EventKind::Any, RawWatchKind::Other),
            ];
            for (kind, expected) in cases {
                assert_eq!(raw_kind(&event(kind)), expected, "{kind:?}");
            }
            let tracked_from =
                event(EventKind::Modify(ModifyKind::Name(RenameMode::From))).set_tracker(7);
            assert_eq!(raw_kind(&tracked_from), RawWatchKind::RenameFrom);
            let overflow = event(EventKind::Other).set_flag(Flag::Rescan);
            assert_eq!(raw_kind(&overflow), RawWatchKind::QueueOverflow);
        }

        /// A non-recursive watch on a real temp directory, dropped and
        /// re-created, proves the per-subscription watcher installs,
        /// follows an added directory and cleans up on drop.
        #[test]
        fn watch_installs_root_and_added_directories() {
            let dir = tempfile::tempdir().unwrap();
            let root = std::fs::canonicalize(dir.path())
                .unwrap()
                .to_string_lossy()
                .into_owned();
            std::fs::create_dir(format!("{root}/sub")).unwrap();
            let (sender, receiver) = std::sync::mpsc::channel::<RawWatchEvent>();
            let id = FsIdentity {
                uid: nix::unistd::getuid().as_raw(),
                gid: nix::unistd::getgid().as_raw(),
                home: root.clone(),
            };
            let watcher = NotifyWatcher::new(IdentitySwitch::KeepCurrent);
            let subscription = watcher
                .watch(
                    &id,
                    &root,
                    &[format!("{root}/gone")],
                    Box::new(move |raw| {
                        let _ = sender.send(raw);
                    }),
                )
                .unwrap();
            std::fs::write(format!("{root}/sub/before.txt"), b"x").unwrap();
            subscription
                .add_directory(&id, &format!("{root}/sub"))
                .unwrap();
            std::fs::write(format!("{root}/sub/after.txt"), b"x").unwrap();
            std::fs::write(format!("{root}/marker"), b"x").unwrap();
            let mut seen = Vec::new();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while std::time::Instant::now() < deadline {
                let Ok(raw) = receiver.recv_timeout(std::time::Duration::from_millis(200)) else {
                    continue;
                };
                let done = raw.paths.iter().any(|path| path.ends_with("/marker"));
                seen.extend(raw.paths);
                if done {
                    break;
                }
            }
            assert!(seen.iter().any(|path| path.ends_with("/sub/after.txt")));
            assert!(!seen.iter().any(|path| path.ends_with("/sub/before.txt")));
            assert_eq!(
                subscription
                    .add_directory(&id, &format!("{root}/missing"))
                    .unwrap_err(),
                WatchError::NotFound
            );
            assert_eq!(
                watcher
                    .watch(&id, &format!("{root}/missing"), &[], Box::new(|_| {}))
                    .err(),
                Some(WatchError::NotFound)
            );
        }
    }
}

#[cfg(not(unix))]
mod unsupported {
    use rayd_core::filesystem::{
        FsIdentity, RawWatchEvent, WatchError, WatchSubscription, Watcher,
    };

    use crate::adapters::IdentitySwitch;

    #[derive(Debug, Default, Clone, Copy)]
    pub struct UnsupportedWatcher;

    impl UnsupportedWatcher {
        #[must_use]
        pub fn new(_identity_switch: IdentitySwitch) -> Self {
            Self
        }
    }

    impl Watcher for UnsupportedWatcher {
        fn watch(
            &self,
            _id: &FsIdentity,
            _canonical_root: &str,
            _subdirectories: &[String],
            _sink: Box<dyn Fn(RawWatchEvent) + Send + Sync>,
        ) -> Result<Box<dyn WatchSubscription>, WatchError> {
            Err(WatchError::Unsupported)
        }
    }
}
