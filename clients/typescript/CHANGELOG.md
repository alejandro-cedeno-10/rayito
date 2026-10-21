# Changelog

Todos los cambios notables del paquete `rayito` (SDK TypeScript). El formato
sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el
versionado [SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Añadido

- **Despliegue en Amazon Bedrock AgentCore Runtime**: `deployAgent(ruta, { target:
  "agentcore" })`, `planDeploy()` y `rayito agent deploy ./mi-agente --target
  agentcore` despliegan un agente de cero configuración sin Docker local: la
  imagen arm64 se construye en CodeBuild a partir del export de `agentcore`, se
  sube a ECR (repositorio, proyecto y roles en la pila `rayito-agentcore-<nombre>`)
  y el runtime se crea o se actualiza sin duplicarlo, con MMDSv2 siempre exigido.
  `rayito agent deploy --target agentcore` muestra el plan, la región, el coste
  aproximado y la seguridad, y pide confirmación (`--yes`, `--dry-run`, `--json`,
  `--tag K=V` para las organizaciones que exigen etiquetas). `AgentCoreFleet`
  (`connect()`, `invoke()`, `stream()`, `status()`, `stopSession()`, `delete()`)
  tiene la misma API y las mismas sesiones que `AgentFleet`; `rayito agent
  invoke|status|stop --target agentcore` y las órdenes nuevas `rayito agent list`
  y `rayito agent delete` lo usan. Las opciones de MicroVMs que AgentCore no
  admite (`min_vms`, `warm_vms`, `--memory`...) dan `option_not_supported` en vez
  de aproximarse. AgentCore no tiene la pasarela de secretos de Rayito: el agente
  lee las credenciales del rol del runtime, que sólo puede invocar los modelos de
  `agentcore.bedrockModels` y leer los secretos de `agentcore.readableSecrets`.
  Cuatro clientes de AWS nuevos como peers opcionales
  (`@aws-sdk/client-bedrock-agentcore`, `-bedrock-agentcore-control`, `-codebuild` y
  `-ecr`); el destino por defecto sigue siendo la flota de MicroVMs y no los carga.
- El export `agentcore` lleva el argumento de build `AGENT_BASE_IMAGE` y, para
  Vercel AI SDK, el ayudante que lee las credenciales del rol del runtime
  (`botocore` fijado por hash); el `Dockerfile` y el README del export `docker` no cambian.
- **OpenCode obtiene el rol del runtime con un perfil `credential_process`**:
  el export `agentcore` de un agente OpenCode añade el ayudante de botocore, un
  `AWS_CONFIG_FILE` con un perfil cuyo `credential_process` lo ejecuta (con
  `Expiration`, para que el SDK renueve) y `AWS_CONFIG_FILE` y `AWS_PROFILE` en la
  imagen; sin ellos OpenCode no arranca su proveedor de Bedrock. MicroVM, el
  export `docker` y los demás frameworks no cambian.
- El host del agente retiene hasta 25 s la primera petición de una sesión nueva de
  AgentCore esperando el calentamiento, en vez de contestar 503 (un calentamiento
  fallido sigue contestando 503 al instante).
- **`rayito setup`**: prepara una cuenta y una región de AWS para Rayito en una
  orden idempotente, como `cdk bootstrap`. Lee la cuenta, muestra el plan y el
  coste aproximado, pide confirmación (`--yes` sin terminal o con `--json`;
  `--dry-run` sólo lee) y termina con `rayito doctor`. Crea o actualiza la pila
  de IAM `rayito-m0-iam` (sólo `ArtifactBucket`), reutiliza el bucket de
  `--bucket`, `RAYITO_BUCKET`, la pila o el bootstrap de CDK y publica `rayito-base` desde el `rayito-image.zip` de la release
  `rayd-v<versión>`, comprobado contra su `SHA256SUMS`, o desde `--artifact`.
  Un build en curso se espera en vez de lanzar otro. Mismas opciones, textos,
  documento JSON y códigos de salida que la CLI de Python.
- `rayito setup` crea el bucket de artefactos con la pila `rayito-m0-artifacts`
  (`infra/artifact-bucket.yaml`: cifrado, privado, sólo TLS, zips caducando a los
  7 días, conservado al borrar la pila) cuando no hay ninguno que reutilizar;
  un `--bucket` o `RAYITO_BUCKET` que no existe se crea con ese nombre.
- `rayito agent deploy` y `deployAgent()` encuentran el bucket que creó
  `rayito setup` (la salida `BucketName` de `rayito-m0-artifacts`, sólo
  lectura) cuando no hay `RAYITO_BUCKET` ni bucket del bootstrap de CDK.
- **Constructos de AWS CDK en `rayito/cdk`** (peers opcionales `aws-cdk-lib`
  `^2.261.0` y `constructs` `^10.5.0`). `RayitoAgent` despliega con `cdk deploy`
  el mismo agente que `deployAgent(ruta, { target: "agentcore" })`, con la misma
  plantilla `infra/agentcore-runtime.yaml` (IAM idéntico): la imagen se construye
  en CodeBuild por defecto (`AgentImageBuild.codeBuild()`, sin Docker local) o
  con tu Docker (`AgentImageBuild.localDocker()`), y `Custom::RayitoRuntimeMmds`
  exige MMDSv2. `RayitoMicrovmImage` publica una imagen de Lambda MicroVMs con
  `rayd` (y sus tamaños) desde `await MicrovmImageArtifact.downloadRelease()`
  (comprobado contra `SHA256SUMS`, con caché para síntesis sin red) o
  `fromFile()`, con su propio rol de build y la misma configuración que
  `rayito image publish`. `cdk synth` no llama a AWS; la región del stack debe
  ser concreta. Errores propios en `RayitoCdkError`; los del plan siguen siendo
  `AgentCoreError`. Ningún otro punto de entrada (`rayito`, `rayito/e2b`, la CLI,
  el servidor MCP) importa `aws-cdk-lib`. Guía: `guias/desplegar-con-cdk.md`.
- **La CLI respeta lo que gestiona CloudFormation**: un runtime con la marca
  `rayito.agentcore/1+cdk` se lista, se consulta y se invoca como cualquier otro,
  pero `rayito agent deploy`, `rayito agent delete` y
  `deployAgent(ruta, { target: "agentcore" })` lo rechazan con `name_conflict`, y
  `rayito image publish` rechaza una imagen con la etiqueta
  `rayito:managed-by=rayito-cdk`. Los runtimes con la marca `rayito.agentcore/1`
  no cambian.

### Cambiado

- **Una vez, el hash de contenido de las imágenes de agentes de MicroVMs**:
  el host del agente (`agent_host`) y, para los frameworks de Node (Strands TS,
  LangGraph JS, Deep Agents JS, AI SDK de Vercel, Strands harness TS), el puente
  `node_bridge.mjs` llevan ahora el soporte de AgentCore (id de sesión y
  credenciales del rol). Como los dos forman parte del contexto de build, el
  primer `deploy_agent` tras actualizar construye una versión de imagen nueva
  (unos $0,011 y al menos una semana de almacenamiento de su snapshot) aunque tu
  código no haya cambiado; los siguientes la reutilizan. El `Dockerfile` y el
  README del export `docker` no cambian; sus ficheros de `agent_host/` y, en
  Node, `node_bridge.mjs` sí.
- Los errores de `Sandbox.create()` (`ResourceNotFoundException` de
  `RunMicrovm`) y de `deployAgent()` por falta de bucket o de imagen base
  terminan con la pista "si la cuenta o la región aún no están preparadas,
  ejecuta `rayito setup`". La clase, el código y la condición no cambian.

## [0.14.0] - 2026-10-17

### Añadido

- **`AgentProject.detect()` une la detección con `deployAgent`**: devuelve
  `[proyecto, informe]` a partir de una carpeta, sin ejecutar su código, con
  el proveedor y el modelo que encuentra (también en los ficheros de
  configuración de Hermes y OpenCode), la pasarela del modelo y los plugins de
  Node de LangGraph, DeepAgents y Vercel AI SDK como `custom` con su
  adaptador. Strands instala con `uv` cuando el proyecto lo usa, y el host
  recibe `queue_max` y `queue_timeout_seconds` en `host.json`. La detección
  de una app HTTP que lee su puerto de `PORT` ahora lo declara (`port_env`),
  de modo que el host se lo pasa y la app escucha donde él la espera.
- **Opciones tipadas para servir un agente en varios VMs (`agent-zero-config`)**:
  `ServeOptions` suma `queueMax` y `queueTimeoutMs` (la cola del host;
  con 0, el valor por defecto, la llamada que no cabe recibe 429 como hasta
  ahora) y `maxConcurrency` queda acotada por las conexiones que admite el
  MicroVM de `memoryMib`. `AgentProject` suma `scaling` (`ScalingOptions`:
  `minVms`, `maxVms`, `warmVms`, sesiones `shared` o `dedicated` y
  `scaleInAfterMs`), `env` (sin secretos), `network` con `allowOut` y
  `denyOut`, `region`, `bucket`, `baseImageVersion`, `language`, `framework`
  y `versions` (`RuntimeVersions`). El fichero `rayito-agent.toml` admite las
  tablas `[scaling]`, `[env]` y `[deploy]`, las claves `allow_out`, `deny_out`,
  `queue_max` y `queue_timeout_seconds`, y en `[runtime]` las de `language`,
  `framework`, `python_version` y `node_version`. Todo es aditivo y los valores
  por defecto son los de un solo VM. Los errores de lectura suman los códigos
  `ambiguous`, `not_detected` y `unsupported`. Además, la detección estática de
  un proyecto de agente (lenguaje, framework, punto de entrada, app HTTP, plan
  de instalación, versiones y proveedor, sin ejecutar su código) y las reglas
  que comparten los dos SDK (`agent-detection.json`) quedan listas para
  `deploy_agent`.
- **Host y adaptadores de instalación del agente**:
  el host del agente sirve una cola FIFO acotada (`queue_max` y
  `queue_timeout_seconds` de `host.json`; sin cola por defecto: la llamada que
  no cabe recibe 429, como hasta ahora)
  y supervisa las apps de larga vida (`http-app` y Hermes): una app que muere
  se relanza con espera creciente, `/ping` es `HealthyBusy` y las invocaciones
  reciben `503` con `Retry-After` mientras tanto, y agotado el presupuesto el
  marcador de fallo lleva `app_crashloop` con la cola saneada de su salida de
  error. `new HttpApp({ protocol: "json" })` traduce `/invocations` al contrato propio de
  una app (`requestField` y `responseField`) y reenvía sus demás rutas;
  `agentcore` sigue siendo el valor por defecto de un `HttpApp` explícito. Un
  único puente de Node (`node_bridge.mjs`, driver `node-callable`) ejecuta
  agentes de Strands (el antiguo `strands-ts`, que sigue como alias),
  LangGraph.js y deepagents-js (`adapter: "langgraph"`), el AI SDK de Vercel
  (`adapter: "vercel-ai"`) y una función propia (`adapter: "callable"`) con
  `new CustomRuntime({ entrypoint: "ruta.ts#exportacion", packageManager })`. Y
  `CustomRuntime` y `StrandsRuntime({ language: "typescript" })` aceptan
  `packageManager` (`pip`, `uv`, `npm`, `pnpm`, `yarn`), `pythonVersion` y
  `nodeVersion`: el plan de instalación (`install-plan.ts`) baja CPython y Node
  con sha256 fijado en `limits.json` y hace la instalación congelada del gestor.
  Cuando el modelo va por la pasarela, el puente fuerza HTTP/1.1 en el AWS SDK de
  JavaScript, que de otro modo habla HTTP/2 con Bedrock. Nada cambia para los
  runtimes y los ficheros de proyecto de 0.13.
- **`deployAgent()` y `AgentFleet`: despliega el agente que ya tienes con una
  llamada (`agent-zero-config`)**: `deployAgent(ruta)` (y
  `deploy_agent_async()`) detecta el proyecto sin ejecutar su código, resuelve
  la región, la imagen base, el secreto del modelo, el bucket del artefacto
  (el de `RAYITO_BUCKET` o el de activos del bootstrap de CDK; nunca crea un
  bucket) y el access token, construye la imagen sólo si cambió y devuelve un
  `AgentFleet`: uno o varios MicroVMs que sirven el agente. `ScalingOptions`
  fija `minVms`, `maxVms`, las plazas calientes (`warmVms`, del pool de
  plazas aparcadas) y si las sesiones se comparten o cada una tiene su VM
  (`dedicated`). Con más de un VM posible, el `sessionId` del resultado es un
  handle `rs1.<sandbox>.<sesión>` que enruta derecho a su VM sin servidor
  propio; una flota llena espera en una cola cliente y, al agotarse, lanza
  `CapacityError` con `code: "agent_capacity"`; un VM que desaparece es
  `SandboxError` con `code: "agent_vm_lost"`. Los VMs por encima de
  `minVms` se terminan solos (`setTimeout`), un VM que deja de contestar se
  sustituye y `AgentFleet.connect()` reconstruye la flota en otro proceso.
  `planDeploy()` calcula el plan y el coste de lista sin llamar a AWS.
  `AgentDeployOptions`, `DetectOverrides` y
  `PoolConfig.accessToken` son aditivos: `AgentProject.deploy()` no cambia.
  Nada que cueste se crea sin pedirlo: un secreto sólo con
  `createSecretFromEnv: true`, la imagen base sólo con un publicador.

- **`rayito agent detect` y `rayito agent deploy [RUTA]`**: la CLI detecta el
  proyecto y lo despliega como una flota con `--min-vms`, `--max-vms`,
  `--warm`, `--sessions`, `--max-sessions`, `--memory`, `--idle`, `--queue`,
  `--env`, `--allow-out`, `--bucket`, `--secret proveedor=nombre`,
  `--create-secret`, `--base-artifact` con `--yes`, `--build/--no-build`,
  `--dry-run` (sólo plan y coste, sin AWS) y `--verbose`. El token se guarda
  en `.rayito/agent-token` (modo 0600) si no se pasa `--token-file` ni
  `RAYITO_ACCESS_TOKEN`, y los logs del build salen resumidos. `invoke`,
  `status` y `stop` aceptan el nombre de la flota además del id de un sandbox;
  de los VMs de esa imagen sólo cuentan los que son de un agente (los `RUNNING`
  con las etiquetas de un despliegue de Rayito, sondeados con un `Health` sin
  token), y si la imagen tiene un `RUNNING` que no lo es, tampoco se cuentan los
  suspendidos, que no se pueden sondear sin reanudarlos: `stop` nunca termina un
  sandbox ajeno.
  La salida de 0.13 de `agent deploy` se conserva (mismas claves, con otras
  añadidas) y `--file` sigue valiendo; lo que cambia está en "Cambiado".

- **Strands, Strands harness, Hermes, LangGraph y agentes propios como
  runtimes de primera clase (`agent-runtimes-byo`)**: `StrandsRuntime` (SDK y
  harness), `HermesRuntime` y `CustomRuntime` con `HttpApp` suman runtimes a
  `AgentTemplate`, `Agent` y `AgentPool`, y el tipo de proyecto `langgraph`
  cubre un grafo propio. Cada runtime instala sus dependencias con hashes
  fijados, el modelo sale por la pasarela de secretos (el agente nunca lee la
  credencial) y todo lo que sale a Internet queda apagado por defecto. Los
  puertos nuevos `InstallableRuntime` y `ServableRuntime` (con `HostConfig`,
  `GatewayWiring`/`DirectWiring` e `InstallTarget`) describen cómo se instala y cómo se sirve un
  runtime sin tocar el puerto `AgentRuntime`, así que los runtimes de 0.13
  siguen funcionando. `AgentTemplate` acepta nombres del registro u objetos
  runtime.

- **`AgentProject` y `AgentDeployment`: del fichero `rayito-agent.toml` a un
  agente desplegado o exportado**: `AgentProject` construye la imagen,
  ejecuta una vez, despliega un servicio (`rayito.agent-service/1`, el contrato
  HTTP de AgentCore: `GET /ping` y `POST /invocations`, con SSE) o exporta un
  contexto Docker/AgentCore (arm64, puerto 8080) sin llamar a AWS.
  `AgentDeployment` invoca, consulta `DeploymentStatus` y detiene el
  servicio. Es una función con coste (build, run y deploy crean imágenes y
  MicroVMs), apagada por defecto: nada corre sin llamar al método.

- **Peer opcional `smol-toml`**: se carga de forma perezosa sólo al leer un
  `rayito-agent.toml`; sin él, el resto del SDK no cambia.

- **`AgentProject` y `rayito agent`: describe un agente en un fichero y
  construye, ejecuta, despliega o exporta (`agent-runtimes-byo`)**: la CLI suma
  `rayito agent init`, `build`, `run`, `deploy`, `export`, `invoke`, `status` y
  `stop`, conchas sobre `AgentProject` y `AgentDeployment` con `--json`, los
  valores por defecto del SDK y salida 2 para un fichero de proyecto inválido.
  `init` escribe `rayito-agent.toml` y un andamiaje por runtime (Strands,
  Strands harness, Hermes, LangGraph, deepagents, OpenCode o propio) sin
  sobrescribir nada sin `--force`; `export` y `init` no llaman a AWS, y
  `deploy` nunca imprime las cabeceras de acceso. El andamiaje de `init` se
  comparte con la CLI de Python en `testdata/cli/agent-scaffold/`. Guía
  nueva, [Despliega tu agente](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/site/docs/guias/despliega-tu-agente.md),
  y un adaptador de Strands `Sandbox` sobre Rayito en
  `docs/examples/strands_rayito_sandbox.py`.

- **`AgentProject.deploy()` acepta el access token del sandbox y `rayito agent
  deploy|invoke|status|stop` se pueden encadenar entre procesos**: `deploy({
  accessToken })` lo pasa a `Sandbox.create()`, y la CLI guarda el token en
  `--token-file` (o usa `RAYITO_ACCESS_TOKEN`) antes de crear el sandbox, igual
  que `sandbox create --detach`. Sin ninguno de los dos, `deploy` sale con 2 sin
  crear nada: antes dejaba un MicroVM que ni `invoke`, ni `status` ni `stop`
  podían volver a abrir. `invoke` y `status` leen el token con `--token-file` y
  sin él salen con 2; `stop` ya no lo necesita (`terminate-microvm` por id).

- **`AgentDeployment.pause()`, `resume()` y `accessToken`, y `deploy({ keepOnFailure })`
  (`agent-runtimes-dx`)**: `AgentDeployment` suspende y reanuda el sandbox del
  despliegue (`Sandbox.pause()` y `resume()`) y expone el access token con el que
  se creó, también el que se generó cuando no se pasó `accessToken`, para
  reconectarse con `AgentDeployment.connect(sandboxId, { accessToken })`.
  `AgentProject.deploy({ keepOnFailure: true })` deja vivo el sandbox de un
  despliegue que falla (apagado por defecto: sigue facturando hasta que lo
  termines), exige un `accessToken` que el llamador conozca y
  anota el id del sandbox en el error. `rayito agent deploy --keep-on-failure`
  hace lo mismo.

- **`AgentResult.usageReported` y `Done.usageReported`**: `false` cuando el
  agente no informó de tokens (una `HttpApp` cuya respuesta no lleva `usage`, un
  comando con `protocol: "text"` o una función que no lo devuelve). `usage` vale
  entonces cero porque se desconoce, no porque no se gastara nada, y la CLI
  imprime `tokens=desconocido` y `usageReported` en `--json`. Una `HttpApp`
  informa de `usage` si su respuesta lo trae (`input`/`output`,
  `input_tokens`/`output_tokens` o `prompt_tokens`/`completion_tokens`).

- **`HttpApp.hidesOwnRoutes` y el aviso de `rayito agent deploy`**: con
  `protocol: "agentcore"` u `"openai-chat"` el despliegue sólo expone `/ping` y
  `/invocations`; si tu `healthPath` o `invokePath` son otros, esas rutas no
  se alcanzan desde fuera (404). `rayito agent deploy` lo avisa y apunta a
  `protocol: "raw"`; la guía explica cuándo usar cada protocolo.

- **`rayito image publish --base-image-version latest`**: resuelve la versión
  más reciente que lista `list-managed-microvm-image-versions` y la imprime
  (`base_image_version=...`), así no hay que buscarla a mano y el cambio de la
  base nunca pasa en silencio; la opción sigue siendo obligatoria y un número
  explícito sigue fijándola para una build reproducible.

### Cambiado

- **`rayito agent deploy` ahora despliega una flota, con más llamadas a AWS
  (`agent-zero-config`)**: el mismo comando con un `--file` de 0.13 ya no crea
  un VM suelto sin más: comprueba la imagen base (`GetMicrovmImage`) y el
  secreto del modelo (`DescribeSecret`), resuelve el bucket (`HeadBucket` y
  `GetCallerIdentity`, y el de activos del bootstrap de CDK si no pasas
  `--bucket` ni `RAYITO_BUCKET`), construye la imagen del proyecto si su
  contenido cambió (excluye por defecto `.git`, `.env*`, `.venv`, `venv`,
  `node_modules`, `__pycache__`, `*.pyc` y `.rayito`, así que un proyecto que
  los subía a la imagen la reconstruye) y devuelve una flota. Las credenciales
  necesitan por tanto `sts:GetCallerIdentity`, `s3:ListBucket` sobre el bucket,
  `lambda-microvms:GetMicrovmImage` y `secretsmanager:DescribeSecret`, además
  de las de 0.13. Con `--no-build` y sin bucket reutiliza la imagen existente
  sin construir nada.

- **`ServeOptions({ maxConcurrency })` queda acotada por la memoria de la VM**:
  un MicroVM de `memoryMib` admite un número limitado de conexiones (7 con los
  2048 MiB por defecto, 15 con 4096 y 31 con 8192); 0.13 aceptaba cualquier
  valor desde 1. Un proyecto de 0.13 con un valor mayor falla al cargarse con
  `InvalidArgumentError`: baja `maxConcurrency` o sube `memoryMib`.

- `AgentTemplate.runtimes` y `AgentTemplateOptions.runtimes` pasan de
  `readonly AgentTemplateRuntime[]` a `readonly (string | AgentRuntime)[]`:
  quien sólo lee la propiedad como `string` debe tratar también objetos.

- **`CustomRuntime` corre tu app y tu comando desde el código copiado, con su
  venv primero en el `PATH` (`agent-runtimes-dx`)**: el directorio de trabajo de
  una `HttpApp` es el del `source` copiado (`/opt/agents/app`) y va el primero en
  su `PYTHONPATH`, así que `python -m paquete.modulo` funciona; con
  `requirements`, `python` y `pip` son los del venv, y sin él `python` es el
  intérprete de la imagen (que sólo trae `python3`). Los `command` de un
  `CustomRuntime` reciben el mismo `PATH` y `PYTHONPATH`, y el directorio de
  trabajo de un `command` no cambia. Hace falta reconstruir la imagen para que
  una ya construida lo use.

- **El uso de un agente que no informa de tokens es desconocido, no cero**: el
  host marca `usage_reported` en `POST /invocations` y deja `usage` sin valor en
  el `step_finished` de un comando con `protocol: "text"` o de una función sin
  `usage`. Un cliente de 0.13 que no conoce el campo sigue leyendo `usage`.

- **Un arranque fallido del host cuenta qué falló**: el marcador de fallo lleva el
  `detail_code` (`app_exited`, `app_not_ready`, la clase de la excepción) y, en
  una `HttpApp`, el final de su salida de error (acotado a
  `AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES`, sin valores de entorno ni credenciales);
  `deploy()` lo incluye en la `SandboxException` en vez del nombre de clase
  opaco `AgentFailure`.

- **`rayito agent init --runtime custom` escribe una app HTTP y `langgraph` un
  grafo de LangGraph a secas**: `custom` escribe `[runtime.app]` (con `protocol =
  "raw"`, sus rutas `/health` e `/invoke`), `requirements`, `install` y un
  `serve.py` mínimo; `langgraph`, un `create_react_agent` en vez de
  `create_deep_agent`. El runtime `langgraph` traduce además el nodo `agent` de
  `create_react_agent` (antes sólo `model`), así que un grafo de LangGraph a
  secas devuelve texto, pasos, herramientas y uso.

### Corregido

- **Exportar a Docker**: el contexto crea los directorios de configuración del
  agente con permisos de lectura para el usuario sin privilegios (BuildKit
  los dejaba en `0600` con `--chmod`), la imagen de OpenCode instala `curl`
  antes de descargar los binarios y los módulos y fábricas de un grafo se
  resuelven como punto de entrada.
- **El host relanzado mata antes lo que dejó el hijo muerto en su grupo**: un
  envoltorio como `npm run start` que muere dejaba al servidor huérfano
  ocupando el puerto y el relanzado nunca quedaba listo.
- **`exclude` de un `AgentProject`/`AgentTemplate({ appExclude })` filtra antes de
  leer**: un `.venv` excluido se leía entero y luego se descartaba (y su
  `cacert.pem` disparaba el aviso de ficheros con secretos). Ahora los patrones
  de `exclude` se suman a los de `.dockerignore` y filtran antes de leer ningún
  fichero.

### Obsoleto

- `AgentTemplateRuntime`: usa `string | AgentRuntime`. Sigue exportado y se
  retira en una versión posterior.

## [0.13.0] - 2026-10-16

### Añadido

- **Documentación de la CLI, el servidor MCP y el panel en TypeScript
  (ADR-027, fase 9)**: `cli.md`, `mcp.md`, las páginas del panel, el proxy
  local, la instalación y `optional-features.md` llevan pestañas Python y
  TypeScript, y `optional-features.md` suma una fila para `rayito-mcp`. La
  API de TypeScript sigue siendo sólo asíncrona (ADR-027, D8).

- **CLI `rayito` en TypeScript, fases 2, 3 y 5 (ADR-027)**: `doctor`
  (las diez comprobaciones y `--efs-vpc-id`, con `@aws-sdk/client-iam` como peer
  opcional para la simulación de IAM), `template build|status|logs`,
  `agent template build`, `events deploy|status|destroy|list` y `events webhook
  add|list|remove`, `domain deploy|status|destroy`, `dashboard cloudwatch` e
  `image publish|prune|zip`, con la misma superficie, salida y códigos de
  salida que la CLI de Python. La tabla de compatibilidad SDK a rayd, el
  contrato de `doctor`, los dashboards de CloudWatch, los contratos puros de
  `image` (nombres, configuración deseada, planes de poda, entradas del zip) y
  los casos de órdenes por pila se comparten con Python en `testdata/`.

- **CLI `rayito` en TypeScript, fase 4 (ADR-027)**: `sandbox connect` y
  `sandbox create` sin `--detach` abren una terminal real (PTY del sandbox en
  modo raw, Ctrl-C como byte, cambio de tamaño reenviado, EOF en tuberías;
  salen con el código del shell y `create` termina el sandbox pase lo que
  pase), y `sandbox proxy` expone un puerto del guest en `127.0.0.1` con las
  mismas garantías que la CLI de Python: `Host` (421) y `Origin` (403)
  validados, un solo mensaje por conexión, delimitación ambigua o cabecera con
  CR/LF suelto en 400, tope de conexiones (503) y sin JWE en ningún informe.
  Los vectores de contrabando de cabeceras, delimitación del cuerpo y
  Host/Origin (`testdata/cli/proxy-vectors.json`) los ejecutan las dos CLI.

- **Servidor MCP `rayito-mcp` en TypeScript (ADR-027, fase 6)**: el paquete
  declara el ejecutable `rayito-mcp` (`npx rayito-mcp`; `dist/mcp.mjs`, sólo
  ESM, sin tocar `exports` ni importarse desde `index`) con las mismas seis
  herramientas que el de Python (`run_code`, `run_command`, `read_file`,
  `write_file`, `list_files`, `list_sandboxes`), las mismas anotaciones, las
  mismas variables `RAYITO_TEMPLATE` y `RAYITO_MCP_*` y un sandbox por proceso
  creado en la primera herramienta que lo necesita y terminado al salir. stdio
  por defecto y `--http` para streamable HTTP en loopback con `Host` y `Origin`
  exactos. `@modelcontextprotocol/server` (2.x) y `zod` son peers opcionales:
  sin ellos `rayito` sigue funcionando y el ejecutable explica la orden
  `npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp`. Los
  nombres, descripciones, anotaciones y esquemas de entrada de las herramientas
  (`testdata/mcp/tools.json`), los casos de configuración
  (`testdata/mcp/settings-cases.json`) y las autoridades de `--http`
  (`testdata/mcp/host-vectors.json`) los ejecutan pytest y vitest.

- **CLI `rayito` en TypeScript, fase 1 (ADR-027)**: el paquete declara el
  ejecutable `rayito` (`npx rayito ...`; `dist/cli.mjs`, sólo ESM, sin tocar
  `exports` ni importarse desde `index`) con las opciones globales `--profile`,
  `--region`, `--json`, `--verbose` y `--version` (`rayito 0.x (node)`), los
  mismos códigos de salida que la CLI de Python y estas órdenes: `sandbox list |
  info | kill | logs | metrics | exec | create --detach`, `stack list | status |
  deploy | destroy` e `image list | sizes`. Salida y documentos `--json`
  idénticos a los de Python (tabla de `rich` incluida), comprobados con
  vectores y casos de `testdata/cli/` que ejecutan las dos CLI. Las demás
  órdenes (`doctor`, `template`, `events`, `domain`, `dashboard`,
  `sandbox connect|proxy`, `create` sin `--detach` e `image publish|prune|zip`)
  se describen en las entradas siguientes y entran en el mismo cambio.

- **Panel local en TypeScript, fases 7 y 8 (ADR-027)**: `npx rayito dashboard`
  sirve en `127.0.0.1` la misma SPA y el mismo contrato `/api/v1` que
  `rayito dashboard` de Python, con las mismas defensas (`Host`/`Origin`, token
  de arranque de un solo uso, cookie `HttpOnly`, `Sec-Fetch-Site`, CSRF, sólo
  lectura por defecto, CSP sin `unsafe-inline`, nada se registra) y las mismas
  opciones (`--port`, `--allow-actions`, `--cost-explorer`, `--events-stack`,
  `--log-group`, `--theme`, `--logo`, `--account`, `--external-id`, `--open`).
  Todo lo que cuesta sigue apagado hasta pedirlo. La SPA tiene una sola fuente
  (la del paquete de Python): el build la copia a `dist/dashboard-static/` y
  `pack:check` falla si el tarball difiere byte a byte. Nuevos vectores
  compartidos con Python en `testdata/dashboard/` (`api-v1-cases.json`,
  `guard-vectors.json`, `security-headers.json`, `theme-vectors.json`). Dependencia de desarrollo nueva: `ajv`
  (valida las respuestas contra `api-v1.schema.json`).

- **Pruebas locales de la CLI, el servidor MCP y el panel**: los escenarios de
  `tests/shared` corren tanto contra AWS real como en el e2e local (Docker +
  Floci), y `make local-e2e` construye `rayito-mcp` para probarlo por stdio.

### Corregido

- **`rayito doctor`: números de cuota como en Python**: el resumen humano de
  `quotas` imprime los valores con `format(value, "g")` de Python (`1e+06` en
  vez de `1000000`), con vectores `generalText` compartidos en
  `testdata/cli/format-vectors.json`.

- **El reuso de una versión de imagen comparaba objetos por el orden de sus
  claves**: AWS devuelve `hooks` con otro orden que el que se envía, así que
  `Template.build()` reconstruía una imagen idéntica. Ahora la igualdad es
  estructural y las variables de entorno (que la lista no devuelve) se
  confirman con `GetMicrovmImageVersion`, como en Python.

- **La CLI escribe los `float` de Python como Python**: `rayito --json sandbox
  metrics` (`cpu_used_pct`), el JSON de `doctor` (el valor de una cuota y su
  mínimo) y las duraciones de `image publish|prune|zip` llevan su parte
  decimal (`0.0`, `10.0`) en vez de `0` y `10`, igual que la CLI de Python y
  que la tabla.

## [0.12.0] - 2026-10-15

### Añadido

- **Cobertura e2e de la superficie del SDK**: nuevos tests contra AWS real
  (`transport`, `processes`, `filesystem`, `code` y `hardening`, espejos de los
  bloques M1 a M4 de Python) y ampliaciones de `m9-e2b`, `poly`,
  `m9-transfer`, `m9-timeout` y `m9-egress`. Sólo tests: no cambia el SDK ni
  su API. Sin `RAYITO_E2E=1` se omiten sin llamar a AWS.

- **Panel: parámetro `AuthMode` del componente `dashboard`** (`aws-iam` pone la
  API tras una Function URL con firma SigV4; por defecto `cognito-jwt`), espejo
  del de Python con los mismos textos de coste.

- e2e de sólo lectura de `Quotas`, `UsageCost` (el coste facturado sólo con
  `RAYITO_E2E_BILLED_COST=1`) y `Fleet`.

- **Coste a la vista en el panel (ADR-026)**: cada clase, método y opción del panel dice si es gratuito o de pago, y los textos de coste de los componentes `dashboard`, `dashboard-cloudwatch`, `dashboard-read-role` y `quota-alerts` son idénticos a los de Python (comprobado con un fichero de referencia común), con las cifras de `limits.ts`.

- **Panel, fase 3 (ADR-026)**: componentes de `OptionalStacks` `dashboard-cloudwatch`,
  `dashboard-read-role` y `quota-alerts` (`deploy("quota-alerts", { artifactBucket })`),
  espejo de los de Python: las mismas plantillas, el mismo zip y los mismos
  parámetros. Apagados hasta que los despliegas. Constantes nuevas en `limits.ts`.
  El generador del dashboard y la vista multicuenta son de la CLI de Python.

- **Pila `dashboard`** (el panel de gestión dentro de tu cuenta, ADR-026, fase
  2): componente de `OptionalStacks` (`deploy("dashboard", { artifactBucket })`),
  espejo del de Python: la misma plantilla, el mismo zip y los mismos parámetros
  (`AllowActions`, `EnableCostExplorer`, `ThemeJson`, `ThemeParameterName`...). De
  sólo lectura y sin Cost Explorer por defecto. Constantes nuevas en `limits.ts`
  (`DASHBOARD_STACK_*`). El coste facturado tiene tope aunque lo pida cualquier
  usuario con sesión (ventanas de meses completos, regiones conocidas y un
  presupuesto diario de consultas de pago para toda la pila) y las acciones se
  comprueban en vivo en Cognito antes de ejecutarse.

- **Servicios del panel de gestión** (`dashboard`, ADR-026, fase 1): `Quotas`
  (cuotas aplicadas de Lambda MicroVMs, memoria asignada contra la cuota de la
  región y avisos al 80 % y al 95 %), `UsageCost` (coste facturado por Cost
  Explorer por tipo de uso, con caché local de 24 h, y coste estimado por
  sandbox, siempre rotulados) y `Fleet` (sandboxes con la memoria de su
  imagen, imágenes y versiones, historial de eventos y acciones masivas con
  una cola que respeta los TPS).
  Apagados por defecto: construirlos no llama a AWS, Cost Explorer ($0,01 por
  petición) sólo se toca con `UsageCost.billed()` y las acciones exigen
  `allowActions: true`. Nueva excepción `DashboardError` y constantes
  nuevas en `limits.json` (catálogo de cuotas, umbrales, TTL y precios de
  cómputo). Los clientes de Service Quotas y Cost Explorer son
  peers opcionales (`@aws-sdk/client-service-quotas`,
  `@aws-sdk/client-cost-explorer`).
  Cada clase, método y opción dice si es gratuito o de pago (`billed()` y
  `events()` son de pago; `resume` vuelve a facturar cómputo) con el mismo
  texto en ambos SDK. Guía:
  [Panel de gestión](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/panel-de-gestion/).
- **`Template.dnfInstall()` y `Template.npmInstall(packages?, { g })`**.
  Atajos del DSL para instalar paquetes del sistema con `dnf` (sin paquetes
  débiles) y paquetes Node con `npm`; los nombres se escapan para el shell y
  se rechazan las listas vacías y los nombres que empiezan por `-`.
  `dnfInstall` y `npmInstall({ g: true })` corren como root aunque un
  `setUser()` previo dejara otro usuario. `npmInstall` falla con el código 127 y un mensaje claro si la imagen no
  tiene Node.js. `aptInstall` sigue lanzando `UnimplementedError`, ahora
  apuntando a `dnfInstall`.
- **`SandboxHealth.imdsBlocked`**: `true` cuando `rayd` instaló y verificó el
  bloqueo de IMDS para los uids del sandbox (imagen con capabilities), como
  `Health.imds_blocked` de Python; `false` en la imagen por defecto y en un
  agente anterior a M6.
- **`sbx.metadata`**: los `metadata` de `create({ metadata })` tal como los
  devolvió el último `Health` (copia congelada), como `Sandbox.metadata` de
  Python.
- **`rayito/e2b` exporta** los tipos de gráficos (`Chart`, `LineChart`,
  `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart`,
  `PointData`, `BarData`, `PieData`, `BoxAndWhiskerData`), `ChartType`,
  `ScaleType`, `ProcessInfo`, `WriteEntry` y `WatchHandle`, los mismos nombres
  que `rayito.e2b` en Python.

### Cambiado

- **La variante de imagen se reconoce con un catálogo cerrado**
  (`parity-image-variants`): `resolveImageVariant` sólo interpreta
  `rayito-<variante>[-<talla>]` con la variante en `base`, `base-caps`,
  `base-caps-efs`, `base-poly` y `base-slim`. Una imagen propia que empieza
  por `rayito-` (p. ej. `rayito-mi-app-caps-efs`) ya no se interpreta como una
  variante conocida que no es caps ni se rechaza antes de lanzar: la decisión
  pasa a `Health.features`, como ya ocurría con un ARN. `volumes` exige
  `rayito-base-caps-efs` cuando la variante se conoce.

- **Componente `dashboard-cloudwatch`**: `DashboardName` vacío (el valor por
  defecto) usa el nombre de la pila en lugar del fijo `rayito`; una pila ya
  desplegada conserva su nombre.

### Corregido

- **Componente `dashboard-cloudwatch`**: con `IncludeOtlp` o `IncludeQuota` los
  widgets añadidos se creaban con la región literal `${AWS::Region}`; la
  plantilla (la misma que usa Python) resuelve ahora la región con `Fn::Join` y
  `Ref AWS::Region`.

## [0.11.0] - 2026-10-13

### Cambiado

- **`AgentTemplate` arranca antes** (`ai-agent-first-start`). Dos cambios en
  la imagen que construye, medidos en AWS real (`AWS_API_NOTES.md` Q155 y
  Q156): `kernelWarmup: false` por defecto (opción nueva) escribe el
  marcador `slim` del sidecar, así que el kernel de `runCode` ya no importa
  numpy, pandas, matplotlib, scipy y scikit-learn en cada arranque, y el
  demonio de prefetch lee una vez los binarios de OpenCode y ripgrep antes
  del snapshot del build, con un `ready_cmd` que lo espera, para que el
  snapshot de memoria ya los lleve. `create()` vuelve antes (6,4 s frente a
  19,1 s de mediana en la misma tanda) y el snapshot de memoria pesa 62 MB
  menos; el primer token mejora poco y con mucha dispersión (20,9 s frente a
  23,7 s de mediana). Las imágenes ya construidas no cambian.
- **Cambio de comportamiento: `AgentTemplate` ya no precalienta el kernel de
  `runCode`** (`kernelWarmup: false` por defecto). En una imagen construida
  con 0.11, la primera celda de `runCode` que importe numpy, pandas,
  matplotlib, scipy o scikit-learn paga esa importación. Para
  volver al comportamiento de 0.10, construye la imagen con
  `new AgentTemplate({ ..., kernelWarmup: true })`.

### Corregido

- **`pool.take()` y `pool.close()` ya no dejan que Node salga mientras
  esperan.** Todos los temporizadores del pool, y los de los calentamientos
  en vuelo, iban sin referencia (`unref()`): con el pool vacío o con un
  relleno en marcha, un script cuyo único trabajo pendiente era
  `await pool.take({ waitMs })` o `await pool.close()` salía con la promesa
  sin resolver (aviso `unsettled top-level await`, código 13) y dejaba
  vivos los MicroVM que se estaban calentando. Ahora `take()` y `close()`
  retienen el proceso con un intervalo con referencia mientras corren; los
  temporizadores del barrido y del backoff siguen sin ella, así que un pool
  ocioso sigue sin mantener vivo el proceso. Encontrado en la aceptación de
  0.11.0 en AWS real.

## [0.10.0] - 2026-10-08

### Añadido

- **Más proveedores para el modelo del agente** (`ai-agent-providers`).
  `AgentModel` admite `provider` `"openai"` (Responses API, también xAI),
  `"google"` (Gemini API) y `"azure"` (Azure OpenAI v1), y hay nueve presets
  de pasarela con clave de API estática: `openaiGateway`,
  `geminiGateway({ models })`, `azureOpenaiGateway({ resource })`,
  `openrouterGateway`, `groqGateway`, `mistralGateway`,
  `deepseekGateway`, `xaiGateway` y `litellmGateway({ upstream,
  basePath: "/v1" })`. OpenCode usa los proveedores nativos `openai`,
  `google` y `azure` a través de la pasarela; deepagents construye
  `ChatOpenAI(use_responses_api=True)`, `ChatGoogleGenerativeAI` o
  `AzureChatOpenAI` (Responses API, clave en `api-key`). El
  `opencode.json` declara el paquete del AI SDK de cada proveedor
  (`@ai-sdk/openai`, `@ai-sdk/google`, `@ai-sdk/azure`, todos dentro del
  binario fijado), Azure apunta a `/openai/v1` y
  `OPENCODE_EXPERIMENTAL_WEBSOCKETS` queda apagado; la prueba de humo de
  `AgentTemplate` importa también `langchain_openai` y
  `langchain_google_genai`. `gen_ai.provider.name` vale
  `gcp.gemini` y `azure.ai.openai` para los nuevos. En las APIs al estilo
  de OpenAI la pasarela no puede limitar el modelo: fija el tope de gasto
  en el proveedor (SECURITY.md T29). Las suscripciones de consumo (plan de
  ChatGPT, Claude Pro/Max, Copilot, SuperGrok) no se admiten; la guía
  "Proveedores del agente" explica por qué. Construir un preset no llama a
  AWS.

### Fixed

- **El timeout y `abort()` de `sbx.agent` paran también lo que lanzó la
  herramienta de shell del agente.** OpenCode y deepagents corren cada orden
  en una sesión propia (`setsid`), fuera del grupo del runtime, y el timeout
  sólo mataba ese grupo: un `sleep` o un servidor lanzado por el agente
  sobrevivía y el siguiente `run()` encontraba el agente `busy`. Cada
  ejecución arranca ahora con `killTree: true` y `rayd` congela y mata todo
  su árbol (también lo demonizado); el stream acaba con `reason: "timeout"`
  cuando ya no queda nada. `abort()` y los límites del SDK sólo matan el
  handle: desaparece la orden `pgrep` que corría antes como segundo comando.
  `commands.run()` acepta `killTree` para lo mismo en
  cualquier comando. Necesita el `rayd` de esta versión: con uno anterior el
  campo se ignora y el timeout y `abort()` sólo alcanzan al grupo del
  runtime; reconstruye la imagen.

- **deepagents cuenta los tokens de los modelos compatibles con OpenAI.**
  Con `"openai-compatible"`, `ChatOpenAI` no pedía el uso en streaming
  (`stream_options.include_usage`): cada paso llegaba con 0 tokens,
  `result.usage` quedaba a cero y `AgentLimits.maxTotalTokens` no cortaba nunca. El
  runner crea ahora `ChatOpenAI(stream_usage=True)`. Medido de verdad, con
  el egress cerrado, contra la Chat Completions compatible con OpenAI de
  Amazon Bedrock (`bedrock-runtime` `/openai/v1` y `bedrock-mantle`) y un
  proxy de LiteLLM; la Responses API (`"openai"`, `"azure"`) ya mandaba el
  uso.

- **deepagents con modelos que no son de Bedrock**: el venv de
  `AgentTemplate` (y del guest local) lleva `socksio`, el extra `socks` de
  httpx. `rayd` exporta `ALL_PROXY=socks5h://...` en el sandbox y, sin él,
  httpx fallaba al crear el cliente (`AgentFailed` con `ImportError`) con
  OpenAI, xAI, Azure OpenAI, Gemini, Anthropic y las APIs compatibles con
  OpenAI, aunque el modelo se llame por la pasarela en loopback. Cambia el
  sha256 de `requirements-deepagents.txt`: reconstruye la plantilla de
  agente.

## [0.9.1] - 2026-10-07

### Fixed

- **`Sandbox.create({ timeoutMs: 120_000 })` sin `idle` ya no lanza
  `InvalidArgumentError`.** Desde 0.3.0, cualquier `timeoutMs` (o
  `maxLifetimeMs` con `onTimeout`) de 300 s o menos fallaba porque el SDK
  validaba su propia política de idle por defecto contra él. Ahora, sin
  `idle`, la auto-suspensión por defecto se desactiva si no cabe (el sandbox
  termina en su plazo, como en E2B) y, con `onTimeout: "pause"`, baja a
  60 s (`PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS`). Un `idle` que pasas tú se
  valida igual que antes.

### Documentation

- Los e2e de `s3-mounts` y `rayd-otlp` documentan como precondición que la
  política gestionada de su pila (`PolicyArn`) esté adjunta al rol de
  ejecución; `rayito stack deploy` sólo la crea.

## [0.9.0] - 2026-10-07

### Cambios que rompen

- **Se retira la opción D del arranque rápido del agente** (pool con
  `opencode serve` residente), sin periodo de obsolescencia: apenas ganaba
  unas décimas a la opción C (`AWS_API_NOTES.md` Q154) y costaba más
  memoria y más por plaza. Desaparecen la opción `serve` de
  `agentPoolWarmup()` y de `sbx.agent.prepare()`, `attach` de
  `AgentRunOptions` (`sbx.agent.run()`/`stream()`) y `RunRequest.attach`;
  el script de OpenCode ya no busca ni se engancha a un servidor
  (`--attach`) ni relee la vuelta de él, y el span `rayito.agent.run` deja
  de llevar `rayito.agent.attached`. La interfaz `AgentRuntime` pierde
  `abortCommand` (ningún runtime lo usaba ya) y `warmupSteps()` no lleva
  argumentos; `OPENCODE_SERVE_PORT` desaparece. **Migración**: quita
  `serve: true` y `attach`; para un primer mensaje rápido usa
  `agentPoolWarmup(runtime)` (opción C) o el arranque normal
  (`Sandbox.create(...)` y `sbx.agent.run(...)`). Un runtime propio borra
  `abortCommand` y el parámetro de `warmupSteps`.
- **Se eliminan `GatewayError` y `GatewayErrorOptions`** (no estarán en
  0.9.0). `GatewayError` nunca se lanzaba: lo que la pasarela de secretos
  rechaza por petición llega al proceso del sandbox como respuesta HTTP
  (403, 429, 502, 504), y una configuración que `rayd` rechaza es
  `SandboxError`. Migración: quita el import y cualquier
  `instanceof GatewayError`; si capturabas fallos de la pasarela al crear o
  refrescar, usa `instanceof SandboxError`.

### Changed

- **TSDoc alineado con la documentación** (barrido de docs de 0.8): los
  errores sin TSDoc lo tienen, los códigos de `MountError`, `AgentError`
  (`runtime_version_mismatch` y `output_limit`, reservados)
  dicen lo que pasa de verdad, y `reincarnate()` lista
  todo lo que reaplica.

### Fixed

- **`Sandbox.connect()` recupera `sbx.gateways`**: otro proceso ya puede usar
  `sbx.agent` sobre un sandbox creado con `gateways` sin recrearlo.
  `connect()` lee una vez el `ConfigureStatus` de `rayd` (nombre, puerto y
  último error de cada ruta; nunca el upstream ni las cabeceras) si el
  agente anuncia `secret_gateway` y el handle no aplicó `gateways` él
  mismo. En un handle recuperado, `refresh()` sólo relee el estado: rotar
  la clave sigue siendo cosa del proceso que creó la pasarela. Si `rayd`
  rechaza el token en esa lectura, `connect()` no falla: se salta la
  recuperación y el `AuthenticationError` sale en la primera llamada
  autenticada, como hasta ahora.

## [0.8.0] - 2026-10-07

### Cambios que rompen

- `registerWebhook` (eventos de ciclo de vida) rechaza con
  `InvalidArgumentError`, antes de cualquier llamada a AWS, una URL cuyo
  host es `localhost` (o `*.localhost`) o una IP literal no pública
  (loopback, privada, link-local, CGNAT, reservada…). Hasta 0.7.1 se
  guardaba y el deliverer la descartaba en cada entrega, así que nunca se
  entregó nada. Migración: registra una URL HTTPS con un nombre público
  (detalle en "Security").

### Added

- **Agente de IA, adaptador de deepagents** (`ai-agent-deepagents`):
  `runtime: "deepagents"` o `runtime: new DeepAgents({ entrypoint: "pkg.mod:build" })`
  corre un grafo de deepagents/LangGraph con el mismo contrato que OpenCode
  (eventos cerrados, límites del SDK, credenciales sólo por la pasarela).
  Configuración, script y traducción del protocolo JSONL v1 idénticos byte
  a byte a los de Python (`testdata/agent/`); el código del runner va en
  `src/agent/assets/deepagents-runner.ts`, generado por
  `scripts/gen_agent_assets.py`. `mcp`, `rawConfig` y `attach: true`
  fallan con `InvalidArgumentError` antes de cualquier RPC.
- **Agente de IA, arranque rápido** (`ai-agent-fast-start`):
  `AgentTemplate` (y `rayito agent template build` en la CLI de Python)
  construye la imagen del agente con OpenCode y ripgrep fijados por
  sha256 y el venv de deepagents con `--require-hashes` (salvo
  `runtimes: ["opencode"]`), todo de root, el manifiesto `rayito.agent-template/1`
  y, salvo `prefetch: false`, un demonio que precarga los binarios tras cada
  restauración del snapshot. `PoolConfig` gana `warmup` (pasos que cada
  plaza corre antes de aparcarse; un paso fallido es un calentamiento
  fallido), `allowInternetAccess` y `network`; `agentPoolWarmup()` da
  los pasos del agente y, con `serve: true`, deja un `opencode serve`
  residente con contraseña propia de cada VM al que `agent.run` se
  engancha tras el `take()`. Coste sólo si construyes la plantilla o pasas
  `warmup` (bloques "Coste y activación").
- **Agente de IA, adaptador de OpenCode** (`ai-agent-core`): `runtime="opencode"`
  ya resuelve a un adaptador (`src/agent/opencode.ts`) que escribe `opencode.json` sin
  credenciales, lanza `opencode run` con el prompt por stdin, un cerrojo por
  sandbox y `--attach` a un `opencode serve` residente si responde, y
  traduce su JSONL a eventos. Configuración y script idénticos byte a byte
  entre Python y TypeScript (`testdata/agent/`).
- **Agente de IA, dominio** (`ai-agent-core`, ADR-025): los tipos con los
  que `sbx.agent` describirá una ejecución, validados al construirlos y sin
  ninguna llamada a AWS: `AgentSpec`, `AgentModel` (Bedrock, Anthropic o
  una API compatible con OpenAI, siempre a través de una pasarela de
  `sbx.gateways`), `AgentPermissions` (sólo `allow`/`deny`; `question`,
  `webfetch` y `websearch` denegadas por defecto), `SubAgent`, `McpLocal`,
  `McpRemote` y `AgentLimits` (50 pasos, 600 s, 16 MiB y 1 000 000 de
  tokens por defecto); los eventos (unión discriminada por `type`;
  `agentFailed()` construye el de fallo), `TokenUsage` y `AgentResult`;
  `AgentError` con un `reason` de una lista cerrada y un mensaje fijo que
  nunca lleva texto del proveedor; y las pasarelas ya hechas
  `bedrockGateway`, `anthropicGateway` y `openaiCompatibleGateway`, cuyo
  `allow` cubre sólo los modelos elegidos. `sbx.agent` llega en un cambio
  posterior.
- **Agente de IA, API pública** (`ai-agent-core`, design.md §4 y §7): el
  campo `sbx.agent` (`run`, `stream`, `prepare`). `run()` corre el agente
  hasta el final y lanza `AgentError` si falla; `stream()` devuelve un
  `AgentStream` (`AsyncIterable<AgentEvent>`) que nunca lanza por un fallo
  del agente —el último evento es `Done` o `AgentFailed`— y expone
  `.abort()`, `.sessionId` y `.result()`; un `AbortSignal` en `stream()`
  dispara `abort()`. `prepare()` dispara los pasos de calentamiento del
  runtime en segundo plano y vuelve enseguida. Aplica la configuración del
  runtime con `files.writeFiles` (una sola vez por sha de configuración),
  lanza el script del runtime con `commands.run({ background: true, stdin:
  true })` y trocea su stdout en eventos; `AgentLimits.maxSteps` y
  `maxTotalTokens` los impone el propio SDK sobre esos eventos, no el
  runtime. `sbx.agent` no manda ningún RPC hasta el primer `run()`/
  `stream()`. Con `tracerProvider`, cada ejecución abre un span
  `rayito.agent.run` con atributos `gen_ai.*` y `rayito.agent.*` (nunca el
  prompt, el texto de la respuesta ni argumentos de herramienta). Un
  `AgentRuntime` concreto (el adaptador de OpenCode) llega en un cambio
  posterior; mientras tanto, `runtime` admite cualquier objeto que
  implemente la interfaz `AgentRuntime`.

### Fixed

- `sbx.agent` con un `opencode serve` residente (`agentPoolWarmup("opencode", { serve: true })`,
  opción D): `opencode run --attach` (1.18.34, igual en 1.18.35) sale en
  cuanto el servidor contesta el prompt, sin esperar a sus propios eventos,
  y el SDK veía un `agent_failed` con `reason: "protocol_error"` o un `Done` sin texto
  aunque el servidor completaba la respuesta. Ahora el script crea la sesión
  en el servidor, y al salir `run` relee de él los mensajes de esa vuelta y
  emite las partes que faltaban (sin duplicar las ya emitidas), incluido el
  error del modelo. Un `sessionId` que no tiene forma de id de OpenCode es
  `InvalidArgumentError`.
- `AgentTemplate` con `prefetch`: el demonio de precarga leía los binarios
  nada más restaurar el snapshot y su E/S competía con el arranque, así que
  `create()` tardaba ≈ 8 s más (Q146). Ahora espera a que el guest lleve
  1 s sin E/S en curso (como mucho 60 s) antes de leer.
- `AgentTemplate`: instala el runner de deepagents en
  `/opt/agents/rayito/deepagents_runner.py` (root, 0755) y pone su sha256 en
  `runner_sha256` del manifiesto. Sin él, `runtime="deepagents"` fallaba
  siempre en una imagen construida con `AgentTemplate` (aceptación en AWS,
  `AWS_API_NOTES.md` Q150).
- `sbx.agent`: `abort()` y los límites `maxSteps`/`maxTotalTokens`
  paran también lo que lanzó la herramienta de shell del agente (OpenCode y
  deepagents la corren en una sesión propia, fuera del grupo que mata
  `rayd`); antes un `sleep` o un servidor lanzado por el agente sobrevivía.
  Un límite del SDK, además, ya no deja el runtime trabajando (y gastando
  tokens) en segundo plano, y el siguiente `run()` no lo encuentra `busy`.
- `sbx.agent`: un timeout de `AgentLimits` terminaba con
  `reason="runtime_error"` en vez de `"timeout"`.
- `runtime: "deepagents"`: las herramientas de ficheros escribían una ruta
  absoluta dentro del workdir (`/home/user/x` acababa en
  `/home/user/home/user/x`); ahora usan la ruta tal cual, como el shell.
- `sbx.agent.prepare()` arranca los pasos en segundo plano sin plazo: con
  `serve: true` el servidor residente moría al agotar `timeoutMs`.

### Changed

- **Montajes S3**: el `MountError` de un bucket que no está en el allowlist de
  la imagen (`code: "not_allowed"`) ya no dice sólo que la sección se rechazó: explica
  que falta en `RAYITO_ALLOWED_MOUNT_BUCKETS` y cómo publicar la imagen con
  él (`--env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>` o
  `make image-publish-caps MOUNT_BUCKETS=<bucket>`), sin nombrar el bucket.
  El texto vive en `MOUNT_ERROR_HINTS`, igual en los dos SDKs
  (`testdata/s3-mounts/error-hints.json`).

### Security

- **Eventos de ciclo de vida (`webhook-url-public-address`)**: `registerWebhook`
  rechaza también, con `InvalidArgumentError` y antes de cualquier llamada a AWS,
  un host `localhost` (o bajo `.localhost`) y una IP literal que el guardián
  SSRF del deliverer bloquearía: loopback, privada, link-local (incluida la
  de metadatos), multicast, reservada, sin especificar o CGNAT, también como
  IPv6 con una IPv4 dentro (`::ffff:127.0.0.1`) o en las formas numéricas
  de IPv4 (`0x7f.1`, `2130706433`). Antes se guardaba y el deliverer la
  descartaba en cada entrega. `isBlockedWebhookAddress` aplica la misma regla que
  el deliverer y los tres pasan `testdata/lifecycle-events/ssrf-address-vectors.json`.
  Un nombre DNS sigue sin resolverse al registrarlo: el DNS rebinding lo
  para el deliverer.

## [0.7.1] - 2026-10-06

### Changed

- Sin cambios en el SDK TypeScript: sube a 0.7.1 por las versiones
  enlazadas, con `rayd` 0.7.1 (avisos de licencia de terceros en el zip, la
  imagen y la release).

## [0.7.0] - 2026-10-05

### Added

- **Dominio propio** (`m15-custom-domain`, ADR-024, **experimental**,
  apagado por defecto): la clase `CustomDomain` despliega una distribución
  CloudFront, una CloudFront Function de enrutado (`cloudfront-js-2.0`) y
  un KeyValueStore (`infra/custom-domain.yaml`), y `register`/`unregister`/
  `refresh`/`hostFor` gestionan las rutas `{puerto}-{alias}.<tu-dominio>`
  (hostname HTTPS normal, sin las cabeceras `x-aws-proxy-*`).
  `register()` exige `trafficToken` salvo `public: true` explícito.
  `deploy({ alternateDomainNames })` sustituye el alias comodín por
  hostnames exactos. Nuevos peers opcionales
  `@aws-sdk/client-cloudfront-keyvaluestore` y `@aws-sdk/signature-v4a` (el
  plano de datos del KeyValueStore firma con SigV4A); sin instanciar
  `CustomDomain` no se importa ninguno. `domain` en `Sandbox.create()` sigue
  lanzando `UnimplementedError`: la integración con `getHost()`/`expose()`
  llega en un cambio posterior. **Experimental** porque no se ha verificado
  de punta a punta en AWS real: la Function compila y enruta en el runtime
  real (`TestFunction`), y la pila crea el KeyValueStore y la Function, pero
  la cuenta de pruebas deniega `cloudfront:CreateDistribution` por una SCP,
  así que nunca se ha servido tráfico por una distribución desplegada con
  esta plantilla (`AWS_API_NOTES.md` Q121, Q140, Q141). La API puede cambiar
  en una minor.
- `rayito-base-caps-efs` (y sus sufijos de tamaño) cuenta como variante caps
  para `mounts`, `volumes` y `telemetry` (`requireCapsFor`, `CAPS_VARIANTS`,
  `EFS_CAPS_VARIANT`).
- **Volúmenes EFS** (`m15-efs-volumes`, ADR-018, **experimental**, apagado
  por defecto): `VolumeStore` (CRUD real de access points EFS:
  `CreateAccessPointCommand`/`DescribeAccessPointsCommand`/
  `DeleteAccessPointCommand`, con `@aws-sdk/client-efs` como peer opcional
  cargado sólo en el primer uso) y `Sandbox.create({ volumes })`, que
  valida la petición (tipos, rutas, variante `caps` y un único conector
  propio en `egress`: sin él, con `INTERNET_EGRESS` o con dos conectores es
  `InvalidArgumentError`, porque un MicroVM sólo admite un conector de
  egress, `AWS_API_NOTES.md` §16 Q131), como mucho 4 volúmenes y
  `executionRoleArn` antes de cualquier llamada a AWS; resuelve la IP de
  mount target que falte con un `DescribeMountTargetsCommand` por sistema de
  ficheros antes de `run-microvm`, manda la sección `efs_volumes` en el
  único `ConfigureSandbox` de `create()` (con plazo de 65 s), sólo vuelve
  con todos los volúmenes `mounted` y, si uno falla, termina el sandbox
  (salvo `keepOnFailure`) y lanza `VolumeMountError` (`code`); sobre una
  imagen sin `amazon-efs-utils`, `UnimplementedError`. `reincarnate()` la
  vuelve a mandar y `sbx.volumes()` da el estado en vivo. `EfsVolume`
  (valida `mountTargetIp` como IPv4), `VolumeStatus` y los errores
  `VolumeError`/`VolumeMountError`/`VolumeNotFoundError`/`VolumePathNotFoundError`.
  El shim de E2B (`Volume`) hace CRUD real sobre
  `new E2B({ volumeStore })` (`volumeId` es el nombre del volumen, el mismo
  que reciben `connect`/`getInfo`/`destroy`); sus operaciones de contenido
  (`UnimplementedError("volume.content")`) siguen sin plano de datos, y
  `Sandbox.create({ volumeMounts })` monta con
  `new E2B({ volumeStore, volumeConnectorArn })`: ese sandbox sale sólo por
  el conector del volumen y `allowInternetAccess: true` explícito es
  `InvalidArgumentError`. Componente
  `rayito stack {deploy,status,destroy} efs-volumes`
  (`infra/efs-volumes.yaml`: sistema de ficheros EFS cifrado, un mount
  target por subred de `SubnetIds`, grupos de seguridad NFS nuevos y
  conector de egress dedicado, sólo dentro de una VPC que ya existe).
  `EfsVolumes`: `check({ vpcId, subnetIds })` comprueba la VPC sin crear
  nada (sólo `Describe*` de EC2 con el peer opcional `@aws-sdk/client-ec2`;
  cuenta las subredes con ruta por defecto a un NAT y a otra puerta, como un
  transit gateway),
  `deploy()` se niega si algún hallazgo es `FAIL`, `volumeStore()` da un
  `VolumeStore` sobre la pila, y `destroy({ deleteFileSystem: true })`/
  `deleteFileSystem(id)` borran el sistema de ficheros conservado (sólo uno
  con la etiqueta `rayito=efs-volumes`). `deploy({ readOnlyAccessPointArns })`
  (`ReadOnlyAccessPointArns`) le deniega `ClientWrite` a los access points de
  sólo lectura: la opción `ro` del montaje no basta, porque el usuario del
  sandbox alcanza el puerto local de `efs-proxy` (Q133). El hallazgo
  `internet-egress` de `check()` explica que un sandbox con volumen sólo
  tiene internet por la VPC.
  `list`/`get` son eventualmente consistentes, como `DescribeAccessPoints`
  (medido en AWS real, `AWS_API_NOTES.md` §16 Q125: hasta 11 s en listar un
  access point nuevo y 8 s en dejar de listar uno borrado): `create` de un
  nombre que ya existe reintenta `get` hasta 30 s y `destroy` de un access
  point que el listado aún mostraba pero ya no existe devuelve `false`.

### Security

- **Eventos de ciclo de vida (`events-webhooks`): una línea maliciosa ya no
  tira los eventos legítimos de su lote.** El forwarder verifica el MAC con
  la clave del sandbox que nombra el *log stream* **antes** de leer el
  contenido, parsea de forma estricta y nunca falla la invocación por una
  línea: la cuenta (`malformed_line`, `mac_invalid`, `stale_event`,
  `internal_error`) y sigue. Si falla una escritura en la tabla, el resto
  del lote se escribe igual y el lote va a la nueva cola
  `ForwarderFailuresQueue` (destino `OnFailure` de su invocación asíncrona)
  en vez de perderse. El nombre del stream deja de presentarse como prueba
  de identidad: sólo el MAC autentica (`SECURITY.md` T22).
- **Eventos: sólo lo que emite `rayd`, y reciente.** Tras el MAC, el evento
  debe ser del mismo sandbox que el stream, con un `event_id` de 32 hex
  (nunca un id del reconciliador), `kill_reason` sólo en `killed` y sólo
  `request`, un ARN de `microvm-image` y un `occurred_at_ms` de menos de
  24 h (y no más de 5 min en el futuro): una línea capturada y repetida
  cuando su fila de deduplicación ya caducó se rechaza. La deduplicación de
  entregas pasa a `DELIVERY#<sandbox_id>#<event_id>`, así que un sandbox no
  puede marcar como entregado el evento de otro (las entregas en curso al
  actualizar la pila pueden repetirse una vez).
- **Webhooks: uno roto o lento ya no bloquea a los demás.** El deliverer
  trata una URL que no puede parsear como un fallo permanente de ese
  webhook (`invalid_url`), aísla cualquier error inesperado a su webhook
  (`internal_error`) y el plazo de cada intento cubre la resolución DNS y
  la respuesta entera. `registerWebhook` rechaza con
  `InvalidArgumentError` una URL que el deliverer no podría alcanzar (puerto
  fuera de 1-65535, host inválido o no codificable en IDNA, espacios o
  barras invertidas), con el mismo criterio en los dos SDKs.
- **Código de las pilas opcionales: nunca se fía de un objeto que ya
  exista.** `OptionalStacks.deploy()` sube el zip a
  `rayito/stacks/<componente>/<sha256>.zip` (dentro del espacio `rayito/`
  que protegen las plantillas de IAM, ya no en la raíz del bucket), manda
  `ExpectedBucketOwner` con la cuenta del llamante y, si el objeto ya
  existe, compara su contenido antes de reutilizarlo (si no coincide, lo
  sobrescribe con `ChecksumSHA256` y avisa por el logger).
- **`infra/iam.yaml`: el lanzador ya no puede publicar imágenes.** Nuevas
  políticas `SandboxLauncherPolicy` (sólo lanzar y manejar sandboxes: la
  del rol de un servicio en producción) e `ImagePublisherPolicy` (sólo
  publicar); `CallerPolicy` sigue igual, como su unión. El `Deny` del
  execution role sobre los artefactos de imagen existe ya siempre, no sólo
  con `PersistenceBucket`.
- **`infra/templates.yaml`: `RayitoTemplateBuilder` sólo lee `rayito/*`**
  del bucket de artefactos o de la imagen base, nunca el bucket entero
  (que puede guardar los `HOME` persistidos y las transferencias).
- **Secretos de firma de webhooks fuera de `secrets=`.**
  `RayitoSecretsReader` niega `rayito/webhooks/*` y el SDK se niega a leer
  esos secretos por `secrets`/`SecretCache` (`InvalidArgumentError`).
  `SecretPrefix` de `infra/secrets-access.yaml` debe terminar en `/`.
- **Mínimo privilegio en `events-webhooks`**: cada Lambda escribe sólo sus
  tipos de fila (`dynamodb:LeadingKeys`) y sólo en su propio log group (la
  pila crea uno por función y lo borra con ella), y el rol del Scheduler
  exige `aws:SourceAccount`.
- **Eventos: el código del sandbox ya no puede inundar la pila con
  `paused`/`resumed`.** Esos dos eventos los emite `rayd` cuando corren sus
  hooks de suspensión y reanudación, que el código sin privilegios del
  sandbox puede invocar. El forwarder sólo admite ahora eventos que hagan
  avanzar el ciclo de vida de su sandbox (nada tras `killed`, ningún
  `created` repetido, ninguna posición repetida o anterior) y limita
  `paused`/`resumed` con un cubo por sandbox (20 seguidos, después uno cada
  30 s); lo rechazado se cuenta como `invalid_transition` o `rate_limited`
  y nunca se guarda ni se entrega. El reconciliador consulta un índice
  disperso de sandboxes abiertos en vez de escanear la tabla entera, y el
  deliverer sólo se invoca por eventos nuevos. `paused`/`resumed` quedan
  documentados como orientativos (`SECURITY.md` T22). Al actualizar la
  pila, un sandbox que ya estaba abierto entra en el índice con su
  siguiente evento.
- **Webhooks: segunda firma con marca de tiempo.** Cada entrega lleva
  además `rayito-signature: t=<segundos>,v1=<HMAC-SHA256>` sobre
  `"<t>.<webhook_id>."` y el cuerpo; la guía de eventos explica cómo
  verificarla con una ventana de 5 minutos. `e2b-signature` no cambia.
- **Webhooks: un secreto rotado deja de usarse en 5 minutos.** El deliverer
  guardaba cada secreto en caché mientras viviera su contenedor; ahora lo
  relee cada 5 minutos y, si el receptor responde `401`/`403`, lo relee en
  el acto y reintenta una vez con el nuevo.
- **Políticas del llamante por tarea en `events-webhooks`.** La pila emite
  `EventsLauncherPolicy` (`events=`: sólo la clave del stack),
  `EventsReaderPolicy` (`getEvents`: sólo filas de eventos; ya no puede
  registrar un webhook ni leer la clave del stack) y
  `EventsWebhookAdminPolicy` (`registerWebhook`/listar/borrar: sólo filas `WEBHOOK`).
  La guía de eventos tiene la tabla de qué llamada necesita cuál y avisa de
  que los webhooks son de toda la pila.
- **`sizes-guard`: `RayitoRunAllowedSizes` niega también publicar
  imágenes.** Sin ello, una identidad con la `CallerPolicy` estándar podía
  reconstruir una imagen permitida a un tamaño mayor y lanzarla sin salir
  de la lista. La política ya no dice que valga "sin importar lo demás": una
  versión antigua y mayor bajo un nombre permitido sigue siendo lanzable con
  `imageVersion` (`SECURITY.md` T27).
- **`SECURITY.md`: filas T20, T23, T26 y T27**, que los cambios de M15
  planearon y nunca se aplicaron (`infra/otlp-export.yaml` ya citaba T23).
- `infra/ci-oidc-role.yaml` y `infra/README.md` piden una cuenta dedicada a
  e2e (o imágenes de test propias): el rol puede acuñar tokens y terminar
  cualquier sandbox de las imágenes de `TestImageArns`.
- **`SecretGateway` rechaza una ruta de `allow` que `rayd` nunca dejaría
  pasar** (`sec-rayd-agent-hardening`): las rutas de `allow` siguen ahora
  la misma lista de permitidos que `rayd` aplica a cada petición
  (caracteres de ruta de RFC 3986 salvo `;`, sin segmentos vacíos salvo el
  último, y cada segmento decodificado una sola vez UTF-8 válido, sin `/`,
  `\`, `%`, `;` ni bytes de control y distinto de `.`/`..`), y una que no
  la cumpla lanza `InvalidArgumentError` antes de cualquier llamada,
  con los mismos vectores compartidos que `rayd`
  (`testdata/secret-gateway/request-paths.json`).
- **`.dockerignore` con la semántica de Docker** en `Template.build`:
  anclado en la raíz, `**` como cero o más directorios (así `**/.env`,
  `**/.git` y el resto de los valores de `docker init` excluyen también los
  de la raíz, que antes acababan en la imagen), un directorio excluido
  excluye lo de dentro y la última coincidencia gana; mismos vectores que el
  SDK de Python. **Cambio**: `*` y `?` ya no cruzan `/` (`*.pyc` sólo
  excluye los de la raíz; usa `**/*.pyc`). Un `process.emitWarning` de tipo
  `RayitoContextWarning` (con las rutas, nunca el contenido) avisa si el
  contexto va a empaquetar `.env*`, `.git/`, `.aws/`, `.ssh/`, `*.pem` o
  `*.key`. Cada fichero del contexto se vuelve a comprobar dentro del
  contexto y se abre con `O_NOFOLLOW` justo antes de leerlo.
- **`Template.build()`**: un rechazo de AWS distinto de la cuota es ahora
  `BuildError({ reason: "aws_error" })` con el resumen saneado como mensaje y
  `cause`, y el adaptador sanea cada llamada al SDK de AWS (el error crudo de
  smithy lleva `$response`, la petición firmada; un error de firma lleva la
  cadena canónica con el token de sesión). El `name` del error se conserva.
  `StackError` usa también el resumen saneado como `cause`.
- **Credenciales de git**: `clone`/`push`/`pull` con `username`/`password`
  corren sin hooks ni credential helpers, se niegan (`GitAuthError`) si la
  configuración de git reescribe URLs (`url.*.insteadOf`), y un `clone` que
  falla sin exit de git intenta igualmente quitar las credenciales de
  `origin`; si una restauración falla, el `logger` del sandbox avisa sin la
  URL. La documentación dice ahora que el token queda al alcance del código
  del sandbox.
- **Salida acotada en memoria**: cada descriptor de un comando o una PTY
  guarda como mucho 64 MiB (`COMMAND_OUTPUT_MAX_BYTES`); se conserva el
  final y `CommandResult.truncated`/`CommandExitError.truncated` lo indican.
  `commands.run`/`connect` aceptan `maxOutputBytes` (`0` no guarda nada; los
  callbacks reciben siempre todo).
- **Access token mínimo**: un token propio (`accessToken`,
  `RAYITO_ACCESS_TOKEN`) tiene que decodificar a al menos 16 bytes
  (`ACCESS_TOKEN_MIN_BYTES`). **Cambio**: uno más corto, aceptado antes,
  ahora es `InvalidArgumentError`. Los generados (32 bytes) no cambian.
- `ProxyToken.jwe` ya no es enumerable: `console.log`/`util.inspect` y
  `JSON.stringify` no lo muestran.
- Tests que fijan que `Template.build()` no sigue enlaces simbólicos dentro
  de un directorio copiado (misma regla que el SDK de Python).
- **`Sandbox.create({ persist })` ligan el sandbox a su prefijo de persistencia** (C-07): el
  `runHookPayload` lleva el bucket y la base `prefix` del `S3Prefix` (nunca
  `prefix/name`), y un `rayd` con la corrección rechaza con
  `permission_denied` cualquier checkpoint o restore fuera de esa base. Con
  un `prefix` por inquilino, el token de un sandbox ya no alcanza el `HOME`
  persistido de otro aunque compartan execution role. Cambia un uso: un
  restore o checkpoint explícito hacia otra base desde un sandbox creado con
  `persist=` ahora falla con `permission_denied`. No añade llamadas a AWS.
- **Recortes y troceo de cadenas en tiempo lineal** (CodeQL
  `js/polynomial-redos`): `git.clone()` con credenciales, `files.download()`
  sin `filename`, el access token y las líneas de `.dockerignore` de un
  template usaban expresiones regulares que retrocedían en tiempo
  cuadrático (50 000 caracteres bloqueaban el bucle de eventos más de un
  segundo). La semántica no cambia: una URL con un fin de línea tras la
  autoridad sigue sin reconocerse como http(s).

### Deprecated

- `EventsOperatorPolicy` (salida `OperatorPolicyArn` de `events-webhooks`):
  sigue siendo la unión de las tres políticas nuevas durante esta versión.
  Vincula a cada identidad la suya (`EventsLauncherPolicy`,
  `EventsReaderPolicy` o `EventsWebhookAdminPolicy`).

## [0.6.1] - 2026-10-04

### Fixed

- **Redesplegar una pila opcional ya no deshace su configuración**
  (`OptionalStacks.deploy()`): los valores por defecto del catálogo sólo se
  aplican al crear la pila; al actualizarla, cada parámetro que no vuelves a
  pasar y la pila ya tiene se manda con `UsePreviousValue`. Antes, por
  ejemplo, redesplegar `s3-mounts` sin repetir `Prefixes` volvía a `'*'`
  (todo el bucket), `metadata-index` sin `TableName` reemplazaba (y borraba)
  la tabla y `secrets-access` sin `KmsKeyArn` quitaba `kms:Decrypt`. Nuevo
  `parameterChanges()` para ver qué cambiaría sin desplegar.
- **Guardián SSRF de los webhooks** (pila `events-webhooks`): una dirección
  IPv6 que encapsula una IPv4 (`::ffff:100.64.0.1`) se clasifica como esa
  IPv4, así que el rango CGNAT queda bloqueado también por esa vía. Se
  regenera el artefacto de la Lambda que el SDK sube.
- `volumes`/`domain` lanzan `UnimplementedError` con "todavía no
  disponible" en vez de "llega en 0.6".
- Se exporta `WorkdirStep` desde `rayito` (paridad con Python: ya era
  miembro de la unión `WireStep`).
- **`reincarnate()` reaplica todas las secciones de `ConfigureSandbox`**
  (`m15-reincarnate-configure-replay`): hasta ahora sólo reenviaba
  `gateways` y un sucesor perdía `mounts`, `events` y `telemetry`.
  `create()` guarda sus opciones 0.6 en `LaunchOptions.features`
  (`relaunchFeatures`: todas menos `size`) y `reincarnate()` las reenvía
  (`relaunchCreateOptions`) al `create()` del sucesor, que las aplica por el
  mismo camino en su único `Configure`: `events` deriva `k_sbx` del nuevo
  `sandboxId`, `mounts` espera otra vez a `mounted` y `telemetry` usa la
  imagen y la memoria del sucesor. De paso, `reincarnate()` de un sandbox
  creado con `size` ya no reenvía el tamaño resuelto junto al ARN de la
  imagen (que ya lo lleva), combinación que `create()` rechaza.
- **Tipos correctos para consumidores CommonJS** (`exports` de
  `package.json`): `"rayito"` y `"rayito/e2b"` declaraban un `types` hermano
  de `import`/`require`, que TypeScript elige antes que ambos, así que un
  proyecto CommonJS con `moduleResolution: node16`/`nodenext` recibía los
  `.d.mts` (ESM) para un `require` que carga el `.cjs` ("Masquerading as
  ESM" de arethetypeswrong). Ahora cada condición lleva su `types` (`import`
  → `.d.mts`, `require` → `.d.cts`), el `types` de nivel superior apunta al
  `.d.cts` de `main`, se exporta `"./package.json"` y `pnpm pack:check`
  rechaza un mapa que vuelva a la forma anterior. Mismos ficheros en tiempo
  de ejecución.

### Documentation

- Bloques "Coste y activación" completos para `mounts`/`S3Mount` y
  `size`/el catálogo de tamaños, y filas de las seis funciones 0.6 en la
  tabla de funciones opcionales; la referencia de la CLI, la paridad con
  E2B, la guía de migración, `limits.md` y las notas de 0.6.0 ya no
  describen como pendientes funciones publicadas en 0.6.0.
- **`DynamoDbIndex` enseña su bloque "Coste y activación" en el hover del
  IDE**: vivía en el comentario de módulo, que tsdown descarta; ahora está
  en el TSDoc de la clase y `check-dts-cost-blocks` lo exige.
- **Metadatos del paquete**: `package.json` declara `homepage` (el sitio de
  documentación) y `bugs`; el README del paquete termina con la licencia, el
  `NOTICE` incluido y la nota de marcas (proyecto independiente, no afiliado
  a E2B). El `NOTICE` conserva además el copyright de E2B en la atribución
  del código git adaptado (Apache-2.0 §4(c)).

## [0.6.0] - 2026-10-03

### Added

- **Convenio `OptionalStack` y `ConfigureSandbox`** (`v06-foundations`,
  M15 foundations, ADR-015/ADR-016, opcional y apagado por defecto): la
  clase `OptionalStacks` (`deploy`/`status`/`destroy`/`components`) sobre
  un catálogo de nueve componentes (`metadata-index` y `secrets-access`,
  migrados sin cambios de comportamiento; los otros siete son stubs hasta
  su propia función), con `@aws-sdk/client-cloudformation` como peer
  opcional; las siete opciones 0.6 de `Sandbox.create()` (`mounts`,
  `volumes`, `size`, `events`, `telemetry`, `gateways`, `domain`) existen
  ya en `SandboxCreateOptions` y lanzan `UnimplementedError` nombrando el
  cambio que las trae mientras sigan siendo un stub, antes de
  `run-microvm`; en cuanto una deja de serlo (`mounts`, ver más abajo),
  `create()` ejecuta sus `configureSections` justo tras el primer
  `Health` (`configure-base.ts`: `requireCapabilities`,
  `buildConfigureRequest`, `checkConfigureResponse`), dentro del mismo
  camino que ya termina el sandbox ante cualquier fallo anterior a
  `agentReady`. Sin ninguna opción nueva, el comportamiento es byte a
  byte el de 0.5.x.
<!-- m15-s3-mounts -->
- **`mounts` (`m15-s3-mounts`, ADR-017, experimental, apagado por
  defecto)**: `Sandbox.create({ mounts })` monta uno o más buckets S3
  (`new S3Mount({ bucket, prefix, readOnly: true, allowOverwrite: false,
  allowDelete: false })`, exportado desde `rayito`) en el guest con
  `mount-s3`/FUSE, sólo sobre `rayito-base-caps` (`requireCapsFor` lo
  exige antes de `run-microvm` cuando la imagen se nombra directamente;
  sobre un ARN opaco la decisión se difiere a `Health.features` tras
  `/run`, que termina el sandbox si falta la capacidad). `mounts` acepta
  un objeto literal (como `volumes`) o un `Map`. `create()` no vuelve
  hasta que cada montaje está montado: sondea `ConfigureStatus` (como
  mucho 15 s) y, si uno falla o no se asienta, termina el sandbox y lanza
  `MountError`. `sbx.mounts()`
  da el estado en vivo de cada montaje (`"pending"`/`"mounted"`/
  `"failed"`, `ConfigureStatus` en cada llamada); una sección rechazada o
  un montaje fallido lanza `MountError` con un `code` cerrado (`network`,
  `iam_denied`, `not_found`, `not_allowed`, `invalid_path`,
  `helper_missing`, `timeout`). `rayd` lanza `mount-s3` como el usuario
  dedicado `rayito-mount` (uid 990) con credenciales resueltas por su
  propio acceso a IMDS, nunca en argv ni en entorno; un daemon caído se
  relanza solo, con backoff. Política IAM `RayitoS3MountAccess` del
  componente `OptionalStack` `s3-mounts` (`infra/s3-mounts.yaml`, pide
  `CAPABILITY_IAM`; su plantilla ya va empaquetada en el SDK), acotada al
  bucket y a sus prefijos (hasta 4) también para leer, escribir y borrar
  objetos. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la
  imagen.
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
- **Catálogo de tamaños** (`m15-sizes-catalog`, ADR-019, opcional y
  apagado por defecto): `Sandbox.create({ size: "4gb" })`/
  `{ memoryMib: ... }` resuelve, enteramente en cliente y sin ningún RPC,
  al primer tamaño del catálogo cerrado (512mb/1gb/2gb/4gb/8gb, Q87) que
  cubra lo pedido (redondea siempre hacia arriba, avisa con
  `process.emitWarning(..., { type: "RayitoCompatWarning" })` si no encaja
  exacto) y antepone el sufijo de imagen (`rayito-base-4gb`) antes de
  resolver el ARN; `size` con un template dado por ARN, o por encima del
  máximo publicado, es `InvalidArgumentError` antes de cualquier llamada a
  AWS. `getInfo()` confirma, con una única llamada cacheada a
  `GetMicrovmImageVersion` por versión de imagen, `SandboxInfo.baselineMemoryMib`
  y `baselineCpu` (vCPU medido exactamente para los cinco tamaños del
  catálogo); `cpuCount`/`memoryMb` siguen siendo lo que el guest reporta de
  verdad. El shim `rayito/e2b` reporta ese mismo baseline en
  `SandboxInfo.cpuCount`/`memoryMB` cuando `size` se usó (como E2B reporta
  lo declarado por el template), la vista real del guest si no.
  Guardarraíles de coste opcional `sizes-guard` (`RayitoRunAllowedSizes`:
  un Deny de `lambda:RunMicrovm` fuera de los ARN de imagen permitidos,
  efectivo aunque la identidad ya tenga el `microvm-image:*` de la
  `CallerPolicy` estándar; `rayito image publish --sizes`/`--env` y
  `rayito image sizes` son sólo CLI Python). Sin `size`, el comportamiento
  sigue siendo exactamente el de 0.5.x.
<!-- m15-events-webhooks -->
- **`LifecycleEvents`** (`m15-events-webhooks`, opcional y apagado por
  defecto): despliega `infra/events-webhooks.yaml` (`deploy`/`status`/
  `destroy`, componente `events-webhooks` de `OptionalStacks`), registra
  webhooks compatibles con E2B (`registerWebhook`/`listWebhooks`/
  `deleteWebhook`, paginado) y lee el historial (`getEvents`, 1–100 filas,
  filtrado por tipo en DynamoDB), con `@aws-sdk/client-dynamodb` y
  `@aws-sdk/client-secrets-manager` como peers opcionales. Los errores de
  AWS llegan como `WebhookError` con sólo el código (`awsCode`). `events` en
  `Sandbox.create()` valida el tipo y que `logging` llegue a CloudWatch
  antes de lanzar y, tras `run-microvm`, manda la clave del sandbox (`k_sbx`,
  derivada de la clave del stack y el `sandboxId`; un `GetSecretValue` por
  instancia de `LifecycleEvents`) en el mismo `ConfigureSandbox` que
  `mounts`/`gateways`/`telemetry` (`LifecycleEventsSectionFactory`); sin la
  pila desplegada, sin `lifecycleEvents` en el agente o con la sección
  rechazada, termina el sandbox (salvo `keepOnFailure`). `events` pasa a
  tiparse `LifecycleEvents` en `SandboxCreateOptions`. Aceptado en AWS real (`AWS_API_NOTES.md`
  Q105–Q108); el `.gen.ts` lleva el código Lambda actualizado (comprobación
  exacta del log stream y una línea JSON por invocación del forwarder y del
  reconciliador).
<!-- m15-rayd-otlp -->
- **`telemetry`: exportación OTLP de `rayd` a CloudWatch** (`m15-rayd-otlp`,
  ADR-021, opcional y apagado por defecto): `TelemetryExport`/`OtlpAuth`
  nuevos (`OtlpAuth.executionRole()`, exige `rayito-base-caps`;
  `OtlpAuth.bearer(secretName)`, experimental, funciona en `rayito-base`;
  el nombre se resuelve bajo `rayito/` por la misma `SecretCache` que
  `secrets`). Se envía como una sección de `ConfigureSandbox` tras `/run`;
  una imagen cuyo `rayd` no exporta termina el sandbox (salvo
  `keepOnFailure`) y lanza `UnimplementedError`. Con `tracerProvider`, cada
  RPC del handle lleva además el `traceparent` del span de esa misma
  llamada hacia `rayd`; sin él, ninguna cabecera nueva. El token de
  `OtlpAuth.bearer(...)` es una API key de CloudWatch Metrics. `sbx.getTelemetryStatus()`
  lee `ConfigureStatus`, como una llamada explícita aparte de `getHealth()`.
  Sin `telemetry`, ningún comportamiento cambia frente a 0.5.x.
<!-- m15-templates -->
- **Templates declarativos** (`m15-templates`, ADR-022, opcional y apagado
  por defecto): `Template` compila un DSL (igual al `Template` de E2B v2)
  a un Dockerfile y un zip deterministas sobre una imagen `rayito-base`/
  `rayito-base-caps` ya publicada; `Template.build()`/`buildInBackground()`/
  `getBuildStatus()`/`exists()` suben el artefacto por hash de contenido y
  llaman a `create`/`update-microvm-image`, reutilizando una versión
  idéntica en vez de reconstruir. Un build fallido se explica con
  `BuildError` (`step`/`command`/`exitCode`/`logTail` del log de BuildKit,
  o `reason: "ready_client_error"|"ready_server_error"` si falló el
  `readyCmd`), sin repetir nada. `fromImage`/`fromTemplate`/
  `fromDockerfile`/`fromGcpRegistry`/`aptInstall` lanzan
  `UnimplementedError` (documentados en ADR-022). El shim
  `rayito/e2b`'s `Template` ya construye de verdad, con la firma de E2B
  (`{ alias, skipCache, memoryMB, cpuCount }`) y `new E2B({ bucket })`;
  `BuildError`/`TemplateError` del shim pasan a ser alias de las clases
  nativas (igual patrón que `NotEnoughSpaceError`/`FileUploadError`).
  `setStartCmd()` necesita una imagen base con `rayd` 0.6. La imagen
  compuesta hereda la configuración de la base
  (`additionalOsCapabilities` incluida); `skipCache()` equivale a
  `force: true`; la cuota de builds de AWS llega como
  `reason: "build_quota"`. El núcleo de build vive en
  `src/images/gateway.ts`. Sin llamar a
  `Template.build()`, el SDK no importa estáticamente
  `@aws-sdk/client-s3` ni carga `@aws-sdk/client-cloudwatch-logs` (peer
  opcional nuevo, sólo para explicar un build fallido). `infra/templates.yaml`
  (`rayito stack deploy templates`): sólo la política IAM
  `RayitoTemplateBuilder`, $0 en reposo, que no puede sobrescribir las
  imágenes base publicadas. Aceptación en AWS real: `pipInstall()` compila
  a `python3 -m pip install --no-cache-dir --break-system-packages`
  (rayito-base no tiene `pip` en el `PATH`); la versión gestionada que se
  hereda se envía como `1`, no como el eco `1.0` que `create-microvm-image`
  rechaza; la política concede `CreateMicrovmImage` sobre `*` (AWS no la
  autoriza por ARN) y `lambda:PassNetworkConnector` sobre los conectores
  gestionados, y su `Deny` cubre `UpdateMicrovmImage` sobre las bases
  (`AWS_API_NOTES.md` Q114-Q116).
<!-- m15-secrets-gateway -->
- **`gateways` — pasarela de secretos en loopback** (`m15-secrets-gateway`,
  M15, ADR-023, opcional y apagado por defecto): `Sandbox.create({ gateways:
  { nombre: new SecretGateway({ upstream, headers, allow, ... }) } })` abre,
  dentro del agente, un listener de loopback por ruta que reenvía sólo lo
  que su `allow` cubre, dentro de su límite de peticiones por minuto,
  inyectando cada cabecera vaultada (resuelta con la misma `SecretCache`
  que `secrets`, nunca antes de `Configure`) y eliminando primero
  cualquier cabecera del mismo nombre que el sandbox intente poner.
  `sbx.gateways.get("nombre")?.url` da la URL de loopback; `refresh()`
  rota el secreto sin recrear el sandbox (relee Secrets Manager aunque la
  `SecretCache` no haya vencido, conserva el puerto y lanza si `rayd`
  rechaza la sección). También `pool.take({ gateways })`. Cualquier fallo
  al configurarla tras `run-microvm` (imagen anterior a 0.6.0, flag
  ausente, secreto que falta, sección rechazada) termina el VM salvo
  `keepOnFailure`. Sin `gateways`, ningún cliente Secrets Manager nuevo se
  construye y no se manda ningún `ConfigureSandbox`.
<!-- m15-custom-domain -->

### Fixed

- `OptionalStacks.deploy("events-webhooks", { artifactBucket })` ya no exige
  repetir `parameters.ArtifactBucket`: `StackArtifact.bucketParameterKey`
  pasa a la plantilla el mismo bucket al que se sube el código, y un valor
  distinto es `InvalidArgumentError` antes de subir nada (aceptación 0.6 en
  AWS real).

## [0.5.1] - 2026-10-01

### Fixed

- Los mensajes de error y los docstrings del SDK nombran la versión de la
  imagen o del SDK que hace falta (por ejemplo, "necesita una imagen 0.3.0 o
  posterior"), en lugar del hito interno (M9, M12…).

### Documentation

- Sitio de documentación reorganizado por tareas: primeros pasos, guías por
  función con ejemplos en Python y TypeScript, funciones opcionales con su
  coste, migración desde E2B y referencia (Python, TypeScript, CLI, errores
  y variables de entorno). Los ejemplos se comprueban en CI.

## [0.5.0] - 2026-10-01

### Added

- **Spans OpenTelemetry del lado del SDK** (`m13-otel-sdk`, opcional y
  apagado por defecto, ADR-014): `tracerProvider` en `Sandbox.create()`
  (también con `pool`), `connect()` (ambas formas) y las variantes
  estáticas `kill`/`pause`/`resume`; instrumenta `commands.run`, `runCode`,
  `files.*` y `kill`/`pause`/`resume` de instancia con spans `rayito.*` de
  `SpanKind.CLIENT`. `@opentelemetry/api` es una peerDependency **opcional**
  importada sólo con `import type` (se borra en el build: ningún
  require/import en tiempo de ejecución, comprobado por `pack:check`), y
  `SpanKind.CLIENT`/`SpanStatusCode.ERROR` son constantes numéricas locales.
  Lista cerrada de atributos (nunca texto de comandos, código, rutas,
  `envs`, secretos ni metadata); un error del span lleva el nombre de la
  clase del error, nunca su `message`. $0 de AWS; el coste (si lo hay) es el
  del backend de exportación del llamante. Fuera de alcance: propagación
  `traceparent`/`tracestate` hacia `rayd`, telemetría del sandbox y el shim
  de E2B.

- **Índice de metadatos sobre DynamoDB** (`m14-metadata-index`, opcional y
  apagado por defecto, ADR-014): `new DynamoDbIndex({ tableName, region,
  credentials, onWriteFailure: "terminate", ttlMarginSeconds: 3600 })` y la
  opción `index` en `Sandbox.create()`, `Sandbox.list()`,
  `Sandbox.paginate()` y `PoolConfig`. `create()` escribe una fila inmutable
  (`PutItemCommand` condicional) tras `run-microvm`; con `metadata` e
  `index`, `list()`/`paginate()` unen `list-microvms` con
  `BatchGetItemCommand` por página y filtran también sandboxes `SUSPENDED`
  sin ninguna sonda. Nuevos errores `SandboxIndexError` e `IndexWriteError`.
  `@aws-sdk/client-dynamodb` es una peerDependency **opcional** que sólo se
  carga con `loadOptionalPeer` al usar el índice. Coste: ~1 WRU por sandbox
  creado y 0,5 RRU por candidato listado. `reincarnate()` conserva `index`:
  el sucesor escribe su propia fila.
- **Shim de E2B**: `Sandbox.list({ query: { metadata, state: ["paused"] },
  index })` y `new E2B({ index })`.
- **Secretos sobre AWS Secrets Manager** (`m13-secrets`, opcional y
  apagado por defecto, ADR-014): `new SecretStore({ region, credentials,
  prefix, kmsKeyId })` con `create`/`update`/`getInfo`/`exists`/`list`/
  `destroy`, y `new SecretCache({ ttlSeconds: 300 })` (una promesa en vuelo
  por clave; los aciertos no llaman a AWS; `refresh()`/`invalidate()`;
  `toJSON`/`inspect` nunca muestran valores). `@aws-sdk/client-secrets-manager`
  es una peerDependency **opcional** que sólo se carga con `loadOptionalPeer`
  al usar la función. Coste: $0,40/secreto-mes hasta `destroy` + $0,05/10 000
  llamadas.
- **`secrets` / `secretCache`** en `Sandbox.create()` (también con `pool`),
  `Sandbox.connect()`, `sbx.connect()`, `pool.take()` y, por llamada, en
  `commands.run`, `pty.create`, `runCode` (contextos Python) y
  `createCodeContext`: entregan el valor como variable de entorno por los
  `envs` que ya viajan a `rayd`, nunca en el `runHookPayload`, `metadata`,
  logs ni errores. La primera vez emiten un `RayitoCompatWarning`
  (`process.emitWarning`): el valor es visible para el código del sandbox.
- `SecretError`/`SecretNotFoundError` en `rayito` y en `rayito/e2b`.
- **Shim de E2B**: `Secret` (estáticos `create`/`update`/`getInfo`/`list`/
  `exists`/`destroy`/`fill`/`iamToken`), `SecretPaginator` y el tipo
  `SecretInfo` sobre `SecretStore`, con los nombres de `e2b` 2.51.0;
  `new E2B({ region }).Secret` usa esa región.

### Changed

- El motivo de `UnimplementedError` de `list(query.state=paused,
  query.metadata)` en `rayito/e2b` nombra ahora la opción `index`.
- `rayito/e2b` `Secret` y `E2B(...).Secret` ya no lanzan `UnimplementedError`.

### Fixed

- `SecretStore.destroy()` (y `Secret.destroy` de `rayito/e2b`) devolvía
  `true` para un nombre que nunca existió, porque AWS acepta el
  `DeleteSecret` forzado de un nombre inexistente sin
  `ResourceNotFoundException` (aceptación de 0.5.0 en AWS real). Ahora hace
  `DescribeSecret` antes y devuelve `false` sin borrar si no existe (o ya
  estaba programado para borrarse, o lo borró otro a la vez), como documenta
  y como E2B. Usa `secretsmanager:DescribeSecret`, ya incluido en
  `RayitoSecretsAdmin`.
- `SecretStore.create()` sobre un nombre recién borrado agotaba su
  presupuesto de reintentos de 30 s: AWS liberó el nombre tras 19–28 s y el
  backoff no llegaba. El presupuesto es ahora de 60 s
  (`CREATE_RETRY_BUDGET_MS` en `src/secrets/store.ts`) con ±25 % de
  jitter.
- `docs/site/docs/secrets.md` avisa de que un `logger` del
  `SecretsManagerClient` del SDK v3 imprime los valores (Rayito nunca lo
  hace) y de que `list()` es eventualmente consistente (~3–5 s).

## [0.4.0] - 2026-09-30

### Cambios que rompen

- `UnimplementedError` es ahora el único tipo para "algo que este sandbox no
  puede dar": un `Unimplemented` de gRPC (un RPC o un kernel de `runCode`/
  `createCodeContext` ausente en la imagen) ya no es `InvalidArgumentError`/
  `SandboxError` con `grpcCode: Code.Unimplemented`, sino `UnimplementedError`,
  fuera de esa jerarquía. `LifecycleUnsupportedError` (un plazo lógico contra
  un agente anterior a M9) pasa a ser subclase de `UnimplementedError`, no de
  `InvalidArgumentError` ni `SandboxError`; su `feature` nombra la llamada que
  pidió el plazo (`connect({ timeoutMs })` también cuando el agente responde
  `Unimplemented` al `SetTimeout` de `connect`) y su `reason` usa los mismos
  textos que el SDK Python. El `reason` de un kernel ausente es el mensaje de
  `rayd` (que ya nombra `rayito-base-poly`), sin la pista de publicar una
  imagen actual. `getMetricsHistory` contra una
  imagen anterior a M9 ya lanzaba `UnimplementedError`; sólo cambia su
  `error.cause`, de `InvalidArgumentError` a `UnimplementedError`.
  **Migración**: cambia `catch (error) { if (error instanceof
  InvalidArgumentError && error.grpcCode === Code.Unimplemented) ... }` o
  `catch (error) { if (error instanceof SandboxError) ... }` alrededor de esos
  RPCs por `error instanceof UnimplementedError` (`LifecycleUnsupportedError`
  sigue distinguiéndose con su propio `instanceof`).
- `Sandbox.probedInfo` (el `static` nativo que usaba internamente `rayito/e2b`)
  se elimina de `rayito`: vivía fuera de lugar en el núcleo nativo y su propio
  docblock ya decía que era interno del shim. **Migración**: ninguna para
  quien no lo llamaba directamente (`rayito/e2b`'s `Sandbox.getInfo` hace
  exactamente lo mismo); quien lo usara pasa a `Sandbox.getInfo(sandboxId)`
  de `rayito/e2b`.

### Changed

- Una opción de conexión de una llamada de instancia del shim de E2B
  (`sbx.kill({ retries, proxy })`, `sbx.setTimeout(ms, { headers })`...) que
  esa llamada no aplica —el canal y el plano ya están construidos— avisa
  ahora con `process.emitWarning` (`RayitoCompatWarning`, nombrando sólo la
  opción, nunca su valor) en vez de perderse en silencio; pásala al crear o
  conectar el sandbox, o usa la variante estática (`Sandbox.<método>(sandboxId,
  ...)`), que sí la aplica. Sin cambio de comportamiento, sólo avisos nuevos.

## [0.3.3] - 2026-09-29

### Security

- `clients/typescript/.npmrc` nuevo (`ignore-scripts=true`): ningún `pnpm
  install` local o de CI ejecuta scripts de ciclo de vida de dependencias.
  `.github/workflows/release.yml`: el job `typescript` se parte en
  `typescript-build` (sin `environment` ni `id-token`, `pnpm install
  --frozen-lockfile --ignore-scripts`) y `typescript-publish` (con el token
  OIDC de npm, sin checkout ni `pnpm`, que verifica con `sha256sum -c` que
  el `.tgz` descargado es el que produjo `typescript-build` antes de `npm
  publish --ignore-scripts`) — cierra C-10/H-02 residuo
  (`docs/SECURITY_AUDIT.md`). Sin cambio de comportamiento en lo publicado
  ni en el Trusted Publisher de npm.

### Changed

- `sbx.files` y `sbx.native.files` del shim de E2B comparten ahora un único
  `TransferClient` (y por tanto una sola caché de soporte de transferencias
  y un solo cliente S3 por sandbox): el `Filesystem` nativo acepta un
  segundo parámetro interno `sharedWith` que reutiliza el `TransferClient`
  del `Filesystem` nativo del que el shim parte, en vez de construir uno
  nuevo sobre el mismo `SandboxCore`. Sin cambio de comportamiento para
  quien sólo use `sbx.files`; `sbx.native.files` deja de duplicar la sonda
  y los clientes S3 (`.d.ts` publicado: sólo gana un parámetro de
  constructor opcional).
- El puerto `ControlPlane` declara ahora `readonly awsClientSettings?:
  AwsClientSettings`: las credenciales y el proxy que los clientes S3 de
  las transferencias heredan del plano de control (ADR-010) ya no llegan
  por un downcast estructural (`AwsClientSettingsSource`, eliminada), sino
  por un campo explícito del puerto que cualquier decorador debe reenviar.
  `awsClientSettingsOf` sigue exportada con el mismo nombre y la misma
  firma; sin cambio de comportamiento.

### Internal

- `sandbox/core.ts` ya no depende de `sandbox/commands.ts`: la clasificación
  de fallos de stream (`STREAM_PROBE_TIMEOUT_MS`, `streamFailureError`) vive
  en el nuevo `sandbox/stream-errors.ts`, sin ciclo con `core.ts` ni con
  `commands.ts`; `commands.ts` reexporta ambos nombres, así que ningún
  import existente cambia. Sin cambio de comportamiento.
- `e2b/compat.ts` (el mapeo puro que refleja `_compat.py`) absorbe
  `historyImageError`, `lifecycleImageError`, `LIFECYCLE_IMAGE_REASON` y la
  nueva `metricsHistoryOrSnapshot`, antes en la clase IO `e2b/sandbox.ts`;
  `Sandbox.getMetrics` y `Sandbox.metricsFor` (estático) llaman a las
  versiones movidas. Sin cambio de comportamiento.

## [0.3.2] - 2026-09-29

### Changed

- `typescript` (dependencia de desarrollo) actualizado de 5.9.3 a 7.0.2 (el
  compilador nativo, `tsgo`, vía `rolldown-plugin-dts`): `pnpm lint
  typecheck build test pack:check` verdes y los `.d.mts`/`.d.cts` generados
  byte a byte idénticos a los de 5.9.3. Sin cambios de API pública.

### Tests

- Cobertura explícita de reconexión tras un `RST_STREAM(CANCEL)` del proxy
  de AWS (`Canceled` con el prefijo `http/2 stream closed`, ya distinguido
  de un `AbortSignal`/`Canceled` propio por `isStreamReset`/`isOwnAbort` en
  `transport/errors.ts`, sin cambio de comportamiento): un stream de
  comandos en vivo reconecta con `Connect(pid, from_seq)`, agota el mismo
  presupuesto de tres reintentos que un `Unavailable`, y `disconnect()`
  sigue ganando la carrera aunque el corte llegue con la forma de un reset
  reconectable justo después (`tests/unit/reconnect.test.ts`), en paridad
  con el arreglo del SDK de Python para el `CANCELLED "Stream removed"` de
  grpcio (`AWS_API_NOTES.md` #33).

## [0.3.1] - 2026-09-28

Versión de mantenimiento, sin cambios de API.

### Documentation

- Instalación del SDK con pnpm, npm, yarn o bun en el README del paquete
  (el de npmjs.com) y en el quickstart; probado en un proyecto limpio con
  ESM, CommonJS, `rayito/e2b` y `tsc --strict`.

### Changed

- Dependencias del AWS SDK v3 actualizadas a 3.1140.0 en el lockfile.

## [0.3.0] - 2026-09-24

Rayito 0.3.0 (M9, paridad con E2B 2.x). Notas completas en
`docs/RELEASE_NOTES_0.3.0.md`; todo lo de M9 exige una imagen publicada con
el `rayd` de M9.

### Cambios que rompen

- Un disco lleno es `DiskFullError` (antes `RateLimitError`): un `catch`
  que distinguía el disco lleno por `RateLimitError` deja de verlo. En
  `rayito/e2b` se exporta también como `NotEnoughSpaceError`.

### Added

- **Transferencias por URLs prefirmadas de S3** (`m9-file-transfer`,
  ADR-010): `Sandbox.create/connect({ transfer })` con un `S3Staging`
  (`bucket` DNS sin puntos, `prefix`, `region`, `maxExpiresIn`,
  `thresholdBytes`, `multipartThresholdBytes`) o las variables
  `RAYITO_TRANSFER_BUCKET`/`_PREFIX`/`_REGION` (`null` lo desactiva), y el
  getter `sandbox.transfer`. `files.uploadUrl(path, { user, expiresIn,
  maxBytes, form })` → `UploadTicket` (la URL como `toString()`, `method`,
  `headers`, `fields`, `wait({ timeoutMs })`, `status()`, `cancel()`; de un
  solo uso) y `files.downloadUrl(path, { user, expiresIn, filename })` →
  `DownloadLink` (una foto con `size` y `sha256`); `sandbox.uploadUrl` /
  `sandbox.downloadUrl(path, { user, useSignatureExpiration })` devuelven la
  URL como `string` (la forma de E2B). El SDK firma con tus credenciales
  (SigV4, host virtual regional, `requestChecksumCalculation:
  "WHEN_REQUIRED"`); `rayd` no guarda ninguna. Las URLs nunca se registran,
  serializan (`toJSON` lanza) ni imprimen.
- Con `transfer` configurado, `files.write`/`writeFiles` de lo que mide
  `>= thresholdBytes` (y de todo `ReadableStream`, subido en streaming con
  `@aws-sdk/lib-storage`) y `files.read` de un fichero así van por S3 con el
  sha256 comprobado; sin `transfer` el camino gRPC no cambia. Los cuatro
  paquetes de S3 se cargan con `import()` sólo al usarlos.
- `files.read(path, { gzip, streamIdleTimeoutMs, format: "blob" })` y
  `files.write/writeFiles(…, { gzip, metadata, useOctetStream })`; el gzip de
  escritura usa un transporte sobre la sesión HTTP/2 de los unarios (nunca
  una tercera conexión). `EntryInfo.metadata` (claves en minúsculas).
  `metadata`, `gzip` al escribir y las transferencias exigen un agente M9:
  en uno anterior lanzan `UnimplementedError` antes de enviar nada.
- Errores `TransferError` (`code`, `reason`), `FileUploadError` y
  `DiskFullError`; los códigos de `StreamError` `failed_precondition`,
  `resource_exhausted`, `unavailable` y `cancelled`.
- Los clientes S3 de las transferencias heredan del plano de control sus
  `credentials` y su `proxy` (`LambdaMicrovmsControlPlane.fromRegion(region,
  { credentials, proxy })`, también detrás de un pool), como el SDK Python
  firma con la sesión del sandbox; antes usaban la cadena por defecto y
  salían sin pasar por el proxy.

- **Plazo del servidor** (`m9-server-timeout`, ADR-011; exige una imagen M9):
  `Sandbox.create({ maxLifetimeMs, onTimeout: "kill" | "pause" })`; con
  cualquiera de los dos, `timeoutMs` es un plazo lógico que `rayd` hace
  cumplir aunque el cliente muera. `sandbox.setTimeout(timeoutMs)` y el
  estático `Sandbox.setTimeout(sandboxId, timeoutMs, { accessToken })`
  (EXACT), `sandbox.connect({ timeoutMs })` y `Sandbox.connect(id, {
  timeoutMs })` (AT_LEAST); `SandboxLifecycle` en `getHealth()`/`getInfo()`;
  `LifecycleUnsupportedError` contra una imagen anterior (el VM se termina).
- **Historial de métricas y listado** (`m9-sandbox-observability`):
  `getMetricsHistory({ start, end, maxPoints })` (instancia y estático con el
  access token; `UnimplementedError` en una imagen anterior, con el mismo
  motivo que Python y el error gRPC en `cause`), `SandboxMetrics.memCacheBytes`, `SandboxHealth.cpuCount` y
  `memoryTotalBytes`, `Sandbox.paginate({ limit, nextToken, order,
  startedAfter })` → `SandboxListPaginator` y `list({ metadata,
  startedAfter, order })`.
- **Política de egress** (`m9-egress-policy`, ADR-012): `Sandbox.create({
  network, allowInternetAccess })`, `updateNetwork()` (instancia y estático),
  `getNetwork()`, `ALL_TRAFFIC`, `EgressEnforcement` y
  `SandboxHealth.egressEnforcement`; sólo en `rayito-base-caps`, en otra
  imagen el SDK termina el VM y lanza `UnimplementedError`.
- `runCode(code, { language: "typescript" })` (alias `ts`) y `"javascript"`
  (`js`) con el kernel de Deno de `rayito-base-poly` (`m9-deno-kernels`).
- `sandbox.git` (`Git`, `GitAuthError`, `GitUpstreamError`), `extraHeaders`
  en `transport` y `proxy` en el plano de control (`m9-e2b-v2-surface`).
- **Entrada `rayito/e2b`** (`m9-e2b-v2-surface`): el shim de la API JS de
  E2B 2.x (`import { Sandbox } from "rayito/e2b"`, export por defecto
  `Sandbox`), con `E2B`, `ConnectionConfig`, estáticos `kill`/`getInfo`/
  `getFullInfo`/`isRunning`/`connect`/`pause`/`setTimeout`/`getMetrics`/
  `list`/`updateNetwork`, `getHost` síncrono, `trafficAccessToken`,
  `NotEnoughSpaceError`, `ServiceBusyError` y `UnimplementedError` explícito
  para lo que no tiene primitiva. `pack:check` exige los cuatro
  `dist/e2b.*`. Como en Python, `getMetrics` (instancia con rango y
  estático) contra una imagen anterior a M9 y `list` con `metadata` y un
  estado `paused` lanzan `UnimplementedError` con el motivo de Python; el
  `getMetrics` estático sin token es `AuthenticationError` (diferencia
  documentada en `docs/site/docs/e2b-compat.md`).

### Fixed

- **El proceso de Node ya no queda vivo hasta el deadline de un stream**: un
  script que llamaba a `runCode` tardaba ~315 s en salir tras su última
  línea (~65 s con `commands.run`). En `@connectrpc/connect` 2.x abortar un
  server-stream sin volver a leerlo no limpia el timer (con ref) de su
  `timeoutMs`, y el SDK abortaba así todo stream ya terminado (tras el
  `EndEvent`) o abandonado. Ahora, al abortar el controller de cualquier
  stream (`runCode`, `commands`, `pty`, `watchDir`, lecturas y vigilancia de
  transferencias), el SDK pide una lectura más que hace a connect soltar el
  timer; el deadline sigue imponiéndose igual mientras el stream vive.
- **Auto-resume tras una pausa por el plazo con el cliente vivo**: en modo
  `onTimeout: "pause"` con `idle.autoResume`, si este cliente suspendió el
  sandbox al vencer y la suspensión real duró menos de 2 s (el vigilante de
  `rayd` no la reconoce como congelación), el sandbox volvía `expired` y la
  siguiente llamada fallaba con `sandbox_timeout`. Ahora el SDK aplica la
  regla de E2B (`max(timeout, 300 s)`, acotada al tope menos 5 s) con un
  `SetTimeout` en el primer `sandbox_timeout` tras reanudar, como en Python.
  Ese `SetTimeout` va por la unaria que reconecta (un `Unavailable`
  transitorio se reintenta), la marca de un solo uso sólo se consume cuando
  la `resumeGeneration` ya avanzó y el `SetTimeout` respondió, y los callers
  concurrentes comparten una sola promesa de reapertura (un único
  `SetTimeout`).
- `sandbox.getInfo()` sólo relee `Health` cuando el sandbox está `RUNNING`
  **con plazo lógico gestionado** (ADR-011): antes sondeaba cualquier
  sandbox `RUNNING`, y cada `Health` es tráfico de entrada que reinicia el
  contador de idle de la plataforma, así que sondear `getInfo()` impedía la
  auto-suspensión. `metadata` y los hechos del guest salen del último
  `Health` sin RPC extra.
- La readiness de `create()`, `connect()` y `resume()` exige además un
  `Health` con `sandboxId`: el proxy deja pasar `Health` antes de que `rayd`
  reciba `/run`, y ese `Health` (kernel del snapshot sin rotar) daba por
  listo un sandbox cuya rotación de `/run` ponía `kernelReady=false` justo
  después (AWS_API_NOTES.md Q78), como en Python.
- `requestTimeoutMs` acota también la pata S3 de una escritura enrutada y de
  una lectura enrutada `bytes`/`text`/`blob` (aborta la subida o el
  `GetObject`, borra el objeto de staging y lanza `TimeoutError`), como en
  Python.
- `signal` llega a `setTimeout`, `connect`, `isRunning`, `getHealth`,
  `getMetrics`, `getMetricsHistory`, `getNetwork` y `updateNetwork` (de
  instancia y estáticos) y a los mismos métodos de `rayito/e2b`.
- `rayito/e2b`: `Sandbox.getInfo(id)` sondea un `Health` sobre un sandbox
  `RUNNING` (`endAt` es el plazo lógico, con `metadata` y `lifecycle`); un
  `httpsPorts` no vacío es `UnimplementedError` (regla QE2, como Python);
  `network`/`allowInternetAccess` siguen D10 (política leída sea cual sea su
  `enforcement`, respaldo en `INTERNET_EGRESS`) y `getInfo()` sólo absorbe
  `UnimplementedError` de `GetNetwork`. `SandboxInfo` gana `ingress`/`egress`.
- Un `nextToken` con `startedAtMs` negativo es `InvalidArgumentError`.
- `SandboxInfo` gana `metadata` (los de `create({ metadata })` leídos del
  último `Health`; `undefined` si no se leyeron), como en Python:
  `sandbox.getInfo()` los trae y, en `rayito/e2b`,
  `(await Sandbox.connect(id)).getInfo().metadata` ya no sale vacío.
- `rayito/e2b`: `runCode`/`createCodeContext` siguen el contrato de kernels
  de Python: `r`, `java` y el resto son `UnimplementedError` sin tocar el
  agente, y el `Unimplemented` de una imagen sin el kernel también (con el
  error nativo en `cause`); `python` viaja sin `language`.
- `rayito/e2b`: un `onEvent` de `files.watchDir` o un `onData` de
  `pty.create`/`pty.connect` asíncrono que rechaza se registra como aviso en
  vez de quedar como una promesa rechazada sin manejar (que termina Node).
- `UnimplementedError` acepta `{ cause }` como cuarto argumento.

### Security

- El access token ya no aparece al inspeccionar (`util.inspect`,
  `console.log`), serializar ni hacer spread de `LaunchPlan`, del núcleo del
  sandbox ni de un `SlotRecord` del pool, ni el `runHookPayload` (con los
  `envs`) de `LaunchRequest`: son propiedades no enumerables, como el
  `repr=False` de Python.
- **`JsonFilePoolBackend` escribe por un temporal exclusivo** (auditoría
  interna, fila H-04; mismo defecto que H-03 en el SDK Python): el fichero del
  pool se escribía por `<path>.tmp`, un nombre fijo que otro usuario del
  sistema podía crear o apuntar con un enlace antes que el SDK; ahora se abre
  con `open(temp, "wx", 0o600)` sobre un nombre aleatorio, se fija el modo con
  `handle.chmod()` antes de escribir y se renombra al destino. La lectura usa
  `O_NOFOLLOW`. Sin esto la promesa de `0600` de T14 no era cierta.
- **Los errores de AWS ya no exponen la petición firmada**: el `cause` de
  los errores que traduce el plano de control (`translateAwsError`) era el
  error crudo del SDK v3, cuyo `$response` arrastra la petición HTTP y los
  buffers del socket; `util.inspect`, `console.error` o el "Serialized
  Error" de vitest imprimían `authorization: AWS4-HMAC-SHA256
  Credential=ASIA…` y `x-amz-security-token`. Ahora el `cause` es un
  resumen (`sanitizeAwsError`, `src/aws/sanitize.ts`) con sólo `name`,
  `code`, el mensaje redactado, `$fault` y `$metadata.{httpStatusCode,
  requestId, extendedRequestId, attempts}`. Los errores de S3 de las
  transferencias (`translateS3Error`), que no llevaban `cause`, ganan el
  mismo resumen sin mensaje (para conservar el `requestId`). El mensaje de
  un `InvalidSignatureException`/`SignatureDoesNotMatch`, que AWS devuelve
  con la cadena canónica (y el token de sesión dentro), pierde esa parte y
  cualquier cabecera de firma, parámetro `X-Amz-*` de una URL prefirmada o
  id de clave de acceso. `statusCode`, `awsCode`, `retryAfter` y
  `quotaCode` no cambian.

## [0.2.0] - 2026-09-17

Primera versión con número de lockstep: el SDK TypeScript salta de 0.0.5 a
0.2.0 para llevar la misma `MAJOR.MINOR` que `rayito` 0.2.0 en Python y
`rayd` 0.2.0 (`docs/RELEASING.md`, plugin `linked-versions`). Aceptado
contra AWS real en M7 (`MILESTONES.md`).

### Added

- **Persistencia del `HOME` en S3** (`m7-s3-persistence`, ADR-009):
  `S3Prefix({ bucket, prefix, name, region })`, `Sandbox.create({ persist,
  persistTimeoutMs })` (exige `executionRoleArn`; con `name` restaura en
  `sandbox.lastRestore`), `Sandbox.connect(id, { persist })`,
  `sandbox.persist`, `checkpointFiles({ target, exclude, timeoutMs,
  onProgress })` → `CheckpointResult`, `restoreFiles({ source, timeoutMs,
  onProgress })` → `RestoreResult` (`NotFoundError` sin checkpoint) y
  `reincarnate({ exclude, persistTimeoutMs })`; nuevos `PersistenceError`
  (`code`), `CheckpointProgress`, `RestoreProgress`, `LaunchOptions`,
  `DEFAULT_PERSIST_TIMEOUT_MS`; `create({ pool })` rechaza `persist`.
- `runCode(code, { language })` y `createCodeContext({ language })` aceptan
  `python`, `bash` y `javascript` (alias `js`, sin distinguir mayúsculas;
  `m7-poly-kernels`): `language` selecciona el contexto por defecto de ese
  kernel (`default-bash`), creado por el agente en la primera celda, y es
  excluyente con `context` (`InvalidArgumentError`). El kernel `bash` sólo
  lo trae la variante de imagen `rayito-base-poly`; `javascript` es un
  nombre reservado que hoy ninguna imagen trae (`AWS_API_NOTES.md` Q57). Un
  kernel que la imagen no trae es `InvalidArgumentError` (`Unimplemented`)
  con `rayito-base-poly` en el mensaje; `envs` por ejecución sólo en
  contextos Python (`docs/site/docs/kernels.md`).
- **Pool de sandboxes suspendidos** (`SandboxPool`, `PoolConfig`,
  `PoolStats`, `PoolSlotInfo`, `PoolBackend`, `InMemoryPoolBackend`,
  `JsonFilePoolBackend`, `PoolClosedError`; ADR-008, `m7-suspended-pool`):
  N MicroVMs calentados con `create()`, asentados con una celda trivial,
  aparcados con `pause({ wait: true })` y entregados por `take()` con
  `resumeMicrovm` explícito y el token de la plaza (sin `getMicrovm` en la
  toma); fallback a `create()` sin plaza; relleno con backoff 1 s → 60 s
  por los token buckets compartidos; reciclado antes del muro de 8 h y
  reconciliación con `listMicrovms`; recuperación desde el backend JSON
  (`rayito.pool/1`, el mismo fichero que escribe Python); temporizadores
  sin referencia; `Sandbox.create({ pool })` como azúcar que rechaza toda
  opción de lanzamiento. Documentación en `docs/site/docs/pool.md`.
- `metadata` y `cpuTimeLimit` en `Sandbox.create()` (el mismo
  `runHookPayload` que Python: `metadata` y `limits.cpu_seconds`), y
  `sandbox.launchInfo` (la `SandboxInfo` con la que se abrió el handle).
- Un endpoint con puerto explícito (`host:puerto`) manda sobre
  `transport.port` (los `rayd` falsos por plaza de los tests).

### Changed

- Licencia MIT → Apache-2.0 (`"license": "Apache-2.0"` en `package.json`,
  `LICENSE` con el texto Apache-2.0 y `NOTICE` incluidos en el tarball;
  `pnpm pack:check` los exige).

## [0.0.5] - 2026-09-16

Primera versión candidata a publicar (**aún no publicada en npm**; el
primer `npm publish` es manual, `docs/RELEASING.md` §3). Es la misma
generación de SDK que `rayito` 0.1.0 en Python: la superficie aceptada contra
AWS real en M6 (`MILESTONES.md`), en camelCase y milisegundos, sólo async.

### Added

- **Ciclo de vida**: `Sandbox.create` (`run-microvm` + JWE del proxy + sondeo
  de `Health` hasta `agentReady` y `kernelReady`), `connect`, `kill`,
  `list`, `getInfo`, `isRunning`, `getHost(port)` con las cabeceras del
  proxy, `getHealth`, `getMetrics`; `idle` (auto-suspensión a los 300 s por
  defecto); `timeoutMs` como vida máxima del MicroVM (tope 8 h);
  `await using` para terminar al salir del bloque.
- **Comandos**: `sbx.commands.run` en foreground y background, `onStdout` /
  `onStderr`, `stdin`, `list`, `kill`, `connect(pid, { fromSeq })`,
  `sendStdin`, `closeStdin`; `CommandHandle` con `wait`, `kill`,
  `disconnect`, iteración `for await` y reconexión.
- **Ficheros**: `sbx.files.read` (`text` / `bytes` / `stream`), `write`,
  `writeFiles` en un solo stream, `list({ depth })`, `exists`, `getInfo`,
  `remove`, `rename`, `makeDir`, `watchDir` con `WatchHandle`.
- **Código**: `sbx.runCode` sobre kernels Jupyter con estado, `Execution`
  con `results` (mime bundles y charts de E2B), `logs` y `error` como dato;
  contextos (`createCodeContext`, `listCodeContexts`, `removeCodeContext`,
  `restartCodeContext`).
- **PTY y suspend/resume**: `sbx.pty` (`create`, `connect`, `sendInput`,
  `resize`, `kill`; `PtyHandle`), `pause()` / `resume()` con procesos, PTYs,
  watches y kernels vivos al otro lado, el contrato de reconexión
  (`Connect(fromSeq)`, `Pty.Connect`, `WatchDir`, `Reattach`) y
  `reconnectTimeoutMs`.
- **Red y rol**: `executionRoleArn` (ninguno por defecto), `ingress` /
  `egress` con nombres gestionados (`ALL_INGRESS`, `INTERNET_EGRESS`, ...) o
  ARNs de conectores propios; `getHealth()` expone `agentVersion`,
  `resumeGeneration`, `clockOffsetMs` y `kernelStateLost`.
- Empaquetado: ESM + CJS con `tsdown`, tipos `.d.mts` / `.d.cts`, `exports`
  map, tarball comprobado (`scripts/pack-check.mjs`), `limits.ts` generado
  desde `limits.json` (`scripts/gen_limits.py`).

### Requisitos de imagen

- `runCode` necesita `rayito-base` ≥ 7.0 (sidecar de kernels); `pty` y la
  reconexión tras `pause()` necesitan ≥ 10.0 (M5). La aceptación de 0.0.5
  corrió sobre `rayito-base` 16.0 (`pnpm test:e2e`, 2026-09-16).

### Limitaciones conocidas

- Sólo async: no hay árbol síncrono (Node no tiene cliente gRPC bloqueante).
- Sin metadatos por sandbox (`create({ metadata })`, `list({ metadata })`),
  sin `cpuTimeLimit` ni campos de endurecimiento en `Health`: la superficie
  de M6 del SDK Python llega al SDK TypeScript en una versión posterior.
- La vida de un sandbox no se puede extender (no existe `UpdateMicrovm`);
  tope 8 h running + suspended.
- Sólo kernels Python; sin URLs firmadas; sin historial de métricas.

## [0.0.1] - [0.0.4]

Builds internos de los hitos M1-M5, nunca publicados.

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.14.0...HEAD
[0.14.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.13.0...typescript-v0.14.0
[0.13.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.12.0...typescript-v0.13.0
[0.12.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.11.0...typescript-v0.12.0
[0.11.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.10.0...typescript-v0.11.0
[0.10.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.9.1...typescript-v0.10.0
[0.9.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.9.0...typescript-v0.9.1
[0.9.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.8.0...typescript-v0.9.0
[0.8.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.7.1...typescript-v0.8.0
[0.7.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.7.0...typescript-v0.7.1
[0.7.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.6.1...typescript-v0.7.0
[0.6.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.6.0...typescript-v0.6.1
[0.6.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.5.1...typescript-v0.6.0
[0.5.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.5.0...typescript-v0.5.1
[0.5.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.4.0...typescript-v0.5.0
[0.4.0]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.3...typescript-v0.4.0
[0.3.3]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.2...typescript-v0.3.3
[0.3.2]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.1...typescript-v0.3.2
[0.3.1]: https://github.com/alejandro-cedeno-10/rayito/compare/typescript-v0.3.0...typescript-v0.3.1
[0.3.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/typescript-v0.3.0
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.2.0.md
