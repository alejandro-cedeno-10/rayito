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

## 5. Acceptance

- [ ] 5.1 `events-webhooks` acceptance on real AWS (deploy, forward,
  deliver, reconcile) with the new templates, before archiving.
