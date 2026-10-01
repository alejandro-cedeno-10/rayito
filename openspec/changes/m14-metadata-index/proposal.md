## Why

`list-microvms` does not know a sandbox's metadata. Today
`Sandbox.list(metadata=...)` reads it from each agent with a `Health` probe
(O(n), one JWE and one channel per sandbox) and can only look at `RUNNING`
sandboxes: probing a suspended one would wake it. That leaves E2B's
`SandboxQuery(metadata=..., state=[PAUSED])` as `UnimplementedError`
(`e2b-parity.md` row 40, "fuera por SPEC": SPEC §4 excluded a client-side
metadata store). ADR-014 (M11) now allows optional components that live in
the customer's account and are off by default; the research read
(`docs/research/2026-10-e2b-out-of-scope.md` §5) shows a DynamoDB table with
one immutable row per sandbox, joined with `list-microvms`, answers the query
for any state at well under $0.10 per 10 000 sandboxes a month, with no
Rayito-hosted server and no change to `rayd`.

## What Changes

- **`AWS_API_NOTES.md` §20 (DynamoDB contract).** `PutItem` (`TableName`,
  `Item`, `ConditionExpression="attribute_not_exists(pk)"`) and
  `BatchGetItem` (`RequestItems{table: {Keys (1–100), ConsistentRead=false}}`
  → `Responses`, `UnprocessedKeys`; 16 MB), verified offline against botocore
  1.43.103 and `@aws-sdk/client-dynamodb` 3.1144.0; the TTL attribute format
  (seconds epoch) and the "within a few days" deletion caveat (rows are
  filtered on read); on-demand prices; error codes. No `DeleteItem`,
  `GetItem`, `Query`, `Scan` or `ProjectionExpression` (why: written there).
  IDX-1 marked "A MEDIR" for the e2e.
- **Pure core + adapter.** Python `rayito/_index.py` (`IndexRecord`,
  `record_for`, `join_index`, `DynamoDbIndex`); TypeScript
  `src/index/{record,join,dynamodb}.ts`. The row holds only immutable data
  written once after `run-microvm` (`pk`, `image_arn`, `image_version`,
  `started_at_ms`, `metadata`, `sdk`, `expires_at` = startedAt + max
  duration + `ttl_margin_seconds`), never the access token, its hash, envs,
  secrets, the payload or the JWE. The join keeps a listed item only if a
  non-expired row has the same id, image ARN and `startedAt` (±1 s) and its
  metadata contains every wanted pair; state always comes from
  `list-microvms`; an item without a row is excluded, never probed.
  `SandboxIndexException`/`IndexWriteException` (TS `SandboxIndexError`/
  `IndexWriteError`). The boto3 `dynamodb` client and the TS optional peer
  `@aws-sdk/client-dynamodb` (via `loadOptionalPeer`) are created lazily on
  first use.
- **SDK option `index=` / `index`** (default `None`/`undefined`) on
  `Sandbox.create()`, `Sandbox.list()`, `Sandbox.paginate()` (sync, async,
  TS) and `PoolConfig`. `create(index=)` puts the row after `run-microvm` and
  before readiness; on failure `on_write_failure='terminate'` (default)
  terminates the VM (unless `keep_on_failure=True`) and raises
  `IndexWriteException`, `'warn'` logs and continues. `create(pool=,
  index=)` is `InvalidArgumentException` (the pool writes on refill with
  `PoolConfig(index=)`). `list/paginate(metadata=, index=)`: no `Health`, no
  `CreateMicrovmAuthToken`, no `GetMicrovm`; every non-terminal state
  accepted; one `BatchGetItem` per `list-microvms` page; the `next_token`
  fingerprint binds the table (golden vector shared by both SDKs). `index`
  without `metadata`, and no `index`, are exactly the 0.4.0 path. `kill()`
  never touches the index.
- **E2B shim.** `Sandbox.list(query=SandboxQuery(metadata=, state=[PAUSED]),
  index=)` and `E2B(index=)` (TS `list({ query, index })`, `new E2B({ index
  })`) map to the indexed native listing; without an index the same
  `UnimplementedError`, whose reason now names the option.
- **CLI.** `rayito sandbox list --metadata K=V --state suspended
  --index-table NAME` (`--state` also works alone); without the flag the
  command is unchanged.
