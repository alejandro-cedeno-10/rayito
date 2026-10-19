---
title: Despliega tu agente
description: Describe un agente (Strands, Strands harness, Hermes, LangGraph, deepagents, OpenCode o el tuyo) en un fichero, y constrúyelo, ejecútalo, despliégalo como servicio HTTP o expórtalo a un contenedor, desde Python, TypeScript o la CLI.
---

# Despliega tu agente

<small>Desde 0.14.0.</small>

`AgentProject` es la descripción de un agente en un solo objeto o en un solo
fichero (`rayito-agent.toml`): qué framework usa, su código, su modelo (siempre
por la [pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md))
y cómo se sirve. Con él haces cuatro cosas, con el mismo objeto en Python, en
TypeScript y en la CLI:

| Operación | Qué hace | Llama a AWS |
|---|---|---|
| `build()` | construye la imagen con el framework y tu código, todo fijado por hash | sí |
| `run()` | crea un sandbox, ejecuta el agente una vez y lo destruye | sí |
| `deploy()` | deja el agente como servicio HTTP en un sandbox vivo | sí |
| `export()` | escribe un contexto de build de contenedor (Docker o AgentCore) | no |

Los frameworks con soporte propio son Strands (SDK de Python, harness y SDK de
TypeScript), Hermes, LangGraph, deepagents y OpenCode. Cualquier otro
(CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK, Vercel AI SDK…)
entra por [`CustomRuntime`](#otros-frameworks-customruntime).

## Sin escribir nada: `rayito agent deploy` { #sin-escribir-nada }

Si ya tienes un agente (LangGraph, LangChain, deepagents, Strands, Hermes,
OpenCode, Vercel AI SDK o una app HTTP propia), no hace falta describirlo:
`deploy_agent()` lee los ficheros del proyecto, decide cómo se construye y se
sirve, y lo despliega. No ejecuta tu código para averiguarlo.

=== "CLI"

    ```bash
    rayito agent deploy ./mi-agente --dry-run   # qué detectó y cuánto costaría; no llama a AWS
    rayito agent deploy ./mi-agente             # lo despliega
    ```

=== "Python"

    ```python
    from rayito import AgentDeployOptions, deploy_agent

    fleet = deploy_agent(
        "./mi-agente", options=AgentDeployOptions(bucket="amzn-s3-demo-bucket")
    )
    try:
        reply = fleet.invoke("Resume el README.")
        print(reply.text, reply.session_id)
    finally:
        fleet.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AgentDeployOptions, deploy_agent_async


    async def main() -> None:
        fleet = await deploy_agent_async(
            "./mi-agente", options=AgentDeployOptions(bucket="amzn-s3-demo-bucket")
        )
        try:
            reply = await fleet.invoke("Resume el README.")
            print(reply.text, reply.session_id)
        finally:
            await fleet.stop()
    ```

=== "TypeScript"

    ```ts
    import { deployAgent } from "rayito";

    const fleet = await deployAgent("./mi-agente", {
      options: { bucket: "amzn-s3-demo-bucket" },
    });
    try {
      const reply = await fleet.invoke("Resume el README.");
      console.log(reply.text, reply.sessionId);
    } finally {
      await fleet.stop();
    }
    ```

Lo que devuelve es una [flota](flota-de-agentes.md) (`AgentFleet`): uno o varios
MicroVMs que sirven el agente, con las sesiones repartidas entre ellos. Con los
valores por defecto es un solo VM, igual que `AgentProject.deploy()`.

### Qué detecta y de dónde { #que-detecta-y-de-donde }

| Qué | De dónde sale |
|---|---|
| Lenguaje | `pyproject.toml`, `requirements*.txt`, `uv.lock` (Python); `package.json` (TypeScript y JavaScript) |
| Framework | las dependencias (`langgraph`, `langchain`, `deepagents`, `strands-agents`, `strands-harness`, `ai`, `@langchain/langgraph`, `@strands-agents/sdk`…), `langgraph.json`, la configuración de Hermes u OpenCode, y las apps web (`fastapi`, `flask`, `express`, `hono`, `bedrock-agentcore`…) |
| Punto de entrada | el objeto del agente o una fábrica sin argumentos: `agent = create_react_agent(...)`, `graph = workflow.compile()`, `export const agent = new Agent(...)`, `export async function createAgent()` |
| App HTTP (`custom`) | el comando de arranque (`uvicorn`, `flask`, el script `start`), el puerto, la ruta de salud y la de invocación, y el campo del cuerpo y de la respuesta |
| Protocolo | `agentcore` si hay `/ping` y `/invocations`; `json` para una app con una ruta POST propia; `raw` si no hay nada invocable |
| Instalación | el fichero de bloqueo: `uv.lock`, `requirements.txt`, `package-lock.json`, `pnpm-lock.yaml` o `yarn.lock` |
| Versiones | `requires-python`, `.python-version`, `engines.node`, `.nvmrc`; se elige la compilación fijada más alta que las cumple |
| Proveedor y modelo | las dependencias, los nombres de variable (`AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_API_KEY`…) y el id del modelo como literal en el código o en la configuración |

Nunca se leen los valores de un `.env`, y `.git`, `.env*`, `.venv` y
`node_modules` no suben a la imagen: las dependencias se instalan en ella, no se
copian de tu portátil.

Cuando un proyecto tiene un objeto de framework **y** una app HTTP (un grafo y
un `server.py` con FastAPI, por ejemplo), gana el framework, porque trae
`invoke()`, los eventos y el uso de tokens. La app queda como alternativa en el
informe; `--entry http` sirve la app en su lugar.

### Cambiar lo detectado { #cambiar-lo-detectado }

El orden es: el fichero de proyecto, después la bandera o el argumento, después
la detección y después el valor por defecto con nombre. Un `rayito-agent.toml`
sólo necesita las claves que quieras fijar; `rayito agent detect --write` deja
escrito lo detectado para que lo edites.

| Bandera | Qué fija |
|---|---|
| `--framework`, `--language`, `--entry`, `--protocol` | lo que la detección decidiría (`--entry http` sirve la app HTTP) |
| `--memory` | MiB de cada VM (se fija en la imagen: cambiarla construye una versión nueva) |
| `--max-sessions`, `--queue`, `--queue-timeout` | sesiones por VM y la cola del host ([Varias sesiones y varias VMs](flota-de-agentes.md)) |
| `--min-vms`, `--max-vms`, `--warm`, `--sessions`, `--idle` | cuántos VMs y cómo se reparten las sesiones |
| `--env CLAVE=VALOR`, `--allow-out destino` | variables no secretas y salidas de red permitidas |
| `--secret proveedor=nombre`, `--create-secret`, `--bucket`, `--base-artifact`, `--yes` | dónde está la credencial del modelo y el bucket, y qué se crea |
| `--dry-run`, `--write` | mostrar el plan sin llamar a AWS, y guardar lo detectado |

Un caso poco claro falla con un error que nombra lo que lo resuelve, nunca
adivina: `ambiguous` (hay dos frameworks o dos puntos de entrada posibles: usa
`--framework` o `--entry`), `not_detected` (no se reconoce el framework, o falta
el id del modelo: escríbelo como literal o en `[model]`) y `unsupported` (una
combinación que no llega a la pasarela sin cambiar el código).

### Qué se resuelve solo y qué no { #que-se-resuelve-solo }

Cada valor sale, en este orden, del argumento o la bandera, del fichero, de una
variable de entorno y del descubrimiento; ninguno crea nada en silencio:

- La **región** es `--region`, `[deploy] region`, `AWS_REGION` o la de tu perfil.
- La **imagen base** es la versión más reciente de `rayito-base-caps`. Si no
  existe, se publica sólo con `--base-artifact` y `--yes`.
- El **bucket** del artefacto es, por este orden, `--bucket`, `[deploy] bucket`,
  `RAYITO_BUCKET`, el del bootstrap de CDK de la cuenta si existe (`HeadBucket`)
  y, si no, el que creó `rayito setup`: la salida `BucketName` de la pila
  `rayito-m0-artifacts` cuando está lista (`DescribeStacks`, sólo lectura; sin
  permiso para leerla se salta).
  `rayito agent deploy` no crea buckets: si no hay ninguno, ejecuta antes
  [`rayito setup`](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup),
  que crea uno si hace falta.
- El **secreto del modelo** es `--secret`, el del fichero, o el de nombre
  convencional `rayito/agents/<proyecto>/<proveedor>`. Sólo con `--create-secret`
  se crea el que falte, desde la variable estándar del proveedor, que se lee en
  tu proceso y no se escribe en disco ni en logs.
- El **access token** es el de `RAYITO_ACCESS_TOKEN`, el de `--token-file` o uno
  nuevo que se guarda en `.rayito/agent-token` (modo `0600`).

La imagen del agente sólo se construye si cambió su contenido (código,
dependencias, runtime o memoria); si no, se reutiliza.

!!! warning "Si vienes de 0.13"
    `rayito agent deploy --file rayito-agent.toml` sigue valiendo y conserva sus
    claves de salida, pero ahora despliega una flota y hace más llamadas a AWS
    que en 0.13: lee la imagen base (`GetMicrovmImage`), el secreto del modelo
    (`DescribeSecret`) y el bucket (`HeadBucket`, `GetCallerIdentity`), y
    construye la imagen si cambió su contenido. Tus credenciales necesitan
    `lambda-microvms:GetMicrovmImage`, `secretsmanager:DescribeSecret`,
    `sts:GetCallerIdentity` y `s3:ListBucket` sobre el bucket, además de las de
    0.13. La imagen ya no sube `.git`, `.env*`, `.venv`, `venv`,
    `node_modules`, `__pycache__`, `*.pyc` ni `.rayito`, así que un proyecto que
    los subía reconstruye la imagen. Con `--no-build` reutilizas la existente.
    Y `max_concurrency` de `[serve]` está acotada por la memoria (7 con 2048 MiB,
    15 con 4096, 31 con 8192): un valor mayor falla al cargar el proyecto.

### Un framework que no está en la lista { #un-framework-que-no-esta-en-la-lista }

Si el framework no tiene soporte propio, el contrato mínimo es una de estas tres
formas, las de [`CustomRuntime`](#otros-frameworks-customruntime):

| Forma | Qué esperamos | Protocolo |
|---|---|---|
| App HTTP | un `GET` de salud y un `POST` JSON de invocación | `json` (o `agentcore` si ya habla `/ping` y `/invocations`) |
| Función | `módulo:atributo` (Python) o `ruta#exportación` (Node) que recibe `(prompt, session_id)` y devuelve texto o `{text, usage}` | — |
| Comando | lee el prompt por la entrada estándar y escribe texto, o líneas del protocolo v1, por la salida | `text` o `rayito-v1` |

!!! info "Coste y activación"
    - **Activa:** llamar a `deploy_agent()`, `deploy_agent_async()`,
      `deployAgent()` o `rayito agent deploy`. `plan_deploy()`/`planDeploy()` y
      `--dry-run` calculan el plan y el coste sin llamar a AWS ni cargar ningún
      paquete de AWS; `rayito agent detect` y `AgentProject.detect()` sólo leen
      ficheros.
    - **Recursos y llamadas AWS:** `min_vms` MicroVMs en marcha y `warm_vms`
      aparcados; una versión de imagen nueva si cambió el contenido (y un
      objeto en el bucket del artefacto); lecturas de `GetCallerIdentity`,
      `HeadBucket`, `GetMicrovmImage` y `DescribeSecret`; con `--create-secret`,
      `CreateSecret`; con `--base-artifact`, una imagen base.
    - **Coste aproximado:** por cada VM en marcha, $0,126 por hora con 2 GB
      (suspendido sólo paga el almacenamiento del snapshot); una versión de imagen
      ≈ $0,056 por semana; un secreto $0,40 al mes. Precios de lista, us-east-1,
      consultados el 2026-10-06: [Precios](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** los de `Sandbox.create()`, `Sandbox.list()` y
      `AgentTemplate.build()` (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
      `sts:GetCallerIdentity`, `s3:ListBucket` sobre el bucket,
      `secretsmanager:DescribeSecret` y, con `--create-secret`,
      `secretsmanager:CreateSecret`.
    - **Cómo apagarla:** `fleet.stop()` o `rayito agent stop <nombre>` terminan
      todos los VMs de la flota; los valores por defecto no crean ningún
      secreto, imagen base ni índice.
    - **Ejemplo:** `deploy_agent("./mi-agente").invoke("Hola").text`.

## Cuándo usarlo

- Quieres que el agente entero (el bucle del modelo y sus herramientas) corra
  **dentro** del sandbox, con el modelo detrás de la pasarela, y poder
  pedírselo como un servicio: usa esta guía.
- Quieres controlar tú el sandbox y llamar al agente turno a turno desde tu
  código: usa [`sbx.agent`](agente-en-el-sandbox.md). Es el mismo motor; esta
  guía añade la imagen, el servicio y la exportación.
- Quieres dar un intérprete de código a un agente que ya corre en otro sitio:
  usa [LangChain y Vercel AI](langchain-y-vercel-ai.md) o el
  [servidor MCP](../mcp.md).

!!! info "Coste y activación"
    - **Activa:** llamar a `build()`, `run()` o `deploy()` de `AgentProject`
      (en la CLI, `rayito agent build`, `run` o `deploy`). Construir o cargar
      un proyecto, `export()`, `rayito agent init` y `rayito agent export` no
      llaman a AWS ni crean ningún cliente.
    - **Recursos y llamadas AWS:** `build()` crea una versión de imagen
      (`AgentTemplate`). `run()` y `deploy()` lanzan un MicroVM y leen el
      secreto de la pasarela de Secrets Manager; `run()` lo destruye al
      terminar y `deploy()` lo deja vivo hasta su `timeout` (8 h como máximo).
    - **Coste aproximado:** una versión de imagen del agente cuesta ≈ $0,056 por
      semana (mínimo una semana); Hermes y Node la hacen más grande, así que
      mide la tuya con `rayito image sizes`. Un despliegue activo cuesta
      $0,126 por hora de VM de 2 GB (≈ $1,01 en las 8 h del máximo) más el
      lanzamiento ($0,0014); con `idle` suspende y sólo paga el
      almacenamiento del snapshot. El modelo se factura aparte, por tokens.
      Precios de lista, us-east-1, consultados el 2026-10-06:
      [Precios](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** `RayitoTemplateBuilder` para `build()` (pila `templates`) y
      `RayitoSecretsReader` para `run()` y `deploy()` (pila `secrets-access`),
      las mismas que usa [Agente en el sandbox](agente-en-el-sandbox.md).
    - **Cómo apagarla:** no llames a esas operaciones. Un despliegue se para
      con `deployment.stop()` o `rayito agent stop` (o se suspende con
      `deployment.pause()`, que no factura cómputo); las versiones de imagen,
      con `rayito image prune`. `deploy(keep_on_failure=True)` deja vivo el
      sandbox de un arranque fallido para depurarlo y sigue facturando hasta
      que lo termines; está apagado por defecto.
    - **Uso de tokens:** los runtimes con soporte propio informan siempre de
      los tokens de cada paso. Una app, un comando o una función propios sólo
      informan si su respuesta lleva `usage`; si no, `result.usage_reported` es
      `False` y `result.usage` vale cero porque se desconoce, no porque no se
      gastara nada. Consulta el consumo en tu proveedor.
    - **Ejemplo:** `AgentProject.load(".").deploy()` (los cuatro lenguajes
      están [más abajo](#3-despliegalo-como-servicio)).

## Antes de empezar

1. [Configurar AWS](../primeros-pasos/configurar-aws.md) hecho (`rayito setup`)
   y `rayito-base-caps` publicada: es la base de la imagen del agente, la única
   que aplica el egress cerrado.
2. La clave del modelo en Secrets Manager, y `RayitoSecretsReader` para quien
   llama al SDK ([Agente en el sandbox](agente-en-el-sandbox.md#1-el-secreto-y-la-pasarela)).
3. Acceso al modelo en tu proveedor
   ([Proveedores del agente](agente-proveedores.md)).
4. Un bucket de S3 para el artefacto del build (el que deja `rayito setup`) y `RayitoTemplateBuilder` para
   quien construye ([Templates de agente](../funciones-opcionales/templates-de-agente.md)).

`rayito agent init` y `rayito agent export` funcionan sin nada de esto: sólo
escriben ficheros.

### Los preparativos de una sola vez

Son tres, se hacen una vez por cuenta y región, y `rayito doctor` comprueba que
están. Con ellos hechos, el primer despliegue es construir la imagen del agente
(≈ 5 minutos la primera vez, porque instala el runtime y tus requisitos) y
desplegarla (≈ 20 segundos hasta que el servicio responde).

| Qué | Cómo | Cuánto |
|---|---|---|
| Un bucket de S3 para el artefacto del build | `rayito setup` lo reutiliza o lo crea e imprime `RAYITO_BUCKET=<bucket>`; si prefieres el tuyo, `export RAYITO_BUCKET=amzn-s3-demo-bucket` ([Configurar AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)) | incluido en `rayito setup` |
| La imagen base `rayito-base-caps` | publícala desde el `rayito-image.zip` de la release ([Configurar AWS](../primeros-pasos/configurar-aws.md#a-mano-sin-rayito-setup)) | ≈ 4 min |
| La versión de la base gestionada de AWS | no hace falta buscarla a mano: con `--base-image-version latest`, `rayito image publish` usa la más reciente que lista AWS y la imprime; un número explícito fija una build reproducible | — |

```bash
rayito image publish --artifact rayito-image.zip --base-image-version latest \
  --image-name rayito-base-caps --os-capabilities ALL
rayito agent build --file ./support-bot --bucket "$RAYITO_BUCKET"
```

## Qué runtime elegir

| `runtime` | Qué corre | Proveedores del modelo | Imagen |
|---|---|---|---|
| `opencode` | el agente de código OpenCode | todos | la de `rayito-agent` |
| `deepagents` | un grafo de deepagents (LangChain) | todos | la de `rayito-agent` |
| `langgraph` | tu grafo de LangGraph (`entrypoint` obligatorio) | todos | la de `rayito-agent` + tus requisitos |
| `strands` | un agente de Strands en Python (o en TypeScript, con `language = "typescript"`) | Bedrock, Anthropic, OpenAI y compatibles, Azure | venv propio con Strands |
| `strands-harness` | el harness de Strands (shell, ficheros, subagentes) | los mismos | venv propio con el harness |
| `hermes` | el agente Hermes (API sólo en loopback) | Anthropic, OpenAI y compatibles, Azure; Bedrock no | Hermes fijado por commit |
| `custom` | un comando, una función de Python o una app HTTP | los que cablee tu código por la pasarela | tus pasos de instalación |

Una combinación runtime y proveedor sin soporte falla con
`InvalidArgumentException`/`InvalidArgumentError` antes de cualquier llamada.
Todo lo que se descarga (binarios, requisitos de Python, el commit de Hermes,
Node) va fijado por sha256 en `limits.json`, y las dependencias de Python se
instalan con `--require-hashes`.

## 1. Escribe el agente y su fichero de proyecto

`rayito agent init --runtime <runtime>` escribe `rayito-agent.toml` y un
andamiaje mínimo para el runtime (el agente de ejemplo de cada pestaña). Nunca
sobrescribe nada sin `--force`.

=== "Strands"

    `agent.py`: una función que recibe `ctx.model`, el modelo ya cableado a la
    pasarela, y devuelve el `Agent`.

    ```python
    from strands import Agent, tool


    @tool
    def word_count(text: str) -> int:
        """Cuenta las palabras de un texto."""
        return len(text.split())


    def agent(ctx):
        return Agent(model=ctx.model, tools=[word_count])
    ```

    `rayito-agent.toml`:

    ```toml
    schema = "rayito.agent-project/1"
    name = "support-bot"

    [runtime]
    kind = "strands"
    entrypoint = "agent:agent"

    [source]
    path = "."
    requirements = "requirements.txt"
    exclude = [".venv", "__pycache__"]

    [model]
    provider = "bedrock"
    id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    region = "us-east-1"
    gateway = "model"

    [gateways.model]
    preset = "bedrock"
    secret = "provider-key"
    region = "us-east-1"
    models = ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]

    [agent]
    instructions_file = "AGENTS.md"
    ```

=== "Strands harness"

    Sin `entrypoint`, el servidor monta el harness de Strands con valores
    seguros: la herramienta que ejecuta código escrito por el modelo y la
    búsqueda web están apagadas, `web_fetch` sigue los permisos del
    `AgentSpec` (denegada por defecto) y las intervenciones nunca piden
    confirmación. No hace falta ningún fichero de código.

    ```toml
    schema = "rayito.agent-project/1"
    name = "reviewer"

    [runtime]
    kind = "strands-harness"

    [source]
    path = "."

    [model]
    provider = "anthropic"
    id = "claude-haiku-4-5"
    gateway = "model"

    [gateways.model]
    preset = "anthropic"
    secret = "provider-key"

    [agent]
    instructions = "Revisa el diff y señala errores. No edites ficheros."

    [agent.permissions]
    default = "deny"
    tools = { read = "allow" }
    ```

=== "Strands con Node"

    El agente en TypeScript usa el SDK `@strands-agents/sdk`; la imagen lleva
    Node y `npm ci --ignore-scripts` de tu `package-lock.json`.

    <!-- noqa: example: importa `@strands-agents/sdk` y `zod`, que el repositorio no instala -->
    ```ts
    import { Agent, type Model, tool } from "@strands-agents/sdk";
    import { z } from "zod";

    const wordCount = tool({
      name: "word_count",
      description: "Cuenta las palabras de un texto.",
      inputSchema: z.object({ text: z.string() }),
      callback: ({ text }) => text.split(/\s+/).filter(Boolean).length,
    });

    export function agent(ctx: { model: Model }): Agent {
      return new Agent({ model: ctx.model, tools: [wordCount] });
    }
    ```

    En `rayito-agent.toml` cambia el runtime:

    ```toml
    [runtime]
    kind = "strands"
    entrypoint = "agent.ts"
    language = "typescript"
    ```

=== "Hermes"

    Hermes se instala desde su repositorio en un commit fijo, con su propio
    directorio de estado, y su API sólo escucha en `127.0.0.1`. No hace falta
    código: se configura con el `AgentSpec`.

    ```toml
    schema = "rayito.agent-project/1"
    name = "assistant"

    [runtime]
    kind = "hermes"

    [source]
    path = "."

    [model]
    provider = "openai"
    id = "gpt-4.1-mini"
    gateway = "model"

    [gateways.model]
    preset = "openai"
    secret = "provider-key"

    [agent]
    instructions = "Responde en español, con frases cortas."
    ```

=== "LangGraph"

    `graph.py`: la función recibe el contexto del runner (`ctx.model`, el
    modelo ya cableado a la pasarela, e `ctx.instructions`) y devuelve tu grafo
    compilado. Sirve un grafo de LangGraph a secas, el que ya tengas: un
    `create_react_agent` o un `StateGraph` cuyo nodo del modelo se llame `agent`
    (o `model`) y el de herramientas, `tools`. Rayito traduce de esos nodos el
    texto, los pasos, las llamadas a herramientas y el uso de tokens.

    ```python
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent


    @tool
    def word_count(text: str) -> int:
        """Cuenta las palabras de un texto."""
        return len(text.split())


    def build(ctx):
        return create_react_agent(ctx.model, [word_count], prompt=ctx.instructions)
    ```

    ```toml
    [runtime]
    kind = "langgraph"
    entrypoint = "graph:build"
    ```

    Los permisos del `AgentSpec` los aplica un `AgentMiddleware`, que
    `create_react_agent` y `StateGraph` no tienen: sus herramientas son tuyas y
    no se filtran. Para que se apliquen, construye con
    `langchain.agents.create_agent(..., middleware=ctx.middleware)`. Si quieres
    un agente profundo de deepagents, pasa `ctx.subagents`, `ctx.backend` y
    `ctx.middleware` (sin el middleware se pierden los permisos), o usa
    directamente `kind = "deepagents"`:

    ```python
    from deepagents import create_deep_agent


    def build(ctx):
        return create_deep_agent(
            model=ctx.model,
            system_prompt=ctx.instructions,
            subagents=ctx.subagents,
            backend=ctx.backend,
            middleware=ctx.middleware,
        )
    ```

=== "deepagents"

    Sin `entrypoint`, el servidor monta `create_deep_agent` con el modelo, las
    instrucciones y los subagentes del `AgentSpec`.

    ```toml
    [runtime]
    kind = "deepagents"
    ```

=== "OpenCode"

    OpenCode es el runtime por defecto de `sbx.agent`. Aquí se sirve igual:
    el servidor lanza `opencode run` y traduce sus eventos.

    ```toml
    [runtime]
    kind = "opencode"

    [agent.permissions]
    default = "allow"
    tools = { webfetch = "deny" }
    ```

=== "Personalizado"

    `rayito agent init --runtime custom` escribe una app HTTP: la tabla
    `[runtime.app]` con tu `command`, el `port` en el que escucha y sus rutas,
    más `requirements` e `install`. Rayito arranca el `command` desde el
    directorio de tu código copiado (también en su `PYTHONPATH`, así que
    `python -m agent.server` funciona) y, con `requirements`, con el venv de
    tus requisitos primero en el `PATH`: `python` es el del venv.

    ```toml
    [runtime]
    kind = "custom"
    requirements = "requirements.txt"
    install = []

    [runtime.app]
    command = ["python", "-m", "agent.server"]
    port = 9100
    health_path = "/health"
    invoke_path = "/invoke"
    protocol = "raw"
    ```

    Esta app declara sus propias rutas (`/health` e `/invoke`), así que usa
    `protocol = "raw"`: el despliegue las reenvía tal cual y las llamas con
    `deployment.url` y `deployment.headers`. Con el protocolo por defecto
    (`agentcore`) sólo se exponen `/ping` y `/invocations`, y `/health` e
    `/invoke` contestan 404; [cuál elegir](#que-protocolo-elegir). Si en cambio
    quieres un comando que lee la petición (una línea JSON) por la entrada
    estándar y escribe la respuesta, con `protocol = "text"` lo que imprime es
    la respuesta:

    ```toml
    [runtime]
    kind = "custom"
    command = ["bash", "run.sh"]
    protocol = "text"
    ```

El modelo de cada proyecto lleva siempre **el nombre** del secreto
(`secret = "provider-key"`), nunca su valor: ningún campo del fichero acepta
una credencial. Las claves desconocidas se rechazan con su ruta
(`runtime.entrpoint`), y las rutas relativas se resuelven contra el directorio
del fichero sin poder salir de él. La referencia completa está en
[El fichero de proyecto](#el-fichero-de-proyecto).

## 2. Construye la imagen y pruébalo

`build()` instala el runtime y copia tu código en la imagen; `run()` crea un
sandbox de esa imagen, ejecuta el agente una vez y **siempre** lo destruye,
también si falla.

=== "Python"

    ```python
    from rayito import AgentProject

    project = AgentProject.load("./support-bot")
    info = project.build(bucket="amzn-s3-demo-bucket", on_build_logs=print)
    result = project.run("Resume el README.")
    print(result.text, result.steps, result.usage.total)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        project = AsyncAgentProject.load("./support-bot")
        info = await project.build(bucket="amzn-s3-demo-bucket")
        result = await project.run("Resume el README.")
        print(info.template_id, result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const info = await project.build({ bucket: "amzn-s3-demo-bucket" });
    const result = await project.run("Resume el README.");
    console.log(info.templateId, result.text, result.usage.total);
    ```

=== "CLI"

    ```bash
    rayito agent build --file ./support-bot --bucket amzn-s3-demo-bucket
    rayito agent run "Resume el README." --file ./support-bot --stream
    ```

También puedes definir el proyecto en código, sin fichero. El constructor
valida todo antes de llamar a nada: el nombre, que cada pasarela que el
`AgentSpec` pide esté en `gateways`, el puerto y la combinación
runtime y proveedor.

=== "Python"

    ```python
    from rayito import AgentModel, AgentProject, AgentSpec, StrandsRuntime, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    project = AgentProject(
        name="support-bot",
        runtime=StrandsRuntime(entrypoint="agent:agent"),
        spec=AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
            instructions="Responde en español, con frases cortas.",
        ),
        gateways={"bedrock": bedrock_gateway("provider-key", region="us-east-1", models=[MODEL_ID])},
        source="./support-bot",
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentProject, AgentSpec, StrandsRuntime, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

    const project = new AgentProject({
      name: "support-bot",
      runtime: new StrandsRuntime({ entrypoint: "agent:agent" }),
      spec: new AgentSpec({
        model: new AgentModel({
          provider: "bedrock",
          id: MODEL_ID,
          gateway: "bedrock",
          region: "us-east-1",
        }),
        instructions: "Responde en español, con frases cortas.",
      }),
      gateways: {
        bedrock: bedrockGateway("provider-key", { region: "us-east-1", models: [MODEL_ID] }),
      },
      source: "./support-bot",
    });
    console.log(project.name);
    ```

## 3. Despliégalo como servicio

`deploy()` crea el sandbox, arranca el servidor del agente, espera a que esté
listo y te devuelve un `AgentDeployment`: la URL y las cabeceras para
llamarlo, y los métodos para invocarlo, ver su estado, pausarlo y pararlo. Si
algo falla por el camino, destruye el sandbox (salvo con `keep_on_failure`, ver
[Si el arranque falla](#si-el-arranque-falla)) y lanza la excepción.

=== "Python"

    ```python
    from rayito import AgentProject

    project = AgentProject.load("./support-bot")
    deployment = project.deploy()
    print(deployment.sandbox_id, deployment.url)
    try:
        result = deployment.invoke("Hola")
        print(result.text)
    finally:
        deployment.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        project = AsyncAgentProject.load("./support-bot")
        deployment = await project.deploy()
        try:
            result = await deployment.invoke("Hola")
            print(result.text)
        finally:
            await deployment.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const deployment = await project.deploy();
    try {
      const reply = await deployment.invoke("Hola");
      console.log(deployment.sandboxId, deployment.url, reply.text);
    } finally {
      await deployment.stop();
    }
    ```

=== "CLI"

    ```bash
    rayito --json agent deploy --file ./support-bot --token-file ./agent.token
    rayito agent invoke microvm-00000000-0000-0000-0000-000000000001 "Hola" --token-file ./agent.token
    rayito agent status microvm-00000000-0000-0000-0000-000000000001 --token-file ./agent.token
    rayito agent stop microvm-00000000-0000-0000-0000-000000000001
    ```

`rayito agent deploy` imprime el id del sandbox, la URL y el puerto, y **nunca**
las cabeceras de acceso: salen de `deployment.headers`, que pide una cabecera
nueva en cada lectura. El servicio escucha en el puerto 8088 del MicroVM
(el 8080 y el 9000 son de `rayd`).

Para volver a hablar con el despliegue desde otro proceso hace falta el access
token de su sandbox. `deploy` lo guarda en `--token-file` (un fichero nuevo, con
modo `0600`; nunca va por argv ni se imprime) o usa el de `RAYITO_ACCESS_TOKEN`
si lo tienes definido, y sin ninguno de los dos no empieza, porque no habría
forma de reconectarse. `invoke` y `status` lo leen con `--token-file` o de
`RAYITO_ACCESS_TOKEN`. `stop` no lo necesita: termina el sandbox con su id. Desde
el SDK, `deploy(access_token=...)` (`deploy({ accessToken })` en TypeScript)
hace lo mismo que la variable; si no pasas ninguno, `deploy()` genera uno y
`deployment.access_token` (`deployment.accessToken` en TypeScript) te lo
devuelve para que lo guardes (nunca lo imprimas ni lo versiones).

### Llamar al servicio

`deployment.invoke()` devuelve un `AgentResult` (el mismo que `sbx.agent.run()`),
y `deployment.stream()` los eventos a medida que llegan. Desde otro proceso,
reengánchate con el id del sandbox:

=== "Python"

    ```python
    from rayito import AgentDeployment

    deployment = AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
    with deployment.stream("Hola") as events:
        for event in events:
            print(event.type)
    print(deployment.status())
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentDeployment


    async def main() -> None:
        deployment = await AsyncAgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
        async with deployment.stream("Hola") as events:
            async for event in events:
                print(event.type)
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment } from "rayito";

    const deployment = await AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001");
    for await (const event of deployment.stream("Hola")) {
      console.log(event.type);
    }
    console.log(await deployment.status());
    ```

=== "CLI"

    ```bash
    rayito --json agent invoke microvm-00000000-0000-0000-0000-000000000001 "Hola" --stream
    ```

### Pausar, reanudar y reconectar

`deployment.pause()` suspende el sandbox: no factura cómputo mientras duerme y
el servicio conserva su estado. La siguiente petición lo reanuda (también
`deployment.resume()`), y con `idle`, activo por defecto, se suspende solo al
quedar ocioso. Para volver al despliegue desde otro proceso, pasa el token que
guardaste:

=== "Python"

    ```python
    from rayito import AgentDeployment, AgentProject

    deployment = AgentProject.load("./support-bot").deploy()
    token = deployment.access_token
    deployment.pause()
    deployment.resume()

    same = AgentDeployment.connect(deployment.sandbox_id, access_token=token)
    print(same.status())
    deployment.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentDeployment, AsyncAgentProject


    async def main() -> None:
        deployment = await AsyncAgentProject.load("./support-bot").deploy()
        token = deployment.access_token
        await deployment.pause()
        await deployment.resume()

        same = await AsyncAgentDeployment.connect(deployment.sandbox_id, access_token=token)
        print(await same.status())
        await deployment.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment, AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const deployment = await project.deploy();
    const token = deployment.accessToken;
    await deployment.pause();
    await deployment.resume();

    const same = await AgentDeployment.connect(deployment.sandboxId, { accessToken: token });
    console.log(await same.status());
    await deployment.stop();
    ```

### Si el arranque falla

Si la app no arranca (falta un módulo, el comando no existe, no contesta a su
ruta de salud), `deploy()` lanza `SandboxException` con el código del fallo
(`app_exited`, `app_not_ready`, la clase de la excepción) y el final de la
salida de error de la app, acotado a 2 KiB y sin valores de entorno ni
credenciales. Por defecto destruye el sandbox. Para entrar a depurarlo, pide
que se conserve (`keep_on_failure=True`; hace falta un `access_token`, porque
sin él nadie podría conectarse); el error lleva el id en una nota. Cuesta hasta
que lo termines con `Sandbox.kill(sandbox_id)` o `rayito agent stop`.

=== "Python"

    ```python
    from rayito import AgentProject
    from rayito.exceptions import SandboxException

    project = AgentProject.load("./support-bot")
    try:
        project.deploy(access_token="<token>", keep_on_failure=True)
    except SandboxException as exc:
        print(exc)
        print(*getattr(exc, "__notes__", []), sep="\n")
    ```

=== "TypeScript"

    ```ts
    import { AgentProject, SandboxError } from "rayito";

    const project = await AgentProject.load("./support-bot");
    try {
      await project.deploy({ accessToken: "<token>", keepOnFailure: true });
    } catch (error) {
      if (error instanceof SandboxError) {
        console.error(error.message);
      }
    }
    ```

=== "CLI"

    ```bash
    rayito agent deploy --file ./support-bot --token-file ./agent.token --keep-on-failure
    ```

### Qué protocolo elegir

El `protocol` de una `HttpApp` decide qué expone el despliegue. El valor por
defecto es `agentcore`, y con él tus rutas propias no se alcanzan desde fuera:

| `protocol` | Qué expone el despliegue | `invoke()` | Cuándo |
|---|---|---|---|
| `agentcore` (por defecto) | `GET /ping` y `POST /invocations`; el host traduce a tu `invoke_path` | sí | tu app recibe `{"prompt"}` en una ruta y quieres `deployment.invoke()` y `stream()` |
| `openai-chat` | lo mismo, traduciendo Chat Completions con streaming | sí | tu app es un servidor compatible con OpenAI |
| `json` | `GET /ping` y `POST /invocations`, traducidos a tu ruta (el prompt va en `request_field`), y tus demás rutas tal cual | sí | tu app tiene su propio contrato JSON y quieres `invoke()` **y** que `/health` o `/chat` sigan alcanzables; es lo que elige la [detección](#sin-escribir-nada) |
| `raw` | tus rutas tal cual (`/health`, `/invoke`, un servidor MCP...) | no: `UnimplementedError` | tu app ya tiene sus propias rutas y las llamas con tu cliente HTTP |

Con `agentcore` u `openai-chat`, un `health_path` o un `invoke_path` distintos
de `/ping` y `/invocations` sólo los usa el host por dentro: pedir `/health` o
`/invoke` por la URL del despliegue da 404. `rayito agent deploy` lo avisa en
ese caso, y `rayito agent init --runtime custom` escribe `protocol = "raw"`
porque su app declara sus propias rutas.

### El contrato del servicio

El servicio habla el contrato de runtime de AgentCore, así que el mismo agente
sirve igual en un MicroVM y en un contenedor exportado:

- `GET /ping` devuelve `200 {"status": "Healthy"}`, o `"HealthyBusy"` mientras
  atiende una petición.
- `POST /invocations` recibe `{"prompt", "session_id"?, "model"?, "reasoning"?}`
  (como mucho 1 MiB). Sin `Accept: text/event-stream` responde un JSON con el
  `text`, el `session_id` y el uso; con él, un stream SSE con un evento por
  cada evento del protocolo del agente, que acaba en `done` o `agent_failed`.
  Un fallo del agente es un `200` con `failed`; los códigos HTTP son sólo para el
  contrato (`400`, `413`, `429` si está ocupado, `503` si aún no está listo).
  El JSON lleva `usage_reported`: `false` si el agente no informó de tokens
  (`usage` vale entonces cero porque se desconoce); un servicio anterior sin
  el campo equivale a `true`.
- Atiende una petición a la vez. La cabecera
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` hace de `session_id` si el cuerpo
  no lo trae.

## 4. Exporta a un contenedor

`export()` escribe, sin ninguna llamada de red, un directorio con el `Dockerfile`
(`linux/arm64`, base fijada por digest), el servidor, tu código y un `README.md`
con las órdenes exactas. Con `agentcore` comprueba además las restricciones del
contrato de AgentCore (arm64 y puerto 8080). No sobrescribe un directorio con
contenido salvo con `overwrite`.

=== "Python"

    ```python
    from rayito import AgentProject

    exported = AgentProject.load("./support-bot").export(target="agentcore", out_dir="./build")
    print(exported.path, exported.port, exported.platform)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        exported = await AsyncAgentProject.load("./support-bot").export(
            target="docker", out_dir="./build"
        )
        print(exported.files)
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const exported = await project.export({ target: "docker", outDir: "./build" });
    console.log(exported.path, exported.port);
    ```

=== "CLI"

    ```bash
    rayito agent export --file ./support-bot --target agentcore --out ./build
    ```

!!! warning "Fuera de Rayito la credencial la pone la plataforma"
    Dentro de un MicroVM el agente nunca puede leer la credencial del modelo: la
    pasarela la añade al reenviar la petición. En un contenedor exportado esa
    garantía **no existe**: no hay pasarela, así que el agente lee la credencial
    del origen estándar del proveedor (variables de entorno, el rol de la tarea o
    del runtime). Dale sólo los permisos que necesita y, si el contenedor es
    accesible desde fuera, protege el servicio con
    `RAYITO_AGENT_SERVICE_TOKEN`: el servidor exige entonces
    `Authorization: Bearer <token>`.

## Cómo llega la credencial al modelo

Dentro de Rayito, cada framework llama al modelo sólo a través de la pasarela
y con una clave de relleno:

- La clave real vive en Secrets Manager y la añade la pasarela. El código del
  agente (el tuyo y el del framework) sólo ve una URL local y un marcador.
- Bedrock usa una clave de API de Bedrock por la pasarela, nunca credenciales de
  AWS que el agente pudiera leer. IMDS y el resto de Internet siguen cerrados
  mientras `allow_internet_access` sea `False` (el valor por defecto).
- La telemetría de los frameworks está apagada, Hermes escucha sólo en
  loopback y el harness de Strands no tiene la herramienta que ejecuta código
  escrito por el modelo.
- Las pasarelas de los proveedores al estilo de OpenAI no pueden limitar el
  modelo: fija el tope de gasto en el proveedor
  ([Proveedores del agente](agente-proveedores.md#limites-del-lado-del-proveedor)).

## Un despliegue dura 8 horas como mucho

Un MicroVM no puede vivir más de 8 h entre lanzado y suspendido, y las cabeceras
del proxy de AWS caducan a los 60 minutos. Por eso un despliegue no es un
servicio permanente:

- `deployment.headers` acuña cabeceras nuevas en cada lectura; úsalas en cada
  petición en vez de guardarlas.
- Con `idle` (activo por defecto) el sandbox se suspende al quedar ocioso y se
  reanuda con la siguiente petición, sin pagar cómputo mientras duerme. El
  tiempo suspendido cuenta para las 8 h.
- Para seguir más allá, despliega uno nuevo antes de que venza el anterior y
  cambia el tráfico; el viejo se para con `stop()`.

=== "Python"

    ```python
    from rayito import AgentDeployment, AgentProject

    project = AgentProject.load("./support-bot")
    old = AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
    fresh = project.deploy()
    print(fresh.sandbox_id)
    old.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment, AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const old = await AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001");
    const fresh = await project.deploy();
    console.log(fresh.sandboxId);
    await old.stop();
    ```

## El fichero de proyecto

`rayito-agent.toml` (preferido) o `rayito-agent.json`, con el mismo contenido,
se busca en ese orden en un directorio. El esquema es `rayito.agent-project/1`
y está publicado como JSON Schema en `testdata/agent-project/schema.json` del
repositorio.

| Tabla | Claves | Qué es |
|---|---|---|
| (raíz) | `schema`, `name` | versión del esquema y nombre (imagen y etiqueta del despliegue) |
| `[runtime]` | `kind`, `entrypoint`, `language`, `framework`, `command`, `protocol`, `requirements`, `package_manager`, `python_version`, `node_version`, `adapter`, `install`, `python`, `toolsets`, `skills_dir` | el framework y cómo se instala y se arranca; cada `kind` admite sólo algunas (abajo). Con la [detección](#sin-escribir-nada), `kind` es opcional y el resto lo rellena lo detectado |
| `[runtime.app]` | `command`, `port`, `health_path`, `invoke_path`, `protocol`, `request_field`, `response_field`, `port_env` | sólo con `kind = "custom"`: tu app HTTP (`HttpApp`); `command` y `port` son obligatorias; `request_field` y `response_field` sólo con `protocol = "json"` |
| `[source]` | `path`, `requirements`, `exclude` | el directorio que se copia a la imagen y sus requisitos |
| `[model]` | `provider`, `id`, `region`, `gateway`, `base_path`, `prompt_caching` | el modelo, y qué pasarela usa |
| `[gateways.<nombre>]` | `preset`, `secret`, `region`, `models`… | una pasarela: uno de los presets de [Proveedores del agente](agente-proveedores.md) y el **nombre** del secreto |
| `[agent]` | `instructions` o `instructions_file`, `small_model`, `runtime_version`, `raw_config`, `permissions`, `mcp.<nombre>`, `agents.<nombre>` | instrucciones (mutuamente excluyentes), permisos, servidores MCP y subagentes |
| `[limits]` | `max_steps`, `timeout_seconds`, `max_output_bytes`, `max_total_tokens` | los límites de una ejecución |
| `[serve]` | `port`, `timeout_seconds`, `idle_seconds`, `max_concurrency`, `queue_max`, `queue_timeout_seconds`, `metadata` | puerto del servicio, vida del despliegue (≤ 8 h), suspensión por inactividad, concurrencia por VM, su cola y etiquetas |
| `[scaling]` | `min_vms`, `max_vms`, `warm_vms`, `sessions`, `scale_in_after_seconds` | cuántos VMs y cómo se reparten las sesiones ([Varias sesiones y varias VMs](flota-de-agentes.md)) |
| `[env]` | `NOMBRE = "valor"` | variables de entorno no secretas del agente; un nombre o un valor con forma de secreto se rechaza (`secret_value`): las credenciales van por una pasarela |
| `[network]` | `allow_internet_access`, `egress`, `allow_out`, `deny_out` | red del sandbox: cerrada salvo que la abras; `allow_out` y `deny_out` son la política de salida (necesita `rayito-base-caps`) |
| `[deploy]` | `region`, `bucket`, `base_image_version` | dónde se despliega: la región, el bucket del artefacto y la versión de la imagen base (`latest` por defecto) |
| `[image]` | `base`, `template`, `memory_mib` | la imagen base, el nombre de una imagen ya construida y su memoria |

Claves de `[runtime]` por `kind`: `langgraph` admite `entrypoint`; `strands` y
`strands-harness`, `entrypoint`, `language` y `requirements`; `hermes`,
`toolsets` y `skills_dir`; `custom`, `install`, `requirements`, `python` y
exactamente una de `command` (con `protocol`: `rayito-v1` o `text`),
`entrypoint` o la tabla `[runtime.app]`; `opencode` y `deepagents`, ninguna.
`install` son órdenes de shell que se ejecutan al construir la imagen, antes de
copiar tu código, y `requirements`, un fichero de requisitos de tu `source` que
se instala en un venv propio.

Los tiempos van en segundos en el fichero, en los dos SDK. La CLI nunca cambia
lo que dice el fichero salvo que pases la opción (`--port`, `--timeout`).

## Otros frameworks: `CustomRuntime`

`CustomRuntime` sirve cualquier agente con una de tres formas, y exactamente una:

- `command`: un comando que lee la petición por la entrada estándar. Con
  `protocol="rayito-v1"` escribe líneas del protocolo del agente; con
  `protocol="text"`, lo que imprime es la respuesta.
- `entrypoint`: una función de Python `paquete.modulo:funcion`.
- `app`: una aplicación HTTP (`HttpApp(command=..., port=...)`). Con
  `protocol="agentcore"` (por defecto) el host sirve `/ping` y `/invocations` y
  traduce a la ruta de tu app (`invoke_path`). Si tu app tiene sus propias rutas
  o habla otro protocolo (por ejemplo, un servidor MCP), `protocol="raw"` sólo
  la expone: `deploy()` te da la URL y las cabeceras, y `invoke()` lanza
  `UnimplementedError`; [cuál elegir](#que-protocolo-elegir). El `command` corre
  desde el directorio del código copiado, con ese directorio en su
  `PYTHONPATH`, y `python` es el del venv de `requirements` (sin él, el de la
  imagen).

=== "Python"

    ```python
    from rayito import CustomRuntime, HttpApp

    command = CustomRuntime(command=["bash", "run.sh"], protocol="text")
    function = CustomRuntime(entrypoint="my_agent.main:answer")
    server = CustomRuntime(
        requirements="requirements.txt",
        app=HttpApp(
            command=["python", "-m", "agent.server"],
            port=9100,
            health_path="/health",
            invoke_path="/invoke",
            protocol="raw",
        ),
    )
    ```

=== "TypeScript"

    ```ts
    import { CustomRuntime, HttpApp } from "rayito";

    const command = new CustomRuntime({ command: ["bash", "run.sh"], protocol: "text" });
    const server = new CustomRuntime({
      install: ["npm ci --ignore-scripts"],
      app: new HttpApp({ command: ["node", "serve.js"], port: 9100 }),
    });
    console.log(command, server);
    ```

| Framework | Forma recomendada |
|---|---|
| CrewAI | `entrypoint` con una función que lanza el `Crew`, o `app` si ya lo sirves |
| OpenAI Agents SDK | `entrypoint`, con el `base_url` de la pasarela en `OpenAIChatCompletionsModel` |
| Google ADK | `app`: el servidor HTTP de ADK detrás de `HttpApp` |
| Claude Agent SDK | `command`, con `protocol="text"` |
| Vercel AI SDK | `command` con `node`, o `app` con tu servidor |

En todos, el modelo se llama con la URL de la pasarela y una clave de relleno:
no hay ningún camino para que el agente lea la credencial real.

## Strands con Rayito como sandbox

Si tu agente de Strands corre fuera de Rayito pero quieres que sus herramientas
de shell, ficheros y código se ejecuten en un MicroVM, el adaptador cabe en
menos de cincuenta líneas:

```python
--8<-- "docs/examples/strands_rayito_sandbox.py"
```

## Errores

Un proyecto inválido lanza `InvalidArgumentException`/`InvalidArgumentError`
antes de cualquier llamada (en la CLI, salida 2). Un fallo del agente es
`AgentException`/`AgentError`, con la misma lista cerrada de `reason` que
[Agente en el sandbox](agente-en-el-sandbox.md). Contra un servicio desplegado,
`429` es `AgentException(reason="busy")`, `503` pasado el plazo de arranque es
`TimeoutException`, y un `403` dos veces seguidas es `AuthenticationException`.
Los mensajes nunca llevan el prompt, las cabeceras ni el nombre de un secreto.

## Ver también

- [Agente en el sandbox](agente-en-el-sandbox.md)
- [Proveedores del agente](agente-proveedores.md)
- [Templates de agente](../funciones-opcionales/templates-de-agente.md)
- [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)
- [Precios (MicroVMs, pool, agentes)](../cost.md#coste-de-un-agente-desplegado)
- [Seguridad: agente de código dentro del sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
