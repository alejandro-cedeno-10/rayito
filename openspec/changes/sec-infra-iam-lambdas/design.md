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

## Not in this change (follow-ups)

- Optional `rayito-signature` header with a timestamped HMAC and verify
  helpers (new public API).
- Optional `KmsKeyArn` for the events table, queues and stack secret, and a
  resource policy on the stack secret.
- Tag-based trust for protected base-image names (needs a product decision
  and AWS verification of tag condition keys); documented mitigation:
  publish every protected name in each region beforehand.
- SDK-side validation of `TransferPrefix`/`PersistencePrefix` overlaps
  (the `S3Prefix` default `rayito` would need to change first).
- Post-deploy `CodeSha256` verification of stack functions.
