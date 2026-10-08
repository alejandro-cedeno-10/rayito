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

Rayito sólo admite claves de API. Estas formas de acceso se revisaron y no
se admiten:

| Opción | Decisión | Por qué |
|---|---|---|
| Plan de ChatGPT con el OAuth que trae OpenCode | Rechazada | Usa el `client_id` de otra aplicación y el `backend-api` de ChatGPT, que OpenAI pide no usar, y exige el refresh token dentro del sandbox, donde el agente podría leerlo. |
| Plan de ChatGPT con "Sign in with ChatGPT" | Aplazada | OpenAI lo permite para apps open-source alojadas en local; las apps alojadas necesitan su aprobación. Se espera su confirmación para este caso. |
| Claude Pro/Max | Rechazada | Los términos de Anthropic prohíben que terceros enruten peticiones por credenciales de planes de consumo o las intermedien. Usa una clave de API de Anthropic o Bedrock. |
| GitHub Copilot | Rechazada | Su soporte en OpenCode es para uso interactivo; no hay términos para flotas desatendidas y exigiría reutilizar el OAuth de OpenCode. |
| SuperGrok | Rechazada | No hay términos publicados para automatización. Usa la clave de API de xAI. |

Tampoco se pueden colar por otro camino: `provider`, `enabled_providers` y
`plugin` son claves reservadas de `raw_config` (así no se carga un plugin
OAuth de Copilot, Codex o Gemini), ningún tipo del SDK tiene campo de
credencial, y el adaptador de OpenCode nunca escribe un `auth.json` ni las
claves `auth` o `plugin`.
