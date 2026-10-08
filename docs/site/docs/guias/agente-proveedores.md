---
title: Proveedores del agente
description: Qué proveedores de modelo puede usar sbx.agent a través de la pasarela de secretos (OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI, LiteLLM), cómo configurar cada uno y cuáles no se admiten y por qué.
---

# Proveedores del agente

El agente de [`sbx.agent`](agente-en-el-sandbox.md) llama a su modelo sólo a
través de la [pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md):
la clave vive en Secrets Manager, `rayd` la inyecta en la cabecera y el
runtime (OpenCode o deepagents) sólo ve un marcador. Además de Bedrock,
Anthropic y `openai_compatible_gateway`, hay un preset por proveedor con
clave de API estática. Cada preset fija el `upstream`, la cabecera y las
rutas permitidas, y se combina con un `AgentModel`.

## Catálogo

| Proveedor | Preset (Python / TypeScript) | Upstream | Cabecera (valor del secreto) | Rutas permitidas | `AgentModel` |
|---|---|---|---|---|---|
| OpenAI | `openai_gateway(secret)` / `openaiGateway(secret)` | `https://api.openai.com` | `authorization` (`Bearer sk-…`) | `POST /v1/responses`, `POST /v1/chat/completions` | `provider="openai"` |
| Gemini (AI Studio) | `gemini_gateway(secret, models=[…])` / `geminiGateway(secret, { models })` | `https://generativelanguage.googleapis.com` | `x-goog-api-key` (la clave tal cual) | por modelo: `POST /v1beta/models/<m>:streamGenerateContent` y `:generateContent` | `provider="google"` |
| Azure OpenAI v1 | `azure_openai_gateway(secret, resource="…")` / `azureOpenaiGateway(secret, { resource })` | `https://<resource>.openai.azure.com` | `api-key` (la clave tal cual) | `POST /openai/v1/responses`, `POST /openai/v1/chat/completions` | `provider="azure"` |
| OpenRouter | `openrouter_gateway(secret)` / `openrouterGateway(secret)` | `https://openrouter.ai` | `authorization` (`Bearer …`) | `POST /api/v1/chat/completions` | `provider="openai-compatible"`, `base_path="/api/v1"` |
| Groq | `groq_gateway(secret)` / `groqGateway(secret)` | `https://api.groq.com` | `authorization` (`Bearer …`) | `POST /openai/v1/chat/completions` | `provider="openai-compatible"`, `base_path="/openai/v1"` |
| Mistral | `mistral_gateway(secret)` / `mistralGateway(secret)` | `https://api.mistral.ai` | `authorization` (`Bearer …`) | `POST /v1/chat/completions` | `provider="openai-compatible"`, `base_path="/v1"` |
| DeepSeek | `deepseek_gateway(secret)` / `deepseekGateway(secret)` | `https://api.deepseek.com` | `authorization` (`Bearer …`) | `POST /chat/completions` | `provider="openai-compatible"` sin `base_path` |
| xAI | `xai_gateway(secret)` / `xaiGateway(secret)` | `https://api.x.ai` | `authorization` (`Bearer xai-…`) | `POST /v1/responses`, `POST /v1/chat/completions` | `provider="openai"` |
| LiteLLM (proxy propio) | `litellm_gateway(secret, upstream="https://…", base_path="/v1")` / `litellmGateway(secret, { upstream, basePath })` | el `upstream` que pases (`https://host`) | `authorization` (`Bearer <clave virtual>`) | `POST <base_path>/responses`, `POST <base_path>/chat/completions` | `provider="openai-compatible"`, `base_path` igual al del preset |

Todos aceptan `rate_per_minute` / `ratePerMinute`. Los valores exactos están
en `testdata/agent/provider-catalogue.json`, el fichero que comprueban los
tests de los dos SDKs.

Notas por proveedor:

- **Gemini.** `models` es obligatorio y lleva el id sin el prefijo
  `models/` (`gemini-2.5-flash`). Incluye también el `small_model` del
  agente: OpenCode lo usa para los títulos y la pasarela rechaza cualquier
  modelo que no esté en la lista.
