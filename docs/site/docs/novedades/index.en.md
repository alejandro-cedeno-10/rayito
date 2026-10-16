---
title: What's new
description: What each Rayito version brings, how to upgrade, what is experimental and what is in development.
---

# What's new

One page per version with what changes for you: new features with their
example, fixes that change a behavior and the steps to upgrade. The full
detail, change by change, is in each component's
[Changelog](../referencia/changelog.md).

<div class="grid cards" markdown>

-   :material-tag:{ .lg .middle } **0.13.0** · 2026-10-16 · current

    ---

    The `rayito` CLI, the `rayito-mcp` server and the local dashboard in
    TypeScript too (`npx rayito`), with the same surface, output and exit
    codes as in Python. `rayito --version`, UTC timestamps in the Python CLI
    and a stricter proxy. No API-breaking changes.

    [:octicons-arrow-right-24: What's new in 0.13.0](0.13.0.md)

-   :material-tag:{ .lg .middle } **0.12.0** · 2026-10-15

    ---

    The management dashboard: `Quotas`, `UsageCost` and `Fleet` in the SDK,
    `rayito dashboard` locally and the `dashboard` stack in your account, with
    the CloudWatch dashboard, multiple accounts and quota alerts as optional
    extras. Everything off by default and no API-breaking changes.

    [:octicons-arrow-right-24: What's new in 0.12.0](0.12.0.md)

-   :material-tag:{ .lg .middle } **0.11.0** · 2026-10-13

    ---

    `AgentTemplate` images start sooner (`create()` in about 6 s): the
    `run_code` kernel is no longer warmed up by default, a behavior change with
    a way back (`kernel_warmup=True`). Per-run artifact keys when publishing
    images, and the TypeScript pool no longer keeps Node from exiting. No
    API-breaking changes.

    [:octicons-arrow-right-24: What's new in 0.11.0](0.11.0.md)

-   :material-tag:{ .lg .middle } **0.10.0** · 2026-10-08

    ---

    The agent can use OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral,
    DeepSeek, xAI or your LiteLLM proxy; the timeout and `abort()` stop
    everything it launched; and deepagents works outside Bedrock and counts its
    tokens. No breaking changes.

    [:octicons-arrow-right-24: What's new in 0.10.0](0.10.0.md)

-   :material-tag:{ .lg .middle } **0.9.1** · 2026-10-07

    ---

    `Sandbox.create()` with a `timeout` of 300 s or less and no `idle` works
    again: the default auto-suspend adapts to the deadline. No API changes.

    [:octicons-arrow-right-24: What's new in 0.9.1](0.9.1.md)

-   :material-tag:{ .lg .middle } **0.9.0** · 2026-10-07

    ---

    Cleanup and fixes: the agent's option D and the gateway exceptions that
    nothing raised are removed, `connect()` recovers `sbx.gateways` from
    another process and the Python SDK honors `AWS_REGION`. Breaking changes,
    with their migration.

    [:octicons-arrow-right-24: What's new in 0.9.0](0.9.0.md)

-   :material-tag:{ .lg .middle } **0.8.0** · 2026-10-07

    ---

    An AI agent inside the sandbox (`sbx.agent` with OpenCode or deepagents),
    fast start with `AgentTemplate` and pool warm-up, gateway presets for
    models and the pricing page. One breaking change in `register_webhook`.

    [:octicons-arrow-right-24: What's new in 0.8.0](0.8.0.md)

-   :material-tag:{ .lg .middle } **0.7.1** · 2026-10-06

    ---

    `rayd` carries its third-party license notices in the release, in
    `rayito-image.zip` and in the image (`/usr/share/doc/rayd/`), and
    `SHA256SUMS` is signed. No API changes.

    [:octicons-arrow-right-24: What's new in 0.7.1](0.7.1.md)

-   :material-tag:{ .lg .middle } **0.7.0** · 2026-10-05

    ---

    EFS volumes in your VPC, custom domain (experimental), local testing with
    Docker and Floci, and security hardening of `rayd`, the SDKs, the
    infrastructure and the release.

    [:octicons-arrow-right-24: What's new in 0.7.0](0.7.0.md)

-   :material-tag:{ .lg .middle } **0.6.1** · 2026-10-04

    ---

    Redeploying an optional stack keeps its parameters, `reincarnate()`
    reapplies all the 0.6 options and `rayd` reaps zombie processes as PID 1.

    [:octicons-arrow-right-24: What's new in 0.6.1](0.6.1.md)

-   :material-tag:{ .lg .middle } **0.6.0** · 2026-10-03

    ---

    S3 mounts, size catalog, events and webhooks, OTLP export, declarative
    templates, secret gateway and `rayito stack`.

    [:octicons-arrow-right-24: What's new in 0.6.0](0.6.0.md)

</div>

## Which version you have { #que-version-tienes }

=== "Python"

    ```python
    import rayito

    print(rayito.__version__)  # "0.13.0"
    ```

=== "TypeScript"

    ```ts
    import { VERSION } from "rayito";

    console.log(VERSION); // "0.13.0"
    ```

=== "CLI"

    ```bash
    rayito doctor --template rayito-base  # the "compatibility" check compares SDK and rayd
    ```

The Python and TypeScript SDKs and the `rayd` agent share the `MAJOR.MINOR`
series. A feature that lives inside the sandbox also needs an image published
with a `rayd` of that series:
[SDK ↔ rayd ↔ image compatibility](../limits.md#compatibilidad-sdk-rayd-imagen).

## How to upgrade { #como-actualizar }

=== "Python"

    ```bash
    pip install -U "rayito[cli]"    # with uv: uv lock --upgrade-package rayito
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito@latest          # or: npm i rayito@latest
    ```

Then, republish your image on the new `rayd` if you want to use what runs
inside the sandbox (for example `volumes=`, `mounts=`, `events=` or
`gateways=`, the `rayd` 0.7.0 hardening, the `rayd` 0.7.1 license notices in
`/usr/share/doc/rayd/` or the `rayd` 0.10.0 agent timeout that stops its whole
process tree):
[Images](../images.md#publicar-las-tres). Since 0.11, `rayito doctor`
requires the `rayd` from the `rayd-v0.11.0` tag, and the AI agent
(`sbx.agent`) also needs an image with its runtime, built with
[`AgentTemplate`](../funciones-opcionales/templates-de-agente.md) (rebuild it
on 0.11.0 so it starts sooner; if you use the scientific stack in `run_code`,
with `kernel_warmup=True`). Without republishing, the new SDK keeps working
with your current image, and each feature that needs the new `rayd` fails
closed with `UnimplementedError` and terminates the sandbox it just launched.
Going from 0.10.x to 0.11.0 there are no API-breaking changes, but there is a
[behavior change in `AgentTemplate`](0.11.0.md#si-usas-run_code-con-el-stack-cientifico);
from 0.9.x to 0.10.0 there are no breaking changes
([How to upgrade from 0.9.x](0.10.0.md#como-actualizar-desde-09x)); from
0.8.x to 0.9.0, review the
[breaking changes](0.9.0.md#como-actualizar-desde-08x) (the agent's option D,
`GatewayException`/`GatewayError` and, in Python, `AWS_REGION`); from 0.7.x to
0.8.0, the
[breaking change in `register_webhook`](0.8.0.md#como-actualizar-desde-07x);
from 0.6.x to 0.7.0, the
[behavior changes](0.7.0.md#como-actualizar-desde-06x).

## Available as experimental { #disponible-como-experimental }

Published features, off by default, whose API may change in a minor.

| Feature | Since | How it is used | Why it is experimental |
|---|---|---|---|
| [EFS volumes](../funciones-opcionales/volumenes-efs.md) | 0.7.0 | `volumes=` / `volumes`, `VolumeStore`, `EfsVolumes` (`rayito stack deploy efs-volumes`), on the optional `rayito-base-caps-efs` image (`rayito image publish --with-efs`) | accepted on real AWS, but they depend on an optional image and on your VPC |
| [Custom domain](../funciones-opcionales/dominio-propio.md) | 0.7.0 | `CustomDomain` (`rayito domain deploy`) and `register()` for each route | not verified end to end on real AWS (creating the distribution and serving traffic); `domain=` in `Sandbox.create()` not wired up |

If you try one, tell us how it went in a
[GitHub issue](https://github.com/alejandro-cedeno-10/rayito/issues/new/choose).

## In development { #en-desarrollo }

Parts with a pending change that are **not available yet**. Their option
already exists in `create()`'s signature so the API does not change when they
arrive, and today it raises `UnimplementedError` before calling AWS:

| Feature | Option | Status |
|---|---|---|
| Custom domain integrated into the sandbox | `domain=` / `domain` in `Sandbox.create()`, and `get_host()`/`expose()` returning the URL | no committed version; in the meantime, [`CustomDomain`](../funciones-opcionales/dominio-propio.md) (experimental) |

In the meantime, to expose a port during development,
[`rayito sandbox proxy`](../funciones-opcionales/proxy-local.md).

## Designed, no code yet { #disenado-sin-codigo-todavia }

Parts with an already accepted design but not a single line merged into
`main`: they do not exist in any published SDK, neither as an option nor as
`UnimplementedError`.

None for now: `AgentTemplate`, `PoolConfig.warmup` and the deepagents runtime
were published in [0.8.0](0.8.0.md).

## Earlier versions { #versiones-anteriores }

The notes for releases 0.1.0 to 0.5.x are in the
[Changelog](../referencia/changelog.md) and in the
[GitHub Releases](https://github.com/alejandro-cedeno-10/rayito/releases).
