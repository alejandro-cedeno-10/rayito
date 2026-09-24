//! `WatchDir` (design D8): the root and, for a recursive watch, its
//! readable non-denied subdirectories are resolved under the user's
//! identity, a bounded raw-event queue plus an overflow flag are created
//! before the watches are installed (so nothing between installation and
//! the first poll is lost), and a pump task translates raw events into
//! wire items. A directory that appears inside a recursive watch is
//! followed by the pump, never by the backend's thread, after the same
//! identity-bound check. The pump owns the subscription and the live-watch
//! slot, and exits as soon as the client drops the stream, which removes
//! every inotify watch.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use rayd_core::filesystem::{
    DenyList, Entry, FileSystem, FilesystemError, FilesystemOps, FsIdentity, NameResolver,
    RawWatchEvent, RequestPath, Translation, WatchEnd, WatchEvent, WatchEventKind,
    WatchSubscription, WatchTranslator,
};
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::{TryRecvError, TrySendError};
use tokio_stream::wrappers::ReceiverStream;

use super::manager::{FilesystemManager, join_error};

/// Translated items are handed to the response stream one at a time so
/// the raw queue, not this channel, is what bounds a stalled client.
pub const WATCH_OUTPUT_CAPACITY: usize = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WatchItem {
    Started,
    Event {
        event: WatchEvent,
        entry: Option<Box<Entry>>,
    },
}

pub type WatchStream = ReceiverStream<Result<WatchItem, FilesystemError>>;

impl FilesystemManager {
    pub async fn watch_dir(
        &self,
        user: Option<String>,
        path: String,
        recursive: bool,
        include_entry: bool,
    ) -> Result<WatchStream, FilesystemError> {
        self.stream_gate()?;
        let slot = LiveWatchSlot::acquire(&self.live_watch_counter(), self.settings().max_watches)?;
        let identity = self.identity(user.as_deref())?;
        let target = {
            let id = identity.clone();
            self.blocking(move |ops| ops.prepare_watch(&id, &path, recursive))
                .await?
        };
        let (sender, receiver) = mpsc::channel(self.settings().watch_queue_capacity);
        let overflow = Arc::new(AtomicBool::new(false));
        let sink = overflow_sink(sender, overflow.clone());
        let subscription: Arc<dyn WatchSubscription> = {
            let watcher = self.watcher();
            let root = target.canonical_root.clone();
            let subdirectories = target.subdirectories;
            let id = identity.clone();
            tokio::task::spawn_blocking(move || watcher.watch(&id, &root, &subdirectories, sink))
                .await
                .map_err(|error| join_error(&error))?
                .map_err(FilesystemError::from_watch)?
                .into()
        };
        let entries = include_entry.then(|| EntryResolver {
            fs: self.filesystem(),
            names: self.names(),
            deny: self.deny(),
            identity: identity.clone(),
            root: target.path,
            canonical_root: target.canonical_root.clone(),
        });
        let follower = recursive.then(|| SubdirectoryFollower {
            fs: self.filesystem(),
            names: self.names(),
            deny: self.deny(),
            identity,
        });
        let pump = WatchPump {
            receiver,
            overflow,
            translator: WatchTranslator::new(target.canonical_root, self.deny().as_ref().clone()),
            entries,
            follower,
            subscription,
            _slot: slot,
        };
        let (output, stream) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        tokio::spawn(pump.run(output));
        tracing::info!(
            rpc = "WatchDir",
            recursive,
            live_watches = self.live_watches(),
            "watch started"
        );
        Ok(ReceiverStream::new(stream))
    }
}

/// One of the `max_watches` slots, released when the pump ends.
struct LiveWatchSlot {
    counter: Arc<AtomicUsize>,
}

impl LiveWatchSlot {
    fn acquire(counter: &Arc<AtomicUsize>, max: usize) -> Result<Self, FilesystemError> {
        counter
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |live| {
                (live < max).then_some(live + 1)
            })
            .map_err(|_| FilesystemError::TooManyWatches { max })?;
        Ok(Self {
            counter: counter.clone(),
        })
    }
}

