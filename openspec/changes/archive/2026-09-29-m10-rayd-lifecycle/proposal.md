## Why

The M9 architecture review (`docs/research/2026-09-m9-architecture-review.md`)
left five findings on `rayd`'s lifecycle side. The one that matters most is
structural: the "was the VM really frozen" verdict that backs ADR-011's
guarantee against a forged `/suspend` + `/resume` pair is computed inside
the watcher thread's adapter (`TickRecord::tick` / `frozen` in
`crates/rayd/src/lifecycle/timeout_watcher.rs`), so it cannot be
table-tested as a pure domain rule. Separately, the same SystemTime to
Unix-milliseconds conversion is copied in `metrics.rs` and
`timeout_watcher.rs` (a third copy in `transfer::poll` is removed by
`m10-rayd-transfer` against the function this change adds).

## What Changes

- `rayd_core::sandbox_timeout::freeze` gains two pure functions,
  `is_thaw(since_last_tick, threshold)` (gap `>=` threshold) and
  `was_frozen(since_tick, since_thaw, threshold)` (no tick for a threshold,
  or a thaw seen less than a threshold ago), re-exported from
  `sandbox_timeout`, with the ADR-011 rationale and table tests.
  `TickRecord` keeps its atomics, the `NEVER` sentinel, `since()` and the
  millisecond truncation, and delegates both comparisons.
- `rayd_core::clock::unix_millis` is the single SystemTime to Unix-ms
  conversion (pre-epoch clamps to 0, saturates at `i64::MAX`, truncates).
  `metrics::unix_millis` becomes a re-export so its callers compile
  unchanged; the private copy in `timeout_watcher.rs` is deleted.
- No wire, status code, log field or timing changes.

## Reported, not implemented

- **Two freeze rules** (`suspend_watchdog.rs:42`, `elapsed >` 5 s, vs the
  watcher's `>=` 2 s): unifying them changes the stale-suspend recovery
  verdict at the boundary and needs a decision on the watchdog's threshold.
- **`SetTimeout(timeout_ms = 0)`** (`grpc/lifecycle.rs:42`): the adapter's
  `INVALID_ARGUMENT` pre-empts the domain's phase check; removing it
  changes the status code (`FAILED_PRECONDITION` `lifecycle_unmanaged` on
  an unmanaged sandbox) and the message.
- **`TimeoutWatcher::wake()` after every deadline move**
  (`grpc/lifecycle.rs:58`, hooks `/run` and `resume_deadline`): a complete
  fix touches `hooks/mod.rs` (owned by `m10-rayd-network` in this wave) and
  `SandboxSession`; a waker port for one transport only would be a half
  abstraction. Deferred to one change after this wave.
- **`FileMetadata::from_xattrs`** (`filesystem/metadata.rs:60`) caps the
  key count but not `METADATA_MAX_BYTES`, and lowercase-colliding keys
  overwrite silently; enforcing either changes the metadata returned for
  existing files.
- `sandbox_timeout::machine::wall_millis` is left alone: it computes wall
  time at a monotonic instant with signed nanosecond flooring, a different
  operation from `unix_millis`.

## Impact

- Code: `crates/rayd-core/src/sandbox_timeout/{mod,freeze}.rs`,
  `crates/rayd-core/src/clock.rs`, `crates/rayd-core/src/metrics.rs`,
  `crates/rayd/src/lifecycle/timeout_watcher.rs`.
- Behaviour: none. Public API: additive (`sandbox_timeout::freeze`,
  `clock::unix_millis`); `metrics::unix_millis` still resolves.
- Ordering: merge before `m10-rayd-transfer`, which deletes
  `transfer::poll::unix_millis` against `clock::unix_millis`.
