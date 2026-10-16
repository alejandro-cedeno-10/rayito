# Metadata index

A DynamoDB table **in your account** with a copy of each sandbox's
`metadata`, to filter the listing by metadata also over paused sandboxes,
without waking them and without probing them one by one.

!!! info "Cost and activation"
    - **Default**: off. Without `index=` (TypeScript: `index`) the SDK builds
      no DynamoDB client and makes no call.
    - **Turns on**: `index=DynamoDbIndex("rayito-sandboxes")` in
      `Sandbox.create()`, `Sandbox.list()`, `Sandbox.paginate()`,
      `PoolConfig`, the E2B shim (`Sandbox.list(..., index=)`,
      `E2B(index=)`) and the CLI (`rayito sandbox list --index-table`).
    - **Resources and AWS calls**: you deploy the table yourself
      (`rayito stack deploy metadata-index`, template
      `infra/metadata-index.yaml`, on-demand, TTL on `expires_at`).
      `dynamodb:PutItem` once per created sandbox and `dynamodb:BatchGetItem`
      once per `list-microvms` page when listing with `metadata` and
      `index`. Never `DeleteItem`: the TTL deletes the rows for free.
    - **Approximate cost** (us-east-1, checked 2026-09-30,
      [DynamoDB on-demand pricing](https://aws.amazon.com/dynamodb/pricing/on-demand/)):
      $0.625 per million WRUs (≈ 1 per `create`) and $0.125 per million RRUs
      (0.5 per candidate sandbox when listing), plus $0.25/GB-month after 25
      GB free. 10,000 sandboxes a month with one listing a day ≈ $0.03/month.
      An empty table costs $0.
    - **IAM** (credentials of whoever calls the SDK, not the sandbox role):
      `dynamodb:PutItem` (policy `RayitoIndexWriter`) and
      `dynamodb:BatchGetItem` (policy `RayitoIndexReader`) on the table ARN.
    - **How to turn it off**: stop passing `index=`; to stop paying for
      storage, delete the stack: `rayito stack destroy metadata-index`
      (deletes the table and its two policies).

!!! warning "Pending acceptance on real AWS"
    The index is implemented and tested with a simulated DynamoDB in both
    SDKs. Its test against real AWS (that `run-microvm`'s `startedAt` and
    `list-microvms`'s match, and that listing wakes nobody) has not been run
    yet.

## When to use it { #cuando-usarlo }

- You filter a fleet of tens or hundreds of sandboxes by metadata (`user`,
  `run`, `tenant`): without an index, each `RUNNING` sandbox costs a probe
  of ≈ 0.5–1 s.
- You need to find **suspended** sandboxes by their metadata: without an
  index, `list(metadata=)` never sees them, because probing them would wake
  them up.
- **When not to**: few sandboxes, always `RUNNING`; `list(metadata=)`
  without an index is enough and costs nothing.

## Quick example { #ejemplo-rapido }

Deploy the table once (the `metadata-index` component of
[`rayito stack`](pilas-opcionales.md); it prints the cost and asks for
confirmation):

```bash
rayito stack deploy metadata-index --param TableName=rayito-sandboxes
rayito stack status metadata-index   # outputs: the table and the reader/writer policies
```

From the SDK, `OptionalStacks().deploy("metadata-index")` does the same. The
template is `infra/metadata-index.yaml`, in case you prefer to deploy it with
CloudFormation by hand.

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")  # (1)!
    with Sandbox.create(metadata={"user": "42"}, index=idx) as sbx:  # (2)!
        sbx.pause()
        for item in Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx):
            print(item.sandbox_id, item.state, item.metadata)  # (3)!
    ```

    1. Building it does not call AWS: the DynamoDB client is created on its
       first use.
    2. A conditional `PutItem` after `run-microvm`.
    3. One `BatchGetItem` per `list-microvms` page; no probe.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox, DynamoDbIndex


    async def main() -> None:
        idx = DynamoDbIndex("rayito-sandboxes")
        async with await AsyncSandbox.create(metadata={"user": "42"}, index=idx) as sbx:
            await sbx.pause()
            paused = await AsyncSandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx)
            print([item.sandbox_id for item in paused])


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito"; // + npm i @aws-sdk/client-dynamodb

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    await using sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state, item.metadata);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
    ```

## How it works { #como-funciona }

1. `create(index=)` writes an immutable row (`metadata`, image,
   `startedAt`, TTL) after `run-microvm`, before waiting for the agent. A
   pool writes it when refilling if its `PoolConfig` carries `index=`.
2. `list(metadata=, index=)` walks `list-microvms` and, for each page,
   reads the candidates' rows with a `BatchGetItem` and joins them.

Join rules (a row never invents a sandbox):

- The **state** always comes from `list-microvms`; the row only contributes
  the metadata.
- A sandbox appears if its row has the same id, the same image ARN and the
  same `startedAt` (±1 s), has not expired and its metadata contains every
  requested pair.
- A sandbox **without a row** (created without `index=`) is skipped: its
  metadata is unknown and it is never probed.
- With an index, `states` accepts any non-terminal state (all of them by
  default); `TERMINATING`/`TERMINATED` are `InvalidArgumentException`.
- `index=` without `metadata=` changes nothing and does not touch DynamoDB.
- `kill()` does not delete the row: the TTL (`startedAt` + maximum lifetime +
  `ttl_margin_seconds`, 3600 by default) deletes it for free, and on read
  the SDK discards expired rows that DynamoDB has not yet deleted.

## `DynamoDbIndex` options { #opciones-de-dynamodbindex }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `table_name` (1st argument) | `tableName` | — | table name |
| `region` | `region` | the session's | table region |
| `session` | `credentials` | the default session | AWS credentials |
| `on_write_failure` | `onWriteFailure` | `"terminate"` | if `PutItem` fails: `"terminate"` kills the freshly launched sandbox and raises `IndexWriteException`; `"warn"` warns and returns the sandbox |
| `ttl_margin_seconds` | `ttlMarginSeconds` | 3600 | TTL margin over the maximum lifetime |

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Python | TypeScript | When | What to do |
|---|---|---|---|
| `IndexWriteException` | `IndexWriteError` | `PutItem` failed on create (permission, nonexistent table) with `on_write_failure="terminate"` | check `RayitoIndexWriter` and the table name |
| `SandboxIndexException` | `SandboxIndexError` | `BatchGetItem` failed, or keys were left unprocessed after 5 retries | check `RayitoIndexReader`; it never silently returns an incomplete list |
| `InvalidArgumentException` | `InvalidArgumentError` | `states` with `TERMINATING`/`TERMINATED`; the `@aws-sdk/client-dynamodb` peer is missing in TypeScript | fix the filter; `npm i @aws-sdk/client-dynamodb` |

## Differences from E2B { #diferencias-con-e2b }

E2B filters by metadata on its server. Rayito has no server: without an
index, the filter probes each sandbox; with an index, the table lives in your
account. In the shim, `Sandbox.list(query=SandboxQuery(metadata=...,
state=[SandboxState.PAUSED]), index=idx)` uses the same index; only
sandboxes created with the native SDK's `create(index=)` or a
`PoolConfig(index=)` show up.

## See also { #ver-tambien }

- [Optional features](../optional-features.md#metadata-index)
- [Metrics and listing](../observability.md#listado-reanudable)
- [IAM](../operacion/iam.md)
- Template: [`infra/metadata-index.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/metadata-index.yaml)

??? info "Sources and measurements"
    - DynamoDB contract and the pending measurement: `AWS_API_NOTES.md` §20
      (IDX-1), on [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Deployment and deletion: [`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md).
    - Design decision (optional, in your account, no server): ADR-014 in
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
