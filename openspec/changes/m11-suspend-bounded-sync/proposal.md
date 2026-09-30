## Why

Step 4 of the `/suspend` checklist called `sync(2)` through `spawn_blocking`
and awaited it with no bound (`crates/rayd/src/hooks/mod.rs`,
`flush_page_cache`). `sync(2)` flushes every filesystem in the guest, so one
hung network or FUSE mount (a hard NFS mount whose server is gone, a FUSE
daemon that stopped answering) or a very slow disk leaves it in `D` state.
`/suspend` then runs out of its window, and a `/suspend` that does not answer
in time makes AWS terminate the `MicroVM` (`AWS_API_NOTES.md`: the hook's
timeout is 1–60 s and a failing `/suspend` ends in `TERMINATED`). The hook
budget (80 % of the declared 30 s) still produced a 200, but after 24 s and
with a blocking-pool thread stuck forever, which the runtime also waits for
on shutdown.

Both research notes of M11 (on their research branches:
`docs/research/2026-10-e2b-out-of-scope.md`
§1.4 and §8.2, `docs/research/2026-10-efs-persistence.md` R12 and the
`/suspend` row of the lifecycle table) put this first, independent of any
volume work: a bounded, per-filesystem `syncfs` off the hook's critical path,
with a single `SuspendBudget` in the domain.

## What Changes

- `rayd_core::suspend_sync` (new, pure): `SuspendBudget` (sync deadline,
  `SUSPEND_SYNC_DEADLINE` = 5 s, clamped so grace + quiesce + deadline fit in
  half the hook budget), `parse_mountinfo`, `plan_sync` (writable, backed,
  one per device, skip filesystems whose previous sync is still running, cap
  64, `/` when the table is unreadable), `FlushReport` and the
  `FilesystemSync` port.
- `rayd::adapters::bounded_sync` (new): `PlatformFilesystemSync`
  (`/proc/self/mountinfo` + `syncfs(2)` on Linux) and `BoundedFlush`, which
  starts one detached `std::thread` per planned filesystem and stops waiting
  at the deadline; a fake `FilesystemSync` for tests.
- `rayd::hooks`: `/suspend` uses `BoundedFlush` instead of
  `flush_page_cache`; `router_with_flush` lets tests inject the flush;
  a hit deadline, failed syncs and skipped filesystems are logged with counts
  only. `router`/`router_with` keep their signatures.

No wire change: the hook reply, the gRPC contract and the SDKs are untouched.

## Impact

- Specs: `suspend-resume` (the two requirements that said "call `sync`"
  now point to the bounded sync; one added requirement).
- Code: `crates/rayd-core`, `crates/rayd`; `ARCHITECTURE.md` (step 4 of the
  `/suspend` checklist); `crates/rayd/CHANGELOG.md`
  (`Fixed`).
- Behaviour: a healthy guest syncs as before (every writable filesystem);
  a hung one costs `/suspend` at most 5 s instead of the whole hook budget.
  Pages of a filesystem whose sync did not finish may miss the checkpoint,
  which is logged; before, the whole VM was at risk.
- Real-AWS check (VOL-5 of the research plan, "`/suspend` with S3
  unreachable") stays with the volumes measurement campaign; nothing here
  needs AWS.
