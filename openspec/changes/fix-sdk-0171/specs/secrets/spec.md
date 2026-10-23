## RENAMED Requirements

- FROM: `### Requirement: Secret values never reach Rayito's logs and the docs warn about AWS SDK debug logs`
- TO: `### Requirement: Secret values never reach Rayito's logs nor botocore's DEBUG bodies`

## MODIFIED Requirements

### Requirement: Secret values never reach Rayito's logs nor botocore's DEBUG bodies
Rayito SHALL NOT write a secret value or name to its own loggers (`rayito.*` in Python, the `logger` option in TypeScript). `import rayito` SHALL install on `botocore.endpoint`, `botocore.parsers`, `botocore.auth`, `botocore.regions`, `boto3.resources.action`, `boto3.resources.collection`, `boto3.dynamodb.table`, `s3transfer.tasks` and `s3transfer.futures` a filter that drops their records below `INFO`, the records that carry request parameters, request and response bodies or signed headers, so a root logger at DEBUG or `boto3.set_stream_logger()` no longer prints a `SecretString` nor the `Item` of a resource `put_item`; their warnings and errors and every other logger SHALL be unchanged. `docs/site/docs/secrets.md` and the T18 security notes SHALL describe that filter and SHALL warn that a Secrets Manager client logger set by the caller (TypeScript) prints request and response bodies with the `SecretString`. The SEC-10 e2e SHALL capture only Rayito's logger.

#### Scenario: the SEC-10 e2e ignores botocore's DEBUG bodies
- **WHEN** `test_secrets_e2e.py` runs against real AWS
- **THEN** `caplog` is set to DEBUG only for the `rayito` logger and neither the value nor the name appears in the captured text

#### Scenario: a DEBUG root logger never sees a value in boto3 resource or transfer calls
- **WHEN** the root logger is at DEBUG and boto3 calls `Table.put_item`, a `batch_writer` and `upload_fileobj` with `Metadata` against a local HTTP server after `import rayito`
- **THEN** boto3 records are captured but no botocore, boto3 or s3transfer record contains the value or the secret name

#### Scenario: a DEBUG root logger never sees a secret body
- **WHEN** the root logger is at DEBUG and boto3 calls `PutSecretValue` and `GetSecretValue` against a local HTTP server after `import rayito`
- **THEN** botocore records are captured but none contains the secret value
