# Metrics and listing

A sandbox's metrics history (CPU, memory, disk), the guest's facts (the CPU
and memory it sees) and a resumable listing with ordering and filters.
None of this needs new IAM: the history travels over the same gRPC as the
rest and the listing is `list-microvms`.

## Snapshot and history { #instantanea-e-historial }

`get_metrics()` is a procfs snapshot (two reads of `/proc/stat` 100 ms
apart). `rayd` also samples every 5 s while the sandbox runs and keeps an
8 h ring (5,760 samples); `get_metrics_history(start=, end=,
max_points=)` returns it in ascending order.

=== "Python"

    ```python
    from datetime import datetime, timedelta, timezone

    from rayito import Sandbox

    with Sandbox.create() as sbx:
        now = sbx.get_metrics()
        print(now.cpu_used_pct, now.mem_used_bytes, now.mem_cache_bytes, now.disk_used_bytes)

        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        for sample in sbx.get_metrics_history(start=since, max_points=60):
            print(sample.timestamp, sample.cpu_used_pct, sample.mem_used_bytes)

        # From another process: the class form needs the access token
        series = Sandbox.get_metrics_history(sbx.sandbox_id, access_token=sbx.access_token)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            print((await sbx.get_metrics()).cpu_used_pct)
            series = await sbx.get_metrics_history(max_points=30)
            print(len(series))
            again = await AsyncSandbox.get_metrics_history(
                sbx.sandbox_id, access_token=sbx.access_token
            )
            print(len(again))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const now = await sbx.getMetrics();
    console.log(now.cpuUsedPct, now.memUsedBytes, now.memCacheBytes);

    const history = await sbx.getMetricsHistory({
      start: new Date(Date.now() - 600_000),
      maxPoints: 60,
    });
    for (const sample of history) {
      console.log(sample.timestamp.toISOString(), sample.cpuUsedPct);
    }
    const series = await Sandbox.getMetricsHistory(sbx.sandboxId, {
      accessToken: sbx.accessToken,
    });
    console.log(series.length);
    ```

- `SandboxMetrics`: `cpu_used_pct`, `cpu_count`, `mem_used_bytes`,
  `mem_total_bytes`, `mem_cache_bytes`, `disk_used_bytes`,
  `disk_total_bytes` and `timestamp`. In the history, `cpu_used_pct` is the
  average since the previous sample (≈ 5 s), not the snapshot's 100 ms
  window.
- `start` and `end` are inclusive (a naive `datetime` is local time);
  `max_points` reduces the series to that number of buckets (the last
  sample of each bucket with the CPU averaged). A full response weighs
  ≈ 350 KB.
- **Gap while suspended**: it is not sampled, so the series jumps from the
  last sample before the pause to the first after the resume. Sampling does
  not touch the network and does not count as activity for the idle policy.
- The class form needs the access token (`access_token=` or
  `RAYITO_ACCESS_TOKEN`) and **never wakes** a suspended sandbox.
- On an image earlier than 0.3.0, `get_metrics_history()` (TS
  `getMetricsHistory()`), on an instance and in the class form, raises
  `UnimplementedError` (it is not a `SandboxException`) asking you to
  publish a new image, like transfers and the server-side deadline; the
  generic `UnimplementedError` stays in `__cause__` (TS `cause`), and gRPC's
  `UNIMPLEMENTED` one level further down, in `__cause__.__cause__` (TS
  `cause.cause`).

## Guest facts { #hechos-del-guest }

`get_health()` carries `cpu_count` and `memory_total_bytes`, and
`get_info()` fills in `agent_version`, `cpu_count` and `memory_mb`: they
are the **guest's view**, which may not match the image size (the guest
reported 8016 MiB and 4 CPUs with a 2048 MiB image). In TypeScript:
`agentVersion`, `cpuCount`, `memoryMb` and `memoryTotalBytes`.

## Resumable listing { #listado-reanudable }

`Sandbox.list()` walks `list-microvms` (pages of 50) lazily.
`Sandbox.paginate()` gives a resumable paginator: `next_items()`,
`has_next` and `next_token`, an **opaque** cursor (it carries AWS's
`nextToken` and the filter) that you can store and pass to another process.

