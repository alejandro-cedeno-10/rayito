---
title: Desplegar con CDK
description: Dos constructos de AWS CDK, RayitoAgent y RayitoMicrovmImage, para declarar en tu propia app de CDK un agente en Amazon Bedrock AgentCore Runtime y una imagen de Lambda MicroVMs con rayd, con los mismos recursos e IAM que la CLI, en Python y en TypeScript.
---

# Desplegar con CDK

<small>Desde 0.15.0 (todavía sin publicar).</small>

Si tu equipo ya gestiona la infraestructura con
[AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html), Rayito trae dos
constructos para declarar lo suyo en la misma app y desplegarlo con
`cdk deploy`:

| Constructo | Qué despliega | Equivale a |
|---|---|---|
| `RayitoAgent` | un agente en Amazon Bedrock AgentCore Runtime: la imagen arm64 (construida en CodeBuild, sin Docker local), el repositorio de ECR, los roles y el runtime | `rayito agent deploy --target agentcore` ([Desplegar en AgentCore](desplegar-en-agentcore.md)) |
| `RayitoMicrovmImage` | una imagen de Lambda MicroVMs con `rayd` (y una imagen más por tamaño), con su rol de build y sus grupos de logs | `rayito image publish` ([Imágenes](../images.md)) |

Los dos están en los mismos paquetes que el SDK: `rayito.cdk` en Python
(extra `cdk`) y `rayito/cdk` en TypeScript. Usan el mismo código de
planificación que la CLI, así que un proyecto significa lo mismo por los dos
caminos, y `RayitoAgent` crea el repositorio, el proyecto de CodeBuild y los
roles con la misma plantilla que `rayito agent deploy`: **el IAM es idéntico**.
`cdk synth` no llama a AWS.

Los MicroVMs en marcha no son recursos de CloudFormation: con
`RayitoMicrovmImage` la imagen vive en tu pila y los sandboxes se siguen
creando con el SDK (`Sandbox.create(template=...)`).

