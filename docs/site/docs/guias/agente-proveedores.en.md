---
title: Agent providers
description: Which model providers sbx.agent can use through the secrets gateway (OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI, LiteLLM), how to configure each one and which are not supported and why.
---

# Agent providers

<small>Since 0.10.0 ([What's new in 0.10.0](../novedades/0.10.0.md)).</small>

The [`sbx.agent`](agente-en-el-sandbox.md) agent calls its model only
through the [secrets gateway](../funciones-opcionales/pasarela-de-secretos.md):
the key lives in Secrets Manager, `rayd` injects it into the header and the
runtime (OpenCode or deepagents) only sees a marker. Besides Bedrock,
Anthropic and `openai_compatible_gateway`, there is one preset per provider
with a static API key. Each preset fixes the `upstream`, the header and the
allowed paths, and is combined with an `AgentModel`.

Everything is configured in three steps: you store the key in Secrets
Manager, pass the preset in `gateways=` when creating the sandbox and point
the `AgentModel` at that gateway ([example](#ejemplo)).

## Which one do I choose? { #cual-elijo }

| Your situation | Choose |
|---|---|
| You already work on AWS and want a single bill | **Bedrock** ([`bedrock_gateway`](agente-en-el-sandbox.md#1-el-secreto-y-la-pasarela)): the gateway also limits which models are called, and it is the most tested path |
| You have an OpenAI key | **OpenAI** |
| Your organization uses Azure | **Azure OpenAI**: the model is a deployment of your resource, with its quota |
| You want Gemini | **Gemini**: as with Bedrock, the gateway limits the models |
| You want to try many models with a single key | **OpenRouter** |
| You look for speed or a low price | **Groq**, **DeepSeek** or **Mistral** |
| You have an xAI (Grok) key | **xAI** |
| You already have a LiteLLM proxy, or want local models | **LiteLLM**: virtual keys with a budget and any model the proxy serves |
| You have a consumer subscription (ChatGPT, Claude Pro/Max, Copilot, SuperGrok) | not supported: [Unsupported providers](#proveedores-no-admitidos) |

If in doubt, start with Bedrock or with LiteLLM in front of Bedrock: they
are the paths tested with a real model ([What has been tested](#que-se-ha-probado)).

## Catalog { #catalogo }

| Provider | Preset (Python / TypeScript) | Upstream | Header (secret value) | Allowed paths | `AgentModel` |
|---|---|---|---|---|---|
| OpenAI | `openai_gateway(secret)` / `openaiGateway(secret)` | `https://api.openai.com` | `authorization` (`Bearer sk-…`) | `POST /v1/responses`, `POST /v1/chat/completions` | `provider="openai"` |
| Gemini (AI Studio) | `gemini_gateway(secret, models=[…])` / `geminiGateway(secret, { models })` | `https://generativelanguage.googleapis.com` | `x-goog-api-key` (the key as is) | per model: `POST /v1beta/models/<m>:streamGenerateContent` and `:generateContent` | `provider="google"` |
| Azure OpenAI v1 | `azure_openai_gateway(secret, resource="…")` / `azureOpenaiGateway(secret, { resource })` | `https://<resource>.openai.azure.com` | `api-key` (the key as is) | `POST /openai/v1/responses`, `POST /openai/v1/chat/completions` | `provider="azure"` |
| OpenRouter | `openrouter_gateway(secret)` / `openrouterGateway(secret)` | `https://openrouter.ai` | `authorization` (`Bearer …`) | `POST /api/v1/chat/completions` | `provider="openai-compatible"`, `base_path="/api/v1"` |
| Groq | `groq_gateway(secret)` / `groqGateway(secret)` | `https://api.groq.com` | `authorization` (`Bearer …`) | `POST /openai/v1/chat/completions` | `provider="openai-compatible"`, `base_path="/openai/v1"` |
| Mistral | `mistral_gateway(secret)` / `mistralGateway(secret)` | `https://api.mistral.ai` | `authorization` (`Bearer …`) | `POST /v1/chat/completions` | `provider="openai-compatible"`, `base_path="/v1"` |
| DeepSeek | `deepseek_gateway(secret)` / `deepseekGateway(secret)` | `https://api.deepseek.com` | `authorization` (`Bearer …`) | `POST /chat/completions` | `provider="openai-compatible"` without `base_path` |
| xAI | `xai_gateway(secret)` / `xaiGateway(secret)` | `https://api.x.ai` | `authorization` (`Bearer xai-…`) | `POST /v1/responses`, `POST /v1/chat/completions` | `provider="openai"` |
| LiteLLM (your own proxy) | `litellm_gateway(secret, upstream="https://…", base_path="/v1")` / `litellmGateway(secret, { upstream, basePath })` | the `upstream` you pass (`https://host`) | `authorization` (`Bearer <virtual key>`) | `POST <base_path>/responses`, `POST <base_path>/chat/completions` | `provider="openai-compatible"`, `base_path` equal to the preset's |

All of them accept `rate_per_minute` / `ratePerMinute`. The exact values are
in `testdata/agent/provider-catalogue.json`, the file that both SDKs' tests
check.

Notes per provider:

- **Gemini.** `models` is required and takes the id without the `models/`
  prefix (`gemini-2.5-flash`). Include the agent's `small_model` too:
  OpenCode uses it for titles and the gateway rejects any model that is not
  in the list.
- **Azure OpenAI.** `resource` is the resource subdomain (1-63 characters
  `[a-z0-9-]`, no hyphen at the start or the end). The `AgentModel`'s `id`
  is the deployment name. Both runtimes use the Responses API
  (`/openai/v1/responses`) with the key in `api-key`: OpenCode with
  `@ai-sdk/azure` and deepagents with `AzureChatOpenAI`.
- **xAI** uses the `openai` provider: OpenCode and deepagents talk to it
  with the Responses API as they do to OpenAI, against another upstream.
- **LiteLLM.** The proxy has to be behind HTTPS and reachable from the
  sandbox's VPC. A `localhost` on your laptop is not: the preset rejects
  `localhost`, loopback (`127.0.0.0/8`), `0.0.0.0` and link-local
  (`169.254.0.0/16`).
- **vLLM, LM Studio, Ollama, Together, Fireworks…** still go through
  `openai_compatible_gateway`, with the same conditions as LiteLLM.

## Example { #ejemplo }

The secret holds the full value of the header (`Bearer …` for OpenAI-style
APIs). With OpenAI:

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, openai_gateway

    SecretStore().create("openai-key", "Bearer <OpenAI API key>")

    spec = AgentSpec(model=AgentModel(provider="openai", id="gpt-5-mini", gateway="openai"))

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={"openai": openai_gateway("openai-key", rate_per_minute=60)},
    ) as sbx:
        result = sbx.agent.run("Summarize the README.", spec=spec)
        print(result.text)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentModel, AgentSpec, AsyncSandbox, gemini_gateway

    MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


    async def main() -> None:
        spec = AgentSpec(
            model=AgentModel(provider="google", id=MODELS[0], gateway="gemini"),
            small_model=MODELS[1],
        )
        async with await AsyncSandbox.create(
            template="rayito-agent",
            allow_internet_access=False,
            gateways={"gemini": gemini_gateway("gemini-key", models=MODELS)},
        ) as sbx:
            result = await sbx.agent.run("Summarize the README.", spec=spec)
            print(result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, openrouterGateway } from "rayito";

    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "openai-compatible",
        id: "anthropic/claude-haiku-4.5",
        gateway: "openrouter",
        basePath: "/api/v1",
      }),
    });

    await using sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: { openrouter: openrouterGateway("openrouter-key", { ratePerMinute: 60 }) },
    });
    const result = await sbx.agent.run("Summarize the README.", { spec });
    console.log(result.text);
    ```

!!! info "Cost and activation"
    - **Activates:** passing the preset in `gateways=` / `gateways` of
      `Sandbox.create` and using it from an `AgentModel`. Building a preset
      does not call AWS or load any optional peer.
    - **AWS resources and calls:** those of the secrets gateway: one Secrets
      Manager read (`GetSecretValue`) per header and cache TTL when the
      gateway is applied. No new resources.
    - **Approximate cost:** $0.05 per 10,000 reads plus $0.40 per month per
      secret (us-east-1, checked on 2026-10-07,
      [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/)).
      The model dominates it, which the provider bills per token.
    - **IAM:** `secretsmanager:GetSecretValue` on the secret
      (`RayitoSecretsReader`).
    - **How to turn it off:** do not pass the gateway or call `sbx.agent`.

## Configure each provider { #configurar-cada-proveedor }

Each section creates the secret (once, from your machine or your backend), builds the preset and the `AgentModel`. Both are passed as in the [example](#ejemplo): the preset in `gateways=` and the model in `AgentSpec`. The key you use in `gateways=` (Python) or `gateways` (TypeScript) is the name `AgentModel.gateway` points to: for example `gateways={"openai": gateway}` with `AgentModel(gateway="openai")`; if they do not match, `sbx.agent.run()` fails with `InvalidArgumentException`. The model ids are examples: use the ones your account offers.

### OpenAI { #openai }

Create the key in a **project** of your own on the OpenAI platform and set the monthly budget and the allowed models there. Reference: [platform.openai.com](https://platform.openai.com/docs/api-reference).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, openai_gateway

    SecretStore().create("openai-key", "Bearer <OpenAI API key>")
    gateway = openai_gateway("openai-key")
    model = AgentModel(provider="openai", id="gpt-5-mini", gateway="openai")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, openaiGateway } from "rayito";

    await new SecretStore().create("openai-key", "Bearer <OpenAI API key>");
    const gateway = openaiGateway("openai-key");
    const model = new AgentModel({ provider: "openai", id: "gpt-5-mini", gateway: "openai" });
    ```

### Gemini (AI Studio) { #gemini-ai-studio }

The secret holds the key as is, without `Bearer`. It is the only non-Bedrock preset that limits the model at the gateway: put the `small_model` in `models` too. Also set the quota and billing of the Google project. Reference: [ai.google.dev](https://ai.google.dev/gemini-api/docs/api-key).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, gemini_gateway

    SecretStore().create("gemini-key", "<Gemini API key>")
    gateway = gemini_gateway("gemini-key", models=["gemini-2.5-flash", "gemini-2.5-flash-lite"])
    model = AgentModel(provider="google", id="gemini-2.5-flash", gateway="gemini")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, geminiGateway } from "rayito";

    await new SecretStore().create("gemini-key", "<Gemini API key>");
    const gateway = geminiGateway("gemini-key", { models: ["gemini-2.5-flash", "gemini-2.5-flash-lite"] });
    const model = new AgentModel({ provider: "google", id: "gemini-2.5-flash", gateway: "gemini" });
    ```

### Azure OpenAI { #azure-openai }

The secret holds the key as is (`api-key` header). The `id` is the deployment name. Create only the deployments you need and limit their quota (TPM). It works with OpenCode and with deepagents. Reference: [learn.microsoft.com](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, azure_openai_gateway

    SecretStore().create("azure-openai-key", "<Azure OpenAI resource key>")
    gateway = azure_openai_gateway("azure-openai-key", resource="my-resource")
    model = AgentModel(provider="azure", id="my-deployment", gateway="azure")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, azureOpenaiGateway } from "rayito";

    await new SecretStore().create("azure-openai-key", "<Azure OpenAI resource key>");
    const gateway = azureOpenaiGateway("azure-openai-key", { resource: "my-resource" });
    const model = new AgentModel({ provider: "azure", id: "my-deployment", gateway: "azure" });
    ```

### OpenRouter { #openrouter }

Put a **credit limit** on the key: OpenRouter's documentation warns that a runaway agent can spend the whole balance. Reference: [openrouter.ai](https://openrouter.ai/docs/api/reference/authentication).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, openrouter_gateway

    SecretStore().create("openrouter-key", "Bearer <OpenRouter key>")
    gateway = openrouter_gateway("openrouter-key")
    model = AgentModel(provider="openai-compatible", id="anthropic/claude-haiku-4.5", gateway="openrouter", base_path="/api/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, openrouterGateway } from "rayito";

    await new SecretStore().create("openrouter-key", "Bearer <OpenRouter key>");
    const gateway = openrouterGateway("openrouter-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "anthropic/claude-haiku-4.5", gateway: "openrouter", basePath: "/api/v1" });
    ```

### Groq { #groq }

Review the organization's usage limits in the Groq console. Reference: [console.groq.com](https://console.groq.com/docs/openai).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, groq_gateway

    SecretStore().create("groq-key", "Bearer <Groq key>")
    gateway = groq_gateway("groq-key")
    model = AgentModel(provider="openai-compatible", id="<Groq model>", gateway="groq", base_path="/openai/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, groqGateway } from "rayito";

    await new SecretStore().create("groq-key", "Bearer <Groq key>");
    const gateway = groqGateway("groq-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<Groq model>", gateway: "groq", basePath: "/openai/v1" });
    ```

### Mistral { #mistral }

Set the workspace spending limits in the Mistral console. Reference: [docs.mistral.ai](https://docs.mistral.ai/api).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, mistral_gateway

    SecretStore().create("mistral-key", "Bearer <Mistral key>")
    gateway = mistral_gateway("mistral-key")
    model = AgentModel(provider="openai-compatible", id="<Mistral model>", gateway="mistral", base_path="/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, mistralGateway } from "rayito";

    await new SecretStore().create("mistral-key", "Bearer <Mistral key>");
    const gateway = mistralGateway("mistral-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<Mistral model>", gateway: "mistral", basePath: "/v1" });
    ```

### DeepSeek { #deepseek }

No `base_path`: the API hangs off the root. DeepSeek is prepaid: the balance you load is the cap. Reference: [api-docs.deepseek.com](https://api-docs.deepseek.com/).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, deepseek_gateway

    SecretStore().create("deepseek-key", "Bearer <DeepSeek key>")
    gateway = deepseek_gateway("deepseek-key")
    model = AgentModel(provider="openai-compatible", id="<DeepSeek model>", gateway="deepseek")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, deepseekGateway } from "rayito";

    await new SecretStore().create("deepseek-key", "Bearer <DeepSeek key>");
    const gateway = deepseekGateway("deepseek-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<DeepSeek model>", gateway: "deepseek" });
    ```

### xAI { #xai }

It uses the `openai` provider against another upstream. Set the team's spending limits in the xAI console. Reference: [docs.x.ai](https://docs.x.ai/docs/api-reference).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, xai_gateway

    SecretStore().create("xai-key", "Bearer <xAI API key>")
    gateway = xai_gateway("xai-key")
    model = AgentModel(provider="openai", id="<xAI model>", gateway="xai")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, xaiGateway } from "rayito";

    await new SecretStore().create("xai-key", "Bearer <xAI API key>");
    const gateway = xaiGateway("xai-key");
    const model = new AgentModel({ provider: "openai", id: "<xAI model>", gateway: "xai" });
    ```

### LiteLLM (your own proxy) { #litellm-tu-propio-proxy }

If you already have a [LiteLLM proxy](https://docs.litellm.ai/docs/proxy/user_keys),
the agent can use any model that proxy serves (from any provider or
local) with a single **virtual key**. Conditions:

- The proxy listens on **HTTPS** and is reachable from the sandbox's VPC.
  `upstream` is `https://host` or `https://host:port`, with no path. The
  preset rejects `http://`, `localhost`, loopback, `0.0.0.0` and
  link-local.
- The secret holds `Bearer <virtual key>`. Create the virtual key with a
  budget (`max_budget`) and a list of models: it is the only real cap,
  because the gateway does not see the model.
- `base_path` is `/v1` by default; the `AgentModel`'s has to be the same.
  `POST <base_path>/chat/completions` and `POST <base_path>/responses` are
  allowed.

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, litellm_gateway

    SecretStore().create("litellm-key", "Bearer <LiteLLM virtual key>")
    gateway = litellm_gateway("litellm-key", upstream="https://llm.example.com")
    model = AgentModel(provider="openai-compatible", id="<proxy model>", gateway="litellm", base_path="/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, litellmGateway } from "rayito";

    await new SecretStore().create("litellm-key", "Bearer <LiteLLM virtual key>");
    const gateway = litellmGateway("litellm-key", { upstream: "https://llm.example.com" });
    const model = new AgentModel({ provider: "openai-compatible", id: "<proxy model>", gateway: "litellm", basePath: "/v1" });
    ```

### Local models { #modelos-locales }

vLLM, LM Studio, Ollama and the like work if you publish them behind
**HTTPS** at an address the sandbox's VPC can reach (an internal load
balancer, for example). You can point at them directly with
`openai_compatible_gateway(secret, upstream=, base_path=)` /
`openaiCompatibleGateway(secret, { upstream, basePath })`, or put them
behind LiteLLM to get virtual keys and budgets. The `localhost` on your
laptop is never reachable from the microVM.

## Provider-side limits { #limites-del-lado-del-proveedor }

Only Bedrock and Gemini carry the model in the **path**, so the gateway's
allowlist limits it. With Anthropic (`anthropic_gateway`, only
`POST /v1/messages`) and with OpenAI-style APIs (OpenAI, Azure OpenAI, xAI,
OpenRouter, Groq, Mistral, DeepSeek, LiteLLM) the model goes in the
**body**, which the gateway forwards without reading: the code in the
sandbox can ask for any model the key allows, outside the SDK's token
budget ([SECURITY.md T29](../security.md#agente-de-codigo-dentro-del-sandbox)).
Set the cap at the provider:

- **OpenAI:** a project of its own for the key, with a monthly budget and
  allowed models.
- **OpenRouter:** a credit limit on the key. Its own documentation warns
  that a runaway agent can spend the whole balance.
- **Azure OpenAI:** only the deployments you need and their quota (TPM).
- **Anthropic:** a workspace of its own for the key, with a spending limit.
- **LiteLLM:** budget and allowed models per virtual key.
- **Groq, Mistral, DeepSeek, xAI:** spending or usage limits on the account
  or the team.

`rate_per_minute` also helps: it limits the requests that go through the
gateway, whether the agent or any other code in the sandbox makes them.

## What has been tested { #que-se-ha-probado }

| Path | How it was tested |
|---|---|
| Bedrock (`bedrock_gateway`), OpenCode and deepagents | with a real model, on real AWS (MicroVMs), from Python and TypeScript, in each release's acceptance |
| Bedrock's OpenAI-compatible API (`bedrock-runtime` `/openai/v1` and `bedrock-mantle`) through `openai_compatible_gateway` | with a real model, in the [local environment](probar-en-local.md#otros-proveedores-sin-claves-de-terceros) with egress closed: tools, session and token usage |
| `provider="openai"` (Responses API) against `bedrock-mantle` | with a real model, locally, one turn without tools |
| LiteLLM (`litellm_gateway`) in front of Bedrock | with a real model, locally: tools, session and token usage |
| OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek and xAI | only against a [fake upstream](probar-en-local.md#proveedores-de-modelo-sin-claves): each preset with OpenCode and deepagents, from Python and TypeScript, completes a turn, the key arrives in its header, the marker never leaves and a path outside the list receives 403 |

The last eight have not been tested with a real provider key. With a key of
your own, the
[smoke test](probar-en-local.md#prueba-de-humo-contra-las-apis-reales)
does a short turn against the real API for less than 0.01 USD per
provider.

## Unsupported providers { #proveedores-no-admitidos }

Rayito only supports API keys. These forms of access were reviewed
(checked on 2026-10-07) and are not supported:

| Option | Decision | Why |
|---|---|---|
| ChatGPT plan with the OAuth that ships with OpenCode | Rejected | It uses another application's `client_id` and ChatGPT's `backend-api`. OpenAI expressly asks not to point at those endpoints ([models-and-inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)). It also requires the refresh token inside the sandbox, where the agent could read it. |
| ChatGPT plan with "Sign in with ChatGPT" | Deferred | OpenAI documents it for open-source and locally hosted apps; for a remotely hosted app it asks you to fill in an interest form ([overview](https://developers.openai.com/siwc/token-sharing-open-source)). It is pending OpenAI confirming this case. For programmatic use, OpenAI recommends an API key ([Codex auth](https://learn.chatgpt.com/docs/auth)). |
| Claude Pro/Max | Rejected | [Anthropic's consumer terms](https://www.anthropic.com/legal/consumer-terms) and Claude Code's [legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) page do not allow third parties to route requests with consumer-plan credentials. OpenCode removed that access ([provider docs](https://opencode.ai/docs/providers/)). Use an Anthropic or Bedrock API key. |
| GitHub Copilot | Rejected | GitHub supports Copilot in OpenCode for interactive use ([changelog](https://github.blog/changelog/2026-01-16-github-copilot-now-supports-opencode/)), but there are no terms for unattended fleets (its [acceptable use policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) limits mass automated activity), and it would require reusing OpenCode's OAuth. |
| SuperGrok | Rejected | xAI offers it in OpenCode ([announcement](https://x.ai/news/grok-opencode)), but there are no published terms for automation. Use the xAI API key. |

Nor can they sneak in some other way: `provider`, `enabled_providers` and
`plugin` are reserved keys of `raw_config` (so a Copilot, Codex or Gemini
OAuth plugin is not loaded), no SDK type has a credential field, and the
OpenCode adapter never writes an `auth.json` or the `auth` or `plugin`
keys.