impl Drop for LiveWatchSlot {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Runs on notify's thread: never blocks; a full queue sets the overflow
/// flag and the event is dropped, the pump ends the stream once the queue
/// is drained.
fn overflow_sink(
    sender: mpsc::Sender<RawWatchEvent>,
    overflow: Arc<AtomicBool>,
) -> Box<dyn Fn(RawWatchEvent) + Send + Sync> {
    Box::new(move |raw| {
        if overflow.load(Ordering::SeqCst) {
            return;
        }
        if let Err(TrySendError::Full(_)) = sender.try_send(raw) {
            overflow.store(true, Ordering::SeqCst);
        }
    })
}

struct EntryResolver {
    fs: Arc<dyn FileSystem>,
    names: Arc<dyn NameResolver>,
    deny: Arc<DenyList>,
    identity: FsIdentity,
    root: RequestPath,
    canonical_root: String,
}

impl EntryResolver {
    /// `lstat` under the user's identity for `CREATE`/`WRITE`/`CHMOD`; a
    /// failure (the entry is already gone) simply omits the entry.
    async fn resolve(&self, event: &WatchEvent) -> Option<Entry> {
        if !matches!(
            event.kind,
            WatchEventKind::Create | WatchEventKind::Write | WatchEventKind::Chmod
        ) {
            return None;
        }
        let fs = self.fs.clone();
        let names = self.names.clone();
        let deny = self.deny.clone();
        let identity = self.identity.clone();
        let root = self.root.clone();
        let canonical_root = self.canonical_root.clone();
        let name = event.name.clone();
        tokio::task::spawn_blocking(move || {
            FilesystemOps::new(fs.as_ref(), &deny, names.as_ref())
                .entry_in(&identity, &root, &canonical_root, &name)
                .ok()
        })
        .await
        .ok()
        .flatten()
    }
}

/// Follows directories that appear inside a recursive watch: the same
/// identity-bound check the initial walk applied (`lstat` is a directory,
/// `read_dir` succeeds, not denied), then one more watch on the
/// subscription. Both steps are blocking calls on the pool.
struct SubdirectoryFollower {
    fs: Arc<dyn FileSystem>,
    names: Arc<dyn NameResolver>,
    deny: Arc<DenyList>,
    identity: FsIdentity,
}

impl SubdirectoryFollower {
    /// `Ok(true)` when the directory is now watched; `Ok(false)` when it
    /// may not be (unreadable, denied, a symlink, already gone); an error
    /// only for the inotify limit, which ends the watch like at install.
    async fn follow(
        &self,
        subscription: &Arc<dyn WatchSubscription>,
        canonical_dir: String,
    ) -> Result<bool, FilesystemError> {
        if !self.is_watchable(canonical_dir.clone()).await {
            return Ok(false);
        }
        let subscription = subscription.clone();
        let id = self.identity.clone();
        let added =
            tokio::task::spawn_blocking(move || subscription.add_directory(&id, &canonical_dir))
                .await
                .map_err(|error| join_error(&error))?;
        match added.map_err(FilesystemError::from_watch) {
            Ok(()) => Ok(true),
            Err(FilesystemError::WatchLimitReached) => Err(FilesystemError::WatchLimitReached),
            Err(error) => {
                tracing::debug!(rpc = "WatchDir", reason = %error, "new directory not followed");
                Ok(false)
            }
        }
    }

    async fn is_watchable(&self, canonical_dir: String) -> bool {
        let fs = self.fs.clone();
        let names = self.names.clone();
        let deny = self.deny.clone();
        let identity = self.identity.clone();
        tokio::task::spawn_blocking(move || {
            FilesystemOps::new(fs.as_ref(), &deny, names.as_ref())
                .is_watchable_directory(&identity, &canonical_dir)
        })
        .await
        .unwrap_or(false)
    }
}

enum Next {
    Event(RawWatchEvent),
    Overflow,
    Closed,
}

struct WatchPump {
    receiver: mpsc::Receiver<RawWatchEvent>,
    overflow: Arc<AtomicBool>,
    translator: WatchTranslator,
    entries: Option<EntryResolver>,
    follower: Option<SubdirectoryFollower>,
    subscription: Arc<dyn WatchSubscription>,
    _slot: LiveWatchSlot,
}

impl WatchPump {
    async fn run(mut self, output: mpsc::Sender<Result<WatchItem, FilesystemError>>) {
        if output.send(Ok(WatchItem::Started)).await.is_err() {
            return;
        }
        let mut delivered = 0usize;
        let end = loop {
            let raw = tokio::select! {
                () = output.closed() => break None,
                next = self.next_raw() => match next {
                    Next::Event(raw) => raw,
                    Next::Overflow => break Some(FilesystemError::WatchOverflow),
                    Next::Closed => break None,
                },
            };
            let translation = self.translator.translate(&raw);
            if let Err(error) = self.follow_new_directories(&raw).await {
                break Some(error);
            }
            match translation {
                Translation::Ignore => {}
                Translation::End(end) => break Some(end_error(end)),
                Translation::Events(events) => {
                    for event in events {
                        let entry = match &self.entries {
                            Some(resolver) => resolver.resolve(&event).await,
                            None => None,
                        };
                        if output
                            .send(Ok(WatchItem::Event {
                                event,
                                entry: entry.map(Box::new),
                            }))
                            .await
                            .is_err()
                        {
                            return;
                        }
                        delivered += 1;
                    }
                }
            }
        };
        if let Some(error) = end {
            tracing::info!(rpc = "WatchDir", events = delivered, reason = %error, "watch ended");
            let _ = output.send(Err(error)).await;
        } else {
            tracing::debug!(
                rpc = "WatchDir",
                events = delivered,
                "watch closed by the client"
            );
        }
    }