!!! info "Coste y activación"
    - **Activa:** instanciar `RayitoAgent` o `RayitoMicrovmImage` en una app de
      CDK y ejecutar `cdk deploy`. Instalar `rayito[cdk]` o importar
      `rayito/cdk` no crea nada, y `cdk synth` no hace ninguna llamada a AWS. Sin
      el extra (Python) o sin los peers `aws-cdk-lib` y `constructs`
      (TypeScript), `import rayito` y la CLI no cargan nada de CDK.
    - **Recursos y llamadas AWS:** `RayitoAgent` crea una pila anidada (repositorio
      de ECR, proyecto de CodeBuild con su grupo de logs, rol de build y rol del
      runtime), un asset de S3 con el código, dos funciones Lambda y una máquina de
      estados de Step Functions que esperan al build (el `Provider` de los recursos
      personalizados), el runtime de AgentCore y un recurso que exige MMDSv2
      (`GetAgentRuntime`, `UpdateAgentRuntime`). En cada contenido nuevo, un build
      de CodeBuild (`StartBuild`, `BatchGetBuilds`, `ecr:DescribeImages`); con
      `localDocker()`, ningún build en CodeBuild: la imagen la construye tu Docker y
      va al repositorio del bootstrap de CDK. `RayitoMicrovmImage` crea un asset de
      S3 con el zip, un rol de build y, por cada imagen (la base y cada tamaño), un
      grupo de logs y un `AWS::Lambda::MicrovmImage`.
    - **Coste aproximado** (us-east-1, precios de lista, consultados el
      2026-10-19): el runtime, como con la CLI, cobra $0,0895 por vCPU-hora y
      $0,00945 por GB-hora de uso real y sólo mientras hay sesiones; el build,
      $0,0034 por minuto de CodeBuild `arm1.small` (100 minutos al mes gratis; sólo
      con `codeBuild()`); la imagen en ECR, $0,10 por GB-mes (se conservan las
      últimas 5; con `localDocker()` van al repositorio del bootstrap de CDK, que
      no tiene ese límite y las acumula hasta `cdk gc`); los logs, $0,50 por GB ingerido y {{cdkMicrovmImageLogRetentionDays}}
      días de retención; cada versión de una imagen de MicroVMs, ≈ $0,04 por semana
      de snapshot (mínimo una semana), una por tamaño; las Lambdas y Step Functions
      que esperan al build, céntimos por despliegue. Los assets quedan en el bucket
      del bootstrap de CDK (S3 estándar) hasta `cdk gc`. Detalle en
      [Precios](../cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** el despliegue lo hace CloudFormation con el rol de ejecución del
      bootstrap de CDK. Con el bootstrap por defecto no hace falta nada más; con uno
      restringido, los permisos de [Bootstrap restringido](#bootstrap-restringido).
    - **Cómo apagarla:** `cdk destroy` (o quitar el constructo y `cdk deploy`)
      borra el runtime, la pila anidada con su repositorio, las imágenes, los
      grupos de logs y las funciones del `Provider`. Los assets del bootstrap se
      borran con `cdk gc`; con `localDocker()` también las imágenes del agente,
      que están en el repositorio del bootstrap y no en el de la pila.
    - **Ejemplo:** [Un agente](#rayitoagent) y [una imagen](#rayitomicrovmimage).
    - **Tracing, VPC y entrega de logs** del runtime siguen apagados, como con la
      CLI.
    - **Medido:** pendiente. La aceptación de los constructos en AWS real todavía
      no se ha hecho; las cifras son las del despliegue con la CLI, que crea los
      mismos recursos.

!!! warning "Seguridad: sin pasarela de secretos en AgentCore"
    Un agente en AgentCore obtiene las credenciales del rol del runtime, y
    cualquier código que corra en la sesión puede leerlas. `RayitoAgent` crea el
    mismo rol de mínimo privilegio que la CLI: sólo invoca los modelos de
    `bedrockModels` y sólo lee los secretos de `readableSecrets`. Detalle en
    [Seguridad en AgentCore](desplegar-en-agentcore.md#seguridad-en-agentcore).

## Antes de empezar { #antes-de-empezar }

1. **Las dependencias de CDK**, que sólo instalas si usas los constructos:

    === "Python"

        ```bash
        pip install "rayito[cdk]"
        ```

    === "TypeScript"

        ```bash
        pnpm add rayito aws-cdk-lib constructs
        ```

    Hace falta `aws-cdk-lib` {{cdkAwsCdkLibMinVersion}} o posterior (la primera
    con `CfnMicrovmImage`) y `constructs` {{cdkConstructsMinVersion}} o posterior;
    nunca el paquete alpha de AgentCore. CDK en Python corre sobre Node (jsii),
    así que necesitas Node 20 o posterior también en Python, y el CDK CLI
    (`npm install -g aws-cdk` o `npx aws-cdk`).
2. **Una región concreta en el stack.** El constructo valida la región al
   sintetizar (que tenga AgentCore Runtime, el ARN de la imagen base de
   MicroVMs), así que un stack sin entorno falla con `region_unresolved`. La
   cuenta puede quedar sin fijar: `env={"region": "us-east-1"}` basta. Las
   regiones con AgentCore Runtime y builds arm64 están en
   [Desplegar en AgentCore](desplegar-en-agentcore.md#antes-de-empezar).
3. **El bootstrap de CDK** de esa cuenta y región, una sola vez
   ([abajo](#bootstrap)). No hace falta `rayito setup`: los constructos usan el
   bucket del bootstrap y `RayitoMicrovmImage` crea su propio rol de build.
4. **Acceso al modelo en Bedrock** para el agente, como con la CLI
   ([Proveedores del agente](agente-proveedores.md)).

## Bootstrap de CDK con etiquetas { #bootstrap }

`cdk bootstrap` crea el bucket de assets, el repositorio de ECR de assets y los
roles que usa `cdk deploy`. Si tu organización exige etiquetas al crear
recursos, pásalas también aquí: el bootstrap se despliega antes que tus pilas y
una SCP que exige etiquetas lo rechazaría igual.

```bash
cdk bootstrap aws://123456789012/us-east-1 \
  --tags Owner=plataforma --tags Project=agentes
```

Las etiquetas de tus pilas se ponen con `cdk deploy --tags K=V` o en el código
(`Tags.of(app).add(...)`). Los constructos añaden además las suyas a cada
recurso que admite etiquetas, en el momento de crearlo (también a la pila
anidada, a las funciones Lambda, a la máquina de estados y a los dos recursos de
AgentCore y MicroVMs):

| Etiqueta | Valor |
|---|---|
| `rayito:component` | `agentcore-runtime` (`RayitoAgent`) o `microvm-image` (`RayitoMicrovmImage`) |
| `rayito:managed-by` | `rayito-cdk` |
| `rayito:sdk-version` | la versión del SDK |
| las de la prop `tags` | las tuyas, hasta 50 contando las tres de arriba |

### Bootstrap restringido { #bootstrap-restringido }

Por defecto, el rol de ejecución de CloudFormation del bootstrap tiene
`AdministratorAccess`. Si lo restringes
(`cdk bootstrap --cloudformation-execution-policies ...`), esa política necesita,
además de lo habitual de CDK (S3 de assets, `iam:*` sobre los roles que crea la
pila, Lambda, Step Functions y CloudWatch Logs):

| Para | Permisos |
|---|---|
| `RayitoAgent`: la pila anidada | `cloudformation:CreateStack`, `UpdateStack`, `DeleteStack` y `DescribeStacks`; crear, actualizar y borrar un repositorio de ECR, un proyecto de CodeBuild y un grupo de logs |
| `RayitoAgent`: el runtime | `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `DeleteAgentRuntime`, `CreateAgentRuntimeEndpoint` y `CreateWorkloadIdentity`; `iam:PassRole` sobre el rol del runtime e `iam:CreateServiceLinkedRole` |
| `RayitoMicrovmImage` | `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `GetMicrovmImage`, `GetMicrovmImageVersion`, `DeleteMicrovmImageVersion`, `DeleteMicrovmImage`, `TagResource` y `UntagResource`; `lambda:PassNetworkConnector` sobre el conector gestionado `INTERNET_EGRESS` (AWS lo pide aunque la imagen no nombre ninguno); `iam:PassRole` sobre el rol de build de la imagen |

```bash
cdk bootstrap aws://123456789012/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::123456789012:policy/RayitoCdkDeploy \
  --tags Owner=plataforma
```

Quien ejecuta `cdk deploy` sólo necesita asumir los roles del bootstrap, como
en cualquier app de CDK.

## Un agente en AgentCore: `RayitoAgent` { #rayitoagent }

Una app con un agente, el mismo proyecto que despliega
`rayito agent deploy ./mi-agente --target agentcore`:

=== "Python"

    ```python
    import aws_cdk as cdk
    from aws_cdk import aws_iam as iam
    from rayito.cdk import RayitoAgent

    app = cdk.App()
    stack = cdk.Stack(app, "Agentes", env=cdk.Environment(region="us-east-1"))

    agent = RayitoAgent(
        stack,
        "SupportBot",
        project="./mi-agente",
        name="support-bot",
        bedrock_models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
        readable_secrets=["mi-app/clave-externa"],
        tags={"Owner": "plataforma"},
    )

    backend = iam.Role(
        stack, "Backend", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    agent.grant_invoke(backend)

    app.synth()
    ```

=== "TypeScript"

    ```ts
    import { App, Stack } from "aws-cdk-lib";
    import * as iam from "aws-cdk-lib/aws-iam";
    import { RayitoAgent } from "rayito/cdk";

    const app = new App();
    const stack = new Stack(app, "Agentes", { env: { region: "us-east-1" } });

    const agent = new RayitoAgent(stack, "SupportBot", {
      project: "./mi-agente",
      name: "support-bot",
      bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
      readableSecrets: ["mi-app/clave-externa"],
      tags: { Owner: "plataforma" },
    });

    const backend = new iam.Role(stack, "Backend", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    agent.grantInvoke(backend);
    ```

```bash
cdk synth     # no llama a AWS: valida el proyecto y las opciones como --dry-run
cdk deploy
```

La primera vez tarda unos minutos (casi todo es el build de la imagen); un
`cdk deploy` sin cambios en el proyecto no vuelve a construir nada.

### Props { #props-del-agente }

| TypeScript | Python | CLI equivalente | Qué hace |
|---|---|---|---|
| `project` | `project` | el directorio | El directorio del agente o su fichero de proyecto (obligatoria). Se lee al sintetizar |
| `name` | `name` | `name` del fichero de proyecto | Nombre del agente; por defecto, el del proyecto. Fija el runtime `rayito_<nombre>` y el repositorio `rayito/agentcore/<nombre>` |
| `bedrockModels` | `bedrock_models` | `--bedrock-model` | Modelos o perfiles de inferencia que el rol del runtime puede invocar; por defecto, el modelo del proyecto |
| `readableSecrets` | `readable_secrets` | `--readable-secret` | Secretos que el rol puede leer con `GetSecretValue`; por defecto, ninguno. Nunca se copian al entorno |
| `idleSessionTimeoutSeconds` | `idle_session_timeout_seconds` | `--idle-session-timeout` | Segundos sin uso tras los que AgentCore termina la microVM de una sesión: de {{agentcoreLifecycleMinSeconds}} a {{agentcoreLifecycleMaxSeconds}}, por defecto {{agentcoreIdleSessionTimeoutDefaultSeconds}} |
| `maxSessionLifetimeSeconds` | `max_session_lifetime_seconds` | `--max-session-lifetime` | Vida máxima de la microVM de una sesión, por defecto {{agentcoreLifecycleMaxSeconds}} |
| `tags` | `tags` | `--tag` | Etiquetas para todos los recursos del constructo |
| `build` | `build` | — | `AgentImageBuild.codeBuild()` (por defecto) o `localDocker()` ([abajo](#construir-la-imagen)) |
| `exportOutputs` | `export_outputs` | — | `true` añade `CfnOutput` con el ARN y el id del runtime; por defecto no se añade ninguno |

Las reglas son las de la CLI y los errores también (`name_invalid`,
`tags_invalid`, `lifecycle_order`, `iam_resource_invalid`,
`provider_unsupported`, `network_policy_unsupported`…), que salen al sintetizar
como `AgentCoreException` / `AgentCoreError`. Dos diferencias:

- En TypeScript los tiempos de sesión van en **segundos**
  (`idleSessionTimeoutSeconds`), igual que en el recurso de CloudFormation, no en
  milisegundos como `idleSessionTimeoutMs` de `deployAgent()`.
- Un valor de variable de entorno no puede pasar de
  {{cdkRuntimeEnvironmentValueMaxChars}} caracteres (el límite del recurso
  `AWS::BedrockAgentCore::Runtime`, más estricto que la API): uno más largo es
  `env_invalid` al sintetizar.

### Construir la imagen: CodeBuild o Docker local { #construir-la-imagen }

| Modo | Qué hace | Cuándo |
|---|---|---|
| `AgentImageBuild.codeBuild()` / `code_build()` (por defecto) | Sube el contexto de build (el mismo de `rayito agent export --target agentcore`) como asset de S3 y lo construye en el proyecto de CodeBuild de la pila anidada, en arm64 nativo. La etiqueta es el hash del contenido: si ya existe en ECR, no hay build | Siempre que no quieras Docker en la máquina o en el CI que despliega |
| `AgentImageBuild.codeBuild({ timeoutMinutes: 20 })` / `code_build(timeout_minutes=20)` | Lo mismo, con otro plazo de build. El recurso espera hasta ese plazo más {{cdkBuildTotalTimeoutMarginMinutes}} minutos, consultando cada {{cdkBuildQueryIntervalSeconds}} s | Builds largos o cortos |
| `AgentImageBuild.localDocker()` / `local_docker()` | Construye la imagen con tu Docker (o Finch) para `linux/arm64` al ejecutar `cdk deploy` y la sube al repositorio del bootstrap. No crea el repositorio, el proyecto ni el rol de build de la pila anidada | Si ya tienes Docker con arm64 y no quieres pagar minutos de CodeBuild |

### Qué crea { #que-crea-el-agente }

- **Pila anidada `Build`** con
  [`infra/agentcore-runtime.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md),
  la misma plantilla que despliega la CLI: repositorio de ECR (etiquetas
  inmutables, análisis al subir, se vacía al borrar), proyecto de CodeBuild y su
  grupo de logs, rol de build y rol del runtime. El bucket de artefactos es el del
  bootstrap, y el rol de build puede leer además el objeto exacto del asset del
  código.
- **`ImageBuild`** (`Custom::RayitoImageBuild`): lanza el build si la etiqueta no
  existe, espera a que acabe y comprueba que la imagen no pasa de 2 GB. Con
  `localDocker()` no se crea: Docker sube la imagen en `cdk deploy` y nadie
  comprueba los 2 GB (AgentCore la rechaza al crear el runtime si los pasa).
- **`Runtime`** (`AWS::BedrockAgentCore::Runtime`): `rayito_<nombre>`, la imagen
  recién construida, red `PUBLIC`, protocolo `HTTP`, el ciclo de vida y el
  entorno del plan, y la descripción `Rayito agent <nombre> (rayito.agentcore/1+cdk)`.
- **`Mmds`** (`Custom::RayitoRuntimeMmds`): el recurso de CloudFormation no puede
  exigir MMDSv2, que AgentCore pide desde el 2026-06-30 para invocar, así que
  este recurso lo activa con `UpdateAgentRuntime` y vuelve a comprobarlo en cada
  versión nueva del runtime. La detección de deriva de CloudFormation puede
  marcarlo como cambio hecho fuera de la pila.
- **`Provider`** y **`Handler`**: las dos funciones Lambda (Python, código de
  `infra/agentcore-cdk-handler.py`) y la máquina de estados que sirven a los dos
  recursos personalizados, con logs de 14 días.

Atributos para el resto de tu app: `agentName`, `runtimeName`, `runtimeArn`,
`runtimeId`, `runtimeVersion`, `roleArn`, `repositoryUri`, `imageUri` y
`runtime` (un `IRuntime` de la L2 de `aws-cdk-lib`, para combinarlo con otros
constructos), en `snake_case` en Python. `runtimeVersion` es la versión que ya
exige MMDSv2 (la que publica `Mmds`), no la que creó CloudFormation, así que es
la que puedes fijar en un endpoint. `grantInvoke(grantee)` /
`grant_invoke(grantee)` da `bedrock-agentcore:InvokeAgentRuntime` sobre el
runtime.

### Invocarlo { #invocar }

Un agente desplegado con CDK se invoca, se lista y se consulta exactamente igual
que uno de la CLI, por su nombre:

=== "CLI"

    ```bash
    rayito agent invoke support-bot "Resume el README." --target agentcore
    rayito agent status support-bot --target agentcore
    rayito agent list --target agentcore
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("support-bot", region="us-east-1")
    reply = fleet.invoke("Resume el README.")
    print(reply.text, reply.session_id)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("support-bot", { region: "us-east-1" });
    const reply = await fleet.invoke("Resume el README.");
    console.log(reply.text, reply.sessionId);
    ```

Las sesiones funcionan igual: [Sesiones](desplegar-en-agentcore.md#sesiones).

## Una imagen de MicroVMs: `RayitoMicrovmImage` { #rayitomicrovmimage }

Publica una imagen con `rayd` desde el `rayito-image.zip` de una release (o un
zip tuyo), con los mismos hooks, variables y tamaños que
`rayito image publish`:

=== "Python"

    ```python
    import aws_cdk as cdk
    from rayito.cdk import MicrovmImageArtifact, RayitoMicrovmImage

    artifact = MicrovmImageArtifact.download_release()

    app = cdk.App()
    stack = cdk.Stack(app, "Sandboxes", env=cdk.Environment(region="us-east-1"))

    image = RayitoMicrovmImage(
        stack,
        "TeamSandbox",
        name="team-sandbox",
        artifact=artifact,
        base_image_version="1",
        os_capabilities=True,
        sizes=["4gb"],
        allowed_mount_buckets=["amzn-s3-demo-bucket"],
        tags={"Owner": "plataforma"},
    )
    cdk.CfnOutput(stack, "ImageArn", value=image.image_arn)

    app.synth()
    ```

=== "TypeScript"

    ```ts
    import { App, CfnOutput, Stack } from "aws-cdk-lib";
    import { MicrovmImageArtifact, RayitoMicrovmImage } from "rayito/cdk";

    async function main(): Promise<void> {
      const artifact = await MicrovmImageArtifact.downloadRelease();

      const app = new App();
      const stack = new Stack(app, "Sandboxes", { env: { region: "us-east-1" } });

      const image = new RayitoMicrovmImage(stack, "TeamSandbox", {
        name: "team-sandbox",
        artifact,
        baseImageVersion: "1",
        osCapabilities: true,
        sizes: ["4gb"],
        allowedMountBuckets: ["amzn-s3-demo-bucket"],
        tags: { Owner: "plataforma" },
      });
      new CfnOutput(stack, "ImageArn", { value: image.imageArn });

      app.synth();
    }

    void main();
    ```

`downloadRelease()` es asíncrono en TypeScript porque descarga antes de
construir el stack; por eso la app va dentro de una función `async`.

### Props { #props-de-la-imagen }

| TypeScript | Python | `rayito image publish` | Qué hace |
|---|---|---|---|
| `name` | `name` | `--image-name` | Nombre de la imagen (obligatoria): letras, dígitos, `-` y `_`, hasta 64 caracteres contando el sufijo de tamaño. Los nombres que empiezan por `rayito-base` están reservados para `rayito setup` y `rayito image publish` (`image_name_reserved`) |
| `artifact` | `artifact` | `--artifact` | El zip de la imagen (obligatoria, [abajo](#el-artefacto)) |
| `baseImageVersion` | `base_image_version` | `--base-image-version` | Versión mayor de la imagen base gestionada `al2023-1` (obligatoria). `latest` no vale: resolverla exige leer AWS al sintetizar (`option_invalid`) |
| `variant` | `variant` | `--variant` | `full` (por defecto), `slim` o `poly`; debe coincidir con el marcador del zip (`artifact_variant_mismatch`) |
| `osCapabilities` | `os_capabilities` | la variante `-caps` | `additionalOsCapabilities ALL`: lo que necesitan la política de egress, el bloqueo de IMDS y los montajes S3 |
| `withEfs` | `with_efs` | `--with-efs` | Imagen para volúmenes EFS; exige `variant` `full`, `osCapabilities` y un zip con el marcador de EFS |
| `memoryMib` | `memory_mib` | — | Memoria mínima de la imagen base |
| `sizes` | `sizes` | `--sizes` | Una imagen más por tamaño (`<nombre>-4gb`), del mismo zip |
| `allowedMountBuckets` | `allowed_mount_buckets` | — | Buckets que los sandboxes pueden montar (`RAYITO_ALLOWED_MOUNT_BUCKETS`) |
| `environment` | `environment` | — | Variables de entorno de la imagen |
| `tags` | `tags` | — | Etiquetas de todos los recursos del constructo |

### El artefacto { #el-artefacto }

`MicrovmImageArtifact` dice qué zip se publica, sin leer AWS al sintetizar:

| Fábrica | Qué hace |
|---|---|
| `MicrovmImageArtifact.downloadRelease({ version?, directory? })` / `download_release(*, version=None, directory=None)` | Descarga `rayito-image.zip` y `SHA256SUMS` de la release `rayd-v<versión>` (por defecto, la de tu SDK, como `rayito setup`), comprueba el sha256 y guarda los dos ficheros en `directory` (por defecto `.rayito/releases/<tag>/` bajo el directorio de trabajo). Las síntesis siguientes los reutilizan sin red |
| `MicrovmImageArtifact.fromFile(path, { sha256? })` / `from_file(path, *, sha256=None)` | Un zip tuyo (por ejemplo, el de `rayito image zip`). Con `sha256`, la síntesis falla si no coincide |

Al sintetizar se comprueba que el zip existe (`artifact_not_found`), su sha256
(`artifact_checksum_mismatch`), la variante (`artifact_variant_mismatch`) y el
marcador de EFS (`artifact_efs_mismatch`). El zip se sube una vez al bucket del
bootstrap y lo comparten la imagen base y todos sus tamaños.

### Qué crea { #que-crea-la-imagen }

- **`Artifact`**: el asset de S3 del zip.
- **`BuildRole`**: un rol que sólo puede asumir Lambda en tu cuenta, que lee
  **sólo** el objeto del asset y escribe **sólo** en los grupos de logs de sus
  imágenes. No usa el rol de la pila `rayito-m0-iam`.
- **`LogGroup`** (y `LogGroup<TAMAÑO>`): `/rayito/<nombre>`, con
  {{cdkMicrovmImageLogRetentionDays}} días de retención, que se borra con la pila.
- **`Image`** (y `Image<TAMAÑO>`): un `AWS::Lambda::MicrovmImage` con la misma
  configuración que envía `rayito image publish` y la descripción
  `Rayito image <nombre> (rayito.microvm-image/1+cdk)`. La descripción no lleva
  fecha ni versión a propósito: cualquier cambio de propiedades crea una versión
  nueva de la imagen (con su semana mínima de snapshot), y un redespliegue sin
  cambios no.

Atributos: `imageName`, `imageArn`, `latestActiveImageVersion`, `buildRoleArn`,
`logGroupName` y `sizeImageArns` (tamaño → ARN), en `snake_case` en Python.

### Lanzar sandboxes con la imagen { #lanzar-sandboxes }

Nada cambia en el SDK: un sandbox se crea por el nombre de la imagen, con el
execution role que ya uses (o ninguno).

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="team-sandbox") as sbx:
        print(sbx.commands.run("uname -m").stdout)

    with Sandbox.create(template="team-sandbox-4gb") as big:
        print(big.commands.run("nproc").stdout)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ template: "team-sandbox" });
    console.log((await sbx.commands.run("uname -m")).stdout);

    await using big = await Sandbox.create({ template: "team-sandbox-4gb" });
    console.log((await big.commands.run("nproc")).stdout);
    ```

Fuera de 0.15 quedan los agentes sobre MicroVMs (`AgentTemplate`): construir esa
imagen lee AWS (la imagen base publicada, los secretos), y la flota sólo existe
cuando el SDK arranca los MicroVMs. Puedes publicar la base con CDK y seguir
desplegando los agentes con `rayito agent deploy`.

## Convivencia con la CLI { #convivencia }

CloudFormation es el dueño de lo que crea un constructo, y la CLI lo respeta: lo
lee y lo usa, pero no lo cambia.

| Orden | Con un recurso de CDK |
|---|---|
| `rayito agent list`, `status` e `invoke` con `--target agentcore`, `AgentCoreFleet.connect` | funcionan igual que con uno de la CLI |
| `rayito agent deploy` y `rayito agent delete` con `--target agentcore`, `deploy_agent(target="agentcore")`, `deployAgent(path, { target: "agentcore" })` | `name_conflict` antes de escribir nada: «el agente support-bot lo gestiona CloudFormation (CDK, stack …): despliégalo con cdk deploy y bórralo con cdk destroy» |
| `rayito image publish --image-name team-sandbox` | se niega: «la imagen team-sandbox la gestiona CloudFormation (CDK): cámbiala con cdk deploy» |
| `rayito image prune --image-name team-sandbox` | funciona: sólo borra versiones antiguas, nunca la última activa ni la configuración |
| `Sandbox.create(template="team-sandbox")` | funciona |

Cómo lo reconoce: el runtime lleva la marca `rayito.agentcore/1+cdk` en la
descripción (la de la CLI es `rayito.agentcore/1`), y la imagen, la etiqueta
`rayito:managed-by=rayito-cdk`. Un runtime o una imagen sin marca de Rayito no lo
tocan ni la CLI ni el constructo.

## Migrar un agente desplegado con la CLI { #migrar }

Un agente o una imagen sólo puede tener un dueño. Si ya existe uno con el mismo
nombre creado por la CLI, `cdk deploy` falla (el nombre del runtime, el del
repositorio de ECR o el de la imagen ya existen), CloudFormation deshace lo suyo
y lo de la CLI no se toca.

**Con un corte breve**, el mismo nombre:

1. `rayito agent delete support-bot --target agentcore` borra el runtime, su pila
   `rayito-agentcore-support-bot` y sus logs.
2. `cdk deploy` crea el agente de nuevo bajo CloudFormation.

Las sesiones abiertas se pierden, y el id y el ARN del runtime cambian: quien
invoca con `AgentCoreFleet.connect("support-bot")` o
`rayito agent invoke support-bot` sigue funcionando; quien guardó el ARN tiene
que actualizarlo (`agent.runtimeArn`, o `exportOutputs: true`).

**Sin corte**, con otro nombre:

1. Declara el agente con otro `name` (`support-bot-v2`) y `cdk deploy`.
2. Mueve a tus clientes al nombre nuevo.
3. `rayito agent delete support-bot --target agentcore`.

**Una imagen** publicada con `rayito image publish`: publica la de CDK con otro
nombre, cambia el `template` de tus sandboxes y borra las versiones de la
antigua con `rayito image prune --image-name <antigua>` (la imagen en sí, con la
API `DeleteMicrovmImage`). Las imágenes `rayito-base*` de `rayito setup` no se
migran: siguen siendo de la CLI y conviven con las tuyas.

**De CDK a la CLI**: `cdk destroy` (o quitar el constructo y `cdk deploy`) y
después `rayito agent deploy` o `rayito image publish`.

## Borrar { #borrar }

`cdk destroy` borra el runtime, la pila anidada (el repositorio de ECR se vacía
solo, con sus imágenes), las imágenes de MicroVMs, los
grupos de logs, el rol de build y las funciones del `Provider`. El grupo de logs
que AgentCore crea al primer `invoke` (`/aws/bedrock-agentcore/runtimes/...`) no
es de la pila: bórralo a mano si no lo quieres. Los assets del bootstrap (el
código del agente, el zip de la imagen, el código del manejador) se quedan en
el bucket del bootstrap hasta `cdk gc`. Con `localDocker()` las imágenes del
agente están en el repositorio de ECR del bootstrap, que `cdk destroy` no borra
ni limita a 5: se acumulan hasta `cdk gc`.

## Solución de problemas { #solucion-de-problemas }

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `region_unresolved` al sintetizar | el stack no tiene `env.region` | pon la región (`env={"region": "us-east-1"}`); la cuenta puede quedar sin fijar |
| `InvalidArgumentException` que pide instalar el extra `cdk` al importar `rayito.cdk` | falta el extra | `pip install "rayito[cdk]"` |
| Python sintetiza con un error de jsii o de Node | CDK en Python necesita Node | Node 20 o posterior en el `PATH` |
| `cdk deploy` falla con «already exists» en el runtime, el repositorio o la imagen | ya hay un agente o una imagen con ese nombre (de la CLI u otra pila) | [Migrar](#migrar) o usa otro `name` |
| `rayito agent deploy` da `name_conflict` «lo gestiona CloudFormation» | el agente es de CDK | cámbialo en tu app y `cdk deploy` |
| `env_invalid` al sintetizar con un valor largo | una variable pasa de {{cdkRuntimeEnvironmentValueMaxChars}} caracteres, el límite del recurso de CloudFormation | acórtala, o léela en ejecución de un secreto (`readableSecrets`) |
| `ImageBuild` falla y CloudFormation deshace | el build de CodeBuild falló o la imagen pasa de 2 GB | el motivo (la etiqueta y el estado del build) está en el evento de la pila; el log, en el grupo de logs del proyecto de CodeBuild |
| `ImageBuild` tarda y acaba en plazo | el build tardó más que `timeoutMinutes` más el margen | sube `timeoutMinutes` en `AgentImageBuild.codeBuild(...)` |
| `localDocker()` falla en `cdk deploy` | no hay Docker o no construye `linux/arm64` | arranca Docker con soporte arm64 (buildx o QEMU), o vuelve a `codeBuild()` |
| La detección de deriva marca el runtime | `Mmds` activa MMDSv2 fuera de CloudFormation | es esperado; el siguiente `cdk deploy` lo vuelve a comprobar |
| `AccessDenied` en `PassNetworkConnector`, `CreateMicrovmImage` o `CreateAgentRuntime` | bootstrap restringido | añade los permisos de [Bootstrap restringido](#bootstrap-restringido) |
| `release_download_failed` | sin red o sin acceso a GitHub al sintetizar por primera vez | descarga una vez con red (se guarda en `.rayito/releases/`) o usa `fromFile` |
| `image_name_reserved` | el nombre empieza por `rayito-base` | elige otro; `rayito-base*` es de `rayito setup` |
| Las versiones de imagen se acumulan | cada cambio de propiedades crea una versión con su snapshot | `rayito image prune --image-name <nombre>` borra las antiguas |

## Errores { #errores }

Los errores propios de los constructos son `RayitoCdkException` en Python y
`RayitoCdkError` en TypeScript, con un `code` de una lista cerrada; salen al
sintetizar, antes de añadir ningún recurso. Los del plan del agente siguen
siendo `AgentCoreException` / `AgentCoreError`
([Errores](../referencia/errores.md#agentcore)).

| `code` | Cuándo |
|---|---|
| `region_unresolved` | el stack no tiene una región concreta |
| `account_invalid` | la cuenta del stack está fijada y no es un id de 12 dígitos |
| `image_name_invalid` | el nombre de la imagen, con el sufijo de tamaño, no cumple el patrón o pasa de 64 caracteres |
| `image_name_reserved` | el nombre empieza por `rayito-base` |
| `artifact_not_found` | el zip no existe |
| `artifact_checksum_mismatch` | el sha256 del zip no es el esperado |
| `artifact_variant_mismatch` | el zip es de otra variante |
| `artifact_efs_mismatch` | el marcador de EFS del zip no casa con `withEfs` |
| `option_invalid` | una opción no vale (`latest` como versión base, `withEfs` sin `full` y `osCapabilities`, tamaños fuera del catálogo, memoria que no es un entero positivo o, con `sizes`, distinta de la base) |
| `release_download_failed` | no se pudo descargar o comprobar la release |

## Ver también { #ver-tambien }

- [Desplegar en AgentCore](desplegar-en-agentcore.md): el mismo agente con la CLI
  o el SDK, sus opciones y su modelo de seguridad.
- [Imágenes](../images.md) y [Tamaños](../funciones-opcionales/tamanos.md).
- [Seguridad: constructos CDK](../security.md#cdk)
- [Funciones opcionales y su coste](../optional-features.md)
