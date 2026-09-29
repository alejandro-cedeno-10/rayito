## 1. Tests that pin today's behaviour

- [x] 1.1 `crates/rayd/src/transfer/manager.rs` unit tests: the armed count
  after admit, after `start_running` + `finish_done`, after a cancel and a
  second (idempotent) cancel, and after `apply` on an unknown id.
- [x] 1.2 Same module: `publish_ending` returns the expected (phase, reason)
  pair for `Done`, `Failed`, `Cancelled`, `Terminated` and leaves the
  registry in the expected phase (`Cancelled` touches nothing).
- [x] 1.3 `rayd-core` `transfer::import`: `RequestRetries` retries exactly
  `TRANSFER_REQUEST_RETRIES` times, then gives up with `s3_unavailable`;
  `attempt()` counts 1, 2, 3; `one_shot_verdict` for every outcome at 0 and
  at the limit.
- [x] 1.4 `rayd-core` `transfer::export`: `plan_parts` pairs part n with the
  target's URL n (1 and 3 parts); `classify_put` single 200 -> `Stored`,
  multipart 200 with `ETag` -> `PartStored`.
- [x] 1.5 `rayd-core` `transfer::registry`: `lower_hex` writes two lowercase
  digits per byte, like the adapter copy it replaces.

## 2. Refactor

- [x] 2.1 `TransferHub::with_registry`; `admit`, `apply`, `cancel` use it.
- [x] 2.2 `TaskEnding` + `publish_ending`; import and export `drive` return
  it; each keeps its own "transfer finished" log and import keeps its
  cleanup before publication.
- [x] 2.3 `RequestRetries` / `RetryDecision` in core, re-exported; export
  `PUT` loop and import `next_wait` use it.
- [x] 2.4 `PlannedPart`, `plan_parts -> Vec<PlannedPart>`, `PutOutcome::
  {Stored, PartStored}`; the export adapter drops its URL lookup and its
  `ETag` backfill.
- [x] 2.5 `lower_hex` public in core, re-exported, adapter copy removed.
- [ ] 2.6 Replace `transfer::poll::unix_millis` with
  `rayd_core::clock::unix_millis` (waits for that helper on `main`).

## 3. Gates

- [x] 3.1 `cargo fmt --all --check`, `cargo clippy --workspace --all-targets
  --locked -- -D warnings`, `cargo deny check`.
- [x] 3.2 `cargo test --workspace --locked --offline -j 2` as `tester` in
  the Lima VM (includes `m9_transfer.rs` unchanged); `m9_egress` as root in
  a netns.
- [x] 3.3 SDK and scripts gates; `openspec validate m10-rayd-transfer
  --strict`.