- **Azure OpenAI.** `resource` es el subdominio del recurso (1-63
  caracteres `[a-z0-9-]`, sin guion al principio ni al final). El `id` del
  `AgentModel` es el nombre del despliegue. Los dos runtimes usan la
  Responses API (`/openai/v1/responses`) con la clave en `api-key`:
  OpenCode con `@ai-sdk/azure` y deepagents con `AzureChatOpenAI`.
- **xAI** usa el proveedor `openai`: OpenCode y deepagents le hablan con la
  Responses API como a OpenAI, contra otro upstream.
- **LiteLLM.** El proxy tiene que estar detrás de HTTPS y ser alcanzable
  desde la VPC del sandbox. Un `localhost` de tu portátil no lo es: el preset
  rechaza `localhost`, loopback (`127.0.0.0/8`), `0.0.0.0` y enlace local
  (`169.254.0.0/16`).
- **vLLM, LM Studio, Ollama, Together, Fireworks…** siguen yendo por
  `openai_compatible_gateway`, con las mismas condiciones que LiteLLM.

## Ejemplo

El secreto guarda el valor completo de la cabecera (`Bearer …` para las
APIs al estilo de OpenAI). Con OpenAI:

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, openai_gateway

    SecretStore().create("openai-key", "Bearer <clave de API de OpenAI>")

    spec = AgentSpec(model=AgentModel(provider="openai", id="gpt-5-mini", gateway="openai"))

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={"openai": openai_gateway("openai-key", rate_per_minute=60)},
    ) as sbx:
        result = sbx.agent.run("Resume el README.", spec=spec)
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
            result = await sbx.agent.run("Resume el README.", spec=spec)
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
    const result = await sbx.agent.run("Resume el README.", { spec });
    console.log(result.text);
    ```

!!! info "Coste y activación"
    - **Activa:** pasar el preset en `gateways=` / `gateways` de
      `Sandbox.create` y usarlo desde un `AgentModel`. Construir un preset
      no llama a AWS ni carga ningún peer opcional.
    - **Recursos y llamadas AWS:** los de la pasarela de secretos: una
      lectura de Secrets Manager (`GetSecretValue`) por cabecera y TTL de la
      caché al aplicar la pasarela. Ningún recurso nuevo.
    - **Coste aproximado:** $0,05 por 10 000 lecturas más $0,40 al mes por
      secreto (us-east-1, consultado el 2026-10-07,
      [precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/)).
      Lo domina el modelo, que factura el proveedor por tokens.
    - **IAM:** `secretsmanager:GetSecretValue` sobre el secreto
      (`RayitoSecretsReader`).
    - **Cómo apagarla:** no pasar la pasarela ni llamar a `sbx.agent`.

## Configurar cada proveedor

Cada apartado crea el secreto (una vez, desde tu máquina o tu backend), construye el preset y el `AgentModel`. Los dos se pasan como en el [ejemplo](#ejemplo): el preset en `gateways=` y el modelo en `AgentSpec`. La clave que uses en `gateways=` (Python) o `gateways` (TypeScript) es el nombre al que apunta `AgentModel.gateway`: por ejemplo `gateways={"openai": gateway}` con `AgentModel(gateway="openai")`; si no coinciden, `sbx.agent.run()` falla con `InvalidArgumentException`. Los ids de modelo son ejemplos: usa los que ofrezca tu cuenta.

### OpenAI

Crea la clave en un **proyecto** propio de la plataforma de OpenAI y fija ahí el presupuesto mensual y los modelos permitidos. Referencia: [platform.openai.com](https://platform.openai.com/docs/api-reference).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, openai_gateway

    SecretStore().create("openai-key", "Bearer <clave de API de OpenAI>")
    gateway = openai_gateway("openai-key")
    model = AgentModel(provider="openai", id="gpt-5-mini", gateway="openai")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, openaiGateway } from "rayito";

    await new SecretStore().create("openai-key", "Bearer <clave de API de OpenAI>");
    const gateway = openaiGateway("openai-key");
    const model = new AgentModel({ provider: "openai", id: "gpt-5-mini", gateway: "openai" });
    ```

