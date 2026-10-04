## MODIFIED Requirements

### Requirement: An optional CloudFormation template grants least-privilege secrets IAM
`infra/secrets-access.yaml` SHALL create only two `AWS::IAM::ManagedPolicy` resources (`RayitoSecretsReader`: `GetSecretValue`, `DescribeSecret`; `RayitoSecretsAdmin`: reader + `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DeleteSecret` on `secret:<SecretPrefix>*`, and `ListSecrets` on `*`), with KMS statements only when `KmsKeyArn` is set and conditioned on `kms:ViaService = secretsmanager.<region>.amazonaws.com`. `RayitoSecretsReader` SHALL deny `GetSecretValue` on `secret:rayito/webhooks/*` (the webhook signing secrets), and `SecretPrefix` SHALL be required to end in `/`. It SHALL never be deployed automatically and SHALL pass `cfn-lint`.

#### Scenario: the template is policies only
- **WHEN** `scripts/tests/test_secrets_template.py` parses the template
- **THEN** every resource is a managed policy, every action is on the allow list, only `ListSecrets` uses `Resource: "*"`, and KMS statements live behind `HasKmsKey` with `kms:ViaService`

#### Scenario: the reader never reads a webhook signing secret
- **WHEN** the reader policy's statements are read
- **THEN** a `Deny` of `secretsmanager:GetSecretValue` covers `secret:rayito/webhooks/*`, and `SecretPrefix=rayito` (no slash) fails the parameter pattern

## ADDED Requirements

### Requirement: secrets= never reads a webhook signing secret
`SecretStore.read_value`/`readValue`, the read path of `secrets=` and `SecretCache`, SHALL refuse with `InvalidArgumentException`/`InvalidArgumentError`, before any AWS call and without naming the secret, any name or ARN that resolves under `rayito/webhooks/` (`WEBHOOK_SECRET_PREFIX`, one definition per SDK).

#### Scenario: a webhook signing secret is refused
- **WHEN** `SecretCache.get("webhooks/prod")` is called with the default prefix
- **THEN** `InvalidArgumentException` is raised and `GetSecretValue` is never called
