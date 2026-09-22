//! Files inside the sandbox: how a request path is normalised and checked
//! against the deny list, what an entry looks like, how a listing walks a
//! tree, how a multi-file write stream is sequenced and how raw watch
//! events become the wire events. Pure data and rules over the
//! `FileSystem`, `Watcher` and `NameResolver` ports; every syscall and the
//! per-thread filesystem identity live in the `rayd` adapters.

pub mod entry;
pub mod error;
pub mod events;
pub mod identity;
pub mod listing;
pub mod ops;
pub mod path;
pub mod ports;
pub mod write;

#[cfg(test)]
pub(crate) mod fake;

pub use entry::{Entry, EntryKind, NameCache, RawEntry, build_entry, permissions_string};
pub use error::FilesystemError;
pub use events::{
    RawWatchEvent, RawWatchKind, Translation, WatchEnd, WatchEvent, WatchEventKind, WatchTranslator,
};
pub use identity::{FsIdentity, resolve_identity};
pub use listing::{ListingLimits, effective_depth, walk_listing};
pub use ops::{FilesystemOps, WatchTarget, WriteTarget};
pub use path::{
    DEFAULT_DENIED_PREFIXES, DenyList, PathRejection, RAYD_BINARY, RequestPath, join_canonical,
};
pub use ports::{
    FileSystem, FsIoError, NameResolver, WatchError, WatchSubscription, Watcher, WriteSink,
};
pub use write::{DISK_RESERVE_BYTES, WriteMessage, WriteSession, WriteStep, check_disk_reserve};

/// Size of every `ReadResponse.chunk` but the last.
pub const READ_CHUNK_BYTES: usize = 262_144;
/// Largest `WriteRequest.chunk` accepted; a bigger one is `INVALID_ARGUMENT`.
pub const MAX_WRITE_CHUNK_BYTES: usize = 1_048_576;
pub const DEFAULT_FILE_MODE: u32 = 0o644;
pub const DEFAULT_DIR_MODE: u32 = 0o755;
/// Permission, setuid, setgid and sticky bits: everything a `mode` may carry.
pub const MODE_MASK: u32 = 0o7777;
/// Prefix of the temp file an atomic write creates next to its destination;
/// watch events on such names are dropped.
pub const TEMP_PREFIX: &str = ".rayito-tmp-";
pub const MAX_LIST_ENTRIES: usize = 10_000;
/// Directories one recursive watch may cover, the kernel's default
/// `fs.inotify.max_user_watches` on the VM: a bigger tree is refused before
/// the walk finishes rather than by the last `inotify_add_watch`.
pub const MAX_WATCH_DIRECTORIES: usize = 8_192;