### Gemini (AI Studio)

El secreto guarda la clave tal cual, sin `Bearer`. Es el único preset no Bedrock que limita el modelo en la pasarela: pon en `models` también el `small_model`. Fija además la cuota y la facturación del proyecto de Google. Referencia: [ai.google.dev](https://ai.google.dev/gemini-api/docs/api-key).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, gemini_gateway

    SecretStore().create("gemini-key", "<clave de API de Gemini>")
    gateway = gemini_gateway("gemini-key", models=["gemini-2.5-flash", "gemini-2.5-flash-lite"])
    model = AgentModel(provider="google", id="gemini-2.5-flash", gateway="gemini")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, geminiGateway } from "rayito";

    await new SecretStore().create("gemini-key", "<clave de API de Gemini>");
    const gateway = geminiGateway("gemini-key", { models: ["gemini-2.5-flash", "gemini-2.5-flash-lite"] });
    const model = new AgentModel({ provider: "google", id: "gemini-2.5-flash", gateway: "gemini" });
    ```

### Azure OpenAI

El secreto guarda la clave tal cual (cabecera `api-key`). El `id` es el nombre del despliegue. Crea sólo los despliegues que necesites y limita su cuota (TPM). Funciona con OpenCode y con deepagents. Referencia: [learn.microsoft.com](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, azure_openai_gateway

    SecretStore().create("azure-openai-key", "<clave del recurso de Azure OpenAI>")
    gateway = azure_openai_gateway("azure-openai-key", resource="mi-recurso")
    model = AgentModel(provider="azure", id="mi-despliegue", gateway="azure")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, azureOpenaiGateway } from "rayito";

    await new SecretStore().create("azure-openai-key", "<clave del recurso de Azure OpenAI>");
    const gateway = azureOpenaiGateway("azure-openai-key", { resource: "mi-recurso" });
    const model = new AgentModel({ provider: "azure", id: "mi-despliegue", gateway: "azure" });
    ```

### OpenRouter

