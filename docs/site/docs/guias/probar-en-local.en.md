# Test locally (Docker + Floci)

<small>In the repository since 0.7.0 ([What's new](../novedades/0.7.0.md#probar-en-local-con-docker-y-floci)).</small>

A Docker environment to run the Python and TypeScript SDKs against a real
`rayd` and an emulated AWS, with no AWS account, no credentials and no
cost. It serves the development loop and reviewing a PR before spending on
acceptance against real AWS, which remains the one that closes a milestone
or a release.

## What it starts { #que-levanta }

```text
┌──────────── internal network "aws" (no Internet) ─────────┐
│  floci        AWS emulator on :4566                       │
│  runner       Python (uv) + Node (pnpm), the tests        │
│   └─ guest    product image, rayd as PID 1                │
│               (shares the runner's network: loopback)     │
└───────────────────────────────────────────────────────────┘
```

- **floci**: [Floci](https://github.com/floci-io/floci) 2.1.0 (MIT), pinned
  by digest. It emulates S3, DynamoDB, Secrets Manager, CloudFormation,
  STS, CloudWatch Logs and the Lambda MicroVMs control plane.
- **guest**: the `image/Dockerfile` image as is, with `rayd` and the kernel
  sidecar. It acts as the MicroVM: the tests call its hooks (`/ready`,
  `/run`, `/suspend`, `/resume`, `/terminate`) the way the platform would.
- **runner**: the toolchains for both SDKs. Dependencies are installed in
  volumes, never in the `.venv` or the `node_modules` on your machine. It
  runs as uid 993, outside the sandbox range (1000-65535) like the
  platform's agent: `rayd` rejects the `/run` and `/terminate` that come
  from a sandbox uid. If you brought the environment up with an earlier
  version and did not take it down, run `make local-down` before
  `make local-up` so the volumes are created with the new owner.

Nothing publishes ports on your machine, no container is privileged or
mounts the Docker socket, and Floci has no outbound access to the Internet.
The reasoning behind each decision is in
`docs/research/2026-10-local-testing.md`.

## Requirements { #requisitos }

- Docker with Compose v2 (Docker Desktop, Colima, Lima or OrbStack all
  work).
- An **arm64** host (Apple Silicon, Graviton or GitHub's
  `ubuntu-24.04-arm` runner): the product image's base only exists for
  arm64. On x86 it works with QEMU emulation, much more slowly.
- About 6 GB of free disk for Docker. As for memory, 3 GB is enough to run
  the environment, but compiling `rayd` inside Docker needs about 6 GB: the
  `aws-sdk-s3` crate (S3 mounts) alone goes past 2 GB when compiled. With
  less, compile `rayd` on your machine and pass it with `LOCAL_RAYD_BIN`
  (below).
- `make` and `python3` (to copy the sidecar into the guest's context).
- Only for the agent tests (below): `uv` and an AWS session on your
  machine, because `make local-bedrock-key` runs with `uv run`.

## Usage { #uso }

```bash
make local-up      # builds and starts floci, runner and guest; installs dependencies
make local-e2e     # runs the Python and TypeScript `local` tests
make local-down    # stops everything and deletes the volumes
```

`make local-up` compiles `rayd` inside Docker the first time (a few
minutes, no Rust on your machine). If you already have the binary from
`make build` (static, musl, with `cargo zigbuild`, built on Linux or in a
Linux VM), pass it and you save the compilation; it is the way to go if your
Docker VM has less than 6 GB and rustc dies with `SIGKILL`:

```bash
make build
make local-up LOCAL_RAYD_BIN=target/aarch64-unknown-linux-musl/release/rayd
```

To run only a part, the `LOCAL_E2E_ARGS` arguments go to pytest, and each
suite can be launched on its own inside the runner:

```bash
make local-e2e LOCAL_E2E_ARGS="-k commands"
docker compose -f dev/local/compose.yaml exec runner \
  bash -c 'cd clients/python && uv run --no-sync pytest tests/local -m local -k secret'
docker compose -f dev/local/compose.yaml exec runner \
  bash -c 'cd clients/typescript && pnpm exec vitest run --project local'
```

## Agents against a real model { #agentes-contra-un-modelo-real }

The agent tests (`test_local_agents.py` and `agents.local.test.ts`) run
[`sbx.agent`](agente-en-el-sandbox.md) with OpenCode and deepagents inside
a local sandbox, with Claude on Amazon Bedrock as the model and
`bedrock_gateway` / `bedrockGateway` as the only egress. In each runtime
and in both SDKs they check that the agent uses its tools, continues a
session, aborts without leaving live processes, respects `AgentLimits`
(steps, tokens and timeout), emits the expected event stream, cannot read
the credential, does not reach the Internet on its own while the model
does answer, and that no telemetry is on by default. Without a key they are
skipped, so `make local-e2e` and CI still run with no credentials.

!!! info "Cost and activation"
    - **By default**: off. Without `make local-bedrock-key` the agent tests
      are skipped and nothing leaves the local environment.
    - **Activates**: `make local-agent-up` (swaps the guest for the variant
      with OpenCode, ripgrep, deepagents and its runner from
      `dev/local/agent/`, with `CAP_NET_ADMIN` so the egress deny-all is
      really applied) and `AWS_PROFILE=<your-profile> make local-bedrock-key`.
    - **AWS resources and calls**: no resources. The key is a
      `bedrock:CallWithBearerToken` URL presigned on your machine (valid for
      as long as your session, at most 12 h); each suite makes about thirty
      `ConverseStream`/`Converse` calls from `rayd` through the gateway.
      The secret lives in Floci's Secrets Manager.
    - **Approximate cost**: cents per run, in Bedrock tokens
      (Claude Haiku 4.5, us-east-1, checked on 2026-10-06:
      <https://aws.amazon.com/bedrock/pricing/>).
    - **IAM**: `bedrock:CallWithBearerToken` and `bedrock:InvokeModel*` on
      the inference profile, and model access already enabled in the
      account (Rayito does not enable it for you).
    - **How to turn it off**: `make local-down` (deletes the runner's tmpfs
      with the key) or do not run `make local-bedrock-key`.
    - **Example**:

        ```bash
        make local-up
        make local-agent-up
        AWS_PROFILE=<your-profile> make local-bedrock-key
        make local-e2e LOCAL_E2E_ARGS="-k agents"
        make local-down
        ```

The key goes from the host to the runner through a pipe and ends up in
`/tmp/rayito-local-bedrock-key` (`RAYITO_LOCAL_BEDROCK_KEY_FILE`), a tmpfs
that dies with the container: never in a file on your machine or in an
environment variable. The tests look for it inside the sandbox to answer
yes or no and never print it. The model and its region are changed with
`RAYITO_E2E_BEDROCK_MODEL` and `RAYITO_E2E_BEDROCK_REGION` (by default,
Claude Haiku 4.5 in us-east-1). A test marked as an expected failure is a
reminder that the timeout still does not kill what the agent's shell
launched.

### Other providers without third-party keys { #otros-proveedores-sin-claves-de-terceros }

`test_local_agent_providers.py` genuinely tests the OpenAI-style paths of
the [agent providers](agente-proveedores.md) with what you already have on
AWS, with no accounts on OpenAI or other providers:

| Path | Gateway and `AgentModel` | What it checks |
|---|---|---|
| `bedrock-chat` | `openai_compatible_gateway` to `bedrock-runtime` (`/openai/v1`), gpt-oss-120b | tools, session, closed egress, unreadable key |
| `mantle-chat` | `openai_compatible_gateway` to `bedrock-mantle` (`/v1`) | the same |
| `mantle-responses` | `provider="openai"` (Responses API) to `bedrock-mantle` | one turn without tools with token usage |
| `litellm` | `litellm_gateway` to a local LiteLLM proxy in front of Bedrock | tools, session, closed egress, unreadable key |

The first three use the same key from `make local-bedrock-key`. The LiteLLM
one is created by `make local-litellm-up` (`dev/local/agent/litellm.sh`):
it starts LiteLLM, pinned by digest, on the environment's network with the
name `litellm` and no ports on the host; it gives it a certificate from a
test CA that it adds to the guest's system store (which `rayd` uses to
verify the gateway's upstream; since `rayd` reads it at startup, restart
the guest, so do not run it with a live sandbox), and leaves the master key
in the runner's tmpfs. LiteLLM talks to Bedrock with your session's
temporary credentials, which only travel as the container's environment
variables; when they expire, repeat `make local-litellm-up`.

```bash
make local-agent-up
AWS_PROFILE=<your-profile> make local-bedrock-key
AWS_PROFILE=<your-profile> make local-litellm-up
make local-e2e LOCAL_E2E_ARGS="-k providers"
make local-litellm-down
```

`bedrock-mantle` departs from the OpenAI API in two points (measured on
2026-10-07): the `response.output_item.done` of a tool call arrives with
`id: null` and it rejects a resent assistant message without `id` or
`status`. That is why `mantle-responses` only tests a turn without tools;
against the OpenAI API the full path is not genuinely tested. OpenAI,
Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek and xAI are
covered only by the unit tests against a fake upstream.

## Model providers without keys { #proveedores-de-modelo-sin-claves }

The provider tests (`test_local_agent_providers_fake.py` and
`agent-providers.local.test.ts`) test the nine presets of the
[agent providers](agente-proveedores.md) (OpenAI, Gemini, Azure OpenAI,
OpenRouter, Groq, Mistral, DeepSeek, xAI and LiteLLM) with OpenCode and
with deepagents, without real keys or Internet. `make local-providers-up`
adds to the agents variant a fake HTTPS upstream
(`dev/local/providers/fake_upstream.py`): on the internal `providers`
network it answers to the names of the nine providers, with a single-use CA
that only `rayd` trusts (the guest's `SSL_CERT_FILE`), and replies the
minimum valid Chat Completions, Responses and Gemini. Per preset and
runtime, with egress closed, the tests check that:

- the agent completes a turn through the gateway;
- the upstream receives the secret value in the preset's header
  (`authorization`, `x-goog-api-key` or `api-key`), and the marker the
  runtimes see never leaves, neither in headers nor in the body;
- only allowlisted paths get through, and a path outside it receives 403
  from `rayd` without reaching the upstream.

CI runs them in `local-e2e` after `make local-e2e`.

```bash
make local-up
make local-providers-up
make local-providers-e2e
make local-down
```

### Your deployed agent: `AgentProject` { #tu-agente-desplegado-agentproject }

`test_local_agent_project.py` tests `AgentProject`
against the local `rayd`: `run()`, `deploy()`, `invoke()`, `stream()` and
`stop()` with the custom runtime (`CustomRuntime`: a command that speaks v1
and an HTTP app, both in `dev/local/agent/apps`) and, with the fake upstream
of `make local-providers-up`, with OpenCode and deepagents. The guest shares
the `runner` network namespace, so the deployment client talks to the agent
host over loopback, without TLS; everything else (the sandbox, the gateways,
the host in service mode, the limits) is the production code. It also checks
that a second `invoke()` with the slot full gets `busy`, that the deployment
does not hold the credential and that `stop()` terminates the sandbox. It
runs with `make local-providers-e2e`, and skips itself when the guest is not
the one from `make local-agent-up`.

The container that `export()` writes is tested separately, with the host's
Docker: `make agent-export-test` builds the exported context for
`linux/arm64`, starts the container and probes `/ping` and `/invocations`
(JSON, SSE and the token) inside it.

### The sample agents, from detection to the container { #los-agentes-de-ejemplo }

`make agent-zero-config-test` takes the fifteen agents in
`testdata/agent-samples/` (LangGraph with FastAPI, LangChain, deepagents, Strands,
Strands harness, Hermes, a Flask app, a native AgentCore agent, LangGraph.js,
deepagents-js, Strands TypeScript and its harness, the Vercel AI SDK, an Express
app and OpenCode), detects them with no options, exports their Docker context,
builds it for `linux/arm64` with the host's Docker and tests it against a fake
Bedrock (`dev/local/providers/fake_bedrock.py`, which calls the `add` tool and
answers with its result): `/ping`, an invocation with a tool, the SSE stream, the
service token, the host queue and the `429` with three simultaneous requests, and
the restart of an app that dies. It uses no AWS and no keys. `RAYITO_ZC_SAMPLES=a,b`
picks samples and `RAYITO_ZC_ENDPOINT_HOST` is the name the container uses to see
the host (`host.docker.internal` by default; `host.lima.internal` with Lima). A Docker
with BuildKit is required: the host's Docker client may not ship `buildx`, in which
case run it with the virtual machine's `docker`.

### Smoke test against the real APIs { #prueba-de-humo-contra-las-apis-reales }

`test_local_agent_providers_smoke.py` (Python) and
`agent-providers-smoke.local.test.ts` (TypeScript) do a short turn of each
runtime against the real API of every provider whose key you export, with
the sandbox's egress closed: the agent answers with text and the input and
output token usage is greater than zero. Both read the presets from the
same catalogue (`testdata/agent/provider-catalogue.json`), and
`make local-providers-smoke` runs the Python one first and then the
TypeScript one. It runs on top of `make local-agent-up`, without the fake
upstream: with it up, both are skipped.

!!! info "Cost and activation"
    - **By default**: off. Each preset is skipped if you do not export
      `RAYITO_SMOKE_<PRESET>_SECRET` (`OPENAI`, `GEMINI`, `AZURE_OPENAI`,
      `OPENROUTER`, `GROQ`, `MISTRAL`, `DEEPSEEK`, `XAI` or `LITELLM`).
    - **Activates**: `make local-providers-smoke` with, per preset, the key
      in `RAYITO_SMOKE_<PRESET>_SECRET` (without `Bearer`) and the model in
      `RAYITO_SMOKE_<PRESET>_MODEL`; Azure also needs
      `RAYITO_SMOKE_AZURE_OPENAI_RESOURCE` and LiteLLM
      `RAYITO_SMOKE_LITELLM_UPSTREAM`. The variables reach the runner by
      name, never by value on the command line.
    - **AWS resources and calls**: none. The key lives in Floci's Secrets
      Manager; `rayd` makes one or two calls to the provider per runtime
      through the gateway.
    - **Approximate cost**: each run carries
      `AgentLimits(max_total_tokens=20000)` (`maxTotalTokens: 20_000` in
      TypeScript) and a prompt without tools.
      With a model of up to 0.20 USD per million input tokens, both
      runtimes together stay under 0.01 USD per provider and SDK (under
      0.02 USD with both SDKs). Choose the cheapest model in your account.
    - **IAM**: none on AWS. The key only needs to call the model; set a
      spending limit on it at the provider.
    - **How to turn it off**: do not export the variables, or
      `make local-down`.
    - **Example**:

        ```bash
        make local-up
        make local-agent-up
        export RAYITO_SMOKE_GROQ_SECRET=<your-key> RAYITO_SMOKE_GROQ_MODEL=<cheap-model>
        make local-providers-smoke
        make local-down
        ```

## How it works inside { #como-funciona-por-dentro }

The tests (`clients/python/tests/local/` and `clients/typescript/tests/local/`)
build a `LocalGuestControlPlane`: another adapter of the `ControlPlane`
port, which wraps the real plane pointed at Floci. `run-microvm`,
`get-microvm`, `list-microvms` and `terminate-microvm` go through the real
adapter (Floci validates the requests against the service model); what
Floci does not emulate is replaced by the guest:

| Operation | On AWS | Locally |
|---|---|---|
| MicroVM endpoint | `*.lambda-microvm.<region>.on.aws` over TLS | `127.0.0.1`, h2c over loopback |
| Proxy token | `create-microvm-auth-token` | a fixed value (the proxy validates it, not `rayd`) |
| `/run` | called by the platform after restoring the snapshot | called by the local plane after `/ready` |
| Pause and resume | `suspend-microvm` / `resume-microvm` | the guest's `/suspend` and `/resume` hooks |
| Terminate | `terminate-microvm` | `/terminate`: `rayd` exits and Docker restarts it |

The published SDK does not change and does not know this exists: the tests
hand it the plane and the transport through the parameters it already has
(`control_plane=` and `transport=` in Python, `controlPlane` and
`transport` in TypeScript). Without the `RAYITO_LOCAL_GUEST` variable,
which only the runner defines, the `local` tests are skipped.

## What it covers and what it does not { #que-cubre-y-que-no }

| Area | Locally | Only against real AWS |
|---|---|---|
| Commands, files, PTY, `run_code`, contexts, `connect`, metrics | yes, against the real `rayd` | |
| Pause and resume | `rayd`'s state machine | memory snapshot and restore |
| `OptionalStacks` (`rayito stack`) | deploy, status, redeploy and delete of six stacks (`metadata-index`, `secrets-access`, `otlp-export`, `s3-mounts`, `sizes-guard`, `templates`), plus `events-webhooks` from `LifecycleEvents` | `custom-domain` and `efs-volumes`; real IAM permissions and cost |
| Metadata index, `SecretStore`, `secrets=` | yes | |
| `rayito` CLI, Python and TypeScript | stack, image, `sandbox create --detach`, `info`, `exec`, `metrics`, `list` and `kill`, and the `create` and `connect` terminals (TypeScript), with the same scenarios as against real AWS | `sandbox proxy` (it needs the AWS proxy), `doctor` with IAM and image builds |
| MCP server (`rayito-mcp`) | over stdio, the shared `testdata/mcp/tools.json` contract and the startup errors; the six tools against a sandbox over in-process `--http` (TypeScript) | the six tools over stdio against a MicroVM |
| Local dashboard `rayito dashboard` (TypeScript) | the read-only routes validated with `api-v1.schema.json` and the security guards (no session, an action without `--allow-actions`, billed cost off) | Cost Explorer and the real bulk actions |
| Secrets gateway (`gateways=`) | the `ConfigureSandbox` section and the listener | the HTTPS forwarding to the upstream |
| `Template.build` | zip upload to S3 and `create-microvm-image` | the real image build and its `/ready` |
| Events and webhooks | the stack, the webhooks, `create(events=)` and the three Lambdas run in process, with a signed delivery to a local receiver | the CloudWatch Logs subscription and the HTTPS delivery with its SSRF filter |
| AWS proxy (JWE, 403, keepalives) | no | yes |
| `sbx.agent` (OpenCode, deepagents) through the gateway | yes, with `make local-agent-up` and a Bedrock key: real egress deny-all, tools, session, abort, limits, events | the first `exec` and the memory in a MicroVM |
| Provider presets (`openai_gateway`…`litellm_gateway`) | yes, with `make local-providers-up`: the header that reaches the upstream, the marker that does not leave, the 403 outside the allowlist and one turn of each runtime against a fake upstream | each API's real response (optional smoke test with your key) |
| IMDS blocking, egress, S3 mounts | no: they need privileges in the guest (except egress with `make local-agent-up`) | yes |
| Persistence and presigned transfers | no: `rayd` uses IMDS and requires HTTPS | yes |
| Cost, quotas, startup times | no | yes |

Floci has some differences from AWS that the tests take into account (a
secret's version, `ListMicrovmImageVersions` of an image that does not
exist yet); they are in the research notes.

## Emulator security { #seguridad-del-emulador }

Floci is third-party open source and its API has no authentication, so the
compose file treats it as untrusted: unprivileged user, no capabilities,
read-only filesystem, no Docker socket, no Internet and no ports on the
host. Version 2.1.0 fixes the critical advisories published to date.
Before bumping the version (Dependabot opens the PR), read its release
notes and its
[security advisories](https://github.com/floci-io/floci/security/advisories).

## In CI { #en-ci }

The `local-e2e` workflow runs `make local-up` and `make local-e2e` on
`ubuntu-24.04-arm` on PRs that touch the local environment, `rayd`
(`crates/`, `proto/`, `Cargo.lock`), the SDKs and their tools (`clients/`,
without the CHANGELOGs) or the shared data (`testdata/`, `limits.json`), every
night and by hand (`workflow_dispatch`). It uses no repository credentials or
secrets. It is the check that replaces a full AWS regression when a change only
touches an SDK or its tools.
