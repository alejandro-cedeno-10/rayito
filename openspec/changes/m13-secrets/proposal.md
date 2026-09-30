## Why

E2B ships a `Secret`/`AsyncSecret` CRUD API (`e2b` 2.51.0, `e2b/secret/`)
and customers ask how to hand an API key to an agent's sandbox. Rayito had
neither: `e2b-parity.md` row 80 read "fuera por SPEC" (it assumed a hosted
secret store), row 90 had no exceptions, and the only native way was to put
the value in `envs=` of each command by hand — or, worse, in
`Sandbox.create(envs=)`, which travels in the `runHookPayload` (T4:
CloudTrail data events). The research read (`docs/research/2026-10-e2b-out-of-scope.md`
§2, phase 1 = options 1 + 2) shows both halves fit inside ADR-014 with no
Rayito-hosted server: the CRUD is plain AWS Secrets Manager in the
customer's account, and delivery can reuse the per-call `envs` fields that
`ProcessService`, `PtyService` and `CodeService` already carry over the
authenticated channel. The maintainer's hard requirement: **never fetch the
secret on every call** — a cache per (region, credentials, secret, version)
with a TTL, refreshed only on expiry or explicit refresh.

## What Changes

- **`AWS_API_NOTES.md` §19 (Secrets Manager contract).** The seven
  operations and only the parameters the SDKs use (`CreateSecret`,
  `PutSecretValue`, `GetSecretValue`, `DescribeSecret`, `UpdateSecret`,
  `ListSecrets`, `DeleteSecret`), verified offline against botocore 1.43.103
  and `@aws-sdk/client-secrets-manager` 3.1140.0; error codes, limits
  (64 KiB, 2048-char `Description`, 32–64-char `ClientRequestToken`, 100
  versions + last 24 h, the 10-minute write recommendation), prices and IAM
  per operation. SEC-9 (recreate after forced delete, token clash) is marked
  "A MEDIR" in the e2e.
- **Native API, Python `rayito/_secrets.py` and TypeScript
  `src/secrets/{names,store,cache,inject}.ts`.** `SecretRef`, `SecretStore`
  (`create`/`update`/`get_info`/`exists`/`list`/`destroy`; integer version in
  `ClientRequestToken` `rayito-secret-version-{n:020d}`; metadata as
  `rayito:v1:` JSON in `Description`; forced delete; bounded create retry
  while a name is still being deleted; a one-time warning for updates more
  frequent than every 600 s), `SecretCache` (TTL 1..86 400 s, 300 default,
  single-flight per key, misses never cached, values only in process memory
  and masked in `repr`/`toJSON`), `SecretException`/`SecretNotFoundException`
  (TS `SecretError`/`SecretNotFoundError`). TypeScript loads
  `@aws-sdk/client-secrets-manager` as an **optional peerDependency** through
  `loadOptionalPeer`; Python uses boto3's `secretsmanager` client lazily.
- **Injection `secrets=` / `secret_cache=`** on `Sandbox.create()` (also with
  `pool=`), `connect()` (both forms), `SandboxPool.take()`, and per call on
  `commands.run` (fg/bg), `pty.create`, `run_code` (Python contexts only) and
  `create_code_context`, sync, async and TypeScript. The handle stores only
  references; each call merges handle ∪ call secrets (call wins), rejects a
  key also present in `envs`, and resolves from the cache. Values never go
  into the `runHookPayload`, `metadata`, tags, image env, pool records,
  `LaunchOptions`, logs or errors. First use emits a `RayitoCompatWarning`:
  the value is visible to sandbox code (phase 1). No rayd change, no new RPC,
  works on 0.4.0 images.
- **E2B shim.** `Secret`/`AsyncSecret`, `SecretInfo`, `SecretPaginator`/
  `AsyncSecretPaginator` (TS `Secret`, `SecretPaginator`) over `SecretStore`
  with E2B 2.51.0's names, arity and results; `fill()` returns the literal
  placeholder and nothing resolves it; `iam_token` stays
  `UnimplementedError`; `E2B(...).Secret` binds region/session. Divergences
  written in `e2b-compat.md`.