Pon un **límite de crédito** en la clave: la documentación de OpenRouter avisa de que un agente desbocado puede gastar todo el saldo. Referencia: [openrouter.ai](https://openrouter.ai/docs/api/reference/authentication).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, openrouter_gateway

    SecretStore().create("openrouter-key", "Bearer <clave de OpenRouter>")
    gateway = openrouter_gateway("openrouter-key")
    model = AgentModel(provider="openai-compatible", id="anthropic/claude-haiku-4.5", gateway="openrouter", base_path="/api/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, openrouterGateway } from "rayito";

    await new SecretStore().create("openrouter-key", "Bearer <clave de OpenRouter>");
    const gateway = openrouterGateway("openrouter-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "anthropic/claude-haiku-4.5", gateway: "openrouter", basePath: "/api/v1" });
    ```

### Groq

Revisa los límites de uso de la organización en la consola de Groq. Referencia: [console.groq.com](https://console.groq.com/docs/openai).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, groq_gateway

    SecretStore().create("groq-key", "Bearer <clave de Groq>")
    gateway = groq_gateway("groq-key")
    model = AgentModel(provider="openai-compatible", id="<modelo de Groq>", gateway="groq", base_path="/openai/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, groqGateway } from "rayito";

    await new SecretStore().create("groq-key", "Bearer <clave de Groq>");
    const gateway = groqGateway("groq-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<modelo de Groq>", gateway: "groq", basePath: "/openai/v1" });
    ```

### Mistral

Fija los límites de gasto del workspace en la consola de Mistral. Referencia: [docs.mistral.ai](https://docs.mistral.ai/api).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, mistral_gateway

    SecretStore().create("mistral-key", "Bearer <clave de Mistral>")
    gateway = mistral_gateway("mistral-key")
    model = AgentModel(provider="openai-compatible", id="<modelo de Mistral>", gateway="mistral", base_path="/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, mistralGateway } from "rayito";

    await new SecretStore().create("mistral-key", "Bearer <clave de Mistral>");
    const gateway = mistralGateway("mistral-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<modelo de Mistral>", gateway: "mistral", basePath: "/v1" });
    ```

### DeepSeek

Sin `base_path`: la API cuelga de la raíz. DeepSeek es de prepago: el saldo cargado es el tope. Referencia: [api-docs.deepseek.com](https://api-docs.deepseek.com/).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, deepseek_gateway

    SecretStore().create("deepseek-key", "Bearer <clave de DeepSeek>")
    gateway = deepseek_gateway("deepseek-key")
    model = AgentModel(provider="openai-compatible", id="<modelo de DeepSeek>", gateway="deepseek")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, deepseekGateway } from "rayito";

    await new SecretStore().create("deepseek-key", "Bearer <clave de DeepSeek>");
    const gateway = deepseekGateway("deepseek-key");
    const model = new AgentModel({ provider: "openai-compatible", id: "<modelo de DeepSeek>", gateway: "deepseek" });
    ```

### xAI

Usa el proveedor `openai` contra otro upstream. Fija los límites de gasto del equipo en la consola de xAI. Referencia: [docs.x.ai](https://docs.x.ai/docs/api-reference).

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, xai_gateway

    SecretStore().create("xai-key", "Bearer <clave de API de xAI>")
    gateway = xai_gateway("xai-key")
    model = AgentModel(provider="openai", id="<modelo de xAI>", gateway="xai")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, xaiGateway } from "rayito";

    await new SecretStore().create("xai-key", "Bearer <clave de API de xAI>");
    const gateway = xaiGateway("xai-key");
    const model = new AgentModel({ provider: "openai", id: "<modelo de xAI>", gateway: "xai" });
    ```

### LiteLLM (tu propio proxy)

Si ya tienes un [proxy de LiteLLM](https://docs.litellm.ai/docs/proxy/user_keys),
el agente puede usar cualquier modelo que ese proxy sirva (de cualquier
proveedor o local) con una sola **clave virtual**. Condiciones:

- El proxy escucha en **HTTPS** y es alcanzable desde la VPC del sandbox.
  `upstream` es `https://host` o `https://host:puerto`, sin ruta. El preset
  rechaza `http://`, `localhost`, loopback, `0.0.0.0` y enlace local.
- El secreto guarda `Bearer <clave virtual>`. Crea la clave virtual con
  presupuesto (`max_budget`) y lista de modelos: es el único tope real,
  porque la pasarela no ve el modelo.
- `base_path` es `/v1` por defecto; el del `AgentModel` tiene que ser el
  mismo. Se permiten `POST <base_path>/chat/completions` y
  `POST <base_path>/responses`.

=== "Python"

    ```python
    from rayito import AgentModel, SecretStore, litellm_gateway

    SecretStore().create("litellm-key", "Bearer <clave virtual de LiteLLM>")
    gateway = litellm_gateway("litellm-key", upstream="https://llm.example.com")
    model = AgentModel(provider="openai-compatible", id="<modelo del proxy>", gateway="litellm", base_path="/v1")
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, SecretStore, litellmGateway } from "rayito";

    await new SecretStore().create("litellm-key", "Bearer <clave virtual de LiteLLM>");
    const gateway = litellmGateway("litellm-key", { upstream: "https://llm.example.com" });
    const model = new AgentModel({ provider: "openai-compatible", id: "<modelo del proxy>", gateway: "litellm", basePath: "/v1" });
    ```

### Modelos locales

vLLM, LM Studio, Ollama y similares funcionan si los publicas detrás de
**HTTPS** en una dirección que la VPC del sandbox alcance (un balanceador
interno, por ejemplo). Puedes apuntar a ellos directamente con
`openai_compatible_gateway(secret, upstream=, base_path=)` /
`openaiCompatibleGateway(secret, { upstream, basePath })`, o ponerlos detrás
de LiteLLM para tener claves virtuales y presupuestos. El `localhost` de tu
portátil nunca es alcanzable desde la microVM.

## Límites del lado del proveedor

Sólo Bedrock y Gemini llevan el modelo en la **ruta**, así que la allowlist
de la pasarela lo limita. En Anthropic (`anthropic_gateway`, sólo
`POST /v1/messages`) y en las APIs al estilo de OpenAI (OpenAI, Azure
OpenAI, xAI, OpenRouter, Groq, Mistral, DeepSeek, LiteLLM) el modelo va en
el **cuerpo**, que la pasarela reenvía sin leer: el código del sandbox
puede pedir cualquier modelo que la clave permita, fuera del presupuesto
de tokens del SDK ([SECURITY.md T29](../security.md#agente-de-codigo-dentro-del-sandbox)).
Fija el tope en el proveedor:

- **OpenAI:** un proyecto propio para la clave, con presupuesto mensual y
  modelos permitidos.
- **OpenRouter:** límite de crédito en la clave. Su propia documentación
  avisa de que un agente desbocado puede gastar todo el saldo.
- **Azure OpenAI:** sólo los despliegues que necesites y su cuota (TPM).
- **Anthropic:** un workspace propio para la clave, con límite de gasto.
- **LiteLLM:** presupuesto y modelos permitidos por clave virtual.
- **Groq, Mistral, DeepSeek, xAI:** límites de gasto o de uso de la cuenta
  o del equipo.

`rate_per_minute` también ayuda: limita las peticiones que pasan por la
pasarela, las haga el agente o cualquier otro código del sandbox.

## Proveedores no admitidos

Rayito sólo admite claves de API. Estas formas de acceso se revisaron
(consultado el 2026-10-07) y no se admiten:

| Opción | Decisión | Por qué |
|---|---|---|
| Plan de ChatGPT con el OAuth que trae OpenCode | Rechazada | Usa el `client_id` de otra aplicación y el `backend-api` de ChatGPT. OpenAI pide expresamente no apuntar a esos endpoints ([models-and-inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)). Además exige el refresh token dentro del sandbox, donde el agente podría leerlo. |
| Plan de ChatGPT con "Sign in with ChatGPT" | Aplazada | OpenAI lo documenta para apps open-source y alojadas en local; para una app alojada en remoto pide rellenar un formulario de interés ([overview](https://developers.openai.com/siwc/token-sharing-open-source)). Queda pendiente de que OpenAI confirme este caso. Para uso programático, OpenAI recomienda clave de API ([Codex auth](https://learn.chatgpt.com/docs/auth)). |
| Claude Pro/Max | Rechazada | Los [términos de consumo de Anthropic](https://www.anthropic.com/legal/consumer-terms) y la página [legal and compliance de Claude Code](https://code.claude.com/docs/en/legal-and-compliance) no permiten que terceros enruten peticiones con credenciales de planes de consumo. OpenCode quitó ese acceso ([docs de proveedores](https://opencode.ai/docs/providers/)). Usa una clave de API de Anthropic o Bedrock. |
| GitHub Copilot | Rechazada | GitHub admite Copilot en OpenCode para uso interactivo ([changelog](https://github.blog/changelog/2026-01-16-github-copilot-now-supports-opencode/)), pero no hay términos para flotas desatendidas (su [política de uso aceptable](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) limita la actividad automatizada masiva), y exigiría reutilizar el OAuth de OpenCode. |
| SuperGrok | Rechazada | xAI lo ofrece en OpenCode ([anuncio](https://x.ai/news/grok-opencode)), pero no hay términos publicados para automatización. Usa la clave de API de xAI. |

Tampoco se pueden colar por otro camino: `provider`, `enabled_providers` y
`plugin` son claves reservadas de `raw_config` (así no se carga un plugin
OAuth de Copilot, Codex o Gemini), ningún tipo del SDK tiene campo de
credencial, y el adaptador de OpenCode nunca escribe un `auth.json` ni las
claves `auth` o `plugin`.
