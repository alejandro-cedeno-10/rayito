## ADDED Requirements

### Requirement: Filesystem operations act on the object the deny list checked
Every `FilesystemService` operation (and the S3 export and import) SHALL
resolve the request path once, with the deny list checked on that canonical
string, and SHALL NOT resolve the string again by name: the agent SHALL open
the parent of the canonical path one component at a time from `/` without
following a symlink in any component, and SHALL reach the final component
with calls relative to that descriptor. A component that is a symlink when
the operation runs, or a directory on `proc`, `sysfs` or `devpts`, SHALL be
refused with the deny-list error, never followed. Writes SHALL create their
temp file exclusively inside that directory and commit by renaming within
it; recursive removal SHALL never follow a symlink at any depth.

#### Scenario: a component swapped after the check is refused
- **WHEN** code in the sandbox replaces a directory of an already
  deny-checked path with a symlink into `/etc` before the operation runs
- **THEN** `Stat`, `Read`, an export, `ListDir`, `WatchDir`, `Remove`,
  `Move`, `MakeDir`, `Write` and an import of that path answer the deny-list
  error, nothing under `/etc` is read, created, moved or removed, and no temp
  file is left behind

#### Scenario: a kernel filesystem is refused by descriptor
- **WHEN** an operation reaches a directory on `proc` or `sysfs`
- **THEN** it answers the deny-list error whatever path led there

#### Scenario: recursive removal unlinks symlinks without following them
- **WHEN** a recursive `Remove` empties a tree containing symlinks to
  directories outside it
- **THEN** the tree and the links are removed and the link targets are
  untouched