    async fn follow_new_directories(&mut self, raw: &RawWatchEvent) -> Result<(), FilesystemError> {
        let Some(follower) = &self.follower else {
            return Ok(());
        };
        for path in self.translator.created_paths(raw) {
            if follower.follow(&self.subscription, path.clone()).await? {
                self.translator.mark_followed(path);
            }
        }
        Ok(())
    }

    /// Buffered events first; the overflow flag is honoured only once the
    /// queue is empty so nothing already captured is thrown away.
    async fn next_raw(&mut self) -> Next {
        match self.receiver.try_recv() {
            Ok(raw) => Next::Event(raw),
            Err(TryRecvError::Disconnected) => Next::Closed,
            Err(TryRecvError::Empty) => {
                if self.overflow.load(Ordering::SeqCst) {
                    return Next::Overflow;
                }
                self.receiver.recv().await.map_or(Next::Closed, Next::Event)
            }
        }
    }
}

fn end_error(end: WatchEnd) -> FilesystemError {
    match end {
        WatchEnd::RootGone => FilesystemError::WatchRootGone,
        WatchEnd::Overflow => FilesystemError::WatchOverflow,
        WatchEnd::LimitReached => FilesystemError::WatchLimitReached,
    }
}

#[cfg(test)]
mod tests {
    use std::io::Read;
    use std::sync::Mutex;

    use rayd_core::filesystem::{
        EntryKind, FsIoError, RawEntry, RawWatchKind, WatchError, WriteSink,
    };
    use tokio_stream::StreamExt;

    use super::*;

    /// Records the directories the pump asked it to follow; `full` answers
    /// the inotify limit.
    #[derive(Default)]
    struct RecordingSubscription {
        added: Mutex<Vec<String>>,
        full: bool,
    }

    impl WatchSubscription for RecordingSubscription {
        fn add_directory(&self, _id: &FsIdentity, canonical_dir: &str) -> Result<(), WatchError> {
            if self.full {
                return Err(WatchError::LimitReached);
            }
            self.added.lock().unwrap().push(canonical_dir.to_owned());
            Ok(())
        }
    }

    /// Directories only: `/w/ok` readable, `/w/private` not, everything
    /// else missing.
    struct DirectoriesOnly;

    fn directory(name: &str) -> RawEntry {
        RawEntry {
            name: name.to_owned(),
            kind: EntryKind::Directory,
            size: 0,
            mode: 0o755,
            uid: 1000,
            gid: 1000,
            modified_ms: 0,
            symlink_target: None,
        }
    }

    impl FileSystem for DirectoriesOnly {
        fn canonicalize(&self, _id: &FsIdentity, path: &str) -> Result<String, FsIoError> {
            Ok(path.to_owned())
        }

        fn lstat(&self, _id: &FsIdentity, path: &str) -> Result<RawEntry, FsIoError> {
            match path {
                "/w/ok" | "/w/private" => Ok(directory(path)),
                _ => Err(FsIoError::NotFound),
            }
        }

        fn read_dir(&self, _id: &FsIdentity, path: &str) -> Result<Vec<RawEntry>, FsIoError> {
            match path {
                "/w/ok" => Ok(Vec::new()),
                "/w/private" => Err(FsIoError::PermissionDenied),
                _ => Err(FsIoError::NotFound),
            }
        }

