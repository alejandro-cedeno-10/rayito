## Decisions

1. **MAC before parse.** The forwarder takes the sandbox id from the log
   stream (after the last `]`), derives `k_sbx`, verifies the MAC over the
   raw payload, and only then parses. Consequence: a line signed with
   another sandbox's key into a victim's stream is now `mac_invalid`, not
   `sandbox_mismatch`; `sandbox_mismatch` remains for a valid line whose
   payload names a different sandbox than its stream, or a stream without a
   sandbox id. The stream name is documented as routing, not identity.
2. **Strict wire shape.** A JSON object; strings at most 512 characters;
   integers JSON integers (not bool, not float) in `[0, 2^64)`. Any other
   shape is `malformed_line`. `rayd`-only rules (32 lowercase hex
   `event_id`, `kill_reason` exactly `request` on `killed` and absent
   otherwise, a `microvm-image` ARN) live in `domain/forwarding.py`, not in
   `LifecycleEvent`, because reconciler-synthesized events legitimately
   carry `synthetic-…` ids and `unknown`.
3. **Freshness.** `MAX_EVENT_AGE_MS` = 24 h (covers the measured seconds of
   latency and Lambda's 6 h async retry window, half the 7-day dedupe TTL
   at most) and `MAX_CLOCK_SKEW_MS` = 5 min (AWS corrects the guest clock
   on resume, §15). Measured against the forwarder's own clock (injected),
   never the CloudWatch timestamp.
4. **Per-line isolation, write failures retried.** `decide` never raises;
   if it did, the line counts as `internal_error`. A failed table write
   does not stop the batch; after every line the invocation fails, Lambda
   retries it (writes are idempotent) and then sends it to
   `ForwarderFailuresQueue` (`AWS::Lambda::EventInvokeConfig`,
   `MaximumRetryAttempts: 2`). A separate queue from the deliverer's,
   because its messages carry log batches, not stream metadata.
5. **Delivery dedupe per sandbox.** `DELIVERY#<sandbox_id>#<event_id>`.
   The SDK never reads these rows, so no SDK mirror changes.
6. **Deliverer isolation.** `ValueError` (including `UnicodeError`) from
   the sender is `invalid_url`, not retried. Every other exception is
   contained to its webhook (`internal_error`), except the time budget,
   which still stops the batch. The attempt `timeout` becomes a deadline:
   DNS in a bounded worker pool, and every send/receive re-armed with the
   time left.
7. **Webhook URL validation** in the pure domain of both SDKs, with
   `testdata/lifecycle-events/webhook-url-vectors.json`. Raw URLs with
   whitespace, control characters or backslashes are refused because
   `urlsplit` and WHATWG `URL` disagree on them.
8. **Artifacts.** Key `rayito/stacks/<component>/<sha256>.zip`.
   `GetObject` with `ExpectedBucketOwner` (account from
   `GetCallerIdentity`), compare sha256, overwrite on mismatch (warn) or
   missing with `ChecksumSHA256`. Parameter names verified offline with
   `Stubber` (botocore 1.43.103) and `satisfies` against
   `@aws-sdk/client-s3`. A post-deploy `CodeSha256` check is not part of
   this change: it needs `lambda:GetFunction` for deployers and a new
   optional TypeScript peer.
9. **IAM split.** `SandboxLauncherPolicy` and `ImagePublisherPolicy` are new
   managed policies; `CallerPolicy` is unchanged and a template test pins
   that it equals their union, so existing attachments keep working.
   `PassRole` on the execution role keeps no `iam:PassedToService`
   condition (AWS_API_NOTES.md §10: unverified for `RunMicrovm`).
10. **Least privilege in `events-webhooks`.** `dynamodb:LeadingKeys` on
    every `PutItem`/`DeleteItem` and on the deliverer's `Query`; `Scan` and
    the operator's `gsi1` `Query` stay unscoped. Logs on
    `log-group:/aws/lambda/*`. `aws:SourceAccount` only on the Scheduler
    trust (documented by Scheduler); the Lambda trusts stay as they are
    until measured.

11. **Admission before storage.** `domain/admission.py` is pure: an event
    must move its sandbox strictly forward, ordered by `(generation, phase)`
    with `created`/`resumed` opening a generation and `paused` closing it;
    a gap is allowed so a refused or lost line never wedges a sandbox (the
    proposal of "generation == last + 1" would). Repeated `/suspend` never
    re-emits (`hooks::suspend`), so order alone cannot stop a forged loop:
    `paused`/`resumed` spend from a GCRA bucket (one field,
    `rate_tat_ms`; burst 20, one per 30 s) on the forwarder's clock, never
    the event's. The state row is written first, under
    `attribute_not_exists(revision) OR revision = :revision` after a
    consistent `GetItem` (three attempts, then `AdmissionContended`, a
    failed write); `last_event_id` lets the retry of a line whose event
    write failed store it. Trade-off: a line older than the last admitted
    one (an out-of-order retry) is now refused instead of delivered out of
    order; documented. The reconciler goes through the same `admit` after
    storing its event, so a failure in between is closed by the next run.
12. **Sparse `open` index.** `open_pk = "OPEN"` (range `pk`) on open state
    rows only; the tombstone drops it. One partition is enough at this
    stack's scale (each write is rate limited). Rows written before the
    upgrade enter the index with their next event; documented, no backfill
    scan.
13. **Deliverer filter.** `FilterCriteria` on `INSERT` + `NewImage.pk`
    prefix `EVENT#`; the handler keeps its own check.
14. **Secrets.** TTL cache of 300 s (`SECRET_CACHE_TTL_SECONDS`) plus
    `invalidate`; a 401/403 re-reads once and retries only if the value
    changed. `rayito-signature` is always sent (no cost, ignored by E2B
    consumers); verification is documented with Python and TypeScript
    receivers rather than shipped as SDK helpers (no new public API).
15. **Caller policies per job.** Launcher, reader (table `Query` with
    `LeadingKeys` `EVENT#*`, index `Query` in a separate statement so the
    index never depends on how `LeadingKeys` evaluates on a GSI) and
    webhook admin; `EventsOperatorPolicy` kept as the deprecated union.
    Per-function log groups via `LoggingConfig.LogGroup`; no retention set
    (same as Lambda's own groups).
16. **Sizes guard.** Deny the four image-publishing actions on `*`
    (stronger than per-ARN and simpler); the `imageVersion` residual is
    documented because `RunMicrovm` authorizes on the unversioned ARN.

## Not in this change (follow-ups)

- Authenticating the hook peer in `rayd` so a guest cannot drive
  `/suspend`/`/resume` (T2; the rate limit only bounds it).
- `ReservedConcurrentExecutions` on the forwarder: it fails to deploy in
  accounts whose unreserved concurrency would drop below the floor, which
  needs a product decision and a real-AWS check.
- Optional per-webhook filter (sandbox metadata or id prefix) for operators
  that want tenant-scoped webhooks; generating or enforcing strong webhook
  secrets in `register_webhook` (the SDK only sees the secret's name).
- Optional `KmsKeyArn` for the events table, queues and stack secret, and a
  resource policy on the stack secret.
- Tag-based trust for protected base-image names (needs a product decision
  and AWS verification of tag condition keys); documented mitigation:
  publish every protected name in each region beforehand.
- SDK-side validation of `TransferPrefix`/`PersistencePrefix` overlaps
  (the `S3Prefix` default `rayito` would need to change first).
- Post-deploy `CodeSha256` verification of stack functions.
