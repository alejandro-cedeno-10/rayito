//! From the watcher's raw events (absolute paths, backend kinds) to the
//! wire `FilesystemEvent` (names relative to the watched directory, the
//! five E2B types) and the two ways a watch stream ends. A recursive watch
//! is one non-recursive watch per directory, so the translator also picks
//! the directories that appeared inside the root (for the pump to follow)
//! and drops the self events (`IN_DELETE_SELF`, `IN_MOVE_SELF`) of a
//! followed directory, which its parent's watch already reported. An
//! atomic `Write` lands its temp file over the destination with one
//! rename; the translator pairs the two halves by their rename cookie and
//! reports the landing as the `WRITE` an E2B `files.write` produces.

use std::collections::{BTreeSet, VecDeque};

use super::TEMP_PREFIX;
use super::path::DenyList;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchEventKind {
    Create,
    Write,
    Remove,
    Rename,
    Chmod,
}

impl WatchEventKind {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Create => "create",
            Self::Write => "write",
            Self::Remove => "remove",
            Self::Rename => "rename",
            Self::Chmod => "chmod",
        }
    }
}

/// `name` is relative to the watched directory (`a.txt`, `sub/b.txt`),
/// never absolute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchEvent {
    pub name: String,
    pub kind: WatchEventKind,
}

/// What a backend can report, reduced to what the wire distinguishes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RawWatchKind {
    Create,
    /// A create the backend flagged as a directory (`IN_ISDIR`).
    DirectoryCreated,
    DataModified,
    MetadataModified,
    RenameFrom,
    RenameTo,
    /// Paths `[from, to]`.
    RenameBoth,
    /// `IN_MOVE_SELF`: the watched directory itself was moved; its parent's
    /// watch reports the same move as `RenameFrom`.
    MovedSelf,
    Removed,
    RootGone,
    QueueOverflow,
    LimitReached,
    /// Access, open, close and anything else the wire does not carry.
    Other,
}

/// `cookie` is the backend's rename cookie (inotify `cookie`), shared by
/// the `RenameFrom` and `RenameTo` halves of one `rename(2)`; `None` for
/// every other kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawWatchEvent {
    pub kind: RawWatchKind,
    pub paths: Vec<String>,
    pub cookie: Option<usize>,
}

