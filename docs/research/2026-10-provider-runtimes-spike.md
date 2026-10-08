# Spike: OpenCode y deepagents con los nuevos proveedores

2026-10-07, cambio `ai-agent-providers`, tareas §6 (fase 1:
presets de clave de API estática). Objetivo: comprobar, antes de fijar los
adaptadores, que el binario de OpenCode fijado (`agentOpencodeVersion` de
`limits.json`, 1.18.34) y los paquetes de LangChain del venv de deepagents
(`requirements-deepagents.txt`) hablan con OpenAI, Gemini, Azure OpenAI v1
y las APIs compatibles con OpenAI **sólo a través de la pasarela**, sin
descargar nada y con las rutas y cabeceras que permite cada preset.

**Resultado: GO, con una corrección.** Los tres paquetes nativos van dentro
del binario y funcionan sin red; el mapeo de Azure de OpenCode que tenía el
catálogo (`baseURL` = pasarela + `/openai`) producía `/openai/responses`,
fuera de la allowlist, y pasa a `/openai/v1`. deepagents con Azure deja de
ser `UnimplementedError`: `AzureChatOpenAI` manda la clave en `api-key` y
no en `Authorization`.

## Método

Una pasarela falsa (un `http.server` de la biblioteca estándar en
`127.0.0.1`) que contesta 500 a todo y registra método, ruta con su query,
las cabeceras de credencial (`authorization`, `api-key`, `x-goog-api-key`)
y el `model` del cuerpo. Para OpenCode, el `opencode.json` sale de
`build_opencode_config` del SDK con la URL de esa pasarela, y se ejecuta el
binario de la release (variante darwin-arm64 de la misma versión; el
código JavaScript empaquetado es el mismo que en linux-arm64) con las
variables de `OPENCODE_FLAG_ENVS`, un `HOME` vacío y
`HTTPS_PROXY`/`HTTP_PROXY` hacia un puerto cerrado (salvo `127.0.0.1`),
para que cualquier descarga falle. Para deepagents, un venv con los pines
del fichero de requisitos (`langchain-openai` 1.6.7, `openai` 3.25.0,
`langchain-google-genai` 4.4.0, `google-genai` 2.28.0) y las clases tal
como las construye `build_model` del runner, con `invoke` y `ainvoke`.

## OpenCode 1.18.34

- **Paquetes empaquetados.** `provider.ts` de la etiqueta `v1.18.34`
  (`BUNDLED_PROVIDERS`) y las cadenas del binario incluyen
  `@ai-sdk/openai`, `@ai-sdk/openai-compatible`, `@ai-sdk/google`,
  `@ai-sdk/azure`, `@ai-sdk/xai`, `@ai-sdk/mistral`, `@ai-sdk/groq`,
  `@ai-sdk/anthropic` y `@ai-sdk/amazon-bedrock`, entre otros. Ninguno se
  instala de npm en tiempo de ejecución. El adaptador declara `npm` de
  forma explícita para no depender del catálogo de models.dev.
- **Rutas y cabeceras medidas** (`baseURL` = pasarela + ruta base):

| `AgentModel.provider` | Ruta base | Petición observada | Cabecera |
|---|---|---|---|
| `openai` (y xAI) | `/v1` | `POST /v1/responses` | `authorization: Bearer <marcador>` |
| `google` | `/v1beta` | `POST /v1beta/models/<m>:streamGenerateContent?alt=sse` | `x-goog-api-key` |
| `azure` (antes) | `/openai` | `POST /openai/responses` (fuera de la allowlist) | `api-key` |
| `azure` (ahora) | `/openai/v1` | `POST /openai/v1/responses`, sin `api-version` | `api-key` |
| `openai-compatible` (OpenRouter) | `/api/v1` | `POST /api/v1/chat/completions` | `authorization` |
| `openai-compatible` (DeepSeek) | (vacía) | `POST /chat/completions` | `authorization` |
| `openai-compatible` (LiteLLM) | `/v1` | `POST /v1/chat/completions` | `authorization` |

  La pasarela compara la allowlist sólo con la ruta (la query se reenvía
  tal cual), así que `?alt=sse` de Gemini no afecta.
- **Sin red.** Cada ejecución termina con `error` (`APIError`, 500 de la
  pasarela falsa) tras los reintentos del AI SDK (seis peticiones, unos
  70 s); ninguna otra conexión hace falta para cargar la configuración ni
  el proveedor.
- **WebSockets.** El plugin interno de OpenAI de OpenCode puede abrir un
  WebSocket hacia la Responses API (`OPENCODE_EXPERIMENTAL_WEBSOCKETS`,
  activo por defecto sólo en los canales `local`, `dev` y `beta`). Con
  `OPENCODE_DISABLE_DEFAULT_PLUGINS=1` ese plugin ni se carga, pero el
  adaptador fija además `OPENCODE_EXPERIMENTAL_WEBSOCKETS=0` para que un
  cambio de canal o de versión no lo encienda.
- **Suscripciones.** Los plugins OAuth (Codex/ChatGPT, Copilot, Azure) son
  internos y van detrás de `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`; el
  adaptador no monta `auth.json` y `plugin` es clave reservada de
  `raw_config`.

## deepagents (LangChain)

| Proveedor | Clase | Petición observada | Cabecera |
|---|---|---|---|
| `openai` | `ChatOpenAI(base_url=<gw>/v1, use_responses_api=True)` | `POST /v1/responses` | `authorization` |
| `google` | `ChatGoogleGenerativeAI(base_url=<gw>)` | `POST /v1beta/models/<m>:generateContent` (`stream` usa `:streamGenerateContent`) | `x-goog-api-key` |
| `azure` | `AzureChatOpenAI(base_url=<gw>/openai/v1, api_version="v1", use_responses_api=True)` | `POST /openai/v1/responses?api-version=v1` | sólo `api-key` |

- `ChatOpenAI` contra Azure mandaría la clave en `Authorization`, que Azure
  lee como token de Entra; quitarla con `default_headers` o con un cliente
  propio choca con la validación de LangChain o con el cliente de OpenAI 3.x.
  `AzureChatOpenAI` usa la autenticación por `api-key` del cliente
  oficial de Azure.
- Con `use_responses_api=False`, el cliente de Azure reescribe
  `chat/completions` a `/openai/v1/deployments/<m>/chat/completions`, fuera
  de la allowlist: el runner usa siempre la Responses API con Azure.
- `AzureChatOpenAI` exige `api_version`. La documentación de la API v1
  (learn.microsoft.com/azure/ai-foundry/openai/api-version-lifecycle,
  consultada el 2026-10-07) dice que la v1 ya no necesita versiones con
  fecha; `v1` es además el valor por defecto que usaba antes `@ai-sdk/azure`.
  Falta comprobarlo contra un recurso real (ver abajo).
- Los tres paquetes ya estaban en el venv; la prueba de humo de
  `AgentTemplate` los importa ahora en el build.

## Pendiente

- Una prueba real contra cada proveedor con el egress cerrado (barata, con
  opción explícita y fuera de CI): Azure con `api-version=v1`, Gemini con
  `streamGenerateContent` desde deepagents y xAI con la Responses API.
