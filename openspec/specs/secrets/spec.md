# secrets Specification

## Purpose
TBD - created by archiving change m13-secrets. Update Purpose after archive.

## Requirements

### Requirement: Secrets are off by default and only explicit SDK options turn them on
The SDKs SHALL make zero Secrets Manager calls, build no boto3 `secretsmanager` client and import no `@aws-sdk/client-secrets-manager` module unless the caller passes `secrets=`/`secret_cache=` (TS `secrets`/`secretCache`) or calls a `SecretStore` (or E2B `Secret`) method. Constructing `SecretStore` or `SecretCache` SHALL NOT call AWS. No environment variable, configuration file or global setter SHALL enable the feature. In TypeScript `@aws-sdk/client-secrets-manager` SHALL be an optional peerDependency loaded only through `loadOptionalPeer`.

#### Scenario: a sandbox without secrets builds no Secrets Manager client
- **WHEN** `Sandbox.create()` runs commands and code without `secrets=`
- **THEN** no `secretsmanager` client is constructed (Python) and `loadOptionalPeer` is never called (TypeScript), and no shared secret cache is created

#### Scenario: constructing the configuration objects is free
- **WHEN** `SecretStore(...)` or `SecretCache(...)` is constructed
- **THEN** no AWS call is made and no client is built until the first method call

### Requirement: SecretStore is a CRUD over Secrets Manager restricted to AWS_API_NOTES.md §19
`SecretStore(region=, session=, prefix='rayito/', kms_key_id=)` (TS `new SecretStore({ region, credentials, prefix, kmsKeyId })`) SHALL resolve a name to `SecretId = prefix + name` (an `arn:` reference is used as-is), store values as `SecretString`, encode the integer version as `ClientRequestToken` `rayito-secret-version-{n:020d}` (1 on create, current + 1 on update, the current read from the `AWSCURRENT` entry of `VersionIdsToStages`), store metadata as `rayito:v1:` + compact JSON in `Description` validated to ≤ 2048 characters before calling AWS, and list with the `name` prefix filter and `IncludePlannedDeletion=false`. `destroy` SHALL call `DescribeSecret` first and return `False` without calling `DeleteSecret` when the secret does not exist or is scheduled for deletion; otherwise it SHALL delete with `ForceDeleteWithoutRecovery=true` and return `True`, or `False` if that delete reports `ResourceNotFoundException`. A create that fails because the name is still scheduled for deletion SHALL retry with the same token and exponential backoff with ±25 % jitter for at most one per-SDK constant budget of 60 s (`CREATE_RETRY_BUDGET_SECONDS` / `CREATE_RETRY_BUDGET_MS`) and then raise `SecretException`. Updating the same secret more often than once every 600 s in a process SHALL warn once. Only operations and parameters listed in `AWS_API_NOTES.md` §19 SHALL be used.

#### Scenario: create sends only the documented parameters
- **WHEN** `SecretStore(kms_key_id="alias/x").create("openai", value, metadata={"team": "ml"})` runs against a botocore `Stubber`
- **THEN** `CreateSecret` receives exactly `Name`, `SecretString`, `Description`, `KmsKeyId` and `ClientRequestToken` with version 1

#### Scenario: update writes version n + 1
- **WHEN** the current version is 2 and `update` is called
- **THEN** `DescribeSecret` is followed by `PutSecretValue` with the version-3 token, and `UpdateSecret(Description)` only when `metadata` is given

#### Scenario: update after an external rotation skips past Rayito's versions
- **WHEN** `AWSCURRENT` is a version Rayito did not write and version 2 (a Rayito token) is still listed with `AWSPREVIOUS`
- **THEN** `update` writes the version-3 token, never re-using token 1

#### Scenario: errors never repeat the name or the value
- **WHEN** any Secrets Manager call fails
- **THEN** the raised error (and its cause) contains neither the secret name nor its value, maps by AWS error code (`ResourceNotFoundException` → `SecretNotFoundException`, `ThrottlingException` → `RateLimitException`, others → `SecretException`)

