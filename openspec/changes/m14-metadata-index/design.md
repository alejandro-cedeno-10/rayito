## Context

ADR-014 allows optional, customer-account components that are off by
default. The metadata index is the third one (after secrets): a DynamoDB
table that mirrors the immutable launch facts of each sandbox so the SDK can
filter by metadata without talking to the agent.

## Decisions

- **D1 — Join, never trust.** `list-microvms` stays the source of truth for
  which sandboxes exist and their state. A row only contributes metadata and
  only when `pk`, `image_arn` and `started_at_ms` (±1 s) match the listed item
  and the row has not expired. A forged or stale row cannot create a phantom
  sandbox; a missing row hides the sandbox from indexed listings (never
  probed, because probing is what wakes a paused VM).
- **D2 — Write once, conditionally, before readiness.** `create()` puts the
  row right after `run-microvm` with `attribute_not_exists(pk)`. Writing
  before readiness means a failed write never leaves a "ready" sandbox that
  cannot be found; the default policy terminates it (`keep_on_failure`
  respected). `'warn'` exists for callers that prefer availability.
- **D3 — No deletes.** `kill()` does not call `DeleteItem`: the join hides
  rows of dead sandboxes and the TTL (`startedAt + maximumDurationInSeconds
  + margin`) removes them for free. The SDK filters `expires_at < now` on
  read because TTL deletion lags by days.
- **D4 — Per-page batch.** One `BatchGetItem` per `list-microvms` page (≤ 50
  ids, chunked at 100 anyway) over the candidates that pass the state and
  date filters, with bounded retries of `UnprocessedKeys`; an undrained
  batch raises instead of returning a partial list.
- **D5 — Pool writes at refill.** `create(pool=, index=)` is rejected; the
  pool passes `PoolConfig.index` to every slot launch so parked (suspended)
  slots carry the pool metadata. Launch kwargs omit `index` when unset, so a
  pool without it launches exactly as in 0.4.0.
- **D6 — Token binding.** The listing fingerprint adds `"index": <table>`
  only when the index is effective (`metadata` + `index`); without it the
  canonical document, and therefore every 0.4.0 token and golden vector,
  is unchanged. Both SDKs share a golden vector (`ae65162235b32204`).
- **D7 — One type per concept.** `DynamoDbIndex` is both the option and the
  adapter (lazy boto3 client / lazy optional peer); the pure core
  (`IndexRecord`, `record_for`, `join_index`) has no I/O. Async Python runs
  the boto3 calls in `asyncio.to_thread`, sharing the listing core with sync.

## Risks

- **Clock/format skew of `startedAt`** between `run-microvm` and
  `list-microvms` (IDX-1): the ±1 s tolerance assumes both are the same
  timestamp; the e2e measures it before archive.
- **A writer can relabel** (T19): a principal with `PutItem` on the table can
  add a row for a live sandbox that has none (created without the index), so
  it appears in indexed listings with chosen metadata; it cannot overwrite a
  row the SDK wrote (conditional put) nor invent a sandbox (D1). Accepted:
  metadata is not an access control (T4), and reaching the sandbox still
  needs IAM and its access token. Writer and reader policies are separate.
