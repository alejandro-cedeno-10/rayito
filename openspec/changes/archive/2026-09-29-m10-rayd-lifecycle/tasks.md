## 1. Pure freeze verdict in core (rayd-timeout-watcher-125)

- [x] 1.1 `sandbox_timeout::freeze::{is_thaw, was_frozen}` with the ADR-011
  rationale, re-exported from `sandbox_timeout`.
- [x] 1.2 Table tests: `is_thaw` for `None`, under, exactly and over the
  threshold; `was_frozen` over every (tick gap x thaw gap) combination.
- [x] 1.3 `TickRecord::tick` / `frozen` delegate; atomics, `NEVER`,
  `since()` and ms truncation unchanged. Existing watcher tests pass.

## 2. Two freeze rules (rayd-timeout-watcher-125b)

- [x] 2.1 Reported in the proposal and the PR; not implemented
  (behaviour change at the boundary).

## 3. One Unix-ms conversion (rayd-core-machine-386)

- [x] 3.1 `clock::unix_millis` with `metrics.rs`'s body; its test moved
  next to it.
- [x] 3.2 `metrics::unix_millis` is a re-export; `health.rs` and
  `metrics_history.rs` untouched.
- [x] 3.3 `timeout_watcher.rs` imports `clock::unix_millis`; private copy
  deleted. `machine::wall_millis` untouched.

## 4. SetTimeout zero check (rayd-grpc-lifecycle-42)

- [x] 4.1 Reported; not implemented (status code change).

## 5. Deadline waker (rayd-grpc-lifecycle-58)

- [x] 5.1 Reported; deferred (needs `hooks/mod.rs` and `session.rs`).

## 6. FileMetadata from xattrs (rayd-core-metadata-61)

- [x] 6.1 Reported; not implemented (metadata returned would change).

## 7. Gates

- [x] 7.1 `cargo fmt --check`, `clippy -D warnings`, `cargo deny check`,
  `cargo test --workspace` as `tester` in the VM, `m9_egress` in a netns.
- [x] 7.2 `openspec validate m10-rayd-lifecycle --strict`.