=== "Python"

    ```python
    from datetime import datetime, timedelta, timezone

    from rayito import Sandbox

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    pages = Sandbox.paginate(limit=20, order="desc", started_after=since, states=["RUNNING"])
    first = pages.next_items()
    for item in first:
        print(item.sandbox_id, item.state, item.started_at, item.template_version)

    if pages.has_next:
        cursor = pages.next_token                     # store it wherever you like
        more = Sandbox.paginate(limit=20, order="desc", started_after=since,
                                states=["RUNNING"], next_token=cursor).next_items()

    for item in Sandbox.list(template="rayito-base", order="asc"):
        print(item.sandbox_id)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncSandbox


    async def main() -> None:
        pages = AsyncSandbox.paginate(limit=20, order="desc")
        while pages.has_next:
            for item in await pages.next_items():
                print(item.sandbox_id, item.state)
        print(len(await AsyncSandbox.list(states=["RUNNING"])))


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const pages = Sandbox.paginate({ limit: 20, order: "desc", states: ["RUNNING"] });
    const first = await pages.nextItems();
    console.log(first.map((item) => item.sandboxId));
    if (pages.hasNext) {
      const cursor = pages.nextToken;
      const more = await Sandbox.paginate({
        limit: 20,
        order: "desc",
        states: ["RUNNING"],
        nextToken: cursor,
      }).nextItems();
      console.log(more.length);
    }
    for await (const item of Sandbox.list({ template: "rayito-base" })) {
      console.log(item.sandboxId, item.state);
    }
    ```

| Filter (Python / TS) | Where it is applied | Note |
|---|---|---|
| `template`, `template_version` / `templateVersion` | server (`list-microvms`) | image name or ARN |
| `states` | client | by default everything except `TERMINATING`/`TERMINATED` |
| `started_after` / `startedAfter` | client | inclusive |
| `order="asc"` or `"desc"` | client, by `startedAt` | AWS does not sort: the first item arrives after walking **all** the pages (O(pages)); a resumed token skips by identity the items already delivered |
| `metadata` | client, O(n) | one `Health` probe per `RUNNING` sandbox (≈ 0.5-1 s each, counts as traffic for its idle); it never probes a suspended one. With `index=` (optional, below): no probes and over any non-terminal state |
| `index` (optional) | your DynamoDB table | `DynamoDbIndex(...)`: one `BatchGetItem` per page; see [Metadata index](funciones-opcionales/indice-de-metadatos.md) |
| `limit` (only `paginate`) | client | items per `next_items()`; all if missing |

A `next_token` is only valid with the same filters it was issued with; a
tampered one or one from another filter is `InvalidArgumentException`.

## Listing by metadata with an index (optional) { #listado-por-metadatos-con-indice-opcional }

Without an index, `list(metadata=...)` only sees `RUNNING` sandboxes: the
metadata lives in the agent and probing a suspended one would wake it. The
**metadata index** (optional, off by default, with a DynamoDB cost) keeps a
copy of each sandbox's `metadata` in a table in your account, so you can
also filter the `SUSPENDED` ones without probing or waking any.

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")
    paused = list(Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx))
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito";

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    const paused = [];
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      paused.push(item);
    }
    ```

Full guide, union rules, cost and IAM:
[Metadata index](funciones-opcionales/indice-de-metadatos.md).

## From the CLI { #desde-la-cli }

```bash
rayito sandbox list --template rayito-base
rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
rayito sandbox metrics microvm-<id> --token-file ~/.rayito/<id>.token
rayito --json sandbox metrics microvm-<id> --follow --interval 5 --token-file ~/.rayito/<id>.token
```

`metrics` prints the `get_metrics()` snapshot ([CLI](cli.md)); connecting
wakes a suspended sandbox.

<a id="instalacion"></a><a id="ejemplo"></a><a id="nombres-de-span"></a><a id="atributos"></a><a id="que-no-incluye-m13b"></a>

## SDK OpenTelemetry traces (optional) { #trazas-opentelemetry-del-sdk-opcional }

SDK-side spans (`rayito.sandbox.create`, `rayito.commands.run`,
`rayito.code.run`, `rayito.files.*`…) over the calls you already make,
exported wherever you configure your `TracerProvider`. Off by default:
without `tracer_provider=` / `tracerProvider` OpenTelemetry is not
imported. It is not sandbox telemetry (for that there is the [metrics
history](#instantanea-e-historial)).

Full guide, span names, attributes and what is never recorded:
[OpenTelemetry](funciones-opcionales/opentelemetry.md).

Sending the history metrics (CPU, memory and disk) **from the sandbox** to
your OTLP collector is another optional feature, also off by default:
`create(telemetry=TelemetryExport(...))`, and
`sbx.get_telemetry_status()` (TypeScript: `getTelemetryStatus()`) counts
what was exported and what was discarded. See
[OTLP export](funciones-opcionales/exportacion-otlp.md).

## In the E2B shim { #en-el-shim-de-e2b }

`sbx.get_metrics(start, end)` returns the history as
`list[SandboxMetrics]` in E2B's format (with `mem_cache`);
`Sandbox.get_metrics(sandbox_id, access_token=...)` is the class form, and
`Sandbox.list(query=SandboxQuery(state=..., started_after=..., template=...,
metadata=...), limit=, next_token=, order=)` returns a `SandboxPaginator`
([E2B compatibility](e2b-compat.md)). With the `index=` extension (or
`E2B(index=...)`), `query.metadata` also filters `state=[PAUSED]`
([E2B compatibility](e2b-compat.md#con-el-indice-de-metadatos-index-opcional)).