#### Scenario: create outlasts the name-reuse delay measured on AWS
- **WHEN** a fake clock makes `CreateSecret` answer `InvalidRequestException` "scheduled for deletion" until 19.3, 26.8, 27.9 or 45 s have passed, with the jitter source at 0, 0.5 or 1
- **THEN** `create` succeeds with version 1 before the 60 s budget, and with a name that never frees it raises `SecretException` (`aws_code` `InvalidRequestException`) at exactly 60 s without naming the secret

#### Scenario: destroy of a name that never existed is False
- **WHEN** `destroy` (native or the E2B `Secret`/`AsyncSecret` shim) is called on a name that was never created, against a fake whose forced `DeleteSecret` succeeds for missing names like AWS
- **THEN** it returns `False` and no `DeleteSecret` is sent; on an existing secret it returns `True`, and a second call returns `False`

### Requirement: SecretCache never fetches a secret on every call
`SecretCache(ttl_seconds=300)` (TS `new SecretCache({ ttlSeconds: 300 })`) SHALL key values by (region, credentials identity, resolved `SecretId`, `VersionId` or `VersionStage`, `AWSCURRENT` by default), accept a TTL only in 1..86400 seconds, allow a single in-flight fetch per key, serve hits with zero AWS calls, refetch only on TTL expiry or explicit `refresh()`/`invalidate()`, never cache a not-found result, keep values only in process memory and never show them in `repr`/`str`/`toJSON`/`inspect`. Without `secret_cache=`, `secrets=` SHALL use one lazily created, process-wide cache per (region, session) with TTL 300.

#### Scenario: N reads within the TTL make one call
- **WHEN** a secret is read 50 times within its TTL
- **THEN** exactly one `GetSecretValue` is made; after the TTL the next read makes a second one

#### Scenario: concurrent readers share one fetch
- **WHEN** ten threads (or tasks, or promises) read the same key at once
- **THEN** exactly one `GetSecretValue` is made

#### Scenario: invalidating one secret keeps other in-flight reads shared
- **WHEN** a read of secret B is in flight and `invalidate(A)` is called
- **THEN** a second reader of B joins the in-flight read and only one `GetSecretValue` for B is made

#### Scenario: a zero TTL is rejected
- **WHEN** `SecretCache(ttl_seconds=0)` is constructed
- **THEN** it raises `InvalidArgumentException`

### Requirement: secrets= injects values through the existing per-call envs, never through the run payload
`secrets={"ENV": "name" | SecretRef}` on `create()` (also with `pool=`), both forms of `connect()`, `SandboxPool.take()`, and per call on `commands.run`, `pty.create`, `run_code` and `create_code_context` SHALL deliver the resolved values only in the per-call `envs` of `ProcessService`, `PtyService` and `CodeService`. The handle SHALL store only references. Each call SHALL merge handle and call secrets (the call wins on a repeated key) and SHALL reject a key present both in `envs` and in `secrets` with `InvalidArgumentException` naming the key. `run_code` with call-level `secrets=` on a non-Python language SHALL raise `InvalidArgumentException` pointing to `create_code_context(secrets=)`; handle secrets SHALL NOT be added to non-Python cells. The handle's secrets SHALL be resolved before `run-microvm` (or before a pool slot is claimed). Values SHALL never appear in the `runHookPayload`, `metadata`, tags, image env, pool slot records, `LaunchOptions`, logs, `repr` or errors, and `reincarnate()` SHALL reuse the references the handle holds at that moment (including those bound by `SandboxPool.take(secrets=)` or `connect(secrets=)`). `connect()` without `secrets=` SHALL keep the handle's references; a `secret_cache=` alone SHALL only replace the cache. The first use in a process SHALL emit a `RayitoCompatWarning` stating that the value is visible to sandbox code.

#### Scenario: three commands, one read
- **WHEN** a sandbox created with `secrets={"K": "name"}` runs three commands
- **THEN** each `StartRequest.envs["K"]` carries the value and Secrets Manager is read once

#### Scenario: the run payload stays clean
- **WHEN** `Sandbox.create(envs=..., secrets=...)` launches a MicroVM
- **THEN** the `runHookPayload` sent to `RunMicrovm` contains neither the value, the secret name nor the variable name

