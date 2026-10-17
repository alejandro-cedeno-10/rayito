# Limits

Lambda MicroVMs quotas and limits as the SDK sees them. Source:
`AWS_API_NOTES.md` §2, §6 and §11 (measured in the development account on
2026-09-15) and `limits.json`, from which `_limits.py` (Python) and
`limits.ts` (TypeScript) are generated, where the SDK validates them on the
client.

| Limit | Value | What the SDK does |
|---|---|---|
| Maximum lifetime of a sandbox (`timeout`, or `max_lifetime`) | 28,800 s (8 h), running + suspended, **not adjustable** | `SandboxLifetimeException` above it; the cap cannot be extended afterwards |
| Regions with Lambda MicroVMs | `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`, `ap-northeast-1`, `ap-south-1`, `ap-southeast-1`, `ap-southeast-2` | `rayito doctor` warns (`WARN`) outside them |
| Auto-suspend (`IdlePolicy.max_idle_seconds`) | ≥ 60 s and less than `timeout` (or `max_lifetime`) | `InvalidArgumentException` if you pass it; without `idle`, the default 300 s one is turned off if it does not fit (60 s in `pause` mode) |
| Logical deadline (`timeout` with `max_lifetime`/`on_timeout`) | ≥ 1 s and at most `max_lifetime − 60 s` from the start (`max_lifetime` 120–28,800 s, `timeout + 60` by default; 3600 in the shim) | `set_timeout` above the cap: `InvalidArgumentException` with the deadline untouched |
| Metrics history | one sample every 5 s while running; ring of 5,760 samples (8 h), ≈ 350 KB per full response | `max_points` ≥ 1 downsamples the series |
| Transfer URLs | 3600 s by default, cap `S3Staging.max_expires_in` (86,400) and 604,800 s (SigV4's 7 days) | `InvalidArgumentException` with `use_signature_expiration <= 0` |
| Files through S3 | from `threshold_bytes` (8 MiB; minimum 1 MiB); multipart export from 5 GiB, ≤ 1,000 parts of ≥ 8 MiB; single `PUT` ≤ 5 GiB | the SDK picks the path |
| Transfers per sandbox | 16 active, 2 moving bytes at a time, 64 finished retained for 30 min | `RateLimitException` (`RESOURCE_EXHAUSTED`) |
| Metadata per file | ≤ 64 keys and ≤ 4,000 B in total (`user.rayito.*` xattrs) | `InvalidArgumentException` before the call |
| Egress policy | ≤ 256 entries per list, ≤ 64 host names, ≤ 4,096 prefixes per family after subtracting | `InvalidArgumentException` |
| Persistence (`persist=S3Prefix(...)`) | key prefix ≤ 900 bytes, ≤ 64 patterns in `exclude`; automatic restore with a 600 s deadline (`persist_timeout` / `persistTimeoutMs`) | `InvalidArgumentException` before the call |
| `runHookPayload` (`envs` + `metadata` + token hash) | 4096 characters | `InvalidArgumentException` before calling AWS, naming `envs` and `metadata` |
| Concurrent connections per MicroVM | 8 (1 vCPU), 16, 32, 64, 128 | ≤ 2 HTTP/2 channels per sandbox: unary calls and streams |
| Endpoint bandwidth | 1 / 2 / 4 / 8 / 16 MB/s by size | measured: 0.65 MB/s write, 6.71 MB/s read at 2 GB |
| `list-microvms` | 50 items per page | paginates internally; `list(metadata=)` adds one probe per sandbox |
| Live processes + PTYs per sandbox | 256 (enforced by `rayd`) | `RateLimitException` (`RESOURCE_EXHAUSTED`) |
| Kernels (code contexts) per sandbox | 8 (enforced by `rayd`) | `RateLimitException` |
| Proxy JWE | 60 min | renewed at 45 min by an SDK thread/task |
| `create-microvm-auth-token` | 50 TPS | per-process token bucket aligned with the quota |
| `run-microvm` / `resume` / `suspend` / `terminate` | 5 / 5 / 2 / 10 TPS | per-process token bucket aligned with the quota |
| `get-microvm` | 100 TPS | per-process token bucket |
| Network connectors per `run-microvm` | 10 | `InvalidArgumentException` |
| `envs` of a command | as the process's environment variables | no limit of its own; the creation payload has one |
| Output retained per process | 64 × 32 KiB per subscriber; `output_truncated` if nobody reads within 30 s | `SandboxException("output_truncated: ...")` |
| Size of a `run_code` `result` | > 12 MiB trimmed to `rayito/omitted` | appears in `Result.extra` |
| Stored output of a command or PTY (since 0.7.0) | 64 MiB per descriptor (`COMMAND_OUTPUT_MAX_BYTES`); the end is kept | `truncated` in the result; adjustable with `max_output_bytes` / `maxOutputBytes` ([Commands](guias/comandos.md#salida-guardada)) |
| EFS volumes per sandbox (since 0.7.0, experimental) | from 1 to 4, and at most 4 between `mounts=` and `volumes=`; exactly one egress connector of its own (`AWS_API_NOTES.md` §16 Q131); `ConfigureSandbox` with a 65 s deadline | `InvalidArgumentException` before launching ([EFS volumes](funciones-opcionales/volumenes-efs.md)) |
| Own access token (since 0.7.0) | at least 16 decoded bytes (`ACCESS_TOKEN_MIN_BYTES`) | `InvalidArgumentException` |
| AI agent (`AgentLimits`, since 0.8.0) | by default 50 steps (`max_steps`), 1,000,000 tokens (`max_total_tokens`; `None`/`null` turns it off), 600 s (`timeout_seconds` / `timeoutMs: 600_000`) and 16 MiB of output (`max_output_bytes`, at most 64 MiB) | `AgentException` with `reason` `max_steps`, `token_budget` or `timeout` ([Errors](referencia/errores.md#agentexception-agenterror)) |
| Agent event line | ≤ 4 MiB; a longer one is dropped and counted in `dropped_lines` | — |
| Memory of an agent image (`AgentTemplate`) | ≥ 2048 MiB | `InvalidArgumentException` |
| Concurrency per VM of a served agent (since 0.14.0) | 1 by default (`max_concurrency`); at most the connections of the size minus one reserved for `/ping`: 7 with 2 GB, 15 with 4 GB, 31 with 8 GB (below 2 GB one vCPU is counted, a conservative value) | `InvalidArgumentException` |
| Host queue of an agent (since 0.14.0) | `queue_max` 0 by default and at most 256; waits `queue_timeout_seconds` (30 s by default, at most 900 s) | `429` with `Retry-After`; `CapacityException` (`agent_capacity`) after the deadline |
| Agent fleet (since 0.14.0) | from `min_vms` to `max_vms`, up to 64 per fleet; `warm_vms` ≤ `max_vms - min_vms`; `scale_in_after_seconds` from 60 s to 8 h (900 s by default) | `InvalidArgumentException` |
| Restart of an agent's app (since 0.14.0) | up to 3 restarts in 300 s, waiting from 2 s up to 30 s | `/ping` 503 and `app_crashloop` in the failure marker |
| Detection scan (since 0.14.0) | up to 5000 files, 6 folder levels and 256 KiB per file | `scan_truncated` warning |

The TPS quotas are per account and region (`AWS_API_NOTES.md` §11); new
accounts may start with lower values.

## Size (CPU/RAM) { #tamano-cpuram }

The size (memory and vCPU) **is a property of the image**
(`resources[0].minimumMemoryInMiB` in `create-microvm-image`,
`AWS_API_NOTES.md` §4). It is the same shape E2B uses: the size is fixed per
**template build**, with `Template.build(cpu_count=, memory_mb=)`
(`e2b template create --cpu-count --memory-mb`).

Since 0.6.0, `Sandbox.create(size="4gb")` (TypeScript: `{ size: "4gb" }`)
picks from a closed catalog of five sizes (512mb, 1gb, 2gb, 4gb, 8gb): the
SDK resolves it on the client, with no AWS call, to the name of the image of
that size (`rayito-base-4gb`). That image has to be published beforehand
with `rayito image publish --sizes 4gb`; `size=` does not change the memory
of an image or of a running sandbox. Without `size=` the usual image is
launched. See [Sizes](funciones-opcionales/tamanos.md);
`Template.build(memory_mb=)` also builds an image of your own at whatever
size you want ([Templates](funciones-opcionales/templates.md)).

Table from the **AWS documentation** (`AWS_API_NOTES.md` §4; endpoint
bandwidth, input + output), with the baseline hourly cost derived from the §12
prices (`AWS_API_NOTES.md` §12 / [Pricing](cost.md)): **nothing in this table
except what is marked has been measured in a real account**.

| `minimumMemoryInMiB` | baseline | peak (4x) | disk | bandwidth | $/h at baseline |
|---|---|---|---|---|---|
| 512 | 0.5 GB / 0.25 vCPU | 2 GB / 1 vCPU | 8 GB | 1 MB/s | $0.0315 |
| 1024 | 1 GB / 0.5 vCPU | 4 GB / 2 vCPU | 8 GB | 2 MB/s | $0.0631 |
| 2048 (default of `rayito image publish`) | 2 GB / 1 vCPU | 8 GB / 4 vCPU | 8 GB | 4 MB/s (**measured** 4.54 MB/s downloading 16 MiB, `AWS_API_NOTES.md` §7) | $0.1261 |
| 4096 | 4 GB / 2 vCPU | 16 GB / 8 vCPU | 16 GB | 8 MB/s | $0.2522 |
| 8192 | 8 GB / 4 vCPU | 32 GB / 16 vCPU | 32 GB | 16 MB/s | $0.5044 |

The only thing in this table verified in a real account (`AWS_API_NOTES.md`
§7 and Q68) is the 2048 row: the measured bandwidth (4.54 MB/s downloading
16 MiB) and what the guest sees (`cpu_count=4`, `memory_mb=8016`, the top of
the range, not the baseline). The rest of the table — baseline, peak, disk
and bandwidth of the other four sizes — comes as is from the AWS
documentation, unmeasured; the `$/h` is arithmetic on those numbers and the
verified §12 prices, not an observed invoice. Other `minimumMemoryInMiB`
values outside this table are neither documented nor measured (RES-1): do
not publish them without measuring them yourself first in your account.

**How to choose a size today**: publish one image per size, with a name that
says so, and create sandboxes against that image:

```bash
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 \
    --bucket amzn-s3-demo-bucket --image-name myimg-4gb --memory-mib 4096
```

```python
from rayito import Sandbox

with Sandbox.create("myimg-4gb") as sbx:
    print(sbx.get_info().memory_mb)
```

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create({ template: "myimg-4gb" });
```

The CLI does not validate `--memory-mib` against the table above (the
unmeasured values might be valid anyway): a value outside what has been
verified simply has no reference number here yet.

**Cost**: while the sandbox is `RUNNING`, AWS bills per second at the
**baseline** vCPU/GB of the column above (the `$/h at baseline` column,
prices in [Pricing](cost.md)); if the sandbox consumes above the baseline (up
to the 4x peak), **that excess is billed separately, at the vCPU/GB actually
consumed** (`AWS_API_NOTES.md` §12), not at the price of the next size in
the table. Each published image version, whatever its size, also costs the
snapshot storage (minimum 1 week of retention, see [Pricing](cost.md) and
[Images and IAM](images.md#publicar-las-tres)).

**What the guest sees is not the baseline**: `SandboxInfo.cpu_count` and
`SandboxInfo.memory_mb` report the guest's view (`nproc` and `MemTotal`),
not `minimumMemoryInMiB`. With a 2048 MiB image, `get_info()` **measured**
`cpu_count=4` and `memory_mb=8016` — the top of the range, not the baseline
(`AWS_API_NOTES.md` Q68). Code that decides its parallelism by looking at
`nproc` or total memory may exceed the contracted baseline — and, per the
previous paragraph, that is **not just a performance matter: that excess is
paid for** at the vCPU/GB consumed above the baseline, it does not just run
faster for free.

## Versioning and support { #versionado-y-soporte }

Rayito follows [SemVer 2.0.0](https://semver.org/) and publishes three
components with the same `MAJOR.MINOR` series: the Python and TypeScript
SDKs (`rayito` on PyPI and npm) and the `rayd` agent (signed asset of the
`rayd-v*` GitHub Release and binary inside the image). Each has its
changelog in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format
([Changelog](referencia/changelog.md)).

- **What the public API is**: what `rayito` and `rayito.e2b` (Python),
  `rayito` and `rayito/e2b` (TypeScript) export without a leading
  underscore, the `rayito` CLI, the `rayito-mcp` tools and the
  `proto/rayito/v1/` contract. `_private` modules, generated types and the
  repository's scripts are not.
- **While it is 0.x** (today, `Development Status :: 3 - Alpha`): a MINOR
  bump may break compatibility and says so: a `feat!:` or `BREAKING CHANGE:`
  commit and an entry under `Changed` or `Removed` in the changelog. A PATCH
  never breaks the public API. From 1.0.0, only a MAJOR breaks.
- **Deprecation**: before removing something from the public API it is
  marked deprecated (`DeprecationWarning` in Python, `@deprecated` in
  TypeScript) and noted under `Deprecated` in the changelog at least one
  MINOR before it is removed. The exceptions are AWS removing the platform
  capability that supports it, or a security fix requiring it; both are
  explained in the release notes.
- **Support**: only the latest `MAJOR.MINOR` line receives fixes, security
  ones included (`SECURITY.md`, "Supported versions"). The SDK and the image
  must be of the same series: the table below fixes each SDK's minimum
  `rayd` and `rayito doctor` checks it.
- **Image versions are not Rayito versions**: `rayito-base` N.0 is a build
  counter that AWS assigns in each account (see below).

## SDK ↔ rayd ↔ image compatibility { #compatibilidad-sdk-rayd-imagen }

The SDKs (Python and TypeScript) and `rayd` advance `MAJOR.MINOR` in lockstep
(the patch may diverge); each row fixes the minimum `agent_version` that
carries the features that SDK uses. The image version (`imageVersion`) **is
not a criterion**: it is each image's build counter in each account
(`rayito-base` 17.0, `rayito-base-poly` 3.0 and the first image of a new
account, 1.0, carry the same `rayd`), and `agent_version` comes from the
binary inside the image, so it already proves which tag it was built from.
`rayito doctor` evaluates this table in its `compatibility` check (and shows
the image version for information only) and
`rayito.cli._compat.COMPATIBILITY` is the same table in code: a test
(`tests/unit/cli/test_compat.py`) prevents them from diverging, and a release
that raises a minimum adds the row in both places. The notes are the CLI's
own strings, which it prints in Spanish; here they are translated.

| SDK | Minimum rayd | Note |
|---|---|---|
| `0.1` | `0.1.0` | M6: imds_blocked, hook_anomalies and metadata require the rayd of tag rayd-v0.1.0 |
| `0.2` | `0.2.0` | M7: Checkpoint/Restore (persist=) and language= require the rayd of tag rayd-v0.2.0 |
| `0.3` | `0.3.0` | M9: max_lifetime/on_timeout, set_timeout, get_metrics_history, network= and the Deno kernels require the rayd of tag rayd-v0.3.0 |
| `0.4` | `0.4.0` | 0.4: single UnimplementedError, SetTimeout validated in the domain and agent messages in Spanish require the rayd of tag rayd-v0.4.0 |
| `0.5` | `0.5.0` | 0.5: /suspend with filesystem-bounded sync and the optional features (secrets, index, OTel) are validated with the rayd of tag rayd-v0.5.0 |
| `0.6` | `0.6.0` | 0.6: ConfigureSandbox and the 0.6 features (S3 mounts, events, OTLP, secret gateway, template start/ready) require the rayd of tag rayd-v0.6.0 |
| `0.7` | `0.7.0` | 0.7: EFS volumes (create(volumes=)) and agent hardening (isolated hooks, persistence tied to /run) require the rayd of tag rayd-v0.7.0 |
| `0.8` | `0.8.0` | 0.8: the AI agent (sbx.agent, AgentTemplate and pool warmup) is validated with the rayd of tag rayd-v0.8.0 |
| `0.9` | `0.9.0` | 0.9: connect() recovers sbx.gateways from ConfigureStatus and the agent's fast start without option D are validated with the rayd of tag rayd-v0.9.0 |
| `0.10` | `0.10.0` | 0.10: the agent's timeout and abort() stop the whole process tree (kill_tree) and the agent providers are validated with the rayd of tag rayd-v0.10.0 |
| `0.11` | `0.11.0` | 0.11: AgentTemplate's fast start (kernel_warmup=False by default) and per-run artifact keys are validated with the rayd of tag rayd-v0.11.0 |
| `0.12` | `0.12.0` | 0.12: the management dashboard (rayito dashboard and the dashboard stack) is validated with the rayd of tag rayd-v0.12.0 |
| `0.13` | `0.13.0` | 0.13: the TypeScript CLI and MCP server (ADR-027) and the CLI's UTC timestamp format are validated with the rayd of tag rayd-v0.13.0 |
| `0.14` | `0.14.0` | 0.14: the agent runtimes (Strands, Hermes, LangGraph, OpenCode or your own), the agent project and the rayito agent CLI are validated with the rayd of tag rayd-v0.14.0 |
