# metadata-index Specification

## Purpose
TBD - created by archiving change m14-metadata-index. Update Purpose after archive.

## Requirements

### Requirement: The index row holds only immutable, non-secret launch facts
`DynamoDbIndex` (Python `rayito._index`, TypeScript `src/index/**`) SHALL write, for a sandbox created with `index=`, exactly one item whose attributes are `pk` (S, the `microvmId`), `image_arn` (S), `image_version` (S), `started_at_ms` (N, the `startedAt` of `run-microvm`), `metadata` (M of S), `sdk` (S, `py/<version>` or `ts/<version>`) and `expires_at` (N, `floor(startedAt/1000) + maximumDurationInSeconds + ttl_margin_seconds`, 3600 by default). It SHALL never contain the access token or its hash, envs, secret references or values, the `runHookPayload` or the JWE. Every DynamoDB operation and parameter SHALL be listed in `AWS_API_NOTES.md` §20.

#### Scenario: allow-listed keys only
- **WHEN** `record_for(info, {"user": "42"})` is encoded as an item
- **THEN** its keys are exactly the seven attributes above, none contains `token`, `env`, `secret`, `payload`, `jwe` or `hash`, and `expires_at` equals startedAt seconds + max duration + margin

### Requirement: The index is opt-in, lazy and absent by default
`index=` / `index` SHALL default to `None` / `undefined` on `Sandbox.create()`, `Sandbox.list()`, `Sandbox.paginate()` (sync, async and TypeScript) and `PoolConfig`. Without it the SDK SHALL build no `dynamodb` client and SHALL NOT import `@aws-sdk/client-dynamodb`; constructing `DynamoDbIndex` SHALL make no AWS call; the client SHALL be created on first use (Python boto3 `dynamodb`; TypeScript the optional peer through `loadOptionalPeer`). No environment variable, file or global setter SHALL turn it on. The option's docstring/TSDoc SHALL carry a "Coste y activación" block.

#### Scenario: no client without the option
- **WHEN** a sandbox is created and listed without `index=`
- **THEN** no boto3 `dynamodb` client is built (Python) and `loadOptionalPeer` is never called with `@aws-sdk/client-dynamodb` (TypeScript)

### Requirement: create(index=) writes the row before readiness and fails closed by default
After `run-microvm` and before the readiness probe, `create(index=idx)` SHALL call `PutItem` with `ConditionExpression="attribute_not_exists(pk)"`. If it fails and `on_write_failure` is `"terminate"` (default) the SDK SHALL terminate the MicroVM (unless `keep_on_failure=True`) and raise `IndexWriteException` (TS `IndexWriteError`) before minting any proxy token; with `"warn"` it SHALL log a warning without metadata values and return the sandbox. `create(pool=, index=)` SHALL raise `InvalidArgumentException`; `PoolConfig(index=)` SHALL make every slot launch write its row with the pool metadata. `kill()` SHALL NOT touch the index. Before `run-microvm` (also on a pool refill), the SDK SHALL run an I/O-free preflight that builds the index client and SHALL raise `InvalidArgumentException` (TS `InvalidArgumentError`) if the index has no region or its optional peer is missing, without launching anything.

#### Scenario: a failed put terminates the VM
- **WHEN** `PutItem` answers `AccessDeniedException` during `create(index=idx)`
- **THEN** the fake control plane records one `TerminateMicrovm`, no `CreateMicrovmAuthToken`, and `IndexWriteException` is raised

#### Scenario: warn keeps the sandbox
- **WHEN** the same failure happens with `DynamoDbIndex(..., on_write_failure="warn")`
- **THEN** the sandbox is returned, a warning is logged and no `TerminateMicrovm` is recorded

#### Scenario: a configuration error fails before launch
- **WHEN** `create(index=idx)` runs and the index has no region (or, in TypeScript, `@aws-sdk/client-dynamodb` is not installed)
- **THEN** `InvalidArgumentException` (TS `InvalidArgumentError`) is raised by an I/O-free preflight and the fake control plane records no `RunMicrovm`

