## MODIFIED Requirements

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

## ADDED Requirements

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
