## Why

The M9 architecture review (`docs/research/2026-09-m9-architecture-review.md`)
left several findings in the transfer path of `rayd` that are pure structure:
the same rule written twice, or a proof the domain already has thrown away
before the adapter needs it.

- The read-after-upload barrier's fast path reads a cached `armed` count
  that `admit`, `apply` and `cancel` each refresh by hand after touching the
  registry; a mutation that forgot the store would silently turn the barrier
  off (design D11).
- `run_import` and `run_export` each carry their own `Ending` enum and
  their own terminal publication block.
- The rule "retry a transient S3 answer `TRANSFER_REQUEST_RETRIES` times,
  then `s3_unavailable`" exists in core for the one-shot `GET` and again,
  inline, in the export adapter.
- `plan_parts` checks that the part count matches the presigned URLs and
  then returns bare ranges, so the adapter looks every URL up again behind
  an impossible `None` branch; `classify_put` refuses a multipart 200 without
  `ETag` and then returns `Stored { etag: Option }`, so the adapter backfills
  an empty `ETag` it can never get.
- The lowercase-hex helper exists twice (core registry and the adapter).

## What Changes

- `TransferHub::with_registry` in `crates/rayd/src/transfer/manager.rs`: the
  only way to mutate the registry; it refreshes the barrier's count before
  releasing the lock. `admit`, `apply` and `cancel` go through it.
- `TaskEnding` and `publish_ending` in the same file replace both `Ending`
  enums and both terminal publication blocks. Each direction keeps its own
  "transfer finished" log with its current fields.
- `rayd_core::transfer::RequestRetries` / `RetryDecision`: the retry budget
  of one request, used by the export `PUT` loop and the one-shot import;
  `one_shot_verdict` keeps its signature and meaning.
- `plan_parts` returns `Vec<PlannedPart { range, url }>`; `PutOutcome` splits
  into `Stored` (single `PUT`) and `PartStored { etag: String }`
  (`UploadPart`).
- `rayd_core::transfer::lower_hex` is public and the adapter's copy is gone.

No behaviour change: same gRPC codes and messages, same states, same log
fields and tokens, same retries and requeues.

Not done here, reported in the PR because each one changes something a
client or a caller can observe: treating a refused `start_running` the same
way in import and export, refusing mutations on terminal records, moving the
barrier into the managers, one phase-admission rule for the three M9
services, and one language for wire-visible messages. Removing
`transfer::poll::unix_millis` in favour of `rayd_core::clock::unix_millis`
waits for that helper to land on `main`.

## Impact

- Affected specs: none (no requirement text changes).
- Affected code: `crates/rayd/src/transfer/{manager,import,export}.rs`,
  `crates/rayd-core/src/transfer/{export,import,registry,mod}.rs`.
- No `.proto`, SDK or image contract change.
