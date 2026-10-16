---
title: Pricing (MicroVMs, pool, agents)
description: "What a Rayito sandbox costs on AWS: MicroVMs per hour, the suspended pool and agents, with list prices and measured timings."
---

# Pricing: MicroVMs, pool and agents

Rayito charges nothing: you pay AWS, on your bill, for what your sandboxes
consume (and, if you use an agent, the model's tokens). Every number on this
page carries one of two labels:

- **List** (list price): comes from AWS's public price list (`us-east-1`,
  ARM), queried on **2026-10-06**, or is arithmetic on it.
- **Measured**: checked against real AWS or Cost Explorer in the test
  account (`rayito-base` image of 2 GB / 1 vCPU, September 2026).

## How much it costs, with examples { #cuanto-cuesta-con-ejemplos }

| Scenario | Approximate cost |
|---|---|
| 1 sandbox of 2 GB for 10 minutes | 600 s × $0.126/h ≈ **$0.021**, plus the launch ($0.0014) ≈ **$0.022** |
| 1 sandbox of 2 GB for 1 hour | **$0.126** + $0.0014 |
| An agent with 100 five-minute sessions a day | VM only: 100 × (300 s × $0.126/h + $0.0014) ≈ **$1.19/day**; the model is paid separately ([cost of an agent](#coste-de-un-agente-vm-frente-a-modelo)) |
| The same sandbox paused for 8 hours | snapshot storage (≈ 0.92 GB × $0.08/GB-month for 8 h) ≈ **$0.0008** + one suspend/resume cycle ($0.0049) |
| Keeping an image version published | ≈ **$0.04 per week** (one-week minimum per version) |
| `rayito doctor --launch` | ≈ **$0.002** |
| A pool of 3 idle slots | ≈ 3 × $0.60 = **$1.80/month** (see below) |

No free tier and no plan quota: you pay per second while the sandbox is
`RUNNING`, per GB on each snapshot and for the storage of image versions and
suspended sandboxes.

<a id="precios-fuente-aws_api_notesmd-12"></a>

## Prices { #precios }

### Price components { #componentes-del-precio }

Each MicroVM bills for five things, each one separately — none includes the
others, and it is easy to mix them up when adding up a scenario:

- **Compute** while the sandbox is `RUNNING`: per vCPU-second and per
  GB-second, with *bursts* above your base size billed as well (there is no
  "included" cap). *List.*
- **Snapshot read**: each `run-microvm` (launch) and each `resume` read the
  full disk and memory snapshot. *List.*
- **Snapshot write**: each `suspend` writes that same snapshot. *List.*
- **Snapshot storage**: per GB-hour while they exist — a published image
  version or a suspended sandbox — with a **one-week minimum** per image
  version, even if you delete it earlier. *List.*
- **Data transfer**: standard AWS data transfer rates (same region: free;
  to the Internet: per GB). *List.*

!!! note "`maximumDuration` is not a price"
    A sandbox cannot live, launched + suspended, more than **8 hours**
    without being terminated; suspended time counts the same as `RUNNING`.
    It is the reason [the pool](pool.md) relaunches and parks each slot
    again at ≈ 7 h, before reaching the limit. *List (limit).*

| Item | Price | Source |
|---|---|---|
| Compute while `RUNNING` | $0.0000276944 per vCPU-s + $0.0000036667 per GB-s ⇒ **$0.126/h** at 2 GB / 1 vCPU | List — AWSLambda price-list offer, 2026-10-01 |
| Snapshot write (each `suspend`) | $0.0037977138 per GB | List — AWSLambda price-list offer; **Measured** −0.06 % in Cost Explorer (2026-09-16) |
| Snapshot read (each `run-microvm` or `resume`) | $0.0015467699 per GB | List — AWSLambda price-list offer; **Measured** −0.21 % in Cost Explorer (2026-09-16) |
| Snapshot storage (image versions, suspended sandboxes) | $0.0001111111 per GB-hour = $0.08 per GB-month at 720 h/month, **one-week minimum** per image version ⇒ ≈ $0.037 per week and version | List — AWSLambda price-list offer; **Measured** exact in Cost Explorer (2026-09-16) |
| One suspend + resume cycle at 2 GB (0.92 GB of snapshot written and read) | ≈ **$0.0049**, the same as ≈ 140 s of compute | Derived — List |
| One launch (`run-microvm`, read of the 0.92 GB snapshot) | ≈ **$0.0014** | Derived — List |

Compute and storage prices match exactly what was billed (Cost Explorer);
snapshot read and write differ from the list price by less than 0.25 % —
within the rounding of the billed quantity, not a different price. The ratio
of billed GB-s to vCPU-s in the test account was **1.87–1.89**, not exactly
2.0 (pending comparison with the benchmark's VM-seconds).

!!! info "Official references (queried 2026-10-06)"
    - [aws.amazon.com/lambda/pricing](https://aws.amazon.com/lambda/pricing/)
      (Lambda MicroVMs section).
    - `AWSLambda` offer of the AWS price list API, published 2026-10-01.
    - [docs.aws.amazon.com/.../microvms-images-snapshots.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html)
      (semantics and storage minimum of a snapshot).
    - [docs.aws.amazon.com/.../microvms-how-it-works.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html)
      (compute, snapshot read/write per lifecycle).
    - [docs.aws.amazon.com/.../gettingstarted-limits.html](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
      (8 h `maximumDuration`, default quotas).

!!! tip "Auto-suspend and the 150 s threshold"
    Since a suspend/resume cycle costs the same as ≈ 140 s of compute, a
    `max_idle_seconds` below ≈ 150 s never saves money. The default value
    (300 s) does. For jobs with short gaps, raise `max_idle_seconds` or pass
    `idle=None` ([Pause and resume](guias/pausar-reanudar.md)).

### The pool: storage versus total cost per slot { #el-pool-almacenamiento-frente-a-coste-total-por-plaza }

A parked (suspended) [pool](pool.md) slot pays two different things, and it
is easy to confuse them:

| Item | Cost per slot |
|---|---|
| Snapshot storage only while parked | 0.92 GB × $0.08/GB-month ≈ **$0.074/month** |
| Recycling: every ≈ 7 h the slot is relaunched and parked again so it never reaches the 8 h limit | ≈ $0.0052 per cycle × ≈ 103 cycles ≈ **$0.53/month** |
| **Total of an idle slot** | ≈ **$0.60/month** (versus ≈ $91/month for a sandbox `RUNNING` all month) |
| Taking a slot | $0.0014 (snapshot read) + normal compute while you use it |

<a id="tiempos-fuente-docsbenchmarks2026-09-cold-startmd-milestonesmd-aws_api_notesmd-16"></a>

## Timings { #tiempos }

| Operation | Measured |
|---|---|
| `create()` until the agent is ready (`agent_ready`), client ≈ 90 ms away | **p50 2.3 s / p95 3.0 s** |
| `create()` until the kernel is ready (`kernel_ready`) | **p50 5.2 s / p95 6.1 s** sequential; p50 5.7 s / p95 8.6 s in a burst of 20 |
| `pool.take()` until the first cell | **p50 0.77 s / p95 0.90 s** |
| `pause()` until `SUSPENDED` | **1.37–1.49 s** |
| Explicit `resume()` | **p50 0.38 s / p95 0.40 s** |
| Auto-resume: first call on a suspended sandbox | **≈ 0.67 s** |
| `files.write` at 2 GB through the proxy | **0.65 MB/s** |
| `files.read` at 2 GB through the proxy | **6.71 MB/s** |
| Large files through S3 (with `transfer=`) | **55–106 MB/s** inside the VM |
| `Sandbox.get_info(id)` with metadata | **0.61–0.66 s** |
| `Sandbox.list(metadata=...)` without an index | **≈ 0.6–0.8 s per `RUNNING` sandbox** |
| `get_metrics()` | ≈ 100 ms |

## Rules of thumb { #reglas-practicas }

- Everything on this page is what **`create()` / `connect()` themselves
  create or call**. The optional features (secrets, metadata index, S3
  mounts, sizes, events and webhooks, OTLP export, templates and secret
  gateway) have their own cost, off by default and enabled only with an
  explicit SDK option: [Optional features](optional-features.md).
- A forgotten sandbox bills until its `timeout` (3600 s by default): use
  `with` / `await using` or `kill()`, and a `timeout` that fits the task.
  With the [server-side deadline](lifecycle.md) and `on_timeout="kill"`, an
  orphaned sandbox bills until its deadline plus ≈ 15 s, not until
  `max_lifetime`.
- Large files with `transfer=S3Staging(...)` go through S3, much faster than
  through the proxy: fewer billed compute seconds. S3 charges for its
  requests and for the temporary storage of the transfers prefix.
- In sandboxes that last seconds, the dominant cost is the snapshot read of
  each launch: the image size matters more than compute.
- `list(metadata=)` without an index counts as traffic for every probed
  sandbox and delays its auto-suspend: do not call it in a loop over a large
  fleet.
- A bigger sandbox costs proportionally more per hour
  ([Limits: size](limits.md#tamano-cpuram)).

??? info "Sources and measurements"
    - Prices: `AWS_API_NOTES.md` §12, on
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md),
      and the public Lambda MicroVMs price list.
    - Suspend/resume cycle and cold start:
      [`docs/benchmarks/2026-09-cold-start.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/benchmarks/2026-09-cold-start.md)
      (§8 and §9).
    - Pool: `clients/python/tests/e2e/test_m7_pool.py` (2026-09-16).
    - `pause()`, auto-resume, file throughput, `get_info` and
      `list(metadata=)`: 0.1.0 acceptances (`MILESTONES.md`) and
      `AWS_API_NOTES.md` Q45; S3: Q59; the server-side deadline: Q58.

## Cost of an agent: VM versus model { #coste-de-un-agente-vm-frente-a-modelo }

An [agent in the sandbox](guias/agente-en-el-sandbox.md) pays **two
different bills**: the VM (like any sandbox) and the model, per token, on
Bedrock, Anthropic or whichever provider you use through the gateway. Almost
always the model's bill is by far the larger one. The Bedrock prices in this
section are **List**: they have not yet been checked against a real invoice
(Q151 of `AWS_API_NOTES.md` is still unmeasured); the tokens in the measured
examples are real. On the invoice they show up as AWS Marketplace usage.

With the other providers
([Agent providers](guias/agente-proveedores.md)) the model is billed by the
provider per token, at its own prices; Rayito only adds the gateway's
Secrets Manager reads. In OpenAI-style APIs (OpenAI, Azure OpenAI, xAI,
OpenRouter, Groq, Mistral, DeepSeek, LiteLLM) the gateway cannot restrict
the model, so the spending cap has to be at the provider: a per-project
budget, a credit limit per key or the LiteLLM virtual key's budget
([Provider-side limits](guias/agente-proveedores.md#limites-del-lado-del-proveedor)).

What keeps both bills bounded from inside the SDK:

- **Model:** `AgentLimits.max_total_tokens` / `maxTotalTokens` stops the run
  once the token budget is exceeded. Since 0.10.0 it also works with
  deepagents and `provider="openai-compatible"`; before, those steps arrived
  with 0 tokens and the limit never fired. It only counts what the agent
  requests, not what other code in the sandbox requests through the gateway:
  for that there is the provider's cap and `rate_per_minute`.
- **VM:** the agent's timeout (`timeout_seconds` / `timeoutMs`) and `abort()`
  stop, since 0.10.0, everything the agent launched, so a forgotten server
  or `sleep` does not leave the agent busy
  ([What the timeout and `abort()` guarantee](guias/agente-en-el-sandbox.md#que-garantizan-el-timeout-y-abort)).
  The sandbox keeps billing while it is `RUNNING`: close it or let its
  `timeout` expire.

### Model prices (Bedrock, us-east-1, queried 2026-10-06) { #precios-del-modelo-bedrock-us-east-1-consultado-2026-10-06 }

| Model | Profile | Input /1M tok | Output /1M tok | Cache write (5 min) | Cache read |
|---|---|---|---|---|---|
| Claude Haiku 4.5 | Regional | $1.10 | $5.50 | $1.375 | $0.11 |
| Claude Haiku 4.5 | Global | $1.00 | $5.00 | $1.25 | $0.10 |
| Claude Sonnet 4.5 | Regional | $3.30 | $16.50 | $4.125 | $0.33 |
| Claude Sonnet 4.5 | Global | $3.00 | $15.00 | $3.75 | $0.30 |

Source: `AmazonBedrockFoundationModels` offer of the price list API
(`us-east-1`, published 2026-09-30) and
[aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing).
[Prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
needs at least 4096 tokens per checkpoint on Haiku (1024 on Sonnet), with up
to 4 checkpoints per request; below the minimum the request works the same,
it simply caches nothing, and `inputTokens` never counts what already comes
from the cache. With a single step it already comes out ≈ 1.25× more
expensive than without a cache (the write costs more than a normal read);
from the second step on it wins: 1.35× cumulative versus 2× without a cache.
OpenCode and the deepagents runner enable it by default
(`prompt_caching=True` in `AgentModel`).

### Example: a 10-step task { #ejemplo-una-tarea-de-10-pasos }

*List* (list price, arithmetic facts, not yet measured against a real
invoice — see the warning above). 10 steps, 15,000 input tokens and 400
output tokens per step, Haiku Regional ($1.10/$5.50/1M, cache $1.375 write /
$0.11 read per 1M):

- **VM**: 300 s × $0.1261/h + launch $0.0014 = 300 × 0.1261/3600 + 0.0014 =
  $0.0105 + $0.0014 ≈ **$0.0119**.
- **Model, without cache**: 10 steps × 15,000 tok × $1.10/1M + 10 × 400 tok
  × $5.50/1M = $0.165 + $0.022 ≈ **$0.187**.
- **Model, with cache**: assumes step 1 writes the whole prefix (15,000 tok)
  and that, from then on, each step adds ≈ 15 % new tokens to the context
  (2,250 written to cache) and reads the remaining 85 % from cache (12,750).
  Step 1: 15,000 × $1.375/1M = $0.0206. Steps 2–10 (9): 9 × (2,250 ×
  $1.375/1M + 12,750 × $0.11/1M) = 9 × ($0.0031 + $0.0014) = $0.0405. Plus
  the output of the 10 steps ($0.022) ⇒ $0.0206 + $0.0405 + $0.022 ≈
  **$0.083**. Without that 15 % of new tokens per step (static context), the
  same calculation gives ≈ $0.057 instead of $0.083 — the figure depends on
  how much the context grows at each step, not just on there being a cache.
- **Global** (same formula, ≈10 % cheaper per the price table above):
  proportional to the Regional numbers.
- **Sonnet 4.5 with cache** (same formula and same 15 % assumption, Sonnet
  Regional prices): $0.0619 + 9 × ($0.0093 + $0.0042) + $0.066 ≈
  **$0.25**.

**The model costs between ≈ 7× and ≈ 21× the VM** in this example ($0.083
and $0.25 versus $0.0119): the sandbox size is almost never the cost lever
of an agent; the number of steps, the context size and how much it grows at
each step are.

<a id="plaza-de-pool-de-agente-c"></a>

### Cost of the VM, with fast start { #coste-de-la-vm-con-fast-start }

Measured on real AWS on 2026-10-07 (`AWS_API_NOTES.md` Q146–Q150): the times
and tokens are **Measured**; the money is the list price for those times
(**List**), because Cost Explorer arrives a day late.

| Scenario | Approximate cost |
|---|---|
| Idle pool slot without an agent (base) | ≈ **$0.60/month** (table above) |
| Pool slot with warm-up (option C, [Pool](pool.md#calentamiento-warmup)) | ≈ **$0.64/month** |
| Sandbox paused between turns (option B) | one suspend/resume cycle of ≈ 0.92–1.2 GB ≈ **$0.005–0.006** + ≈ $0.0001/h stored |
| `rayito-agent` image version | code 2.10 GB + memory 0.86 GB + disk 0.03 GB ≈ 3.0 GB × $0.08/GB-month × 7/30 ≈ **$0.056/week** (one-week minimum; [Agent templates](funciones-opcionales/templates-de-agente.md)) |

Measured cost per scenario (VM + model, a short Claude Haiku 4.5 response
without tools, p50 of n=5):

| Scenario | To first token | VM | Model |
|---|---|---|---|
| `create()` without prefetch | 28.3 s | launch $0.0014 + 28.5 s ≈ $0.0024 | ≈ $0.0009 (7,596 tokens read from cache; $0.0104 the round that writes them) |
| `create()` with prefetch (A), before the fix | 20.5 s | ≈ $0.0021 | ≈ $0.0009 |
| `create()` with prefetch (A), daemon that waits for the guest | 14.7 s (13.3 s without prefetch, same batch) | launch $0.0014 + ≈ 15 s ≈ $0.0019 | ≈ $0.0009 |
| `connect()` after `pause()` (B) | 3.1 s | cycle ≈ $0.005–0.006 | ≈ $0.0009 |
| `take()` from a pool with `warmup` (C) | 5.1 s | read $0.0014 + ≈ 5 s ≈ $0.0016 | ≈ $0.0009 |
| `create()` with deepagents | 7.5 s after `create()` (5.0 s OpenCode) | ≈ $0.0014 + seconds of VM | ≈ $0.0039 (3,503 tokens without cache: below Haiku's 4,096 minimum) |

How the slot figures come out:

- **Base slot**: the 8 h limit includes suspended time, so the pool recycles
  every ≈ 7 h ⇒ 720 h / 7 h ≈ **103 cycles a month**. One cycle is launching
  (reads 0.92 GB: $0.0014) + writing the snapshot when parking (0.92 GB ×
  $0.0038: $0.0035) + a few seconds of compute ≈ $0.0052. 103 × $0.0052 ≈
  $0.53 + storage 0.92 GB × $0.08 ≈ $0.074 ⇒ **≈ $0.60/month**.
- **C (warmup)**: launch $0.0014 + 15.5 s measured until the slot is ready
  ($0.0005) + parking ≈ 0.92 GB ($0.0035) ≈ $0.0054 × 103 ≈ $0.56 + storage
  $0.074 ⇒ **≈ $0.64/month**. The API does not give the snapshot size of a
  `suspend`; it is taken to be equal to the image's because the guest's
  memory in use after the take (503 MiB) is smaller than that of a fresh
  `create()`.
- **Pausing between turns (B)**: the sandbox cannot go past 8 h launched +
  suspended. Past that cap, save the state with
  [persistence](persistence.md) and create a new VM (another launch, $0.0014,
  and the first `exec` cold again).

### Which fast-start option to choose { #que-opcion-de-arranque-rapido-elegir }

| Your case | Option | Extra cost (List) |
|---|---|---|
| The usual: one task or one conversation, no first-message latency requirements | none: `Sandbox.create(...)` + `sbx.agent.run(...)` | $0 |
| Single tasks, you can wait a few seconds | A. Prefetch / `prepare()` | ≈ $0 |
| Conversations with pauses of minutes or hours (< 8 h) | B. `pause()` and `connect()` | ≈ $0.005–0.006 per cycle + ≈ $0.0001/h stored |
| Many takes a day, minimum latency | C. Pool with `warmup` | ≈ $0.64/slot/month |

All are optional. Decided from the measurements: A stays on by default
because, since the daemon waits for the guest to settle, it no longer delays
`create()` (Q153), although end to end its gain today falls within the
noise. C is the recommended one for minimum latency; see
[Pool](pool.md#resultado-de-la-medida-en-aws) and the full table in
[Agent in the sandbox](guias/agente-en-el-sandbox.md#arranque-rapido).

??? info "Sources and measurements (agents)"
    - Design: `design.md` of `ai-agent-core` in
      [`openspec/changes/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/openspec/changes).
    - Spike: [`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md).
    - MicroVM prices: same as the rest of this page, `AWS_API_NOTES.md` §12.
    - Lambda MicroVMs prices:
      [aws.amazon.com/lambda/pricing](https://aws.amazon.com/lambda/pricing/)
      and the `AWSLambda` offer (published 2026-10-01), queried 2026-10-06.
    - Bedrock prices: `AmazonBedrockFoundationModels` offer (published
      2026-09-30),
      [aws.amazon.com/bedrock/pricing](https://aws.amazon.com/bedrock/pricing),
      queried 2026-10-06.
    - Prompt caching:
      [docs.aws.amazon.com/bedrock/.../prompt-caching.html](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html).
    - Times, sizes and tokens measured on real AWS on 2026-10-07 (Q146–Q150
      and Q152 of `AWS_API_NOTES.md` §16); the comparison with a real
      invoice (Q151) is still pending.
