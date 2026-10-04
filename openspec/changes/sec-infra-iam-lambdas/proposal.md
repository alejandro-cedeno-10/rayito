## Why

A security sweep of the infrastructure templates and the `events-webhooks`
Lambdas found gaps where a lower-trust principal could degrade or subvert a
higher-trust one:

- The forwarder parsed an event line before verifying its MAC and caught
  only some parse errors, so one unauthenticated line could fail a whole
  CloudWatch Logs batch, and the asynchronous invocation had no OnFailure
  destination: genuine lifecycle events of the same stream were dropped.
- A MAC-valid event was trusted field by field (no freshness bound, any
  `event_id`, any `kill_reason`, any `image_arn`), and the delivery dedupe
  was keyed by `event_id` alone.
- One webhook with an unparsable URL or a trickling receiver failed the
  stream batch for every other webhook.
- Optional-stack Lambda code was reused on `HeadObject` alone, at a public,
  predictable key in the bucket root.
- `CallerPolicy`, which the docs tell users to attach to their service role,
  also grants image publishing; `RayitoTemplateBuilder` could read the whole
  artifact bucket; `RayitoSecretsReader` exposed the webhook signing
  secrets to `secrets=`; several roles had account-wide log writes and
  unscoped table writes.
- Guest code can drive `rayd`'s `/suspend`/`/resume` hooks over loopback,
  so authentic `paused`/`resumed` events could be emitted at will, and the
  reconciler's full-table scan let one sandbox's flood degrade all of them.
- The events stack had one caller policy for every job, warm deliverers
  kept a rotated webhook secret forever, the delivery signature had no
  timestamp, and the sizes guard could be bypassed by rebuilding an
  allowed image at a larger size. `SECURITY.md` never got the planned
  T20, T23, T26 and T27 rows.

## What Changes

- Forwarder: MAC first (key from the stream's sandbox id), strict parse,
  `rayd`-only fields, a 24 h / 5 min freshness window, per-line isolation,
  write failures retried and then sent to a new `ForwarderFailuresQueue`.
- Delivery dedupe keyed by `DELIVERY#<sandbox_id>#<event_id>`.
- Deliverer: an unparsable URL is a permanent failure of that webhook, any
  other error is contained to its webhook, and the attempt deadline covers
  DNS and every read. `register_webhook`/`registerWebhook` validate the URL
  with shared vectors.
- `OptionalStacks.deploy()`: artifacts under `rayito/stacks/<component>/`,
  `ExpectedBucketOwner`, content compared before reuse, `ChecksumSHA256`.
- `infra/iam.yaml`: `SandboxLauncherPolicy` and `ImagePublisherPolicy`,
  `CallerPolicy` kept as their union; the execution role's artifact `Deny`
  is unconditional.
- `infra/templates.yaml`: base-image reads limited to `rayito/*`.
- `infra/secrets-access.yaml`: `Deny` on `rayito/webhooks/*` for the
  reader, `SecretPrefix` must end in `/`; the SDKs refuse webhook signing
  secrets in `secrets=`/`SecretCache`.
- `infra/events-webhooks.yaml`: `dynamodb:LeadingKeys` per role, logs only
  on `/aws/lambda/*`, `aws:SourceAccount` on the Scheduler trust.
- Forwarder admission (order plus a per-sandbox rate bucket), a sparse
  `open` index for the reconciler, a `FilterCriteria` on the deliverer.
- Deliverer: `rayito-signature` timestamped HMAC header, secret cache TTL
  and a re-read on 401/403.
- `infra/events-webhooks.yaml`: launcher, reader and webhook-admin
  policies (`EventsOperatorPolicy` deprecated), one log group per function.
- `infra/sizes-guard.yaml`: deny image publishing too.
- Docs: `SECURITY.md` T20, T22 (advisory `paused`/`resumed`, stack-wide
  webhooks), T23, T26, T27 and IAM section,
  security, IAM, setup, events, templates and secrets pages, CI role
  warning, `AWS_API_NOTES.md` §21 and §25.

## Impact

- Code: `infra/lambdas/events_webhooks/`, `infra/*.yaml`,
  `clients/python/src/rayito/{_stacks,_lifecycle_events,_secrets.py}`,
  `clients/typescript/src/{stacks,lifecycle-events,secrets}/`.
- Behaviour: `paused`/`resumed` beyond the bucket, and lines older than the
  last admitted one, are no longer stored; open sandboxes from before the
  upgrade enter the reconciler's index with their next event; deliveries
  in flight when the stack is updated may be repeated
  once (new dedupe key); a `SecretPrefix` without a trailing `/` no longer
  validates; `secrets=` refuses `rayito/webhooks/*`; register rejects URLs
  the deliverer could never reach.
- Runtime on AWS: template and Lambda changes need the `events-webhooks`
  acceptance on real AWS before this change is archived.
