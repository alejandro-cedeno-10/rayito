---
title: Desplegar en AgentCore
description: Despliega el agente que ya tienes en Amazon Bedrock AgentCore Runtime con una orden o una llamada, sin Docker local: Rayito construye la imagen arm64 con CodeBuild, crea el runtime con un rol de mínimo privilegio y lo invocas con la misma API que una flota de MicroVMs.
---

# Desplegar en AgentCore

<small>Desde 0.15.0.</small>

`rayito agent deploy ./mi-agente --target agentcore` despliega el mismo proyecto
que [`rayito agent deploy`](despliega-tu-agente.md#sin-escribir-nada) lleva a
una flota de MicroVMs, pero en
[Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html).
Rayito construye la imagen `linux/arm64` en AWS (sin Docker en tu máquina), la
sube a Amazon ECR, crea un rol de ejecución con el mínimo privilegio, crea o
actualiza el runtime, espera a que esté listo y te da un objeto para invocarlo
con la misma API que [`AgentFleet`](flota-de-agentes.md).

Funciona con todos los frameworks que ya despliega la detección sin
configuración de 0.14: LangGraph y LangGraph.js, LangChain y deepagents (Python
y TypeScript), Strands (Python y TypeScript) y su harness, Hermes, el AI SDK de Vercel y una
app propia con Flask o Express, y OpenCode.

!!! info "Coste y activación"
    - **Activa:** `--target agentcore` en `rayito agent deploy`, o
      `target="agentcore"` en `deploy_agent()` / `deploy_agent_async()` (Python)
      y `target: "agentcore"` en `deployAgent()` (TypeScript). Sin eso no se
      crea nada: el destino por defecto sigue siendo `microvm`, y `--dry-run` no
      llama a AWS.
    - **Recursos y llamadas AWS:** una pila de CloudFormation por agente
      (`rayito-agentcore-<nombre>`: repositorio de ECR, proyecto de CodeBuild con
      su grupo de logs, rol de build y rol del runtime), un build de CodeBuild
      por cada versión nueva del contenido, y un runtime de AgentCore. Detalle de
      las llamadas en [Qué hace por dentro](#que-hace-por-dentro).
    - **Coste aproximado** (us-east-1, precios de lista, consultados el
      2026-10-19): el runtime cobra $0,0895 por vCPU-hora y $0,00945 por GB-hora
      de uso real, por segundo, y sólo mientras hay sesiones; el build, $0,0034
      por minuto de CodeBuild `arm1.small` (100 minutos al mes gratis); la imagen
      en ECR, $0,10 por GB-mes (se conservan las últimas 5); los logs, $0,50 por
      GB ingerido. El modelo lo cobra su proveedor. Detalle en
      [Precios](../cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** los permisos de [Antes de empezar](#antes-de-empezar).
    - **Cómo apagarla:** `rayito agent delete <nombre> --target agentcore` (o
      `fleet.delete()`) borra el runtime, la pila con su repositorio y el grupo de
      logs; sin sesiones, un runtime en reposo no cobra cómputo.
    - **Ejemplo:** [Desplegar](#desplegar).
    - **Medido** (aceptación en AWS real, 2026-10-19, us-east-1): el build tardó
      de 38 a 121 s según el framework, el runtime pasó a `READY` unos 13 s tras
      crearse, la primera invocación de una sesión nueva tardó de 10 a 34 s (el
      agente arranca en una microVM nueva) y las siguientes, de 1 a 3 s.

!!! warning "Seguridad: sin pasarela de secretos"
    AgentCore no tiene la pasarela de secretos de Rayito: el agente obtiene las
    credenciales del rol del runtime, y cualquier código que corra en la sesión
    puede leerlas. Dentro de un MicroVM de Rayito el agente nunca ve la
    credencial del modelo; aquí sí puede leer lo que el rol permita. Por eso el
    rol que crea Rayito es de mínimo privilegio: sólo puede invocar en Bedrock el
    modelo del proyecto y los que añadas con `--bedrock-model`, y sólo leer los
    secretos que declares con `--readable-secret`. Detalle en [Seguridad en AgentCore](#seguridad-en-agentcore).

!!! tip "¿Gestionas la infraestructura con AWS CDK?"
    El constructo `RayitoAgent` despliega el mismo agente, con los mismos
    recursos e IAM, desde tu app de CDK con `cdk deploy`:
    [Desplegar con CDK](desplegar-con-cdk.md).

## Antes de empezar { #antes-de-empezar }

1. **Una región con AgentCore Runtime y builds arm64 de CodeBuild.** Son 21:
   `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `ca-central-1`,
   `sa-east-1`, `eu-central-1`, `eu-west-1`, `eu-west-2`, `eu-west-3`,
   `eu-south-1`, `eu-south-2`, `eu-north-1`, `ap-south-1`, `ap-south-2`,
   `ap-southeast-1`, `ap-southeast-2`, `ap-southeast-5`, `ap-southeast-7`,
   `ap-northeast-1` y `ap-northeast-2`. En otra, el despliegue falla antes de
   llamar a AWS con `unsupported_region`.
2. **El bucket de artefactos de esa región**, el que deja
   [`rayito setup`](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)
   (o el de `cdk bootstrap`): `rayito agent deploy` lo busca igual que en
   MicroVMs (`--bucket`, `[deploy].bucket`, `RAYITO_BUCKET`, y si no el
   descubrimiento) y nunca lo crea. CodeBuild sólo lee el código de un bucket de
   su propia región: uno de otra región es `bucket_region`.
3. **Acceso al modelo en Bedrock** en la región del modelo
   ([Proveedores del agente](agente-proveedores.md)). AgentCore sólo cablea el
   proveedor `bedrock`: otro es `provider_unsupported`, porque sin pasarela su
   clave quedaría en claro en la configuración del runtime.
4. **Permisos de quien despliega.** Las credenciales de quien ejecuta la orden
   (no las del agente) necesitan:

    | Para | Permisos |
    |---|---|
    | La pila de cada agente | `cloudformation:CreateStack`, `UpdateStack`, `DescribeStacks` y `DeleteStack` sobre `rayito-agentcore-*`, y los permisos para crear, actualizar y borrar lo que contiene la plantilla, porque CloudFormation lo hace con tus credenciales salvo que uses un rol de servicio: un repositorio de ECR, un proyecto de CodeBuild, un grupo de logs y dos roles de IAM (la pila se despliega con `CAPABILITY_IAM`, así que también `iam:PassRole` sobre esos roles) |
    | El contexto de build | `s3:ListBucket` en el bucket y `s3:PutObject` y `s3:DeleteObject` sobre `rayito/images/agentcore/*` |
    | El build | `codebuild:StartBuild` y `BatchGetBuilds` sobre el proyecto, y `ecr:DescribeImages` sobre el repositorio |
    | El runtime | `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes` y `DeleteAgentRuntime`, y `iam:PassRole` sobre el rol del runtime |
    | Invocar y parar sesiones | `bedrock-agentcore:InvokeAgentRuntime` y `StopRuntimeSession` sobre el runtime |
    | Borrar los logs del runtime | `logs:DeleteLogGroup` sobre `/aws/bedrock-agentcore/runtimes/*` |
    | Identidad | `sts:GetCallerIdentity` |

    `--dry-run` y `rayito agent detect` no necesitan ninguno: no llaman a AWS.
    Las tablas completas de IAM están en [IAM](../operacion/iam.md).

5. **En TypeScript, los clientes de AWS que carga el despliegue.** Son peers
   opcionales: sólo se importan al usar `--target agentcore`, y sin ellos la
   llamada falla diciendo cuál falta.

    ```bash
    pnpm add @aws-sdk/client-bedrock-agentcore @aws-sdk/client-bedrock-agentcore-control @aws-sdk/client-cloudformation @aws-sdk/client-cloudwatch-logs @aws-sdk/client-codebuild @aws-sdk/client-ecr
    ```

    Con npm, lo mismo con `npm install`. `@aws-sdk/client-s3` y
    `@aws-sdk/client-sts` ya vienen con `rayito`. En Python no hace falta nada
    más: `boto3`, que ya instala `rayito`, trae todos los clientes.

!!! note "Esto no usa MicroVMs"
    No hace falta `rayito-base-caps`, ni `RayitoSecretsReader`, ni
    `lambda-microvms:*`: nada de esto lanza un MicroVM.

## Desplegar { #desplegar }

Mira primero qué haría. `--dry-run` no llama a AWS: valida el proyecto y las
opciones, y muestra el plan, la seguridad, los avisos y el coste.

=== "CLI"

    ```bash
    rayito agent deploy ./mi-agente --target agentcore --dry-run
    rayito agent deploy ./mi-agente --target agentcore \
      --bedrock-model us.anthropic.claude-haiku-4-5-20251001-v1:0
    ```

=== "Python"

    ```python
    from rayito import AgentCoreOptions, AgentDeployOptions, deploy_agent

    fleet = deploy_agent(
        "./mi-agente",
        options=AgentDeployOptions(bucket="amzn-s3-demo-bucket"),
        target="agentcore",
        agentcore=AgentCoreOptions(
            bedrock_models=("us.anthropic.claude-haiku-4-5-20251001-v1:0",),
        ),
    )
    reply = fleet.invoke("Resume el README.")
    print(reply.text, reply.session_id)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentCoreOptions, AgentDeployOptions, deploy_agent_async


    async def main() -> None:
        fleet = await deploy_agent_async(
            "./mi-agente",
            options=AgentDeployOptions(bucket="amzn-s3-demo-bucket"),
            target="agentcore",
            agentcore=AgentCoreOptions(
                bedrock_models=("us.anthropic.claude-haiku-4-5-20251001-v1:0",),
            ),
        )
        reply = await fleet.invoke("Resume el README.")
        print(reply.text, reply.session_id)
    ```

=== "TypeScript"

    ```ts
    import { deployAgent } from "rayito";

    const fleet = await deployAgent("./mi-agente", {
      options: { bucket: "amzn-s3-demo-bucket" },
      target: "agentcore",
      agentcore: {
        bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
      },
    });
    const reply = await fleet.invoke("Resume el README.");
    console.log(reply.text, reply.sessionId);
    ```

La CLI imprime el plan y pide confirmación (`--yes` la salta; sin terminal o
con `--json` hace falta `--yes`, o sale con `needs_confirmation`). El SDK no
pregunta nada: llamar a `deploy_agent(..., target="agentcore")` es la
confirmación. Para ver el plan sin desplegar desde el SDK, `plan_deploy()` en
Python y `planDeploy()` en TypeScript aceptan las mismas opciones y tampoco
llaman a AWS.

<!-- noqa: example: salida de consola, no es código -->
```text
rayito agent deploy: support-bot en Amazon Bedrock AgentCore Runtime, región us-east-1
  pila rayito-agentcore-support-bot: crear o actualizar (se decide al leer la cuenta)
  imagen rayito/agentcore/support-bot: construir con CodeBuild si no existe (sin Docker local)
  runtime rayito_support_bot: crear o actualizar
Seguridad:
  - AgentCore no tiene la pasarela de secretos de Rayito: el agente obtiene las credenciales del rol del runtime, y cualquier código que corra en la sesión puede leerlas
  - el rol del runtime puede invocar en Bedrock: us.anthropic.claude-haiku-4-5-20251001-v1:0
  - red PUBLIC: el agente tiene salida a Internet y la política de salida de Rayito no se aplica
  - cada sesión corre en su propia microVM de AgentCore, aislada de las demás; AgentCore no asocia sesiones a usuarios: eso es cosa de tu backend
...
no se creó ni cambió nada (--dry-run)
```

El ejemplo abrevia con `...` la descripción de lo que crea la pila, los avisos
(por ejemplo, un `[scaling]` del fichero de proyecto que AgentCore ignora) y las
líneas de coste.

Con `--json`, la CLI imprime un único documento con `dryRun`, `target`,
`detection` y `plan`, y al desplegar uno con el runtime (`runtimeId`,
`runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `stackName`, `logGroup` y
los `steps` que se hicieron). Un error sale como `{"error": {"code", "message"},
"exitCode"}` por stdout y la línea `rayito: <mensaje>` por stderr, como
`rayito setup`.

La primera vez tarda unos minutos (casi todo es el build de la imagen); un
redespliegue sin cambios, segundos.

## Qué hace por dentro { #que-hace-por-dentro }

El plan son siempre tres pasos, y cada uno se decide al aplicar, de lo que
encuentra en tu cuenta en ese momento:

| Paso | Qué hace | Acciones |
|---|---|---|
| `stack` | despliega `rayito-agentcore-<nombre>` con [`infra/agentcore-runtime.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md): repositorio de ECR, proyecto de CodeBuild y su grupo de logs, rol de build y rol del runtime | `create`, `update`, `reuse`, `wait` |
| `image` | sube el contexto de build (el mismo de [`rayito agent export --target agentcore`](despliega-tu-agente.md#4-exporta-a-un-contenedor)) al bucket, lo construye con CodeBuild en arm64 nativo, lo sube a ECR con una etiqueta que es el hash del contenido, y borra el zip del bucket | `build`, `reuse` |
| `runtime` | crea o actualiza el runtime por nombre y espera a `READY`; tras crearlo lo actualiza para exigir MMDSv2 | `create`, `update`, `reuse`, `wait` |

- **Sin Docker local.** El build corre en CodeBuild (`ARM_CONTAINER`,
  `BUILD_GENERAL1_SMALL`), así que da igual que tu máquina sea x86 o Apple
  silicon. No se usa el despliegue directo de código de AgentCore: sólo admite un
  runtime de lenguaje por agente y los agentes de TypeScript necesitan el host de
  Python más Node.
- **La imagen es la de `export`.** La que construye `deploy` es la misma que
  escribe `rayito agent export --target agentcore`; ese contexto trae un
  argumento de build `AGENT_BASE_IMAGE` para bajar la misma base, con el mismo
  digest, del espejo de ECR Public en vez de Docker Hub.
- **Idempotente.** La etiqueta de la imagen es el contenido (`src-` y 16 caracteres
  del hash del contexto), así que el mismo proyecto no vuelve a construirse; el
  runtime se identifica por su nombre (`rayito_<nombre>`) y por una marca en la
  descripción (`rayito.agentcore/1`). Un redespliegue es una actualización del
  mismo runtime, nunca un duplicado, y un runtime con ese nombre que no es de
  Rayito da `name_conflict` y no se toca.
- **MMDSv2 siempre activo.** Desde el 2026-06-30 AgentCore rechaza invocar un
  runtime sin MMDSv2. `CreateAgentRuntime` no puede activarlo, así que Rayito
  actualiza el runtime justo después de crearlo y en cada actualización.
- **Variables de entorno.** El `env` del proyecto más `AWS_REGION` y
  `AWS_DEFAULT_REGION` con la región del despliegue (ganan sobre el proyecto).
  Nunca secretos: se leen con `GetAgentRuntime`.

## Invocar { #invocar }

`AgentCoreFleet` tiene el mismo `invoke()` y `stream()` que `AgentFleet`, con el
mismo resultado.

=== "CLI"

    ```bash
    rayito agent invoke mi-agente "Resume el README." --target agentcore
    rayito agent invoke mi-agente "¿Y el segundo párrafo?" --target agentcore \
      --session-id rayito-0123456789abcdef0123456789abcdef
    rayito agent invoke mi-agente "Resume el README." --target agentcore --stream
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("mi-agente", region="us-east-1")
    first = fleet.invoke("Mi nombre es Ana.")
    again = fleet.invoke("¿Cómo me llamo?", session_id=first.session_id)
    print(again.text)

    stream = fleet.stream("Resume el README.", session_id=first.session_id)
    for event in stream:
        print(event)
    print(stream.result().text)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("mi-agente", { region: "us-east-1" });
    const first = await fleet.invoke("Mi nombre es Ana.");
    const again = await fleet.invoke("¿Cómo me llamo?", { sessionId: first.sessionId });
    console.log(again.text);

    const stream = await fleet.stream("Resume el README.", { sessionId: first.sessionId });
    for await (const event of stream) {
      console.log(event);
    }
    console.log((await stream.result()).text);
    ```

En la CLI, el argumento que en MicroVMs es el id del sandbox es aquí el
**nombre del agente**. `--token-file` no existe en AgentCore (da
`option_not_supported`): la invocación se autentica con tu identidad de AWS
(SigV4) y necesita `bedrock-agentcore:InvokeAgentRuntime`.

### Sesiones { #sesiones }

Los ids de sesión funcionan como en una flota de MicroVMs: `result.session_id`
(`result.sessionId` en TypeScript) es el id que tienes que pasar para continuar
la conversación, y dos llamadas con el mismo id llegan a la misma microVM y a la
misma memoria. Lo que cambia es cómo se escribe el id en AgentCore, que exige
entre 33 y {{agentcoreSessionIdMaxLength}} caracteres:

| Qué pasas | Qué ocurre |
|---|---|
| nada | Rayito crea `rayito-` más 32 caracteres hexadecimales al azar |
| un id de 33 a {{agentcoreSessionIdMaxLength}} caracteres (`a-z`, `A-Z`, `0-9`, `-`, `_`; empieza por letra o dígito) | se usa tal cual |
| cualquier otro id no vacío (más corto, con otros caracteres) | se convierte en `rayito-` más 32 caracteres del hash de tu id: el mismo id siempre llega a la misma sesión |
| una cadena vacía | `InvalidArgumentException` |

El id va sólo en la cabecera de la petición, no en el cuerpo. Una sesión es una
microVM propia de AgentCore, aislada de las demás: AgentCore **no asocia
sesiones a usuarios**, así que quién puede continuar qué sesión lo decide tu
backend.

## Estado, listar, parar y borrar { #ciclo-de-vida }

=== "CLI"

    ```bash
    rayito agent status mi-agente --target agentcore
    rayito agent list --target agentcore
    rayito agent stop mi-agente --target agentcore --session-id rayito-0123456789abcdef0123456789abcdef
    rayito agent delete mi-agente --target agentcore
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("mi-agente", region="us-east-1")
    status = fleet.status()
    print(status.status, status.runtime_version, status.image_uri)

    fleet.stop_session("rayito-0123456789abcdef0123456789abcdef")

    result = fleet.delete()
    print(result.runtime_deleted, result.stack_deleted, result.log_group_deleted)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("mi-agente", { region: "us-east-1" });
    const status = await fleet.status();
    console.log(status.status, status.runtimeVersion, status.imageUri);

    await fleet.stopSession("rayito-0123456789abcdef0123456789abcdef");

    const result = await fleet.delete();
    console.log(result.runtimeDeleted, result.stackDeleted, result.logGroupDeleted);
    ```

- **`status`** lee el runtime: estado (`READY`, `UPDATING`…), versión, imagen y
  grupo de logs. Gratis.
- **`list`** muestra los agentes de Rayito de la región (los runtimes con la
  marca de Rayito en su descripción), no los que creaste por otra vía. Gratis.
  Hay que pasar `--target agentcore`: una flota de MicroVMs no se lista aquí.
- **`stop`** termina la microVM de **una sesión** (`StopRuntimeSession`) y
  necesita `--session-id` (`session_required`). Sin sesiones AgentCore no cobra
  cómputo, así que no hay nada que «parar» en el agente entero: para quitarlo,
  `delete`.
- **`delete`** quita sólo lo que creó Rayito para ese nombre: el runtime
  (esperando a que desaparezca), la pila `rayito-agentcore-<nombre>` (el
  repositorio de ECR se vacía solo, con sus imágenes) y el grupo de logs del
  runtime. Pide confirmación (`--yes` la salta; sin terminal hace falta). Si no
  hay nada, dice «nada que borrar» y sale con 0. Las imágenes de otro agente, el
  bucket de artefactos y el resto de tu cuenta no se tocan.

Un agente desplegado con CDK (`RayitoAgent`) aparece en `list` y `status` y se
invoca igual, pero `deploy` y `delete` lo rechazan con `name_conflict`: lo
gestiona CloudFormation ([Convivencia con la CLI](desplegar-con-cdk.md#convivencia)).

## Redesplegar { #redesplegar }

Vuelve a ejecutar la misma orden. Rayito calcula el contenido que va a la imagen
y decide:

| Qué cambió | Qué hace |
|---|---|
| nada | tres `reuse`: la pila no cambia, no hay build y el runtime no recibe una versión nueva |
| el código, las dependencias o la configuración que va a la imagen | un build nuevo y una actualización del mismo runtime (versión nueva) |
| las variables de entorno, el ciclo de vida o `--bedrock-model` | se actualiza el runtime (o la pila, si cambia lo que el rol puede hacer); la imagen sólo se vuelve a construir si cambia su contenido |

No hay forma de acabar con dos runtimes del mismo agente: la identidad es el
nombre. Un despliegue cortado se reanuda donde se quedó (espera a la pila o al
runtime en curso, vuelve a subir el zip si hace falta). Con `--no-build` sólo
reutiliza una imagen ya construida con el mismo contenido; si no existe, falla
con `image_missing`.

## Opciones de AgentCore { #opciones }

Sólo valen con `--target agentcore`; sin él, dan `option_requires_agentcore`
antes de llamar a AWS.

| CLI | Python (`AgentCoreOptions`) | TypeScript (`agentcore`) | Qué hace |
|---|---|---|---|
| `--bedrock-model ID` (repetible) | `bedrock_models` | `bedrockModels` | Modelos o perfiles de inferencia de Bedrock que el rol del runtime puede invocar **además** del modelo del proyecto, que siempre se incluye (los repetidos se ignoran). Por defecto, ninguno más. Sin comodines ni comas: un id, o un ARN de tu cuenta y región (`iam_resource_invalid`) |
| `--readable-secret NOMBRE` (repetible) | `readable_secrets` | `readableSecrets` | Secretos de Secrets Manager que el rol puede leer. Por defecto, ninguno. Un nombre sin comodines ni comas, o un ARN de tu cuenta y región (`iam_resource_invalid`) |
| `--idle-session-timeout S` | `idle_session_timeout_seconds` | `idleSessionTimeoutMs` | Segundos sin uso tras los que AgentCore termina la microVM de una sesión: de {{agentcoreLifecycleMinSeconds}} a {{agentcoreLifecycleMaxSeconds}}, por defecto {{agentcoreIdleSessionTimeoutDefaultSeconds}} |
| `--max-session-lifetime S` | `max_session_lifetime_seconds` | `maxSessionLifetimeMs` | Vida máxima de la microVM de una sesión: de {{agentcoreLifecycleMinSeconds}} a {{agentcoreLifecycleMaxSeconds}}, por defecto {{agentcoreLifecycleMaxSeconds}} (8 h) |
| `--tag K=V` (repetible) | `tags` | `tags` | Etiquetas de la pila y del runtime, para organizaciones que las exigen ([abajo](#etiquetas)) |

El tiempo de inactividad no puede superar la vida máxima (`lifecycle_order`).
En TypeScript los milisegundos se convierten a segundos enteros: un valor que no
es múltiplo de 1000 da `lifecycle_invalid`.

### Qué pasa con los modelos y los secretos { #modelos-y-secretos }

El rol del runtime puede invocar en Bedrock sólo los ARNs del modelo del
proyecto y de los que añadas con `--bedrock-model`, y cada uno se resuelve así:

- Un **id de modelo** da el ARN del modelo base en la región del modelo (la del
  proyecto o, si no tiene, la del despliegue).
- Un **id de perfil de inferencia** (`us.`, `eu.`, `apac.`, `au.`, `global.`)
  da el ARN del perfil en la región del modelo y el del modelo base en cualquier
  región, porque el perfil enruta entre ellas; un perfil `global.` suma además el
  ARN del modelo base sin región.
- Un **ARN completo** se usa tal cual.

Ningún otro modelo: el rol no puede invocar nada fuera de esa lista. Del mismo
modo, el rol sólo
lee con `secretsmanager:GetSecretValue` los secretos de `--readable-secret`. Los
valores nunca se copian a la configuración del runtime: el agente los lee con el
rol, en ejecución. Un nombre de secreto se convierte en el ARN de ese secreto en
la región del despliegue (con el sufijo aleatorio de Secrets Manager como
comodín) y un ARN completo se usa tal cual.

```bash
rayito agent deploy ./mi-agente --target agentcore \
  --bedrock-model us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --readable-secret mi-app/clave-externa
```

### Etiquetas para organizaciones que las exigen { #etiquetas }

Una SCP que exige etiquetas deniega crear recursos sin ellas. `--tag K=V`
(`tags=` / `tags:`) las pasa a la pila, y CloudFormation las propaga al
repositorio, al proyecto de CodeBuild, al grupo de logs y a los roles, y se
envían también al crear el runtime. Son las mismas de `rayito setup --tag` y las
mismas reglas: `CLAVE=VALOR`, letras, dígitos, espacios y `_ . : / = + - @`, y
hasta 50 contando las tres que añade Rayito (`rayito:component`,
`rayito:managed-by`, `rayito:sdk-version`).

```bash
rayito agent deploy ./mi-agente --target agentcore --tag Owner=plataforma --tag Project=agentes
```

Las etiquetas de un runtime que ya existe no se actualizan en un redespliegue.

## Qué opciones de MicroVMs no aplican { #opciones-que-no-aplican }

AgentCore no es una flota de MicroVMs, y Rayito no aproxima en silencio una
opción con otra. Cada opción de la llamada (argumento del SDK o bandera de la
CLI) que no tiene sentido aquí es un error `option_not_supported` **antes de
llamar a AWS**; el mismo valor cuando viene del fichero de proyecto (que se
comparte entre destinos) es un aviso en el plan y se ignora.

| Opción de MicroVMs | En AgentCore | Por qué |
|---|---|---|
| `min_vms`, `max_vms`, `warm_vms` (`--min-vms`, `--max-vms`, `--warm`) | error; en el fichero, aviso | AgentCore crea una microVM por sesión y escala solo; el tope lo pone la cuota de sesiones activas de la cuenta |
| `scale_in_after_seconds` | error; en el fichero, aviso | usa `--idle-session-timeout` |
| `memory_mib` (`--memory`) | error; en el fichero, aviso | cada sesión usa hasta 2 vCPU y 8 GB y se cobra por consumo real |
| `[serve].port` (`--port`) | error; en el fichero, aviso | el contenedor sirve en el 8080 |
| `[serve].timeout` (`--timeout`) | error; en el fichero, aviso | usa `--max-session-lifetime` |
| `[serve].idle` (`--idle`) | error; en el fichero, aviso | usa `--idle-session-timeout` |
| `secret_names`, `create_secret_from_env` (`--secret`, `--create-secret`) | error | no hay pasarela de secretos; para dejar leer un secreto, `--readable-secret` |
| `access_token` (`--token-file`) | error | AgentCore autentica con IAM (SigV4), no con un access token |
| `keep_on_failure` (`--keep-on-failure`) | error | AgentCore siempre conserva lo creado si falla; bórralo con `rayito agent delete` |
| `base_image_version`, `ensure_base_image`, `base_publisher` (`--base-artifact`) | error; en el fichero, aviso | AgentCore no usa la imagen base de MicroVMs |
| `session_index`, `pool_backend`, `warm_timeout_seconds` | error | AgentCore enruta las sesiones por su id y no tiene plazas calientes |
| `egress`, `allow_out` (`--allow-out`) y `network` del proyecto | error `network_policy_unsupported` | en modo PUBLIC AgentCore no filtra la salida: aplicarla a medias sería un riesgo silencioso |
| proveedor del modelo distinto de `bedrock` | error `provider_unsupported` | sin pasarela, su clave iría en claro en la configuración del runtime |
| app con `protocol = "raw"` | error `raw_protocol` | no cumple el contrato de AgentCore (`/ping` y `/invocations`) |

Aplican tal cual `max_concurrency` (`--max-sessions`), `queue_max` (`--queue`),
`queue_timeout_seconds` (`--queue-timeout`), `bucket`, `build_image`,
`build_timeout_seconds`, `env` y `on_step`. `sessions` aplica como `dedicated`:
en AgentCore cada sesión tiene siempre su propia microVM.

## Seguridad en AgentCore { #seguridad-en-agentcore }

La diferencia con un MicroVM de Rayito es una sola, y conviene tenerla
presente:

- **MicroVM de Rayito:** el agente llama al modelo por la
  [pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md) y
  nunca puede leer la credencial.
- **AgentCore:** no hay pasarela. El agente obtiene las credenciales del rol del
  runtime por el servicio de metadatos de la microVM, y cualquier código que
  corra en la sesión (el tuyo, el del framework, una herramienta que el modelo
  decida ejecutar) puede leerlas. La compensación es el mínimo privilegio.

El rol que crea Rayito para el runtime puede:

- extraer **su** imagen de ECR y escribir en **su** grupo de logs
  (`/aws/bedrock-agentcore/runtimes/<runtime>-*`);
- enviar trazas a X-Ray y métricas al espacio de nombres `bedrock-agentcore`;
- obtener tokens de identidad de carga de trabajo del runtime
  (`GetWorkloadAccessToken` y `GetWorkloadAccessTokenForJWT`), **sin**
  `GetWorkloadAccessTokenForUserId`, como recomienda la guía de seguridad de
  AgentCore;
- invocar en Bedrock **sólo** el modelo del proyecto y los de `--bedrock-model`;
- leer **sólo** los secretos de `--readable-secret`.

No puede nada más: ni otros modelos, ni otros secretos, ni otros repositorios ni
otros servicios. MMDSv2 está siempre activo en el runtime. La red es `PUBLIC`
(el agente sale a Internet sin filtro: la política de salida de Rayito no se
aplica), y por eso un proyecto con `egress` o `network` no se despliega. Más en
[Seguridad](../security.md#agentcore).

El AI SDK de Vercel es un caso aparte: su cliente de Bedrock no
sabe leer las credenciales del rol por sí mismo. Para ese framework, el
contexto de build incluye un pequeño ayudante que el host ejecuta antes de cada
invocación para leer las credenciales del rol y ponerlas como variables de
entorno del proceso de Node sólo durante esa invocación. El plan lo dice en
`Seguridad`.

OpenCode tampoco las encuentra solo: sólo arranca su proveedor de Bedrock si hay
un perfil o una variable de AWS, y en AgentCore el rol únicamente lo sirve el
servicio de metadatos de la microVM. Para OpenCode, el contexto de build incluye
el mismo ayudante y un perfil de AWS con `credential_process` que lo ejecuta
(`AWS_CONFIG_FILE` y `AWS_PROFILE` en el entorno de la imagen): OpenCode obtiene
credenciales con fecha de caducidad y las refresca solo.

## Errores { #errores }

Los fallos propios del despliegue son `AgentCoreException` en Python y
`AgentCoreError` en TypeScript, con un `code` de una lista cerrada y un código de
salida en la CLI: `2` para uso y entorno (región, opciones, nombre, bucket,
confirmación…) y `1` para lo que falla en AWS (pila, build, runtime, permisos).
Los mensajes van en español y nunca llevan ARNs, URLs ni el texto crudo de AWS.
La lista completa está en [Errores](../referencia/errores.md#agentcore). Los
errores de invocación son los de siempre: `429` es `RateLimitException`, una
cuota de sesiones superada es `QuotaExceededException`, y un fallo del propio
agente (el contenedor respondió 4xx o 5xx) es
`AgentException(reason="runtime_error")`.

## Ver también { #ver-tambien }

- [Desplegar con CDK](desplegar-con-cdk.md): el mismo agente con el constructo
  `RayitoAgent` en tu app de CDK.
- [Despliega tu agente](despliega-tu-agente.md): la detección, el fichero de
  proyecto y el despliegue en MicroVMs.
- [Varias sesiones y varias VMs](flota-de-agentes.md): las flotas de MicroVMs.
- [CLI: `rayito agent`](../cli.md#rayito-agent)
- [Precios: un agente en AgentCore](../cost.md#coste-de-un-agente-en-agentcore)
- [Seguridad: AgentCore](../security.md#agentcore)
- [Funciones opcionales y su coste](../optional-features.md)