- **`infra/secrets-access.yaml`**: optional CloudFormation with two managed
  policies (`RayitoSecretsReader`, `RayitoSecretsAdmin`), KMS statements only
  with a key and `kms:ViaService`; `$0`.
- **Docs**: new `docs/site/docs/secrets.md`; `optional-features.md` rows
  1–2 → "disponible (0.5.0)" with examples; `e2b-parity.md` rows 56, 80
  (divergente), 90 (divergente: the Volume half has no API and
  `SecretException` extends `SandboxException`), the "qué hacer" footer and
  the status counts; `e2b-compat.md`; `SECURITY.md` T18 and
  `docs/site/docs/security.md`; `api.md`; `infra/README.md`.

## Migración

Nothing changes for code that does not opt in: every existing signature
keeps its defaults (`secrets=None`/`secret_cache=None`, TS `undefined`), no
boto3 `secretsmanager` client is built and no TypeScript module is imported
(tests prove it). Two visible changes for E2B-shim users: `Secret`/
`AsyncSecret`/`E2B(...).Secret` no longer raise `UnimplementedError` (they
now call Secrets Manager when invoked), and `RayitoCompatWarning` moved from
`rayito.e2b.exceptions` to `rayito.exceptions` (re-exported from the old
place: same class). TypeScript users who want secrets install
`@aws-sdk/client-secrets-manager` themselves (optional peer).

## Coste

- **Secret CRUD**: $0.40 per secret-month (prorated, **until `destroy`**) +
  $0.05 per 10,000 API calls (us-east-1, checked 2026-09-30). A customer
  managed KMS key adds KMS cost.
- **Injection**: one `GetSecretValue` per secret per TTL (300 s default) per
  process — ≤ 12/hour ≈ $0.04/month per secret; hits cost nothing. No new
  resource.
- **`infra/secrets-access.yaml`**: $0 (IAM only).
- **Off**: no option set = zero extra AWS resources and calls vs 0.4.0.

## Capabilities

### New Capabilities

- `secrets`: SecretStore CRUD over Secrets Manager, SecretCache, and the
  opt-in `secrets=` injection over the existing per-call `envs`, plus the
  optional IAM template.

### Modified Capabilities

- `e2b-compat`: `Secret`/`AsyncSecret`/`SecretInfo`/paginators/exceptions
  implemented over `SecretStore`; parity rows 56, 80, 90 updated.
- `security-docs`: new threat T18 (user secret custody).

## Impact

- **Python**: `rayito/_secrets.py` (new), `exceptions.py`, `__init__.py`,
  `_models.py` (`LaunchOptions.secrets`), `sandbox_{sync,async}/{main,
  commands,pty,code,pool}.py`, `e2b/{_secret.py (new),_unimplemented.py,
  _client.py,exceptions.py,__init__.py}`.
- **TypeScript**: `src/secrets/*` (new), `errors.ts`, `index.ts`,
  `sandbox/{sandbox,commands,pty,code}.ts`, `pool/pool.ts`, `e2b/{secret.ts
  (new),resources.ts,unimplemented.ts,errors via index.ts,client.ts,
  index.ts}`, `package.json` (optional peer + devDependency),
  `pnpm-lock.yaml`, `tsdown.config.ts`.
- **Infra**: `infra/secrets-access.yaml` (new), `infra/README.md`, `Makefile`
  (`infra-lint` lists the new template).
- **Tests**: `clients/python/tests/unit/test_secrets_*.py`,
  `test_e2b_secret_shim.py`, `fake_secrets.py`, the export/unimplemented
  tables of the existing e2b tests; `clients/python/tests/e2e/test_secrets_e2e.py`;
  `clients/typescript/tests/unit/secrets*.test.ts`, `secrets-fake.ts`, the
  existing e2b export tests; `scripts/tests/test_secrets_template.py`,
  `scripts/tests/test_security_docs.py` (T18).
- **No changes** to `crates/` (including `hooks/mod.rs`), proto, the
  `runHookPayload`, volumes, templates, the metadata index (M14), OTel
  (M13b), the loopback credential gateway, Parameter Store or a `rayito
  secret` CLI.