- **`infra/metadata-index.yaml`**: optional template with the table
  (`PAY_PER_REQUEST`, `pk` HASH, TTL on `expires_at`, default SSE, PITR and
  deletion protection off) and two managed policies (`RayitoIndexWriter`:
  `PutItem`; `RayitoIndexReader`: `BatchGetItem`) on the table ARN. No
  Lambda, stream or EventBridge. In `make infra-lint`.
- **Docs**: `observability.md` new section "Listado por metadatos con índice
  (opcional)"; `optional-features.md` row 3 → "implementado, pendiente de
  aceptación en AWS real (M14)" with Python/TS/CLI examples (→ "disponible
  (0.5.0)" once the real-AWS e2e passes); `e2b-parity.md` row 40 →
  divergente and the status counts (72/21/9/11); `e2b-compat.md`; `cli.md`;
  `api.md`; `SECURITY.md` T19 and `security.md`; `infra/README.md`; both
  package READMEs and CHANGELOGs.

## Migración

Nothing changes for code that does not opt in: every new parameter defaults
to `None`/`undefined`, no `dynamodb` client is built and no TypeScript
module is imported (tests prove it), and `list(metadata=)` without `index=`
keeps its 0.4.0 behaviour and `next_token` format. One visible change: the
reason text of the shim's `UnimplementedError` for
`list(state=PAUSED, query.metadata)` now mentions `index=`. Sandboxes created
before adopting the index have no row and are not returned by an indexed
listing (use the probe listing for those). TypeScript users install
`@aws-sdk/client-dynamodb` themselves (optional peer).

## Coste

- **Writes**: ~1 WRU per sandbox created with `index=` (item ≤ 1 KB),
  $0.625 per million WRU on-demand (us-east-1, checked 2026-09-30) ≈
  $0.000000625 each.
- **Reads**: 0.5 RRU per candidate sandbox (eventually consistent),
  $0.125 per million RRU; one `BatchGetItem` per `list-microvms` page.
- **Storage**: $0.25/GB-month after 25 GB free; rows are < 1 KB and expire
  by TTL for free. 10 000 sandboxes/month with a daily listing < $0.10.
- **Template at rest**: $0 (on-demand, empty table; IAM is free).
- **Off**: no option set = zero extra AWS resources and calls vs 0.4.0.

## Capabilities

### New Capabilities

- `metadata-index`: the optional DynamoDB index (row schema, adapter, join,
  `index=` on create/list/paginate/PoolConfig, CLI flag, template).

### Modified Capabilities

- `sandbox-metadata`: the probe listing is now "without `index=`"; with
  `index=` suspended states are accepted.
- `e2b-compat`: `SandboxQuery.metadata` over `PAUSED` with `index=`; parity
  row 40 divergente.
- `security-docs`: new threat T19 (metadata copy at rest in the index).

## Impact

- **Python**: `rayito/_index.py` (new), `_listing_base.py`, `_pool_base.py`,
  `exceptions.py`, `__init__.py`, `sandbox_{sync,async}/{main,listing}.py`,
  `e2b/{_compat,_sync,_async,_client}.py`, `cli/sandbox.py`.
- **TypeScript**: `src/index/*` (new), `errors.ts`, `index.ts`,
  `sandbox/{sandbox,listing,paginator}.ts`, `pool/config.ts`,
  `e2b/{compat,sandbox,types,client}.ts`, `package.json` (optional peer +
  devDependency), `pnpm-lock.yaml`, `tsdown.config.ts`.
- **Infra**: `infra/metadata-index.yaml` (new), `infra/README.md`,
  `Makefile` (`infra-lint`).
- **Tests**: `clients/python/tests/unit/test_index_*.py`, `fake_dynamodb.py`,
  `cli/test_index_cli.py`, `tests/e2e/test_metadata_index_e2e.py`;
  `clients/typescript/tests/unit/index-*.test.ts`, `index-fake.ts`,
  `tests/e2e/metadata-index.e2e.test.ts`, the reason assertion of
  `e2b-compat.test.ts`; `scripts/tests/test_metadata_index_template.py`.
- **No changes** to `crates/`, proto, the `runHookPayload`, lifecycle events,
  webhooks, streams, a reconciler, a REST API, multi-tenant keys,
  `get_info(index=)`, row deletion on `kill`, a `RAYITO_INDEX_TABLE`
  environment variable (forbidden by ADR-014) or `doctor`.
