## 1. Forwarder and deliverer (Lambda)

- [x] 1.1 Regression tests for the poison payloads, stale lines, `rayd`-only
  fields, per-line isolation and failed writes.
- [x] 1.2 `decide`: MAC first, strict parse, freshness; handler isolation.
- [x] 1.3 Delivery dedupe `DELIVERY#<sandbox_id>#<event_id>`.
- [x] 1.4 Deliverer: `invalid_url`, per-webhook isolation, attempt deadline
  (DNS and reads), with tests for each malformed URL and a trickling
  receiver.

## 2. Templates

- [x] 2.1 `events-webhooks.yaml`: `ForwarderFailuresQueue`,
  `ForwarderEventInvokeConfig`, `LeadingKeys`, logs on `/aws/lambda/*`,
  Scheduler `aws:SourceAccount`.
- [x] 2.2 `iam.yaml`: `SandboxLauncherPolicy`, `ImagePublisherPolicy`,
  union test, unconditional artifact `Deny`.
- [x] 2.3 `templates.yaml`: base-image reads on `rayito/*` only.
- [x] 2.4 `secrets-access.yaml`: webhook `Deny`, trailing-slash prefix.
- [x] 2.5 `ci-oidc-role.yaml` description and `infra/README.md` warning.

## 3. SDKs (Python and TypeScript)

- [x] 3.1 `register_webhook`/`registerWebhook` URL validation with shared
  vectors.
- [x] 3.2 `SecretStore.read_value`/`readValue` refuse webhook signing
  secrets.
- [x] 3.3 `put_artifact`/`putArtifact`: namespaced key, owner, content
  comparison, checksum.
- [x] 3.4 Regenerate stack assets.

## 4. Docs

- [x] 4.1 `SECURITY.md` T22 and IAM section; `security.md`, `operacion/iam.md`,
  `configurar-aws.md`, `eventos-y-webhooks.md`, `templates.md`,
  `secrets.md`; `AWS_API_NOTES.md` §21 and §25; CHANGELOGs.

## 5. Second security sweep

- [x] 5.1 Admission (`domain/admission.py`), conditional state write,
  reconciler through `admit`, with tests for the forged loop, order and
  contention.
- [x] 5.2 Sparse `open` index, reconciler `Query`, `FilterCriteria` on the
  deliverer, per-function log groups.
- [x] 5.3 Secret cache TTL, 401/403 re-read, `rayito-signature`.
- [x] 5.4 Launcher, reader and webhook-admin policies; deprecated union.
- [x] 5.5 Sizes guard image-publishing Deny.
- [x] 5.6 `SECURITY.md` T20, T22, T23, T26, T27; events, security, sizes,
  stacks pages; `AWS_API_NOTES.md` §25; CHANGELOGs.

## 6. Acceptance

- [x] 6.1 `events-webhooks` acceptance on real AWS (deploy, forward,
  deliver, reconcile) with the new templates, before archiving: the GSI
  added on update, `LoggingConfig`, `FilterCriteria`, the per-job
  policies and admission under a real suspend/resume loop.
  Partly verified in the 0.7.0 acceptance (2026-10-04, us-east-1): a
  fresh deploy reaches `CREATE_COMPLETE`; the per-job roles forward and
  deliver `created`, `paused`, `resumed` and `killed` to a public receiver
  with valid signatures; deliveries to private and link-local URLs end as
  `failed`; the deliverer logs to the stack's own log group
  (`LoggingConfig`). Completed 2026-10-06 (us-east-1, `AWS_API_NOTES.md`
  Q142): a stack deployed from the 0.6.1 template and seeded with real
  events (7 rows) was updated in place by `rayito stack deploy` to
  `UPDATE_COMPLETE` in 120 s with no failed resource, the same table and
  all 7 rows; the `open` index was added (CloudFormation finishes while it
  backfills, `ACTIVE` about 6.5 min later, queries served meanwhile). The
  new forwarder and deliverer handled `paused`/`resumed`/`killed` of a
  sandbox opened before the update with 0 `Errors` (its old row joins the
  index with its next event), and the reconciler ran on its schedule
  (2 invocations, 0 errors, `{"synthesized": 0}` in its own log group).