#### Scenario: connect with only a cache keeps the references
- **WHEN** a handle created with `secrets={"K": "a"}` is reconnected with `connect(secret_cache=other)`
- **THEN** `K` is still injected, now resolved through `other`

#### Scenario: reincarnate carries the references bound after create
- **WHEN** `connect(secrets={"G": "gh"})` (or `SandboxPool.take(secrets=)`) rebinds a handle and `reincarnate()` runs
- **THEN** the successor is created with `secrets={"G": SecretRef("gh")}` and the handle's cache

#### Scenario: a missing secret launches nothing
- **WHEN** `create(secrets={"K": "missing"})` runs
- **THEN** it raises `SecretNotFoundException` and `RunMicrovm` is never called

### Requirement: An optional CloudFormation template grants least-privilege secrets IAM
`infra/secrets-access.yaml` SHALL create only two `AWS::IAM::ManagedPolicy` resources (`RayitoSecretsReader`: `GetSecretValue`, `DescribeSecret`; `RayitoSecretsAdmin`: reader + `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DeleteSecret` on `secret:<SecretPrefix>*`, and `ListSecrets` on `*`), with KMS statements only when `KmsKeyArn` is set and conditioned on `kms:ViaService = secretsmanager.<region>.amazonaws.com`. `RayitoSecretsReader` SHALL deny `GetSecretValue` on `secret:rayito/webhooks/*` (the webhook signing secrets), and `SecretPrefix` SHALL be required to end in `/`. It SHALL never be deployed automatically and SHALL pass `cfn-lint`.

#### Scenario: the template is policies only
- **WHEN** `scripts/tests/test_secrets_template.py` parses the template
- **THEN** every resource is a managed policy, every action is on the allow list, only `ListSecrets` uses `Resource: "*"`, and KMS statements live behind `HasKmsKey` with `kms:ViaService`

#### Scenario: the reader never reads a webhook signing secret
- **WHEN** the reader policy's statements are read
- **THEN** a `Deny` of `secretsmanager:GetSecretValue` covers `secret:rayito/webhooks/*`, and `SecretPrefix=rayito` (no slash) fails the parameter pattern

### Requirement: Secret values never reach Rayito's logs and the docs warn about AWS SDK debug logs
Rayito SHALL NOT write a secret value or name to its own loggers (`rayito.*` in Python, the `logger` option in TypeScript). `docs/site/docs/secrets.md` and the T18 security notes SHALL warn that enabling botocore/urllib3 DEBUG logging (Python) or a Secrets Manager client logger (TypeScript) prints request and response bodies with the `SecretString`. The SEC-10 e2e SHALL capture only Rayito's logger.

#### Scenario: the SEC-10 e2e ignores botocore's DEBUG bodies
- **WHEN** `test_secrets_e2e.py` runs against real AWS
- **THEN** `caplog` is set to DEBUG only for the `rayito` logger and neither the value nor the name appears in the captured text

### Requirement: The docs state that secret listing is eventually consistent
`docs/site/docs/secrets.md` and the `list()` docstring/TSDoc SHALL state that `ListSecrets` may take ~3–5 s to show a just-created or updated secret (while `DescribeSecret`-based calls do not), and that tests creating then listing SHALL poll with a deadline.

#### Scenario: the listing note is present
- **WHEN** a reader opens the "Versiones y metadatos" section of `secrets.md`
- **THEN** it says `list()` is eventually consistent (~3–5 s) and that tests must poll

### Requirement: secrets= never reads a webhook signing secret
`SecretStore.read_value`/`readValue`, the read path of `secrets=` and `SecretCache`, SHALL refuse with `InvalidArgumentException`/`InvalidArgumentError`, before any AWS call and without naming the secret, any name or ARN that resolves under `rayito/webhooks/` (`WEBHOOK_SECRET_PREFIX`, one definition per SDK).

#### Scenario: a webhook signing secret is refused
- **WHEN** `SecretCache.get("webhooks/prod")` is called with the default prefix
- **THEN** `InvalidArgumentException` is raised and `GetSecretValue` is never called
