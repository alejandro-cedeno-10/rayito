---
title: Agent templates
description: AgentTemplate composes OpenCode, ripgrep and deepagents on top of rayito-base-caps, with sha256 pins and the startup prefetch. Build cost and the cost of each image version.
---

# Agent templates

<small>Since 0.8.0 ([What's new in 0.8.0](../novedades/0.8.0.md)).</small>

`AgentTemplate` is a fixed recipe on top of the [`Template` DSL](templates.md)
that installs, in a single image, everything
[Agent in the sandbox](../guias/agente-en-el-sandbox.md) needs: the
[OpenCode](https://github.com/anomalyco/opencode) binary, `ripgrep` (its
search tools need it and, if it does not find it, it would try to download
it — under closed egress that fails) and, optionally, a venv with
[deepagents](https://github.com/langchain-ai/deepagents). The recipe is the
one the spike validated on real AWS
([`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md)).

Every field has a default value: `AgentTemplate().build(bucket=...)`
builds `rayito-agent` on `rayito-base-caps` with OpenCode, deepagents
and the prefetch. The example spells them out so you can see them:

=== "Python"

    ```python
    from rayito import AgentTemplate

    info = AgentTemplate(
        name="rayito-agent",
        base="rayito-base-caps",
        runtimes=("opencode", "deepagents"),
        prefetch=True,
        kernel_warmup=False,
        memory_mib=2048,
    ).build(bucket="amzn-s3-demo-bucket", on_build_logs=print)
    print(info)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentTemplate


    async def build() -> None:
        info = await AsyncAgentTemplate(runtimes=("opencode",)).build(bucket="amzn-s3-demo-bucket")
        print(info)
    ```

=== "TypeScript"

    ```ts
    import { AgentTemplate } from "rayito";

    const info = await new AgentTemplate({
      name: "rayito-agent",
      base: "rayito-base-caps",
      runtimes: ["opencode", "deepagents"],
      prefetch: true,
      kernelWarmup: false,
      memoryMib: 2048,
    }).build({ bucket: "amzn-s3-demo-bucket" });
    console.log(info);
    ```

=== "CLI"

    ```bash
    rayito agent template build --name rayito-agent --bucket amzn-s3-demo-bucket
    ```

| Field (Python / TypeScript) | CLI | Default | What it is |
|---|---|---|---|
| `name` | `--name` | `rayito-agent` | name of the image that is created or updated |
| `base` | `--base` | `rayito-base-caps` | base image; the variant with capabilities is the only one where `allow_internet_access=False` is enforced |
| `base_version` / `baseVersion` | — | the latest | version of the base image |
| `runtimes` | `--deepagents` / `--no-deepagents` | `("opencode", "deepagents")` | what gets installed; the CLI always installs OpenCode |
| `prefetch` | `--prefetch` / `--no-prefetch` | `True` | bakes in the [prefetch](#prefetch) daemon |
| `kernel_warmup` / `kernelWarmup` | `--kernel-warmup` / `--no-kernel-warmup` | `False` | leaves the `run_code` [kernel warm-up](#kernel-de-run_code-sin-calentar) as in the base image |
| `memory_mib` / `memoryMib` | `--memory-mb` | 2048 (minimum) | image memory |

`build()` accepts `bucket` (required), `force`, `timeout` (seconds;
TS: `timeoutMs`), `on_build_logs`/`onBuildLogs`, `region` and `session`
(TS: `credentials`), like [`Template.build`](templates.md); the CLI takes `--bucket`, `--force` and
`--timeout` (1800 s by default). It returns the build's `BuildInfo`. Without
building anything, `to_dockerfile()`/`toDockerfile()` shows the recipe and
`manifest()` the manifest it bakes in.

`memory_mib`/`memoryMib` below 2048 raises
`InvalidArgumentException`/`InvalidArgumentError` before any call: the
spike measured 575 MiB of RSS for OpenCode alone, and the 2048 MB guest
sees 8016 MiB ([Limits: size](../limits.md#tamano-cpuram) already
documents that the guest sees more memory than configured).

!!! info "Cost and activation"
    - **Default**: nothing is built until you call `.build()` /
      use the CLI (`rayito agent template build`).
    - **Build**: 271–320 s measured on real AWS (2026-10-07, three builds;
      it needs Internet access for GitHub and PyPI; the sandbox that starts
      from the resulting image does not).
    - **Version storage**: three measured snapshots, code
      2.10 GB + memory 0.86 GB + disk 0.03 GB ≈ **3.0 GB** × $0.08/GB-month,
      with the one-week minimum per image version ⇒ ≈
      **$0.056/week** (≈ $0.24/month) per version (measured 2026-10-09, Q156;
      with `kernel_warmup=True` memory goes back to 0.92 GB). It assumes
      storage is the sum of the three snapshots. List prices,
      us-east-1, checked 2026-10-06: see
      [Pricing](../cost.md#coste-de-la-vm-con-fast-start).
    - **Launch**: reading the memory snapshot (0.86 GB)
      ≈ **$0.0013**.
    - **IAM**: `RayitoTemplateBuilder`, the same policy as
      [Declarative templates](templates.md) (`templates` stack).
    - **How to turn it off**: do not build the template; delete its versions
      with `rayito image prune`. A normal sandbox
      (`rayito-base`/`rayito-base-caps`) does not change.

## What it installs { #que-instala }

| Piece | License | Pin | Verified by |
|---|---|---|---|
| OpenCode | MIT | version + sha256 of the release asset | `scripts/check_pins.py` against `limits.json`/`agentRuntimes` |
| ripgrep | MIT/Unlicense | version + sha256 | same |
| deepagents + `langchain-aws`, `langchain-anthropic`, `langchain-openai` and `langchain-google-genai` | MIT | `requirements-deepagents.txt` with `--hash`, `pip install --require-hashes --no-deps --only-binary=:all:` | `pip check` in the `run_cmd` itself; the build's smoke test imports the four packages; `pip-audit` in CI |

Everything ends up `root:root` and `0755`: the sandbox user (uid 1000)
cannot replace the binaries or the venv ([T30](../security.md#agente-de-codigo-dentro-del-sandbox)).
The variables `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_DISABLE_MODELS_FETCH`,
`OPENCODE_DISABLE_LSP_DOWNLOAD`, `OPENCODE_DISABLE_DEFAULT_PLUGINS`,
`OPENCODE_PURE` and `OPENCODE_DISABLE_CLAUDE_CODE` turn off everything
OpenCode would try to download from the Internet on startup and stop it from
reading a `.claude/` in the workdir. `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0`
keeps the `openai` provider from opening a WebSocket to the Responses API:
the gateway only forwards HTTP. The manifest baked into `/opt/agents/rayito-agent.json`
(`rayito.agent-template/1`) lists versions, sha256 values and the paths the
prefetch warms up. `sbx.agent` does not read it: `AgentSpec.runtime_version`
is not compared against it.

## Prefetch { #prefetch }

With `prefetch=True` (the default), the template adds a `start_cmd` that
does two things. At build time, before the snapshot, it reads the OpenCode
and ripgrep binaries once, and the template's `ready_cmd` holds the snapshot
until it finishes: that way the memory snapshot already carries those pages
and the first `exec` after `create()` does not read them from the code disk.
The deepagents modules are not baked in (they would bloat the snapshot for
the runtime that is not the default). And from startup it detects whether
the VM comes from restoring a snapshot (a clock jump greater than
`AGENT_PREFETCH_RESTORE_JUMP_SECONDS`). Then it waits for the guest to go 1
s in a row with no I/O in flight (at most 60 s) and pre-warms, at low
priority (`nice -n 19`), the OpenCode binary and, with the deepagents
runtime installed, the `deepagents`/`langchain_aws` import. It waits because
the restore also reads from disk (the kernel rotation `create()` waits for):
reading at the same time made `create()` longer. After that it does not
yield: the first `sbx.agent.run()` reads the same binary, so reading it
ahead of time gets pages ready for it.
`sbx.agent.prepare()` triggers the same warm-up when there is no daemon
(for example, without `prefetch`, or to force it after a period of
inactivity). When each fast-start option makes sense:
[Agent in the sandbox](../guias/agente-en-el-sandbox.md#arranque-rapido).

!!! success "Measured on real AWS (2026-10-07, n=5)"
    The daemon does not change the memory snapshot size (916–925 MB with
    and without it). **Before the fix** (the daemon read right after
    restoring): after `create()`, OpenCode's first token arrived in a median
    of 4.7 s with prefetch versus 19.6 s without it (−76 %), but `create()`
    took 16.6 s versus 8.7 s, so end to end (from `create()` to the first
    token) the improvement was 28.3 s → 20.5 s (−28 %).
    With the daemon that waits for the guest to settle (Q153, n=5, with a
    no-prefetch control in the same batch): `create()` 9.2 s versus
    8.8 s, so it no longer delays it; the stretch after `create()` 3.8 s
    versus 5.2 s; end to end 14.7 s versus 13.3 s, within the noise.
    The control came out much faster than in the first measurement, so today
    A's gain is small; it stays on by default because it no longer
    costs `create()` time.
    The first launches of a freshly published version are slower
    (`create()` of 26 to 65 s): do not measure right after the build.
    Detail in `AWS_API_NOTES.md` Q146 and Q150.

!!! success "Measured on real AWS (2026-10-09, n=6, `AWS_API_NOTES.md` Q156)"
    With the binary baked into the snapshot and the kernel not warmed up,
    the memory snapshot weighs 858 MB (62 MB less than before) and `create()`
    returns in a median of 6.4 s versus 19.1 s for the earlier image in the
    same batch. The first token's median improved by 2.8 s (20.9 s versus
    23.7 s) and, in another batch with deepagents also baked in, by 3.2 s,
    but with a lot of dispersion: the slow window after restoring changes a
    lot from one batch to another.

## `run_code` kernel without warm-up { #kernel-de-run_code-sin-calentar }

The base image warms up the `run_code` kernel every time it starts,
also in the rotation `create()` waits for: it imports numpy, pandas,
matplotlib, scipy and scikit-learn so that the first cell that uses them
is fast. An agent sandbox rarely needs it, so
`AgentTemplate` turns it off by default (`kernel_warmup=False`): it writes
the same `slim` marker as `rayito image zip --variant slim`. `run_code`
keeps working the same; only the first cell that imports that stack pays
for the import. With `kernel_warmup=True` (`--kernel-warmup`) the kernel is
left as in the base image.

!!! success "Measured on real AWS (2026-10-09, n=6, `AWS_API_NOTES.md` Q155)"
    Without the warm-up, `create()` returns 4.4 s sooner (p50 7.65 s versus
    12.0 s) and the memory snapshot weighs 248 MB less (671 MB versus
    920 MB), so each launch reads less and the version costs less to
    store. By itself it does not bring the first token forward (16.3 s versus
    16.4 s): OpenCode's first `exec` pays the slow window after the
    restore that the kernel used to pay. What does bring it forward is
    baking the binary into the snapshot ([Prefetch](#prefetch)).

## `--no-deepagents`, `--no-prefetch`, `--kernel-warmup` { #-no-deepagents-no-prefetch-kernel-warmup }

```bash
rayito agent template build --bucket amzn-s3-demo-bucket --no-deepagents   # OpenCode only: skips the venv (~409 MB less)
rayito agent template build --bucket amzn-s3-demo-bucket --no-prefetch      # no start_cmd: the first exec always pays the disk cost
rayito agent template build --bucket amzn-s3-demo-bucket --kernel-warmup    # run_code kernel warmed up, as in the base image
```

## `AgentProject` images { #imagenes-de-agentproject }

<small>Since 0.14.0.</small>

`AgentProject.build()` ([Deploy your agent](../guias/despliega-tu-agente.md))
builds its image with this same `AgentTemplate`: each runtime contributes its
own install steps, instead of hand-written recipes. `runtimes` accepts the
registry names (`opencode`, `deepagents`, `langgraph`, `strands`,
`strands-harness`, `hermes`) or a runtime object, and the user's code is copied
into the image with `app=` (root-owned, read-only for uid 1000). With the
default values, the Dockerfile and the manifest are the same as ever, byte for
byte.

Everything that is downloaded is pinned and verified, as above: binaries and
Node by sha256, Python environments with `--require-hashes` and Hermes cloned
at a fixed commit and checked with `git rev-parse`. Heavy frameworks make the
image larger: use one image per runtime and measure yours with
`rayito image sizes`, because each version pays storage for at least a week.

An OpenCode project without `rayito-agent.toml` takes its `AGENTS.md` and
the permissions of its `opencode.json` into the image. `AGENTS.md` is copied as
the runtime's instructions file, under `~/.rayito/agent/opencode/`, and the
permissions go in the `opencode.json` that Rayito generates. Details in
[Agent providers](../guias/agente-proveedores.md#opencode-sin-fichero-de-proyecto).

## See also { #ver-tambien }

- [Agent in the sandbox](../guias/agente-en-el-sandbox.md).
- [Declarative templates](templates.md): the DSL `AgentTemplate` uses
  underneath.
- [Pool: warm-up](../pool.md#calentamiento-warmup):
  avoid paying for the first `exec` on every take.
- [Pricing (MicroVMs, pool, agents)](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
