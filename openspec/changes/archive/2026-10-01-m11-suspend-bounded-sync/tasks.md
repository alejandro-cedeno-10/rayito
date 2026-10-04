## 1. Domain: SuspendBudget and the sync plan

- [x] 1.1 `crates/rayd-core/src/suspend_sync.rs` tests first: default
  deadline inside the budget, clamping (60 s request, 800 ms budget),
  `mountinfo` parsing (escapes, read-only, optional fields), the plan (pseudo
  filesystems, bind mounts, read-only, in-flight skip, cap, `/` fallback),
  `FlushReport::deadline_hit`.
- [x] 1.2 `SuspendBudget`, `parse_mountinfo`, `plan_sync`, `FlushReport`
  and the `FilesystemSync` port.

## 2. Adapter: bounded per-filesystem syncfs

- [x] 2.1 `crates/rayd/src/adapters/bounded_sync.rs` tests with the fake
  `FilesystemSync`: all synced; a filesystem that blocks forever costs the
  deadline and no more; the next flush skips it without a second thread.
- [x] 2.2 `PlatformFilesystemSync` (`/proc/self/mountinfo`, `syncfs(2)`)
  and `BoundedFlush` (detached `std::thread` per filesystem, deadline wait).

## 3. /suspend

- [x] 3.1 `crates/rayd/src/hooks/mod.rs` test: with a fake flusher that
  blocks forever, `/suspend` answers 200 `changed` within half the hook
  budget and the repeated `/suspend` answers `unchanged` without waiting.
- [x] 3.2 Replace `flush_page_cache` with `BoundedFlush`; `router_with_flush`;
  count-only logs.
- [x] 3.3 Existing suspend tests (`m5_suspend_resume`, `m6_hooks`, hooks unit
  tests) unchanged and green, repeated 10 times.

## 4. Docs and gates

- [x] 4.1 `crates/rayd/CHANGELOG.md` `[Unreleased]` → `Fixed`; step 4 of the
  `/suspend` checklist in `ARCHITECTURE.md`.
- [x] 4.2 `openspec validate m11-suspend-bounded-sync --strict`.
- [x] 4.3 `cargo fmt --check`, `cargo clippy --workspace --all-targets -D
  warnings`, `cargo test --workspace --locked`, `cargo deny check` in the
  Lima VM.