        fn open_read(
            &self,
            _id: &FsIdentity,
            _path: &str,
        ) -> Result<Box<dyn Read + Send>, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn open_snapshot(
            &self,
            _id: &FsIdentity,
            _path: &str,
        ) -> Result<rayd_core::filesystem::OpenedSnapshot, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn read_metadata(
            &self,
            _id: &FsIdentity,
            _path: &str,
        ) -> Result<rayd_core::filesystem::FileMetadata, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn free_bytes(&self, _id: &FsIdentity, _dir: &str) -> Result<u64, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn begin_write(
            &self,
            _id: &FsIdentity,
            _dir: &str,
            _mode: u32,
        ) -> Result<Box<dyn WriteSink>, FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn make_dir(&self, _id: &FsIdentity, _path: &str, _mode: u32) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn rename(&self, _id: &FsIdentity, _from: &str, _to: &str) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }

        fn remove(
            &self,
            _id: &FsIdentity,
            _path: &str,
            _kind: EntryKind,
            _recursive: bool,
        ) -> Result<(), FsIoError> {
            Err(FsIoError::Unsupported)
        }
    }

    struct NoNames;

    impl NameResolver for NoNames {
        fn user_name(&self, _uid: u32) -> Option<String> {
            None
        }

        fn group_name(&self, _gid: u32) -> Option<String> {
            None
        }
    }

    fn raw(kind: RawWatchKind, path: &str) -> RawWatchEvent {
        RawWatchEvent {
            kind,
            paths: vec![path.to_owned()],
            cookie: None,
        }
    }

    type Sink = Box<dyn Fn(RawWatchEvent) + Send + Sync>;

    fn make_pump(capacity: usize) -> (Sink, WatchPump, Arc<AtomicUsize>) {
        make_pump_with(capacity, Arc::new(RecordingSubscription::default()), false)
    }

    fn make_pump_with(
        capacity: usize,
        subscription: Arc<RecordingSubscription>,
        recursive: bool,
    ) -> (Sink, WatchPump, Arc<AtomicUsize>) {
        let counter = Arc::new(AtomicUsize::new(0));
        let slot = LiveWatchSlot::acquire(&counter, 2).unwrap();
        let (sender, receiver) = mpsc::channel(capacity);
        let overflow = Arc::new(AtomicBool::new(false));
        let sink = overflow_sink(sender, overflow.clone());
        let deny = Arc::new(DenyList::default());
        let follower = recursive.then(|| SubdirectoryFollower {
            fs: Arc::new(DirectoriesOnly),
            names: Arc::new(NoNames),
            deny: deny.clone(),
            identity: FsIdentity {
                uid: 1000,
                gid: 1000,
                home: "/w".to_owned(),
            },
        });
        let pump = WatchPump {
            receiver,
            overflow,
            translator: WatchTranslator::new("/w", deny.as_ref().clone()),
            entries: None,
            follower,
            subscription,
            _slot: slot,
        };
        (sink, pump, counter)
    }

    async fn next_item(
        stream: &mut ReceiverStream<Result<WatchItem, FilesystemError>>,
    ) -> WatchItem {
        stream.next().await.unwrap().unwrap()
    }

    #[tokio::test]
    async fn recursive_pump_follows_readable_directories_only() {
        let subscription = Arc::new(RecordingSubscription::default());
        let (sink, pump, _counter) = make_pump_with(8, subscription.clone(), true);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(next_item(&mut stream).await, WatchItem::Started);
        sink(raw(RawWatchKind::DirectoryCreated, "/w/ok"));
        sink(raw(RawWatchKind::DirectoryCreated, "/w/private"));
        sink(raw(RawWatchKind::RenameTo, "/w/ok"));
        sink(raw(RawWatchKind::Create, "/w/file"));
        sink(raw(RawWatchKind::DirectoryCreated, "/w/gone"));
        for expected in ["ok", "private", "ok", "file", "gone"] {
            match next_item(&mut stream).await {
                WatchItem::Event { event, .. } => assert_eq!(event.name, expected),
                WatchItem::Started => panic!("started twice"),
            }
        }
        assert_eq!(*subscription.added.lock().unwrap(), vec!["/w/ok", "/w/ok"]);
        sink(raw(RawWatchKind::Removed, "/w/ok"));
        sink(raw(RawWatchKind::Removed, "/w/ok"));
        sink(raw(RawWatchKind::Create, "/w/marker"));
        let mut names = Vec::new();
        loop {
            match next_item(&mut stream).await {
                WatchItem::Event { event, .. } => {
                    let done = event.name == "marker";
                    names.push(event.name);
                    if done {
                        break;
                    }
                }
                WatchItem::Started => panic!("started twice"),
            }
        }
        assert_eq!(names, vec!["ok", "marker"], "self removal deduplicated");
    }