### Requirement: reincarnate() keeps the index of its create()
A sandbox created with `index=` (Python) / `index` (TypeScript) SHALL keep that index in its launch options, and `reincarnate()` SHALL pass it to the successor's `create()`, so the successor writes its own conditional row and stays visible to the indexed listing. Without `index` the successor SHALL make no DynamoDB call.

#### Scenario: the successor is indexed
- **WHEN** a sandbox created with `persist=`, `metadata={"user": "42"}` and `index=` is reincarnated
- **THEN** the fake table receives a second conditional `PutItem` for the successor's id and `list(metadata={"user": "42"}, index=)` returns the successor

### Requirement: list(metadata=, index=) joins without probing and never invents a sandbox
With both `metadata` and `index`, `list()`/`paginate()` SHALL page `list-microvms`, apply the existing filters, call `BatchGetItem` (≤ 100 keys per call, `ConsistentRead=false`, bounded retries of `UnprocessedKeys`, raising `SandboxIndexException` if they never drain) once per page for the candidate ids, and keep an item only if a non-expired row has the same id, image ARN and `startedAt` (±1 s) and its metadata contains every wanted pair; state SHALL come from `list-microvms`; an item without a row SHALL be excluded and never probed. It SHALL make no `Health`, `CreateMicrovmAuthToken` or `GetMicrovm` call and SHALL accept any non-terminal state (default: all), rejecting `TERMINATING`/`TERMINATED`. With `index` but no `metadata`, or without `index`, the listing SHALL be exactly the 0.4.0 path. The `next_token` fingerprint SHALL include the table name only when the index is effective.

#### Scenario: suspended matches without probes
- **WHEN** three sandboxes are created with `metadata={"user": "42"}` and the index, two are suspended, and the SDK calls `list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx)`
- **THEN** exactly those two are returned with state `SUSPENDED` and their metadata, and the control plane records zero `GetMicrovm` and `CreateMicrovmAuthToken` calls and no probe channel is opened

#### Scenario: the token is bound to the table
- **WHEN** a `next_token` from `paginate(metadata=, index=)` is passed to `paginate(metadata=)` without the index
- **THEN** `next_items()` raises `InvalidArgumentException` ("next_token no corresponde a estos filtros")

### Requirement: The CLI and the optional template expose the index explicitly
`rayito sandbox list` SHALL accept `--metadata K=V`, `--state S` and `--index-table NAME`; `--index-table` SHALL build a `DynamoDbIndex` on the CLI session, is off by default, and without `--metadata` SHALL be a usage error (exit 2) that makes no DynamoDB call. `infra/metadata-index.yaml` SHALL create only a `PAY_PER_REQUEST` table keyed by `pk` with TTL on `expires_at` and two managed policies (`RayitoIndexWriter`: `dynamodb:PutItem`; `RayitoIndexReader`: `dynamodb:BatchGetItem`) scoped to the table ARN, with no Lambda, stream or EventBridge, and SHALL pass `cfn-lint`.

#### Scenario: CLI with the flag
- **WHEN** `rayito --json sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes` runs against a fake plane and a fake index
- **THEN** it prints only the suspended sandbox whose row has `user=42`, with its metadata, and mints no token

#### Scenario: CLI flag without metadata
- **WHEN** `rayito sandbox list --index-table rayito-sandboxes` runs without `--metadata`
- **THEN** it exits with code 2 naming `--metadata` and builds no index

#### Scenario: template policies
- **WHEN** `scripts/tests/test_metadata_index_template.py` reads the template
- **THEN** the only data resource is the table and each policy has its single action on the table ARN, never `*`

### Requirement: Indexed listing accepted on real AWS
Before archive, the e2e (`RAYITO_E2E=1`, `RAYITO_E2E_INDEX_TABLE`) SHALL create three sandboxes with the index, pause two, and verify that the indexed listing returns exactly those two and that `get-microvm` still reports them `SUSPENDED` afterwards; IDX-1 SHALL be recorded in `AWS_API_NOTES.md` §20.

#### Scenario: paused sandboxes stay paused
- **WHEN** the e2e lists by metadata over `SUSPENDED` with the index
- **THEN** the two paused ids are returned and both are still `SUSPENDED` at `get-microvm` time
