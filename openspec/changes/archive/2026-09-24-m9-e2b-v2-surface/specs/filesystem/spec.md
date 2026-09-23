## MODIFIED Requirements

### Requirement: Watch events carry relative names and the E2B event types
Each `FilesystemEvent` SHALL carry `name` relative to the watched directory (`sub/n.txt` in recursive mode, never absolute) and `type` mapped as create → `CREATE`, data modification → `WRITE`, metadata change → `CHMOD`, deletion → `REMOVE`, rename → `RENAME` (one event per affected name, `from` before `to`); close-write events SHALL not be surfaced; events whose name starts with `.rayito-tmp-` SHALL be dropped. The rename that lands a `Write` (or `StartImport`) temporary over its destination SHALL be recognised by the inotify cookie shared with the temporary's `MOVED_FROM` and surface as a single `WRITE` of the destination name, never as `RENAME`, so a `Write` RPC into a watched directory yields exactly one event for that name, of type `WRITE`, like the `files.write` example of E2B's watch docs; any other rename, including a move into the directory from outside and `files.rename`, SHALL stay `RENAME`. At most 64 unmatched temporary renames SHALL be remembered per watch, the oldest forgotten first. When `include_entry` is true, `entry` SHALL be filled for `CREATE`, `WRITE` and `CHMOD` if the `lstat` at emission time succeeds.

#### Scenario: shell create, write, remove
- **WHEN** `files.watch_dir("/home/user/m3/watch")` is active and `commands.run` executes `echo hola > a.txt; sleep 0.3; echo mas >> a.txt; sleep 0.3; rm a.txt` inside that directory
- **THEN** within 5 s `get_new_events()` has yielded `("a.txt", CREATE)`, then at least one `("a.txt", WRITE)`, then `("a.txt", REMOVE)` in that relative order, with no name containing `/` or starting with `.rayito-tmp-`

#### Scenario: atomic write and chmod with entries
- **WHEN** a watch with `include_entry=True` is active and the SDK calls `files.write(".../watch/w.txt", b"w")` and then `commands.run("chmod 600 .../watch/w.txt")`
- **THEN** a `("w.txt", WRITE)` event with `entry` set arrives, no `("w.txt", RENAME)` and no `.rayito-tmp-` name appears, and a `("w.txt", CHMOD)` event arrives with `entry.mode == 0o600`

#### Scenario: plain rename stays a rename
- **WHEN** a watch is active and `a.txt` inside the directory is renamed to `b.txt` with `rename(2)`
- **THEN** `("a.txt", RENAME)` then `("b.txt", RENAME)` arrive

#### Scenario: recursive subdirectory
- **WHEN** a watch with `recursive=True` is active and `mkdir sub && echo x > sub/n.txt` runs inside the directory
- **THEN** events `("sub", CREATE)` and `("sub/n.txt", CREATE)` arrive within 5 s