    #[tokio::test]
    async fn non_recursive_pump_never_follows_and_a_full_watcher_ends_the_stream() {
        let subscription = Arc::new(RecordingSubscription::default());
        let (sink, pump, _counter) = make_pump_with(8, subscription.clone(), false);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(next_item(&mut stream).await, WatchItem::Started);
        sink(raw(RawWatchKind::DirectoryCreated, "/w/ok"));
        assert!(matches!(
            next_item(&mut stream).await,
            WatchItem::Event { .. }
        ));
        assert!(subscription.added.lock().unwrap().is_empty());
        let full = Arc::new(RecordingSubscription {
            added: Mutex::new(Vec::new()),
            full: true,
        });
        let (sink, pump, _counter) = make_pump_with(8, full, true);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(next_item(&mut stream).await, WatchItem::Started);
        sink(raw(RawWatchKind::DirectoryCreated, "/w/ok"));
        assert_eq!(
            stream.next().await.unwrap().unwrap_err(),
            FilesystemError::WatchLimitReached
        );
    }

    #[tokio::test]
    async fn started_first_then_events_then_overflow_ends_the_stream() {
        let (sink, pump, counter) = make_pump(2);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        sink(raw(RawWatchKind::Create, "/w/a"));
        sink(raw(RawWatchKind::DataModified, "/w/a"));
        sink(raw(RawWatchKind::Removed, "/w/a"));
        assert_eq!(counter.load(Ordering::SeqCst), 1);
        tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(stream.next().await.unwrap().unwrap(), WatchItem::Started);
        let kinds: Vec<WatchEventKind> = [stream.next().await, stream.next().await]
            .into_iter()
            .map(|item| match item.unwrap().unwrap() {
                WatchItem::Event { event, .. } => event.kind,
                WatchItem::Started => panic!("started twice"),
            })
            .collect();
        assert_eq!(kinds, vec![WatchEventKind::Create, WatchEventKind::Write]);
        assert_eq!(
            stream.next().await.unwrap().unwrap_err(),
            FilesystemError::WatchOverflow
        );
        assert!(stream.next().await.is_none());
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        assert_eq!(counter.load(Ordering::SeqCst), 0, "the slot is released");
    }

    #[tokio::test]
    async fn root_removal_ends_with_not_found_and_client_drop_frees_the_slot() {
        let (sink, pump, counter) = make_pump(8);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(stream.next().await.unwrap().unwrap(), WatchItem::Started);
        sink(raw(RawWatchKind::Other, "/w/ignored"));
        sink(raw(RawWatchKind::Removed, "/w"));
        assert_eq!(
            stream.next().await.unwrap().unwrap_err(),
            FilesystemError::WatchRootGone
        );
        let (sink, pump, counter_two) = make_pump(8);
        let (output, receiver) = mpsc::channel(WATCH_OUTPUT_CAPACITY);
        let task = tokio::spawn(pump.run(output));
        let mut stream = ReceiverStream::new(receiver);
        assert_eq!(stream.next().await.unwrap().unwrap(), WatchItem::Started);
        drop(stream);
        tokio::time::timeout(std::time::Duration::from_secs(1), task)
            .await
            .expect("pump exits when the client drops")
            .unwrap();
        assert_eq!(counter_two.load(Ordering::SeqCst), 0);
        sink(raw(RawWatchKind::Create, "/w/late"));
        drop(counter);
    }

    #[test]
    fn slots_are_capped() {
        let counter = Arc::new(AtomicUsize::new(0));
        let first = LiveWatchSlot::acquire(&counter, 2).unwrap();
        let second = LiveWatchSlot::acquire(&counter, 2).unwrap();
        assert_eq!(
            LiveWatchSlot::acquire(&counter, 2).err(),
            Some(FilesystemError::TooManyWatches { max: 2 })
        );
        drop(first);
        assert!(LiveWatchSlot::acquire(&counter, 2).is_ok());
        drop(second);
    }
}