/// Temp-file renames whose destination half has not arrived yet. Renames
/// in different directories of a recursive watch may interleave, so more
/// than one can be pending; the oldest is forgotten past this bound.
const MAX_PENDING_LANDINGS: usize = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchEnd {
    RootGone,
    Overflow,
    LimitReached,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Translation {
    Events(Vec<WatchEvent>),
    End(WatchEnd),
    Ignore,
}

pub struct WatchTranslator {
    canonical_root: String,
    deny: DenyList,
    followed: BTreeSet<String>,
    pending_self_removal: Option<String>,
    pending_landings: VecDeque<usize>,
}

impl WatchTranslator {
    #[must_use]
    pub fn new(canonical_root: impl Into<String>, deny: DenyList) -> Self {
        Self {
            canonical_root: canonical_root.into(),
            deny,
            followed: BTreeSet::new(),
            pending_self_removal: None,
            pending_landings: VecDeque::new(),
        }
    }

    #[must_use]
    pub fn canonical_root(&self) -> &str {
        &self.canonical_root
    }

    /// Records a subdirectory the pump installed its own watch on, so the
    /// self events that watch emits when the directory goes can be dropped.
    pub fn mark_followed(&mut self, canonical_dir: String) {
        self.pending_self_removal = None;
        self.followed.insert(canonical_dir);
    }

    /// Canonical paths of directories that may have appeared inside the
    /// root: a directory create, or a move-in whose kind the backend does
    /// not tell (the caller must `lstat` it). Denied paths are never
    /// followed.
    #[must_use]
    pub fn created_paths(&self, raw: &RawWatchEvent) -> Vec<String> {
        if !matches!(
            raw.kind,
            RawWatchKind::DirectoryCreated | RawWatchKind::RenameTo
        ) {
            return Vec::new();
        }
        raw.paths
            .iter()
            .filter(|path| self.relative_name(path).is_some())
            .filter(|path| !self.deny.is_denied(path))
            .cloned()
            .collect()
    }

    /// Events outside the root, under a denied prefix and on the temp names
    /// of atomic writes are dropped; a removal or rename of the root itself
    /// ends the watch; `RenameBoth` yields the `from` event before the `to`
    /// event; the second removal a followed directory reports for itself
    /// is dropped; the `RenameTo` that lands an atomic write's temp file
    /// (same cookie as a temp-name `RenameFrom`) becomes a `WRITE`.
    #[must_use]
    pub fn translate(&mut self, raw: &RawWatchEvent) -> Translation {
        match raw.kind {
            RawWatchKind::RootGone => Translation::End(WatchEnd::RootGone),
            RawWatchKind::QueueOverflow => Translation::End(WatchEnd::Overflow),
            RawWatchKind::LimitReached => Translation::End(WatchEnd::LimitReached),
            RawWatchKind::Removed
            | RawWatchKind::RenameFrom
            | RawWatchKind::RenameTo
            | RawWatchKind::RenameBoth
            | RawWatchKind::MovedSelf
                if self.names_the_root(&raw.paths) =>
            {
                Translation::End(WatchEnd::RootGone)
            }
            RawWatchKind::Other | RawWatchKind::MovedSelf => Translation::Ignore,
            kind => {
                if self.is_duplicate_self_removal(kind, &raw.paths) {
                    return Translation::Ignore;
                }
                self.track_followed(kind, &raw.paths);
                let kind = self.landing_kind(kind, raw);
                let events = self.events_for(kind, &raw.paths);
                if events.is_empty() {
                    Translation::Ignore
                } else {
                    Translation::Events(events)
                }
            }
        }
    }

    fn names_the_root(&self, paths: &[String]) -> bool {
        paths.contains(&self.canonical_root)
    }

    /// `rmdir` of a followed directory arrives twice: `IN_DELETE` from the
    /// parent's watch first, then `IN_DELETE_SELF` from its own. The first
    /// is forwarded and remembered; the second is the duplicate. A create
    /// of the same name in between means a new directory, whose removal is
    /// real again.
    fn is_duplicate_self_removal(&mut self, kind: RawWatchKind, paths: &[String]) -> bool {
        let [path] = paths else {
            return false;
        };
        match kind {
            RawWatchKind::Removed if self.pending_self_removal.as_deref() == Some(path) => {
                self.pending_self_removal = None;
                true
            }
            RawWatchKind::Removed if self.followed.contains(path) => {
                self.pending_self_removal = Some(path.clone());
                false
            }
            RawWatchKind::Create | RawWatchKind::DirectoryCreated
                if self.pending_self_removal.as_deref() == Some(path) =>
            {
                self.pending_self_removal = None;
                false
            }
            _ => false,
        }
    }

    /// The backend removes a directory's own watch when the directory is
    /// removed or moved away; a re-created directory is followed again by
    /// the pump.
    fn track_followed(&mut self, kind: RawWatchKind, paths: &[String]) {
        if matches!(kind, RawWatchKind::Removed | RawWatchKind::RenameFrom) {
            for path in paths {
                self.followed.remove(path);
            }
        }
    }

    /// A temp-name `RenameFrom` with a cookie is remembered; the `RenameTo`
    /// carrying the same cookie is the destination of an atomic write and
    /// is reported as a data modification. Any other rename keeps its kind.
    fn landing_kind(&mut self, kind: RawWatchKind, raw: &RawWatchEvent) -> RawWatchKind {
        match (kind, raw.cookie) {
            (RawWatchKind::RenameFrom, Some(cookie))
                if !raw.paths.is_empty() && raw.paths.iter().all(|path| is_temp_name(path)) =>
            {
                if self.pending_landings.len() == MAX_PENDING_LANDINGS {
                    self.pending_landings.pop_front();
                }
                self.pending_landings.push_back(cookie);
                kind
            }
            (RawWatchKind::RenameTo, Some(cookie)) => {
                match self
                    .pending_landings
                    .iter()
                    .position(|pending| *pending == cookie)
                {
                    Some(index) => {
                        self.pending_landings.remove(index);
                        RawWatchKind::DataModified
                    }
                    None => kind,
                }
            }
            _ => kind,
        }
    }

    fn events_for(&self, kind: RawWatchKind, paths: &[String]) -> Vec<WatchEvent> {
        let mapped = match kind {
            RawWatchKind::Create | RawWatchKind::DirectoryCreated => WatchEventKind::Create,
            RawWatchKind::DataModified => WatchEventKind::Write,
            RawWatchKind::MetadataModified => WatchEventKind::Chmod,
            RawWatchKind::Removed => WatchEventKind::Remove,
            RawWatchKind::RenameFrom | RawWatchKind::RenameTo | RawWatchKind::RenameBoth => {
                WatchEventKind::Rename
            }
            RawWatchKind::MovedSelf
            | RawWatchKind::RootGone
            | RawWatchKind::QueueOverflow
            | RawWatchKind::LimitReached
            | RawWatchKind::Other => return Vec::new(),
        };
        paths
            .iter()
            .filter(|path| !self.deny.is_denied(path))
            .filter_map(|path| self.relative_name(path))
            .filter(|name| !is_temp_name(name))
            .map(|name| WatchEvent { name, kind: mapped })
            .collect()
    }

    fn relative_name(&self, path: &str) -> Option<String> {
        let rest = if self.canonical_root == "/" {
            path.strip_prefix('/')?
        } else {
            path.strip_prefix(self.canonical_root.as_str())?
                .strip_prefix('/')?
        };
        (!rest.is_empty()).then(|| rest.to_owned())
    }
}

fn is_temp_name(name: &str) -> bool {
    name.rsplit('/')
        .next()
        .is_some_and(|last| last.starts_with(TEMP_PREFIX))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ROOT: &str = "/home/user/m3/watch";

    fn raw(kind: RawWatchKind, paths: &[&str]) -> RawWatchEvent {
        RawWatchEvent {
            kind,
            paths: paths.iter().map(|path| (*path).to_owned()).collect(),
            cookie: None,
        }
    }

    fn renamed(kind: RawWatchKind, path: &str, cookie: usize) -> RawWatchEvent {
        RawWatchEvent {
            kind,
            paths: vec![path.to_owned()],
            cookie: Some(cookie),
        }
    }

    fn typed(translation: Translation) -> Vec<(String, WatchEventKind)> {
        match translation {
            Translation::Events(events) => events
                .into_iter()
                .map(|event| (event.name, event.kind))
                .collect(),
            Translation::Ignore => Vec::new(),
            Translation::End(end) => panic!("unexpected end {end:?}"),
        }
    }

    fn translator() -> WatchTranslator {
        WatchTranslator::new(ROOT, DenyList::default())
    }

    fn translate(kind: RawWatchKind, paths: &[&str]) -> Translation {
        translator().translate(&raw(kind, paths))
    }

    fn names(translation: Translation) -> Vec<String> {
        match translation {
            Translation::Events(events) => events.into_iter().map(|event| event.name).collect(),
            Translation::Ignore => Vec::new(),
            Translation::End(end) => panic!("unexpected end {end:?}"),
        }
    }

    fn events(kind: RawWatchKind, paths: &[&str]) -> Vec<(String, WatchEventKind)> {
        match translate(kind, paths) {
            Translation::Events(events) => events
                .into_iter()
                .map(|event| (event.name, event.kind))
                .collect(),
            other => panic!("expected events, got {other:?}"),
        }
    }

    #[test]
    fn kinds_map_to_the_five_wire_types_with_relative_names() {
        let file = format!("{ROOT}/a.txt");
        assert_eq!(
            events(RawWatchKind::Create, &[&file]),
            vec![("a.txt".to_owned(), WatchEventKind::Create)]
        );
        assert_eq!(
            events(RawWatchKind::DirectoryCreated, &[&file])[0].1,
            WatchEventKind::Create
        );
        assert_eq!(
            events(RawWatchKind::DataModified, &[&file])[0].1,
            WatchEventKind::Write
        );
        assert_eq!(
            events(RawWatchKind::MetadataModified, &[&file])[0].1,
            WatchEventKind::Chmod
        );
        assert_eq!(
            events(RawWatchKind::Removed, &[&file])[0].1,
            WatchEventKind::Remove
        );
        assert_eq!(
            events(RawWatchKind::RenameTo, &[&file])[0].1,
            WatchEventKind::Rename
        );
        assert_eq!(WatchEventKind::Chmod.as_str(), "chmod");
    }

    #[test]
    fn nested_names_keep_their_relative_path() {
        let nested = format!("{ROOT}/sub/n.txt");
        assert_eq!(events(RawWatchKind::Create, &[&nested])[0].0, "sub/n.txt");
    }

    #[test]
    fn outside_root_and_temp_names_are_dropped() {
        assert_eq!(
            translate(RawWatchKind::Create, &["/home/user/m3/watcher/x"]),
            Translation::Ignore
        );
        assert_eq!(
            translate(RawWatchKind::Create, &["/home/user/m3/other.txt"]),
            Translation::Ignore
        );
        let temp = format!("{ROOT}/.rayito-tmp-abc123");
        assert_eq!(
            translate(RawWatchKind::DataModified, &[&temp]),
            Translation::Ignore
        );
        let nested_temp = format!("{ROOT}/sub/.rayito-tmp-abc123");
        assert_eq!(
            translate(RawWatchKind::Create, &[&nested_temp]),
            Translation::Ignore
        );
    }

    #[test]
    fn rename_both_yields_from_then_to_and_hides_the_temp_side() {
        let from = format!("{ROOT}/old.txt");
        let to = format!("{ROOT}/new.txt");
        assert_eq!(
            events(RawWatchKind::RenameBoth, &[&from, &to]),
            vec![
                ("old.txt".to_owned(), WatchEventKind::Rename),
                ("new.txt".to_owned(), WatchEventKind::Rename)
            ]
        );
        let temp = format!("{ROOT}/.rayito-tmp-xyz");
        assert_eq!(
            events(RawWatchKind::RenameBoth, &[&temp, &to]),
            vec![("new.txt".to_owned(), WatchEventKind::Rename)]
        );
    }

    #[test]
    fn an_atomic_write_landing_is_one_write_of_the_destination() {
        let temp = format!("{ROOT}/.rayito-tmp-abc123");
        let file = format!("{ROOT}/note.txt");
        let mut translator = translator();
        let mut seen = Vec::new();
        for raw in [
            raw(RawWatchKind::Create, &[&temp]),
            raw(RawWatchKind::DataModified, &[&temp]),
            raw(RawWatchKind::MetadataModified, &[&temp]),
            renamed(RawWatchKind::RenameFrom, &temp, 9),
            renamed(RawWatchKind::RenameTo, &file, 9),
        ] {
            seen.extend(typed(translator.translate(&raw)));
        }
        assert_eq!(seen, vec![("note.txt".to_owned(), WatchEventKind::Write)]);
        assert_eq!(
            typed(translator.translate(&renamed(RawWatchKind::RenameTo, &file, 9))),
            vec![("note.txt".to_owned(), WatchEventKind::Rename)],
            "a cookie lands once"
        );
    }

    #[test]
    fn interleaved_landings_pair_by_cookie_and_plain_moves_stay_renames() {
        let mut translator = translator();
        let first = format!("{ROOT}/.rayito-tmp-one");
        let second = format!("{ROOT}/sub/.rayito-tmp-two");
        let _ = translator.translate(&renamed(RawWatchKind::RenameFrom, &first, 1));
        let _ = translator.translate(&renamed(RawWatchKind::RenameFrom, &second, 2));
        assert_eq!(
            typed(translator.translate(&renamed(
                RawWatchKind::RenameTo,
                &format!("{ROOT}/sub/b.txt"),
                2
            ))),
            vec![("sub/b.txt".to_owned(), WatchEventKind::Write)]
        );
        assert_eq!(
            typed(translator.translate(&renamed(
                RawWatchKind::RenameTo,
                &format!("{ROOT}/a.txt"),
                1
            ))),
            vec![("a.txt".to_owned(), WatchEventKind::Write)]
        );
        let old = format!("{ROOT}/old.txt");
        let new = format!("{ROOT}/new.txt");
        let mut moved = Vec::new();
        moved.extend(typed(translator.translate(&renamed(
            RawWatchKind::RenameFrom,
            &old,
            3,
        ))));
        moved.extend(typed(translator.translate(&renamed(
            RawWatchKind::RenameTo,
            &new,
            3,
        ))));
        moved.extend(typed(translator.translate(&raw(
            RawWatchKind::RenameTo,
            &[&format!("{ROOT}/moved-in.txt")],
        ))));
        assert_eq!(
            moved,
            vec![
                ("old.txt".to_owned(), WatchEventKind::Rename),
                ("new.txt".to_owned(), WatchEventKind::Rename),
                ("moved-in.txt".to_owned(), WatchEventKind::Rename),
            ]
        );
    }

    #[test]
    fn pending_landings_are_bounded() {
        let mut translator = translator();
        let temp = format!("{ROOT}/.rayito-tmp-x");
        for cookie in 0..=MAX_PENDING_LANDINGS {
            let _ = translator.translate(&renamed(RawWatchKind::RenameFrom, &temp, cookie));
        }
        let file = format!("{ROOT}/f.txt");
        assert_eq!(
            typed(translator.translate(&renamed(RawWatchKind::RenameTo, &file, 0))),
            vec![("f.txt".to_owned(), WatchEventKind::Rename)],
            "the oldest pending landing was forgotten"
        );
        assert_eq!(
            typed(translator.translate(&renamed(
                RawWatchKind::RenameTo,
                &file,
                MAX_PENDING_LANDINGS
            ))),
            vec![("f.txt".to_owned(), WatchEventKind::Write)]
        );
    }

    #[test]
    fn root_removal_overflow_and_limits_end_the_watch() {
        assert_eq!(
            translate(RawWatchKind::Removed, &[ROOT]),
            Translation::End(WatchEnd::RootGone)
        );
        assert_eq!(
            translate(RawWatchKind::RenameFrom, &[ROOT]),
            Translation::End(WatchEnd::RootGone)
        );
        assert_eq!(
            translate(RawWatchKind::RootGone, &[]),
            Translation::End(WatchEnd::RootGone)
        );
        assert_eq!(
            translate(RawWatchKind::QueueOverflow, &[]),
            Translation::End(WatchEnd::Overflow)
        );
        assert_eq!(
            translate(RawWatchKind::LimitReached, &[]),
            Translation::End(WatchEnd::LimitReached)
        );
        assert_eq!(
            translate(RawWatchKind::MetadataModified, &[ROOT]),
            Translation::Ignore
        );
        assert_eq!(
            translate(RawWatchKind::MovedSelf, &[ROOT]),
            Translation::End(WatchEnd::RootGone)
        );
    }

    #[test]
    fn a_moved_followed_directory_reports_its_own_move_only_once() {
        let sub = format!("{ROOT}/sub");
        let mut translator = translator();
        translator.mark_followed(sub.clone());
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::RenameFrom, &[&sub]))),
            vec!["sub"]
        );
        assert_eq!(
            translator.translate(&raw(RawWatchKind::MovedSelf, &[&sub])),
            Translation::Ignore
        );
    }

    #[test]
    fn a_removed_followed_directory_reports_one_remove_and_a_recreated_one_again() {
        let sub = format!("{ROOT}/sub");
        let mut translator = translator();
        translator.mark_followed(sub.clone());
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::Removed, &[&sub]))),
            vec!["sub"]
        );
        assert_eq!(
            translator.translate(&raw(RawWatchKind::Removed, &[&sub])),
            Translation::Ignore,
            "IN_DELETE_SELF duplicates the parent's IN_DELETE"
        );
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::DirectoryCreated, &[&sub]))),
            vec!["sub"]
        );
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::Removed, &[&sub]))),
            vec!["sub"],
            "the new directory is not followed yet, so its removal is real"
        );
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::Removed, &[&sub]))),
            vec!["sub"],
            "not followed: nothing is deduplicated"
        );
        let mut interleaved = self::translator();
        interleaved.mark_followed(sub.clone());
        assert_eq!(
            names(interleaved.translate(&raw(RawWatchKind::Removed, &[&sub]))),
            vec!["sub"]
        );
        assert_eq!(
            names(interleaved.translate(&raw(RawWatchKind::DirectoryCreated, &[&sub]))),
            vec!["sub"]
        );
        assert_eq!(
            names(interleaved.translate(&raw(RawWatchKind::Removed, &[&sub]))),
            vec!["sub"],
            "a create in between resets the pending self removal"
        );
    }

    #[test]
    fn denied_paths_are_dropped_and_never_followed() {
        let mut translator = WatchTranslator::new("/", DenyList::default());
        assert_eq!(
            translator.translate(&raw(RawWatchKind::Create, &["/etc/passwd"])),
            Translation::Ignore
        );
        assert_eq!(
            names(translator.translate(&raw(RawWatchKind::RenameBoth, &["/etc/x", "/tmp/x"]))),
            vec!["tmp/x"]
        );
        assert_eq!(
            translator.created_paths(&raw(RawWatchKind::DirectoryCreated, &["/proc/1"])),
            Vec::<String>::new()
        );
    }

    #[test]
    fn created_paths_names_directory_creates_and_moves_in_under_the_root() {
        let translator = translator();
        let sub = format!("{ROOT}/sub");
        assert_eq!(
            translator.created_paths(&raw(RawWatchKind::DirectoryCreated, &[&sub])),
            vec![sub.clone()]
        );
        assert_eq!(
            translator.created_paths(&raw(RawWatchKind::RenameTo, &[&sub])),
            vec![sub.clone()]
        );
        assert_eq!(
            translator.created_paths(&raw(RawWatchKind::Create, &[&sub])),
            Vec::<String>::new()
        );
        assert_eq!(
            translator.created_paths(&raw(RawWatchKind::DirectoryCreated, &["/home/user/m3/x"])),
            Vec::<String>::new()
        );
    }

    #[test]
    fn other_kinds_are_ignored() {
        let file = format!("{ROOT}/a.txt");
        assert_eq!(
            translate(RawWatchKind::Other, &[&file]),
            Translation::Ignore
        );
    }

    #[test]
    fn a_root_of_slash_strips_only_the_leading_slash() {
        let mut translator = WatchTranslator::new("/", DenyList::default());
        assert_eq!(translator.canonical_root(), "/");
        match translator.translate(&raw(RawWatchKind::Create, &["/tmp/x"])) {
            Translation::Events(events) => assert_eq!(events[0].name, "tmp/x"),
            other => panic!("unexpected {other:?}"),
        }
    }
}
