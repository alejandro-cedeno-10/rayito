# Changelog

Todos los cambios notables del paquete `rayito` (SDK Python). El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Cambiado

- **`AgentTemplate` arranca antes** (`ai-agent-first-start`). Dos cambios en
  la imagen que construye, medidos en AWS real (`AWS_API_NOTES.md` Q155 y
  Q156): `kernel_warmup=False` por defecto (opción nueva; CLI
  `--kernel-warmup/--no-kernel-warmup`) escribe el marcador `slim` del
  sidecar, así que el kernel de `run_code` ya no importa numpy, pandas,
  matplotlib, scipy y scikit-learn en cada arranque, y el demonio de
  prefetch lee una vez los binarios de OpenCode y ripgrep antes del snapshot
  del build, con un `ready_cmd` que lo espera, para que el snapshot de
  memoria ya los lleve. `create()` vuelve antes (6,4 s frente a 19,1 s de
  mediana en la misma tanda) y el snapshot de memoria pesa 62 MB menos; el
  primer token mejora poco y con mucha dispersión (20,9 s frente a 23,7 s de
  mediana). Las imágenes ya construidas no cambian; en una imagen nueva, la
  primera celda de `run_code` que importe el stack científico paga la
  importación (`kernel_warmup=True` lo evita).

## [0.10.0] - 2026-10-08

### Añadido

- **Más proveedores para el modelo del agente** (`ai-agent-providers`).
  `AgentModel.provider` admite `"openai"` (Responses API, también xAI),
  `"google"` (Gemini API) y `"azure"` (Azure OpenAI v1), y hay nueve presets
  de pasarela con clave de API estática: `openai_gateway`,
  `gemini_gateway(models=)`, `azure_openai_gateway(resource=)`,
  `openrouter_gateway`, `groq_gateway`, `mistral_gateway`,
  `deepseek_gateway`, `xai_gateway` y `litellm_gateway(upstream=,
  base_path="/v1")`. OpenCode usa los proveedores nativos `openai`,
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

### Changed

- `rayito doctor` conoce la serie 0.10: la tabla de compatibilidad
  (`rayito.cli._compat.COMPATIBILITY` y `docs/site/docs/limits.md`) exige
  el `rayd` del tag `rayd-v0.10.0` para el SDK 0.10.

### Fixed

- **El timeout y `abort()` de `sbx.agent` paran también lo que lanzó la
  herramienta de shell del agente.** OpenCode y deepagents corren cada orden
  en una sesión propia (`setsid`), fuera del grupo del runtime, y el timeout
  sólo mataba ese grupo: un `sleep` o un servidor lanzado por el agente
  sobrevivía y el siguiente `run()` encontraba el agente `busy`. Cada
  ejecución arranca ahora con `kill_tree=True` y `rayd` congela y mata todo
  su árbol (también lo demonizado); el stream acaba con `reason="timeout"`
  cuando ya no queda nada. `abort()` y los límites del SDK sólo matan el
  handle: desaparece la orden `pgrep` que corría antes como segundo comando.
  `commands.run()` acepta `kill_tree=` (sync y async) para lo mismo en
  cualquier comando. Necesita el `rayd` de esta versión: con uno anterior el
  campo se ignora y el timeout y `abort()` sólo alcanzan al grupo del
  runtime; reconstruye la imagen.

- **deepagents cuenta los tokens de los modelos compatibles con OpenAI.**
  Con `"openai-compatible"`, `ChatOpenAI` no pedía el uso en streaming
  (`stream_options.include_usage`): cada paso llegaba con 0 tokens,
  `result.usage` quedaba a cero y `AgentLimits.max_total_tokens` no cortaba nunca. El
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

- **`Sandbox.create(timeout=120)` sin `idle` ya no lanza
  `InvalidArgumentException`.** Desde 0.3.0, cualquier `timeout` (o
  `max_lifetime` con `on_timeout`) de 300 s o menos fallaba porque el SDK
  validaba su propia `IdlePolicy` por defecto contra él. Ahora, sin `idle`
  explícito, la auto-suspensión por defecto se desactiva si no cabe (el
  sandbox termina en su `timeout`, como en E2B) y, con `on_timeout="pause"`,
  baja a 60 s (`PAUSE_DEFAULT_IDLE_FALLBACK_SECONDS`). Un `idle` que pasas tú
  se valida igual que antes. Igual en `Sandbox` y `AsyncSandbox`.

### Documentation

- Los e2e de `s3-mounts` y `rayd-otlp` documentan como precondición que la
  política gestionada de su pila (`PolicyArn`) esté adjunta al rol de
  ejecución; `rayito stack deploy` sólo la crea.

## [0.9.0] - 2026-10-07

### Cambios que rompen

- **`rayito.e2b.BuildException` y `rayito.e2b.TemplateException` son las
  clases nativas.** Antes eran clases propias del shim que nada lanzaba, así
  que `except rayito.e2b.BuildException` no atrapaba un `Template.build()`
  fallido. Ahora son `rayito.BuildException`/`rayito.TemplateException`, y
  `BuildException` hereda de `SandboxException` (en E2B hereda de
  `Exception`). Migración: `except rayito.e2b.BuildException` ahora
  atrapa los builds fallidos reales. Como la clase es un `SandboxException`,
  cambian los `isinstance`/`issubclass` que suponían lo contrario. Además
  tiene el constructor nativo (`BuildException(message, *, reason=...,
  step=..., command=..., exit_code=..., log_tail=...)`): el código que la
  lanza o la hereda debe pasar un único mensaje; `BuildException()` o
  `BuildException(a, b)` fallan con `TypeError`.

- **Se retira la opción D del arranque rápido del agente** (pool con
  `opencode serve` residente), sin periodo de obsolescencia: apenas ganaba
  unas décimas a la opción C (`AWS_API_NOTES.md` Q154) y costaba más
  memoria y más por plaza. Desaparecen `agent_pool_warmup(..., serve=)`,
  `sbx.agent.prepare(serve=)`, `attach=` de `sbx.agent.run()`/`stream()`
  (sync y async) y `RunRequest.attach`; el script de OpenCode ya no busca
  ni se engancha a un servidor (`--attach`) ni relee la vuelta de él, y
  el span `rayito.agent.run` deja de llevar `rayito.agent.attached`. El
  puerto `AgentRuntime` pierde `abort_command` (ningún runtime lo usaba ya)
  y `warmup_steps()` no lleva argumentos; `OPENCODE_SERVE_PORT` desaparece.
  **Migración**: quita `serve=True` y `attach=`; para un primer mensaje
  rápido usa `agent_pool_warmup(runtime)` (opción C) o el arranque normal
  (`Sandbox.create(...)` y `sbx.agent.run(...)`). Un runtime propio borra
  `abort_command` y el parámetro `serve` de `warmup_steps`.
- **Se elimina `GatewayException`** (no estará en 0.9.0). Nunca se lanzaba:
  lo que la pasarela de secretos rechaza por petición llega al proceso del
  sandbox como respuesta HTTP (403, 429, 502, 504), y una configuración que
  `rayd` rechaza es `SandboxException`. Migración: quita el import y
  cualquier `except GatewayException`; si capturabas fallos de la pasarela
  al crear o refrescar, captura `SandboxException`.
- **`AWS_REGION` gana a `AWS_DEFAULT_REGION` y a la región del perfil** en el
  SDK de Python (ver "Fixed"). Si exportas `AWS_REGION` con un valor distinto
  de `AWS_DEFAULT_REGION` o de la `region` de tu perfil, el SDK pasa a operar
  (sandboxes, secretos, índice, EFS…) en la región de `AWS_REGION`.
  Migración: para conservar la región anterior, quita `AWS_REGION` del
  entorno o pasa `region=` explícita.

### Fixed

- **El SDK de Python honra `AWS_REGION`**, como el de TypeScript y la CLI.
  Antes boto3 sólo leía `AWS_DEFAULT_REGION` o el perfil. Toda sesión boto3
  del SDK (plano de control, staging S3, secretos, índice DynamoDB, eventos,
  EFS, CloudFormation…) resuelve la región en un único sitio con la
  prioridad `region=` > sesión del llamante > `AWS_REGION` >
  `AWS_DEFAULT_REGION` > perfil; una sesión del llamante sin región sigue
  esa misma cadena. Quien sólo exporta `AWS_DEFAULT_REGION` no nota
  cambios.

- **`Sandbox.connect()` y `AsyncSandbox.connect()` recuperan `sbx.gateways`**: otro proceso ya puede usar
  `sbx.agent` sobre un sandbox creado con `gateways=` sin recrearlo.
  `connect()` lee una vez el `ConfigureStatus` de `rayd` (nombre, puerto y
  último error de cada ruta; nunca el upstream ni las cabeceras) si el
  agente anuncia `secret_gateway` y el handle no aplicó `gateways=` él
  mismo. En un handle recuperado, `refresh()` sólo relee el estado y cada
  `connect()` vuelve a leerlo con el timeout de petición de esa llamada.
  Rotar la clave (`refresh()` o `reincarnate()`) sigue siendo cosa del
  handle que llamó a `create(gateways=)`: en uno recuperado, `reincarnate()`
  lanza. Si `rayd` rechaza el token en esa lectura, `connect()` no falla:
  se salta la recuperación y el `AuthenticationException` sale en la
  primera llamada autenticada, como hasta ahora.

### Changed

- `rayito doctor` conoce la serie 0.9: la tabla de compatibilidad
  (`rayito.cli._compat.COMPATIBILITY` y `docs/site/docs/limits.md`) exige
  el `rayd` del tag `rayd-v0.9.0` para el SDK 0.9.
- **Docstrings, ayuda de la CLI y mensajes alineados con la documentación**
  (barrido de docs de 0.8): las excepciones sin docstring lo tienen, los
  códigos de `MountException`, `AgentException` (`runtime_version_mismatch`
  y `output_limit`, reservados)
  dicen lo que pasa de verdad, `reincarnate()` lista todo lo que reaplica,
  `Sandbox.create(pool=)` lista los kwargs que pasan, y los errores sin
  región del índice y de `Template.build` piden `AWS_REGION`. La ayuda de
  `rayito image sizes`, `doctor`, `sandbox proxy`, `agent template build` y
  `domain` ya no nombra detalles internos.

## [0.8.0] - 2026-10-07

### Cambios que rompen

- `register_webhook` (eventos de ciclo de vida) rechaza con
  `InvalidArgumentException`, antes de cualquier llamada a AWS, una URL cuyo
  host es `localhost` (o `*.localhost`) o una IP literal no pública
  (loopback, privada, link-local, CGNAT, reservada…). Hasta 0.7.1 se
  guardaba y el deliverer la descartaba en cada entrega, así que nunca se
  entregó nada. Migración: registra una URL HTTPS con un nombre público
  (detalle en "Security").

### Added

- **Agente de IA, adaptador de deepagents** (`ai-agent-deepagents`):
  `runtime="deepagents"` o `runtime=DeepAgents(entrypoint="pkg.mod:build")`
  corre un grafo de deepagents/LangGraph con el mismo contrato que OpenCode
  (eventos cerrados, límites del SDK, credenciales sólo por la pasarela).
  Un runner que va como dato del paquete
  (`rayito/_agent/_runner/deepagents_runner.py`) habla el protocolo JSONL
  v1 de Rayito por un descriptor privado (un `print` del usuario no lo
  rompe), construye el modelo contra la pasarela con un marcador como
  clave, aplica los permisos con un middleware, guarda las sesiones
  `rda_…` dentro del sandbox y emite `TextDelta`. `mcp`, `raw_config` y
  `attach=True` fallan con `InvalidArgumentException` antes de cualquier
  RPC. `make agent-runner-test` lo prueba con el venv real en arm64.
- **Agente de IA, arranque rápido** (`ai-agent-fast-start`):
  `AgentTemplate`/`AsyncAgentTemplate` y `rayito agent template build`
  construyen la imagen del agente con OpenCode y ripgrep fijados por
  sha256 y el venv de deepagents con `--require-hashes` (sin
  `--no-deepagents`), todo de root, el manifiesto `rayito.agent-template/1`
  y, salvo `prefetch=False`, un demonio que precarga los binarios tras cada
  restauración del snapshot. `PoolConfig` gana `warmup` (pasos que cada
  plaza corre antes de aparcarse; un paso fallido es un calentamiento
  fallido), `allow_internet_access` y `network`; `agent_pool_warmup()` da
  los pasos del agente y, con `serve=True`, deja un `opencode serve`
  residente con contraseña propia de cada VM al que `agent.run` se
  engancha tras el `take()`. Coste sólo si construyes la plantilla o pasas
  `warmup` (bloques "Coste y activación").
- **Agente de IA, adaptador de OpenCode** (`ai-agent-core`): `runtime="opencode"`
  ya resuelve a un adaptador (`rayito._agent._opencode`) que escribe `opencode.json` sin
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
  tokens por defecto); los eventos (`TextDelta`, `Text`, `Reasoning`,
  `ToolCall`, `StepStarted`, `StepFinished`, `AgentFailed`, `Done`),
  `TokenUsage` y `AgentResult`; `AgentException` con un `reason` de una
  lista cerrada y un mensaje fijo que nunca lleva texto del proveedor; y
  las pasarelas ya hechas `bedrock_gateway`, `anthropic_gateway` y
  `openai_compatible_gateway`, cuyo `allow` cubre sólo los modelos
  elegidos. `sbx.agent` llega en un cambio posterior.
- **Agente de IA, API pública** (`ai-agent-core`, design.md §4 y §7):
  `sbx.agent.run()`/`.stream()`/`.prepare()`, síncronos y `async`. `run()`
  corre el agente hasta el final y lanza `AgentException` si falla;
  `stream()` devuelve un `AgentStream` iterable (gestor de contexto) que
  nunca lanza por un fallo del agente —el último evento es `Done` o
  `AgentFailed`— y expone `.abort()`, `.session_id` y `.result()`;
  `prepare()` dispara los pasos de calentamiento del runtime en segundo
  plano y vuelve enseguida. Aplica la configuración del runtime con
  `files.write_files` (una sola vez por sha de configuración), lanza el
  script del runtime con `commands.run(background=True, stdin=True)` y
  trocea su stdout en eventos; `AgentLimits.max_steps` y
  `max_total_tokens` los impone el propio SDK sobre esos eventos, no el
  runtime. `sbx.agent` es una propiedad perezosa: tocarla no manda ningún
  RPC. Con `tracer_provider=`, cada ejecución abre un span
  `rayito.agent.run` con atributos `gen_ai.*` y `rayito.agent.*` (nunca el
  prompt, el texto de la respuesta ni argumentos de herramienta). Un
  `AgentRuntime` concreto (el adaptador de OpenCode) llega en un cambio
  posterior; mientras tanto, `runtime=` admite cualquier objeto que
  implemente el `Protocol` `AgentRuntime`.

### Fixed

- `sbx.agent` con un `opencode serve` residente (`agent_pool_warmup(serve=True)`,
  opción D): `opencode run --attach` (1.18.34, igual en 1.18.35) sale en
  cuanto el servidor contesta el prompt, sin esperar a sus propios eventos,
  y el SDK veía `AgentFailed(reason="protocol_error")` o un `Done` sin texto
  aunque el servidor completaba la respuesta. Ahora el script crea la sesión
  en el servidor, y al salir `run` relee de él los mensajes de esa vuelta y
  emite las partes que faltaban (sin duplicar las ya emitidas), incluido el
  error del modelo. Un `session_id` que no tiene forma de id de OpenCode es
  `InvalidArgumentException`.
- `AgentTemplate` con `prefetch`: el demonio de precarga leía los binarios
  nada más restaurar el snapshot y su E/S competía con el arranque, así que
  `create()` tardaba ≈ 8 s más (Q146). Ahora espera a que el guest lleve
  1 s sin E/S en curso (como mucho 60 s) antes de leer.
- `AgentTemplate`: instala el runner de deepagents en
  `/opt/agents/rayito/deepagents_runner.py` (root, 0755) y pone su sha256 en
  `runner_sha256` del manifiesto. Sin él, `runtime="deepagents"` fallaba
  siempre en una imagen construida con `AgentTemplate` (aceptación en AWS,
  `AWS_API_NOTES.md` Q150).
- `sbx.agent`: `abort()` y los límites `max_steps`/`max_total_tokens`
  paran también lo que lanzó la herramienta de shell del agente (OpenCode y
  deepagents la corren en una sesión propia, fuera del grupo que mata
  `rayd`); antes un `sleep` o un servidor lanzado por el agente sobrevivía.
  Un límite del SDK, además, ya no deja el runtime trabajando (y gastando
  tokens) en segundo plano, y el siguiente `run()` no lo encuentra `busy`.
- `sbx.agent`: un timeout de `AgentLimits` terminaba con
  `reason="runtime_error"` en vez de `"timeout"`.
- `runtime="deepagents"`: las herramientas de ficheros escribían una ruta
  absoluta dentro del workdir (`/home/user/x` acababa en
  `/home/user/home/user/x`); ahora usan la ruta tal cual, como el shell.
- `sbx.agent.prepare()` arranca los pasos en segundo plano sin plazo: con
  `serve=True` el servidor residente moría al agotar `timeout_seconds`.
- `import rayito` vuelve a no cargar el agente de IA (`rayito._agent.*`,
  `rayito.sandbox_*.agent`) ni el adaptador `boto3` de los eventos de ciclo
  de vida (`rayito._lifecycle_events._aws`): sus nombres públicos
  (`AgentSpec`, `LifecycleEvents`…) se importan en el primer acceso
  (PEP 562), `Sandbox.agent` se construye en el primer uso y
  `PoolConfig(warmup=)` importa `WarmupStep` sólo al validarlo. Unas 20
  entradas menos en `sys.modules` y menos tiempo de import;
  `tests/unit/test_lazy_imports.py` lo vigila.

### Changed

- `rayito doctor` conoce la serie 0.8: la tabla de compatibilidad
  (`rayito.cli._compat.COMPATIBILITY` y `docs/site/docs/limits.md`) exige
  el `rayd` del tag `rayd-v0.8.0` para el SDK 0.8. Sin la fila, `doctor`
  daba FAIL en cada instalación 0.8 y `scripts/prepare_release_pr.py` se
  negaba a preparar la release.
- **Montajes S3**: el `MountException` de un bucket que no está en el allowlist de
  la imagen (`code="not_allowed"`) ya no dice sólo que la sección se rechazó: explica
  que falta en `RAYITO_ALLOWED_MOUNT_BUCKETS` y cómo publicar la imagen con
  él (`--env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>` o
  `make image-publish-caps MOUNT_BUCKETS=<bucket>`), sin nombrar el bucket.
  El texto vive en `MOUNT_ERROR_HINTS`, igual en los dos SDKs
  (`testdata/s3-mounts/error-hints.json`).

### Security

- **Eventos de ciclo de vida (`webhook-url-public-address`)**: `register_webhook`
  rechaza también, con `InvalidArgumentException` y antes de cualquier llamada a AWS,
  un host `localhost` (o bajo `.localhost`) y una IP literal que el guardián
  SSRF del deliverer bloquearía: loopback, privada, link-local (incluida la
  de metadatos), multicast, reservada, sin especificar o CGNAT, también como
  IPv6 con una IPv4 dentro (`::ffff:127.0.0.1`) o en las formas numéricas
  de IPv4 (`0x7f.1`, `2130706433`). Antes se guardaba y el deliverer la
  descartaba en cada entrega. `is_blocked_webhook_address` aplica la misma regla que
  el deliverer y los tres pasan `testdata/lifecycle-events/ssrf-address-vectors.json`.
  Un nombre DNS sigue sin resolverse al registrarlo: el DNS rebinding lo
  para el deliverer.

## [0.7.1] - 2026-10-06

### Changed

- `rayito image zip` (y `scripts/image_zip.py`) se niega a empaquetar un
  directorio de imagen que lleva `rayd` sin sus avisos de licencia en
  `licenses/` (`LICENSE`, `NOTICE` y `THIRD_PARTY_LICENSES.md`, que deja
  `make image-licenses`): `image/Dockerfile` los copia a
  `/usr/share/doc/rayd/`, y sin ellos la build de la imagen fallaría en AWS
  (`third-party-licenses`). Un directorio sin `rayd` no cambia.

## [0.7.0] - 2026-10-05

### Added

- **Dominio propio** (`m15-custom-domain`, ADR-024, **experimental**,
  apagado por defecto): `CustomDomain`/`AsyncCustomDomain` despliegan una
  distribución CloudFront, una CloudFront Function de enrutado
  (`cloudfront-js-2.0`) y un KeyValueStore (`infra/custom-domain.yaml`,
  `rayito domain deploy|status|destroy`), y `register`/`unregister`/
  `refresh`/`host_for` gestionan las rutas `{puerto}-{alias}.<tu-dominio>`
  (hostname HTTPS normal, sin las cabeceras `x-aws-proxy-*`).
  `register()` exige `traffic_token` salvo `public=True` explícito.
  `deploy(alternate_domain_names=[...])` (`--alternate-domain-name`)
  sustituye el alias comodín por hostnames exactos. Nuevo extra
  `rayito[custom-domain]` (`awscrt`), porque el plano de datos del
  KeyValueStore firma con SigV4A. Sin instanciar `CustomDomain` no se crea
  ningún cliente de CloudFront ni de CloudFormation.
  `Sandbox.create(domain=)` sigue lanzando `UnimplementedError`: la
  integración con `get_host()`/`expose()` llega en un cambio posterior.
  **Experimental** porque no se ha verificado de punta a punta en AWS real:
  la Function compila y enruta en el runtime real (`TestFunction`), y la
  pila crea el KeyValueStore y la Function, pero la cuenta de pruebas
  deniega `cloudfront:CreateDistribution` por una SCP, así que nunca se ha
  servido tráfico por una distribución desplegada con esta plantilla
  (`AWS_API_NOTES.md` Q121, Q140, Q141). La API puede cambiar en una minor.
- `rayito image zip --with-efs` y `rayito image publish --with-efs`
  (`m15-efs-volumes`): publican `rayito-base-caps-efs`, la imagen caps con
  `amazon-efs-utils` que necesita `volumes=` (+~198 MB de imagen, snapshot
  igual; exige `--os-capabilities ALL` y un zip con el marcador
  `efs_variant`, comprobado antes de llamar a AWS). `make image-zip-efs` /
  `make image-publish-caps-efs`. Las imágenes por defecto no cambian.
- `rayito-base-caps-efs` (y sus sufijos de tamaño) cuenta como variante caps
  para `mounts=`, `volumes=` y `telemetry=` (también en TypeScript).
- **Volúmenes EFS** (`m15-efs-volumes`, ADR-018, **experimental**, apagado
  por defecto): `VolumeStore`/`AsyncVolumeStore` (CRUD real de access
  points EFS: `CreateAccessPoint`/`DescribeAccessPoints`/
  `DeleteAccessPoint`, sin cliente `efs` hasta el primer uso) y
  `Sandbox.create(volumes=)`, que valida la petición (tipos, rutas,
  variante `caps` y un único conector propio en `egress=`: sin él, con
  `INTERNET_EGRESS` o con dos conectores es `InvalidArgumentException`,
  porque un MicroVM sólo admite un conector de egress, `AWS_API_NOTES.md`
  §16 Q131), como mucho 4 volúmenes y `execution_role_arn=` antes de
  cualquier llamada a AWS; resuelve la IP de mount target que falte con una
  `DescribeMountTargets` por sistema de ficheros antes de `run-microvm`,
  manda la sección `efs_volumes` en el único `ConfigureSandbox` de
  `create()` (con plazo de 65 s), sólo vuelve con todos los volúmenes
  `mounted` y, si uno falla, termina el sandbox (salvo `keep_on_failure`)
  y lanza `VolumeMountException` (`code`: `network`, `iam_denied`,
  `not_found`, `tls`, `helper_missing`, `timeout`, `invalid_path`); sobre
  una imagen sin `amazon-efs-utils`, `UnimplementedError`.
  `reincarnate()` la vuelve a mandar. `sbx.volumes` (`await sbx.volumes()`
  en `AsyncSandbox`) da el estado en vivo. `EfsVolume` (valida
  `mount_target_ip` como IPv4), `VolumeStatus` y las excepciones
  `VolumeException`/`VolumeMountException`/`VolumeNotFoundException`/
  `VolumePathNotFoundException`. El shim de E2B
  (`rayito.e2b.Volume`/`AsyncVolume`) hace CRUD real sobre
  `E2B(volume_store=...)` (`volume_id` es el nombre del volumen, el mismo
  que reciben `connect`/`get_info`/`destroy`); sus operaciones de contenido
  (`UnimplementedError("volume.content")`) siguen sin plano de datos, y
  `Sandbox.create(volume_mounts=)` monta con
  `E2B(volume_store=..., volume_connector_arn=...)`: ese sandbox sale sólo
  por el conector del volumen y `allow_internet_access=True` explícito es
  `InvalidArgumentException`. Componente
  `rayito stack {deploy,status,destroy} efs-volumes`
  (`infra/efs-volumes.yaml`: sistema de ficheros EFS cifrado, un mount
  target por subred de `SubnetIds`, grupos de seguridad NFS nuevos y
  conector de egress dedicado, sólo dentro de una VPC que ya existe).
  `EfsVolumes`/`AsyncEfsVolumes`: `check(vpc_id=, subnet_ids=)` comprueba
  la VPC sin crear nada (sólo `ec2:Describe*`: AZs distintas, IPs libres,
  DNS y ruta por defecto de cada subred, a un NAT o a otra puerta como un
  transit gateway; también `rayito doctor --efs-vpc-id ... --efs-subnet-ids ...`),
  `deploy()` se niega si algún hallazgo es `FAIL`, `volume_store()` da un
  `VolumeStore` sobre la pila, y `destroy(delete_file_system=True)`/
  `delete_file_system(id)` borran el sistema de ficheros conservado (sólo
  uno con la etiqueta `rayito=efs-volumes`). `AccessPointArns` acota la
  política del execution role a access points exactos y
  `deploy(read_only_access_point_arns=...)` (`ReadOnlyAccessPointArns`) le
  deniega `ClientWrite` a los de sólo lectura: la opción `ro` del montaje no
  basta, porque el usuario del sandbox alcanza el puerto local de
  `efs-proxy` (Q133). El hallazgo `internet-egress` de `check()` explica que
  un sandbox con volumen sólo tiene internet por la VPC.
  `list`/`get` son eventualmente consistentes, como `DescribeAccessPoints`
  (medido en AWS real, `AWS_API_NOTES.md` §16 Q125: hasta 11 s en listar un
  access point nuevo y 8 s en dejar de listar uno borrado): `create` de un
  nombre que ya existe reintenta `get` hasta 30 s y `destroy` de un access
  point que el listado aún mostraba pero ya no existe devuelve `False`.

### Changed

- `rayito doctor` conoce la serie 0.7: la tabla de compatibilidad
  (`rayito.cli._compat.COMPATIBILITY` y `docs/site/docs/limits.md`) exige
  el `rayd` del tag `rayd-v0.7.0` para el SDK 0.7, porque los volúmenes EFS
  y el endurecimiento del agente viven en `rayd`.

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
  la respuesta entera. `register_webhook` rechaza con
  `InvalidArgumentException` una URL que el deliverer no podría alcanzar (puerto
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
  esos secretos por `secrets=`/`SecretCache` (`InvalidArgumentException`).
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
  `EventsReaderPolicy` (`get_events`: sólo filas de eventos; ya no puede
  registrar un webhook ni leer la clave del stack) y
  `EventsWebhookAdminPolicy` (`register_webhook`/listar/borrar: sólo filas `WEBHOOK`).
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
  la cumpla lanza `InvalidArgumentException` antes de cualquier llamada,
  con los mismos vectores compartidos que `rayd`
  (`testdata/secret-gateway/request-paths.json`).
- **`rayito sandbox proxy` comprueba `Host` y `Origin`**. El proxy añade tu
  token a todo lo que reenvía, así que una web abierta en tu navegador podía
  usar el servicio del sandbox (DNS rebinding, un POST entre sitios o un
  `WebSocket` de otro origen). Ahora sólo reenvía si `Host` es de loopback
  con el puerto local, la dirección de `--bind`, `<id>.localhost` o un nuevo
  `--allowed-host` (si no, `421`), y si el `Origin`, cuando viene, es uno de
  esos orígenes o un nuevo `--allow-origin` (si no, `403`). **Cambio**:
  `--bind 0.0.0.0`/`::` exige `--allowed-host`, y un cliente que mande otro
  `Host` (p. ej. un proxy inverso) necesita `--allowed-host`. Como mucho
  `--max-connections` (8) conexiones a la vez (`503` después). Anuncia
  también `http://<id>.localhost:<puerto>`, con cookies separadas de tus
  otras apps locales.
- **`rayito sandbox proxy` reenvía un solo mensaje por conexión**: la
  primera petición, con su cuerpo delimitado por `Content-Length` o
  `chunked`; lo que el cliente mande después por esa conexión ya no llega
  al upstream sin reescribir, y un upgrade al que el sandbox no responde
  `101` se reenvía con `Connection: close` y se cierra en vez de quedar como
  túnel sin filtrar. **Cambio**: una delimitación ambigua
  (`Transfer-Encoding` junto a `Content-Length`, un `Transfer-Encoding` que
  no acaba en `chunked`, varios `Content-Length` distintos) es `400`.
- **`.dockerignore` con la semántica de Docker** en `Template.build`:
  anclado en la raíz, `**` como cero o más directorios (así `**/.env`,
  `**/.git` y el resto de los valores de `docker init` excluyen también los
  de la raíz, que antes acababan en la imagen), un directorio excluido
  excluye lo de dentro y la última coincidencia gana. **Cambio**: `*` y `?`
  ya no cruzan `/` (`*.pyc` sólo excluye los de la raíz; usa `**/*.pyc`).
  Un `UserWarning` (con las rutas, nunca el contenido) avisa si el contexto
  va a empaquetar `.env*`, `.git/`, `.aws/`, `.ssh/`, `*.pem` o `*.key`.
- **`Template.build` no sigue enlaces simbólicos** dentro de un directorio
  copiado (ni a fichero ni a directorio), como Docker y el SDK de
  TypeScript (el zip de `rayito image zip`/`publish` los rechaza, ver más
  abajo): un enlace a un fichero de tu máquina ya no
  acaba en el artefacto de S3 ni en la imagen. Cada fichero del contexto se
  vuelve a comprobar dentro del contexto y se abre con `O_NOFOLLOW` justo
  antes de leerlo.
- **Credenciales de git**: `clone`/`push`/`pull` con `username`/`password`
  restauran siempre la URL sin credenciales (también si vence la orden o la
  restauración), conservan el error de la operación y avisan por el logger
  `rayito.git`, sin la URL, si el token puede seguir en `.git/config`. Esas
  órdenes corren sin hooks ni credential helpers y se niegan
  (`GitAuthException`) si la configuración de git reescribe URLs
  (`url.*.insteadOf`). La documentación dice ahora que el token queda al
  alcance del código del sandbox: usa tokens de vida corta y de un solo
  repositorio.
- **Errores de AWS saneados en más caminos**: los stacks opcionales encadenan
  el resumen saneado y no el `ClientError` crudo; `Template.build` traduce un
  rechazo de AWS a `BuildException(reason="aws_error")` (antes subía el
  `ClientError`); el manejador de errores de la CLI, `rayito doctor` y
  `rayito prune` redactan el mensaje de AWS (un error de firma repite la
  cadena canónica con el token de sesión).
- **Salida acotada en memoria**: cada descriptor de un comando o una PTY
  guarda como mucho 64 MiB (`COMMAND_OUTPUT_MAX_BYTES`); se conserva el
  final y `CommandResult.truncated`/`CommandExitException.truncated` lo
  indican. `commands.run`/`connect` aceptan `max_output_bytes` (`0` no
  guarda nada; los callbacks reciben siempre todo) y `rayito sandbox exec`
  ya no guarda la salida que imprime.
- **La CLI neutraliza secuencias de escape**: hacia una terminal, los
  caracteres de control de logs de CloudWatch, logs de build y mensajes de
  AWS salen como `\xNN` visibles. `rayito sandbox logs` sólo usa un stream
  que no es el esperado si tiene exactamente la forma `YYYY/MM/DD[<versión>]<id>`
  y un día no anterior al arranque, y avisa cuando lo hace.
- **Access token mínimo**: un token propio (`access_token=`,
  `RAYITO_ACCESS_TOKEN`, `--token-file`) tiene que decodificar a al menos 16
  bytes (`ACCESS_TOKEN_MIN_BYTES`). **Cambio**: uno más corto, aceptado
  antes, ahora es `InvalidArgumentException`. Los generados (32 bytes) no
  cambian.
- `ProxyToken` y el contexto de `rayito doctor` ya no muestran el JWE en
  `repr`.
- **`Sandbox.create(persist=)` y `AsyncSandbox.create(persist=)` ligan el sandbox a su prefijo de persistencia** (C-07): el
  `runHookPayload` lleva el bucket y la base `prefix` del `S3Prefix` (nunca
  `prefix/name`), y un `rayd` con la corrección rechaza con
  `permission_denied` cualquier checkpoint o restore fuera de esa base. Con
  un `prefix` por inquilino, el token de un sandbox ya no alcanza el `HOME`
  persistido de otro aunque compartan execution role. Cambia un uso: un
  restore o checkpoint explícito hacia otra base desde un sandbox creado con
  `persist=` ahora falla con `permission_denied`. No añade llamadas a AWS.
- **La wheel y el sdist se hashean justo después de `uv build`**, antes de
  que `check_wheel.py` o twine ejecuten nada, y se vuelven a comprobar al
  final del job: el `sha256sum -c` del job que publica ahora prueba que lo
  publicado es lo que produjo `uv build`. twine y su grafo se instalan desde
  requisitos con `--hash` (`.github/release/requirements-twine.txt`), uv va
  fijado por versión y checksum, y la caché de uv está apagada en la release.
- **La release comprueba que `dist/` es exactamente la wheel y el sdist del
  tag** antes de subirlo y antes de publicarlo: `sha256sum -c` no detecta un
  fichero que no esté en `SHA256SUMS`, y la acción de publicación sube todo
  lo que haya en el directorio.
- **`rayito image zip` y `make image-publish` rechazan enlaces simbólicos**
  en el árbol de la imagen y en el sidecar (salvo dentro de los directorios
  que nunca viajan, como `.venv`): un enlace metía en todas las imágenes el
  contenido de un fichero de la máquina que construye, legible por el
  usuario del sandbox.

### Deprecated

- `EventsOperatorPolicy` (salida `OperatorPolicyArn` de `events-webhooks`):
  sigue siendo la unión de las tres políticas nuevas durante esta versión.
  Vincula a cada identidad la suya (`EventsLauncherPolicy`,
  `EventsReaderPolicy` o `EventsWebhookAdminPolicy`).

## [0.6.1] - 2026-10-04

### Fixed

- **Redesplegar una pila opcional ya no deshace su configuración**
  (`OptionalStacks.deploy()`/`AsyncOptionalStacks.deploy()` y
  `rayito stack deploy`): los valores por defecto del catálogo sólo se
  aplican al crear la pila; al actualizarla, cada parámetro que no vuelves a
  pasar y la pila ya tiene se manda con `UsePreviousValue`. Antes, por
  ejemplo, redesplegar `s3-mounts` sin repetir `Prefixes` volvía a `'*'`
  (todo el bucket), `metadata-index` sin `TableName` reemplazaba (y borraba)
  la tabla y `secrets-access` sin `KmsKeyArn` quitaba `kms:Decrypt`. Nuevo
  `parameter_changes()` para ver qué cambiaría sin desplegar; `rayito stack
  deploy` lo imprime antes de pedir confirmación.
- **Guardián SSRF de los webhooks** (pila `events-webhooks`): una dirección
  IPv6 que encapsula una IPv4 (`::ffff:100.64.0.1`, de un registro AAAA) se
  clasifica como esa IPv4, así que el rango CGNAT queda bloqueado también
  por esa vía. Se regenera el zip de la Lambda que el SDK sube.
- `volumes=`/`domain=` lanzan `UnimplementedError` con "todavía no
  disponible" en vez de "llega en 0.6"; `rayito domain` (pendiente) ya no
  aparece en `rayito --help`; `rayito template build|status|logs`,
  `rayito stack` y `rayito events` tienen texto de ayuda.
- **`reincarnate()` reaplica todas las secciones de `ConfigureSandbox`**
  (`m15-reincarnate-configure-replay`, sync y async): hasta ahora sólo
  reenviaba `gateways=` y un sucesor perdía `mounts=`, `events=` y
  `telemetry=`. `create()` guarda sus opciones 0.6 en
  `LaunchOptions.features` (`relaunch_features`: todas menos `size`, que ya
  va en el ARN de la imagen) y `reincarnate()` las reenvía campo a campo
  (`feature_kwargs`) al `create()` del sucesor, que las aplica por el mismo
  camino en su único `Configure`: `events=` deriva `k_sbx` del nuevo
  `sandbox_id`, `mounts=` espera otra vez a `mounted` y `telemetry=` usa la
  imagen y la memoria del sucesor. `LaunchOptions.gateways` pasa a
  `LaunchOptions.features.gateways` (atributo interno).

### Documentation

- Bloques "Coste y activación" completos para `mounts`/`S3Mount` y
  `size`/el catálogo de tamaños, y filas de las seis funciones 0.6 en la
  tabla de funciones opcionales; la referencia de la CLI, la paridad con
  E2B, la guía de migración, `limits.md` y las notas de 0.6.0 ya no
  describen como pendientes funciones publicadas en 0.6.0.
- **Metadatos del paquete**: la URL `Documentation` apunta al sitio de
  documentación y se añade `Issues`; el README del paquete termina con la
  licencia, el `NOTICE` incluido y la nota de marcas (proyecto independiente,
  no afiliado a E2B). El `NOTICE` conserva además el copyright de E2B en la
  atribución del código git adaptado (Apache-2.0 §4(c)).

## [0.6.0] - 2026-10-03

### Added

- **Convenio `OptionalStack` y `ConfigureSandbox`** (`v06-foundations`,
  M15 foundations, ADR-015/ADR-016, opcional y apagado por defecto): el
  servicio `OptionalStacks` (`deploy`/`status`/`destroy`/`components`) y la
  CLI `rayito stack` sobre un catálogo de nueve componentes
  (`metadata-index` y `secrets-access`, migrados sin cambios de
  comportamiento; los otros siete son stubs hasta su propia función); las
  siete opciones 0.6 de `Sandbox.create()` (`mounts`, `volumes`, `size`,
  `events`, `telemetry`, `gateways`, `domain`) existen ya en la firma y
  lanzan `UnimplementedError` nombrando el cambio que las trae mientras
  sigan siendo un stub, antes de `run-microvm`; en cuanto una deja de
  serlo (`mounts=`, ver más abajo), `create()`/`take()` ejecutan sus
  `configure_sections` justo tras el primer `Health` (`_configure_base`:
  `require_capabilities`, `build_configure_request`,
  `check_configure_response`), dentro del mismo camino que ya termina el
  sandbox ante cualquier fallo anterior a `agent_ready`. Fila de
  compatibilidad "0.6" en `rayito.cli._compat.COMPATIBILITY`. Sin ninguna
  opción nueva, el comportamiento es byte a byte el de 0.5.x (traza de oro en
  `tests/unit/fixtures/zero_cost_0_5_trace.json`).
<!-- m15-s3-mounts -->
- **`mounts=` (`m15-s3-mounts`, ADR-017, experimental, apagado por
  defecto)**: `Sandbox.create(mounts=)`/`AsyncSandbox.create(mounts=)`
  monta uno o más buckets S3 (`S3Mount(bucket=, prefix=, read_only=True,
  allow_overwrite=False, allow_delete=False)`, exportado desde `rayito`)
  en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`
  (`require_caps_for` lo exige antes de `run-microvm` cuando la imagen se
  nombra directamente; sobre un ARN opaco la decisión se difiere a
  `Health.features` tras `/run`, que termina el sandbox si falta la
  capacidad). `create()` no vuelve hasta que cada montaje está montado:
  sondea `ConfigureStatus` (como mucho 15 s) y, si uno falla o no se
  asienta, termina el sandbox y lanza `MountException`. `sbx.mounts`
  (propiedad) da el estado en vivo de cada montaje (`"pending"`/`"mounted"`/`"failed"`, `ConfigureStatus` en cada
  lectura); una sección rechazada o un montaje fallido lanza
  `MountException` con un `code` cerrado (`network`, `iam_denied`,
  `not_found`, `not_allowed`, `invalid_path`, `helper_missing`,
  `timeout`). `rayd` lanza `mount-s3` como el usuario dedicado
  `rayito-mount` (uid 990) con credenciales resueltas por su propio
  acceso a IMDS, nunca en argv ni en entorno; un daemon caído se
  relanza solo, con backoff. Política IAM `RayitoS3MountAccess` del
  componente `OptionalStack` `s3-mounts` (`infra/s3-mounts.yaml`,
  desplegable con `rayito stack deploy s3-mounts --param
  BucketName=... --param Prefixes=...`, pide `CAPABILITY_IAM`), acotada
  al bucket y a sus prefijos (hasta 4) también para leer, escribir y
  borrar objetos. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS`
  de la imagen (`rayito image publish --env`, de `m15-sizes-catalog`).
<!-- m15-efs-volumes -->
<!-- m15-sizes-catalog -->
- **Catálogo de tamaños, aceptación en AWS real** (`m15-sizes-catalog`):
  `rayito image publish --env`/`--sizes` vuelve a reutilizar una versión
  ya construida con la misma configuración: `list-microvm-image-versions`
  no devuelve `environmentVariables` (Q118), así que antes toda imagen con
  variables (cada imagen con sufijo de tamaño) se reconstruía en cada
  publicación; ahora se confirman con `GetMicrovmImageVersion` (gratuita),
  sólo cuando la publicación lleva variables. `rayito image sizes` acepta
  `--image-name`, como `publish`, para listar una familia publicada con
  nombre propio.
- **Catálogo de tamaños** (`m15-sizes-catalog`, ADR-019, opcional y
  apagado por defecto): `Sandbox.create(size="4gb")`/`SizeRequest(memory_mib=...)`
  resuelve, enteramente en cliente y sin ningún RPC, al primer tamaño del
  catálogo cerrado (512mb/1gb/2gb/4gb/8gb, Q87) que cubra lo pedido
  (redondea siempre hacia arriba, avisa con `RayitoCompatWarning` si no
  encaja exacto) y antepone el sufijo de imagen (`rayito-base-4gb`) antes
  de resolver el ARN; `size=` con un template dado por ARN, o por encima
  del máximo publicado, es `InvalidArgumentException` antes de cualquier
  llamada a AWS. `rayito image publish --sizes 512mb,4gb` publica, desde
  el mismo artefacto, una imagen adicional por tamaño (en oleadas de hasta
  10 construcciones simultáneas), horneando `RAYITO_BASELINE_MEMORY_MIB`
  en la imagen (nunca un interruptor de activación); `--env KEY=VALUE`
  añade cualquier otra variable de imagen. `get_info()` confirma, con una
  única llamada cacheada a `GetMicrovmImageVersion` por versión de imagen,
  `SandboxInfo.baseline_memory_mib` y `baseline_cpu` (vCPU medido
  exactamente para los cinco tamaños del catálogo); `cpu_count`/`memory_mb`
  siguen siendo lo que el guest reporta de verdad. El shim `rayito.e2b`
  reporta ese mismo baseline en `SandboxInfo.cpu_count`/`memory_mb` cuando
  `size=` se usó (como E2B reporta lo declarado por el template), la vista
  real del guest si no. `rayito image sizes [--variant]` lista, por
  tamaño del catálogo cerrado, qué imagen ya se publicó (`list-microvm-
  images`) y si comparte artefacto con el baseline (`sameArtifact`, una
  `GetMicrovmImageVersion` sin cuota propia por imagen ya publicada, nunca
  lanza ningún sandbox). Guardarraíles de coste opcional
  `rayito stack deploy sizes-guard` (`RayitoRunAllowedSizes`: un Deny de
  `lambda:RunMicrovm` fuera de los ARN de imagen permitidos, efectivo
  aunque la identidad ya tenga el `microvm-image:*` de la `CallerPolicy`
  estándar). Sin `size=`, el comportamiento sigue siendo exactamente el de
  0.5.x.
<!-- m15-events-webhooks -->
- **`LifecycleEvents`/`AsyncLifecycleEvents`** (`m15-events-webhooks`,
  opcional y apagado por defecto): despliega `infra/events-webhooks.yaml`
  (`deploy`/`status`/`destroy`, componente `events-webhooks` de
  `OptionalStacks`), registra webhooks compatibles con E2B
  (`register_webhook`/`list_webhooks`/`delete_webhook`) y lee el historial
  (`get_events`, 1–100 filas, filtrado por tipo en DynamoDB). CLI
  `rayito events deploy|status|destroy|list` y `rayito events webhook
  add|list|remove`, con el mismo bloque de coste y confirmación que
  `rayito stack`. Los errores de AWS llegan como `WebhookException` con sólo
  el código (`aws_code`). `events=` en `Sandbox.create()`/
  `AsyncSandbox.create()` valida el tipo y que `logging` llegue a CloudWatch
  antes de lanzar y, tras `run-microvm`, manda la clave del sandbox
  (`k_sbx`, derivada de la clave del stack y el `sandbox_id`; un
  `GetSecretValue` por instancia de `LifecycleEvents`) en el mismo
  `ConfigureSandbox` que `mounts=`/`gateways=`/`telemetry=`
  (`LifecycleEventsSectionFactory`, ADR-020); sin la pila desplegada, sin
  `lifecycle_events` en el agente o con la sección rechazada, termina el
  sandbox (salvo `keep_on_failure`). Sin `events=`, ni un cliente
  DynamoDB/Secrets Manager nuevo ni una llamada `ConfigureSandbox`. `rayito events deploy --tag K=V` (repetible) para
  cuentas cuya organización exige etiquetas al crear recursos. Aceptado en
  AWS real (`AWS_API_NOTES.md` Q105–Q108): el forwarder exige que el log
  stream termine en `]<sandbox_id>` (formato medido) y el forwarder y el
  reconciliador dejan una línea JSON por invocación con lo que aceptaron,
  rechazaron (por motivo) o sintetizaron.
<!-- m15-rayd-otlp -->
- **`telemetry=`: exportación OTLP de `rayd` a CloudWatch** (`m15-rayd-otlp`,
  ADR-021, opcional y apagado por defecto): `TelemetryExport`/`OtlpAuth`
  nuevos (`OtlpAuth.execution_role()`, exige `rayito-base-caps`;
  `OtlpAuth.bearer(secret_name=...)`, experimental, funciona en
  `rayito-base`; el nombre se resuelve bajo `rayito/` por la misma
  `SecretCache` que `secrets=`). Se envía como una sección de
  `ConfigureSandbox` tras `/run`; una imagen cuyo `rayd` no exporta termina
  el sandbox (salvo `keep_on_failure=True`) y lanza `UnimplementedError`.
  Con `tracer_provider=`, cada RPC del handle lleva además el `traceparent`
  del span de esa misma llamada hacia `rayd` (lo calcula un interceptor
  del canal en el hilo o la tarea que llama, no el `AuthMetadataPlugin`,
  que grpc ejecuta en un hilo suyo sin el contexto OpenTelemetry del
  llamante); sin él, ninguna cabecera nueva. El token de
  `OtlpAuth.bearer(...)` es una API key de CloudWatch Metrics.
  `sbx.get_telemetry_status()` (síncrono y `AsyncSandbox`) lee
  `ConfigureStatus`, como una llamada explícita aparte de `get_health()`.
  Sin `telemetry=`, ningún comportamiento cambia frente a 0.5.x (traza de
  oro sin tocar).
<!-- m15-templates -->
- **Templates declarativos** (`m15-templates`, ADR-022, opcional y apagado
  por defecto): `Template`/`AsyncTemplate` compilan un DSL (igual al
  `Template` de E2B v2) a un Dockerfile y un zip deterministas sobre una
  imagen `rayito-base`/`rayito-base-caps` ya publicada;
  `Template.build()`/`build_in_background()`/`get_build_status()`/
  `exists()` suben el artefacto por hash de contenido y llaman a
  `create`/`update-microvm-image`, reutilizando una versión idéntica en
  vez de reconstruir. Un build fallido se explica con `BuildException`
  (`step`/`command`/`exit_code`/`log_tail` del log de BuildKit, o
  `reason="ready_client_error"|"ready_server_error"` si falló el
  `ready_cmd`), sin repetir nada. `from_image`/`from_template`/
  `from_dockerfile`/`from_gcp_registry`/`apt_install` lanzan
  `UnimplementedError` (documentados en ADR-022). El shim
  `rayito.e2b.Template`/`AsyncTemplate` ya construye de verdad, con la
  firma de E2B (`alias`, `skip_cache`, `memory_mb` redondeado con
  `RayitoCompatWarning`, `cpu_count` con aviso) y `E2B(bucket=...)`;
  `TemplateException`/`BuildException` del shim pasan a ser las clases
  nativas. `set_start_cmd()` necesita una imagen base con `rayd` 0.6. La
  imagen compuesta hereda la configuración de la base
  (`additionalOsCapabilities` incluida); `skip_cache()` equivale a
  `force=True`; la cuota de builds de AWS llega como
  `reason="build_quota"`. El núcleo de build de imágenes pasa a
  `rayito._images`, compartido con `rayito image publish` (sin cambio de
  comportamiento). Sin llamar a `Template.build()`, el SDK no crea ningún
  cliente nuevo. `infra/templates.yaml` (`rayito stack deploy templates`):
  sólo la política IAM `RayitoTemplateBuilder`, $0 en reposo, que no puede
    sobrescribir las imágenes base publicadas. Aceptación en AWS real: `pip_install()` compila
  a `python3 -m pip install --no-cache-dir --break-system-packages`
  (rayito-base no tiene `pip` en el `PATH`); la versión gestionada que se
  hereda se envía como `1`, no como el eco `1.0` que `create-microvm-image`
  rechaza; la política concede `CreateMicrovmImage` sobre `*` (AWS no la
  autoriza por ARN) y `lambda:PassNetworkConnector` sobre los conectores
  gestionados, y su `Deny` cubre `UpdateMicrovmImage` sobre las bases
  (`AWS_API_NOTES.md` Q114-Q116).
<!-- m15-secrets-gateway -->
- **`gateways=` — pasarela de secretos en loopback** (`m15-secrets-gateway`,
  M15, ADR-023, opcional y apagado por defecto): `Sandbox.create(gateways=
  {"nombre": SecretGateway(upstream=..., headers=..., allow=..., ...)})`
  abre, dentro del agente, un listener de loopback por ruta que reenvía
  sólo lo que su `allow` cubre, dentro de su límite de peticiones por
  minuto, inyectando cada cabecera vaultada (resuelta de Secrets Manager
  con la misma `SecretCache` que `secrets=`, nunca antes de `Configure`) y
  eliminando primero cualquier cabecera del mismo nombre que el sandbox
  intente poner. `sbx.gateways["nombre"].url` da la URL de loopback;
  `refresh()`/`arefresh()` rotan el secreto sin recrear el sandbox (releen
  Secrets Manager aunque la `SecretCache` no haya vencido, conservan el
  puerto y lanzan si `rayd` rechaza la sección). También
  `SandboxPool.take(gateways=)`/`AsyncSandboxPool.take(gateways=)`.
  Cualquier fallo al configurarla tras `run-microvm` (imagen anterior a
  0.6.0, flag ausente, secreto que falta, sección rechazada) termina el VM
  salvo `keep_on_failure`. Sin `gateways=`, ningún cliente `secretsmanager`
  nuevo se construye y no se manda ningún `ConfigureSandbox`.
<!-- m15-custom-domain -->

### Fixed

- `rayito stack deploy events-webhooks --artifact-bucket B` (y
  `OptionalStacks.deploy("events-webhooks", artifact_bucket=B)`) ya no exige
  repetir `--param ArtifactBucket=B`: `StackArtifact.bucket_parameter_key`
  pasa a la plantilla el mismo bucket al que se sube el código, y un valor
  distinto es `InvalidArgumentException` antes de subir nada (aceptación 0.6
  en AWS real).

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
  apagado por defecto, ADR-014): `tracer_provider=` en `Sandbox.create()`
  (también con `pool=`), `connect()` (ambas formas) y las variantes de clase
  `kill`/`pause`/`resume`; instrumenta `commands.run`, `run_code`, `files.*`
  y `kill`/`pause`/`resume` de instancia con spans `rayito.*` de
  `SpanKind.CLIENT`. Sin `tracer_provider=` no se importa `opentelemetry`
  (extra `otel`, `pip install rayito[otel]`) ni se crea ningún span
  (`NOOP.span()` devuelve siempre el mismo `nullcontext`; sólo queda el
  diccionario de atributos trivial de cada llamada). Lista cerrada de atributos (nunca texto de comandos, código,
  rutas, `envs`, secretos ni metadata); un error del span lleva el nombre de
  la clase de la excepción, nunca su mensaje. $0 de AWS; el coste (si lo
  hay) es el del backend de exportación del llamante. Fuera de alcance:
  propagación `traceparent`/`tracestate` hacia `rayd`, telemetría del
  sandbox y el shim de E2B.

- **Índice de metadatos sobre DynamoDB** (`m14-metadata-index`, opcional y
  apagado por defecto, ADR-014): `DynamoDbIndex(table_name, region=,
  session=, on_write_failure="terminate", ttl_margin_seconds=3600)` y la
  opción `index=` en `Sandbox.create()`, `Sandbox.list()`,
  `Sandbox.paginate()` (y sus gemelos asíncronos) y `PoolConfig`. `create()`
  escribe una fila inmutable (`PutItem` condicional) tras `run-microvm`; con
  `metadata=` e `index=`, `list()`/`paginate()` unen `list-microvms` con
  `BatchGetItem` por página y filtran también sandboxes `SUSPENDED` sin
  ninguna sonda de `Health`, token ni `get-microvm`. Nuevas excepciones
  `SandboxIndexException` e `IndexWriteException`. Coste: ~1 WRU por sandbox
  creado y 0,5 RRU por candidato listado (DynamoDB on-demand); la tabla la
  despliegas tú con `infra/metadata-index.yaml`. `reincarnate()` conserva
  `index=`: el sucesor escribe su propia fila.
- **Shim de E2B**: `Sandbox.list(query=SandboxQuery(metadata=...,
  state=[PAUSED]), index=...)` y `E2B(index=...)` filtran sandboxes en pausa
  por metadatos con el índice.
- **CLI**: `rayito sandbox list --metadata K=V --state suspended
  --index-table TABLA` (y `--state` sola); `--index-table` sin `--metadata`
  es un error de uso (salida 2); sin `--index-table`, sin cambios.
- **Secretos sobre AWS Secrets Manager** (`m13-secrets`, opcional y
  apagado por defecto, ADR-014): `SecretStore(region=, session=,
  prefix="rayito/", kms_key_id=)` con `create`/`update`/`get_info`/
  `exists`/`list`/`destroy` (versión entera en `ClientRequestToken`,
  metadatos en `Description`, borrado sin ventana de recuperación, reintento
  acotado de `create` tras un borrado reciente) y `SecretCache(ttl_seconds=300)`
  (clave por región, sesión, secreto y versión; una sola lectura en vuelo por
  clave; los aciertos no llaman a AWS; `refresh()`/`invalidate()`; nunca
  muestra valores). Coste: $0,40/secreto-mes hasta `destroy` + $0,05/10 000
  llamadas.
- **`secrets=` / `secret_cache=`** en `Sandbox.create()` (también con
  `pool=`), `connect()`, `SandboxPool.take()` y, por llamada, en
  `commands.run`, `pty.create`, `run_code` (contextos Python) y
  `create_code_context`, síncrono y asíncrono: entregan el valor como
  variable de entorno por los `envs` que ya viajan a `rayd` (sin RPC nueva,
  vale con imágenes 0.4.0), nunca en el `runHookPayload`, `metadata`, logs ni
  errores. Se resuelven antes de `run-microvm`. La primera vez avisan con
  `RayitoCompatWarning`: el valor es visible para el código del sandbox.
- **Shim de E2B**: `Secret`/`AsyncSecret`, `SecretInfo`, `SecretPaginator`/
  `AsyncSecretPaginator`, `SecretException`/`SecretNotFoundException` sobre
  `SecretStore`, con los nombres de `e2b` 2.51.0; `E2B(...).Secret` usa su
  región y su sesión. `fill()` devuelve el placeholder, que nada resuelve;
  `iam_token` sigue en `UnimplementedError`.

### Changed

- El motivo de `UnimplementedError` de `list(state=PAUSED, query.metadata)`
  en el shim de E2B nombra ahora la opción `index=DynamoDbIndex(...)`.
- `RayitoCompatWarning` vive ahora en `rayito.exceptions` (y se re-exporta
  desde `rayito.e2b.exceptions`: es la misma clase).
- `rayito.e2b.Secret`/`AsyncSecret` y `E2B(...).Secret` ya no lanzan
  `UnimplementedError`: llaman a Secrets Manager cuando se usan.

- **`rayito sandbox proxy ID --port N`** (`m12-sizes-proxy`): expone un
  puerto del guest en `http://<bind>:<local-port>` (`--local-port`, por
  defecto igual a `--port`; `--bind`, por defecto `127.0.0.1`;
  `--allow-remote` para escuchar fuera de loopback) sin coste de AWS más
  allá de `GetMicrovm` + `CreateMicrovmAuthToken` (gratuitos). Reutiliza el
  `TokenRefresher`/`TokenStore` del SDK (mismo refresco a los 45 min);
  siempre acuña `PortSpec.single(N)`, nunca `allPorts`; rechaza el puerto
  9000 (hooks, ADR-006) antes de llamar a AWS. Quita cualquier cabecera
  `x-aws-proxy-*` del cliente, fija `Host`/`X-aws-proxy-auth`/
  `X-aws-proxy-port` y fuerza `Connection: close` salvo en peticiones de
  upgrade (`Upgrade` + `Connection: upgrade`; el paso de WebSocket está
  implementado, no medido contra AWS). Sin JWE vigente o sin conexión al
  sandbox responde `502` (motivo en stderr, sin JWE ni ruta); lectura de
  cabecera y conexión al sandbox con tiempo máximo de 30 s. Nunca registra el JWE, las cabeceras, los cuerpos ni las rutas.
  El parseo de la cabecera del cliente es HTTP/1.1 estricto: rechaza (`400`
  y cierra) cualquier CR/LF suelto fuera de un `\r\n` (contrabando de
  cabeceras que podía colar un `x-aws-proxy-*` falso), `obs-fold` y nombres
  fuera de los `tchar` de RFC 9110. `--port` y `--local-port` se validan y
  el socket local se reserva **antes** de `GetMicrovm`/
  `CreateMicrovmAuthToken`, así que un puerto inválido u ocupado falla
  limpio (mensaje con el puerto, salida 1, sin traceback) sin gastar
  ninguna llamada. Sin dependencias nuevas.

### Fixed

- `SecretStore.destroy()` (y `Secret.destroy`/`AsyncSecret.destroy` del shim
  de E2B) devolvía `True` para un nombre que nunca existió, porque AWS
  acepta el `DeleteSecret` forzado de un nombre inexistente sin
  `ResourceNotFoundException` (aceptación de 0.5.0 en AWS real). Ahora hace
  `DescribeSecret` antes y devuelve `False` sin borrar si no existe (o ya
  estaba programado para borrarse, o lo borró otro a la vez), como documenta
  y como E2B. Usa `secretsmanager:DescribeSecret`, ya incluido en
  `RayitoSecretsAdmin`.
- `SecretStore.create()` sobre un nombre recién borrado agotaba su
  presupuesto de reintentos de 30 s: AWS liberó el nombre tras 19–28 s y el
  backoff no llegaba. El presupuesto es ahora de 60 s
  (`CREATE_RETRY_BUDGET_SECONDS`) con ±25 % de jitter.
- El e2e de secretos (SEC-10) ponía el logger raíz a DEBUG y capturaba los
  cuerpos de botocore con el valor del secreto; ahora captura sólo el logger
  `rayito`. `docs/site/docs/secrets.md` avisa de que el DEBUG de
  botocore/urllib3 imprime los valores (Rayito nunca lo hace) y de que
  `list()` es eventualmente consistente (~3–5 s).

## [0.4.0] - 2026-09-30

### Fixed

- `rayito doctor` tiene la fila `0.4` de la tabla de compatibilidad (SDK 0.4
  exige `rayd` 0.4.0); sin ella la comprobación `compatibility` daba FAIL.

- `files.read()` de un fichero grande enrutado por S3: si el plazo
  (`request_timeout`/`stream_idle_timeout`) vencía justo cuando el
  `get_object` seguía en vuelo, el SDK seguía lanzando ese `get_object` tras
  una cancelación ya vista, aunque el resultado tardío se descartaba y el
  llamante siempre recibía `TimeoutException`. `ObjectFetch.run` comprueba
  ahora la cancelación antes de abrir el cuerpo, así que no sale ningún
  `get_object` de más una vez vista la cancelación.

### Cambios que rompen

- `LifecycleUnsupportedException` pasa a ser subclase de `UnimplementedError`
  (ya no de `InvalidArgumentException`/`SandboxException`), y el kernel
  ausente en `run_code`/`create_code_context` (y cualquier RPC que el agente
  no implemente, vía la tabla unaria genérica de `_transport.py`) deja de ser
  `InvalidArgumentException`/`SandboxException`: ahora es `UnimplementedError`
  en los tres casos, un único tipo para "esta feature no está disponible en
  este sandbox". Migración: cambia `except InvalidArgumentException`/`except
  SandboxException` alrededor de esas llamadas por `except
  rayito.UnimplementedError` (o `except LifecycleUnsupportedException`, que
  ahora es subclase suya); `MetricsHistoryUnavailable` sigue siendo
  `UnimplementedError` para quien la captura, pero su `__cause__` cambia: ya
  no es la `RpcError` de gRPC directamente, sino el `UnimplementedError`
  genérico de la tabla unaria (cuya propia `__cause__` sí es la `RpcError`;
  `__cause__.__cause__` para llegar a ella). `rayito.e2b` no cambia de
  superficie: seguía lanzando `UnimplementedError` en los tres casos con el
  mismo `feature`, `reason` y `doc`; sólo se simplifica su traducción
  interna. La pista "publica una imagen con una versión actual de rayd" de
  `unimplemented_rpc_error` sólo sale para un RPC de verdad ausente: un
  kernel ausente en `run_code`/`create_code_context` ya nombra
  `rayito-base-poly` en el propio detalle de `rayd`, así que ya no repite un
  consejo que no aplica (la imagen ya está actualizada).

### Changed

- `sbx.kill()`, `sbx.pause()` y `sbx.connect()` (`rayito.e2b`, llamadas de
  instancia) avisan ahora con `RayitoCompatWarning` cuando se les pasa
  `headers=`, `proxy=` o `retries=` (y `sbx.kill()`/`sbx.pause()` también con
  `request_timeout=`), en vez de perderlos en silencio: esta llamada opera
  sobre el canal y el plano ya construidos de este sandbox y no puede
  reconstruirlos; usa `Sandbox.<kill|pause|connect>(sandbox_id, ...)` si
  necesitas aplicarlos.

## [0.3.3] - 2026-09-29

### Security

- `.github/workflows/release.yml`: el job `python` se parte en `python-build`
  (sin `environment` ni `id-token`, el único que hace checkout y corre `uv
  build`) y `python-publish` (con el token OIDC de PyPI, sin checkout y sin
  `uv`, que verifica con `sha256sum -c` que el `dist/` descargado es el que
  produjo `python-build` antes de publicar) — cierra C-10/H-02 residuo
  (`docs/SECURITY_AUDIT.md`). Sin cambio de comportamiento en lo publicado
  ni en el Trusted Publisher de PyPI.

### Changed

- Shim E2B: `native_call_kwargs` (`rayito.e2b._compat`) ya no lee
  `ConnectionConfig.current_integration()` ni construye un plano de
  control compartido (`shared_control_plane`) — la construcción con AWS
  se mueve al nuevo `bind_control_plane` (`rayito.e2b._connection`), el
  único punto de I/O; `native_call_kwargs` recibe `integration` como
  parámetro y sólo describe, en `NativeCall.plane_settings`, si hace
  falta un plano nuevo. `snapshot_config` ya no escribe el atributo
  privado de `ConnectionConfig` desde fuera de la clase; ahora pasa por
  el nuevo `ConnectionConfig._snapshot`. Sin cambio de comportamiento
  observable ni de ninguna firma pública (`ConnectionConfig`, `Sandbox`,
  `AsyncSandbox`, `E2B`); ver `openspec/changes/m10-python-shim`.

## [0.3.2] - 2026-09-29

### Fixed

- Reconexión tras un reset de stream del proxy de AWS (`RST_STREAM` antes
  del plazo real): grpcio lo presenta como `CANCELLED "Stream removed"`
  (`AWS_API_NOTES.md` #33), que `is_stream_reset` no clasificaba como corte
  reconectable (sólo miraba `UNAVAILABLE`/`INTERNAL`), así que
  `commands.connect`/`pty.connect`, `watch_dir` y el reenganche de
  `run_code` fallaban en vez de reconectar. Ahora `CANCELLED` con esa marca
  (u otra de `STREAM_RESET_MARKERS`) reconecta igual que un `Socket closed`;
  un `CANCELLED` sin la marca (el propio `call.cancel()`, p. ej.
  `CommandHandle.disconnect()`) nunca reconecta, y el chequeo del handle
  desconectado sigue yendo antes que la clasificación, así que un corte que
  llegara disfrazado de reset justo después de `disconnect()` tampoco
  reconecta (`_transport.py`).
- `rayito-mcp` y `python -m rayito.mcp` sin el extra `rayito[mcp]`: antes
  fallaban con un `ModuleNotFoundError` sin capturar (traza completa) porque
  `rayito.mcp/__init__.py` importaba todo el paquete de forma ansiosa, así
  que cualquier import de `rayito.mcp` (incluido `rayito.mcp.__main__`)
  disparaba el fallo antes de que su propio `try`/`except` pudiera
  atraparlo. Ahora `rayito.mcp` resuelve sus cuatro nombres con
  `__getattr__` perezoso (PEP 562) y `rayito.mcp.__main__` imprime un aviso
  de una línea con el comando de instalación y sale con 2, sin traza, igual
  que la CLI `rayito` con `rayito[cli]`.

### Changed

- `tests/e2e/test_m3_filesystem.py`: `UPLOAD_BUDGET_SECONDS` (20 s fijos
  para 8 MB) fallaba en uplinks lentos (0,25-0,44 MB/s medidos). El
  presupuesto ahora se deriva de una línea base medida en el propio run
  (una escritura de prueba de 300 KB) escalada a los 8 MB con un margen de
  3x, acotada entre 5 s y 120 s: el test comprueba una regresión de
  throughput, no la red de quien lo corre.

## [0.3.1] - 2026-09-28

Versión de mantenimiento, sin cambios de API; se publica para mantener las
versiones de los tres componentes sincronizadas.

### Changed

- `boto3`/`botocore` 1.43.103 en el lockfile de desarrollo y rango de
  `uv-build` ampliado a `<0.13`.

## [0.3.0] - 2026-09-24

Rayito 0.3.0 (M9, paridad con E2B 2.x). Notas completas en
`docs/RELEASE_NOTES_0.3.0.md`; todo lo de M9 exige una imagen publicada con
el `rayd` de M9.

### Cambios que rompen

- **`rayito.e2b` pasa de E2B 1.x a 2.x y exige una imagen M9**: `create()`
  siempre pide un plazo lógico a `rayd` y, contra una imagen anterior,
  termina el VM y lanza `UnimplementedError("lifecycle")`. `timeout` deja de
  ser la vida inmutable del MicroVM (ahora es el plazo lógico; el tope es
  `max_lifetime`). Detalle en "Changed (shim)".
- `rayito.e2b`: `sbx.pause()` devuelve `bool` (antes el id del sandbox);
  `files.watch_dir(path, on_event)` y `pty.create(size, on_data)` toman el
  callback por nombre (el segundo posicional es `user`, como en E2B 2.x).
- `rayito.e2b`: `set_timeout`, `upload_url`/`download_url`,
  `get_metrics(start=, end=)`, `list(next_token=)` y
  `allow_internet_access=False` ya no lanzan `UnimplementedError`, y
  `mcp=`, `network=` y `lifecycle=` ya no son `TypeError`: el código que
  capturaba esos errores para degradar cambia de camino.

### Added

- **Transferencias por S3 prefirmado** (`m9-file-transfer`, ADR-010):
  `S3Staging(bucket, prefix="rayito-transfer", region=None,
  max_expires_in=86400, threshold_bytes=8 MiB, multipart_threshold_bytes=5 GiB)`
  (o `RAYITO_TRANSFER_BUCKET`/`RAYITO_TRANSFER_PREFIX`/`RAYITO_TRANSFER_REGION`;
  sin bucket por defecto), `create(transfer=)`, `connect(transfer=)` y
  `sbx.transfer`. `files.upload_url(path, user=, expires_in=3600,
  max_bytes=, form=)` devuelve un `UploadTicket` (un `str` con la URL, más
  `headers`, `fields`, `expires_at`, `wait()`, `status()`, `cancel()`; de un
  solo uso, con la importación ya armada en el sandbox) y
  `files.download_url(path, user=, expires_in=3600, filename=)` un
  `DownloadLink` (`str` con `size`, `sha256` y `expires_at`; una foto del
  fichero al llamar, multiparte desde `multipart_threshold_bytes`).
  `Sandbox.upload_url/download_url(path, user, use_signature_expiration)`
  con la forma de E2B. El SDK firma con tus credenciales (SigV4, host
  virtual regional, vida `min(expires_in, max_expires_in, 604800)`); `rayd`
  no guarda ninguna. `repr` nunca muestra la URL y `pickle` la rechaza.
  Nuevos `TransferStatus`, `AsyncUploadTicket`, `TransferException(code,
  reason)`, `FileUploadException` y `UnimplementedError(feature, reason,
  doc=None)` nativo (sin staging: "configura transfer=S3Staging(...) o
  RAYITO_TRANSFER_BUCKET"; con una imagen anterior: "actualiza la imagen").
- **Ficheros grandes por S3**: con `transfer`, `files.write`/`write_files`
  suben lo que mide al menos `threshold_bytes` (y todo stream binario no
  buscable) directamente a S3 y `rayd` lo importa verificando su sha256;
  `files.read` exporta y descarga verificando el sha256. Sin `transfer`,
  el camino gRPC no cambia. `request_timeout` (o `60 s + 1 s por MB`)
  acota también la pata S3 y `stream_idle_timeout` rige entre trozos de la
  descarga en `format="bytes"`/`"text"`: una subida o descarga colgada
  falla con `TimeoutException` sin esperar a los reintentos de botocore.
  `create(pool=..., transfer=...)` firma con la sesión boto3 del pool.
- `files.read(gzip=, stream_idle_timeout=)`, `files.write`/`write_files(gzip=,
  metadata=, use_octet_stream=)` y `EntryInfo.metadata` (mapa de sólo
  lectura, claves en minúsculas). `gzip` y `metadata` en una escritura
  exigen una imagen M9 (`UnimplementedError` antes de mover bytes);
  `use_octet_stream` se acepta sin efecto. Nuevos códigos de `StreamError`:
  `resource_exhausted` (`DiskFullException` o `RateLimitException`) y
  `failed_precondition` (`InvalidArgumentException`).

- **Plazo del servidor** (`m9-server-timeout`, ADR-011; exige una imagen M9):
  `create(max_lifetime=, on_timeout="kill" | "pause")`. Con cualquiera de los
  dos, `timeout` es un plazo lógico que `rayd` hace cumplir aunque el cliente
  muera y `max_lifetime` (120–28 800 s, por defecto `timeout + 60`) es
  `maximumDurationInSeconds`. `set_timeout(timeout)` (instancia y clase, con
  el access token) lo fija con `SetTimeout` EXACT (puede acortarlo; más allá
  del tope, `InvalidArgumentException`) y `connect(timeout=)` (instancia y
  clase) lo alarga con `AT_LEAST`. `on_timeout="pause"` suspende al vencer y
  `idle.auto_resume` reanuda con un plazo nuevo de `max(timeout, 300 s)`.
  Nuevos `SandboxLifecycle` (en `get_health().lifecycle` y
  `get_info().lifecycle`) y `LifecycleUnsupportedException` (una imagen
  anterior a M9 con un ciclo de vida pedido: el VM se termina salvo
  `keep_on_failure`). Sin `max_lifetime` ni `on_timeout` el cable y la
  semántica son los de siempre. `TimeoutException` también cuando una llamada
  choca con el plazo (`sandbox_timeout`).
- **Historial de métricas y listado reanudable**
  (`m9-sandbox-observability`): `get_metrics_history(start=, end=,
  max_points=)` (instancia y clase con el access token; una muestra cada 5 s,
  anillo de 8 h, hueco mientras está suspendido; `UnimplementedError` en una
  imagen anterior, como las transferencias y el plazo, con el
  `UNIMPLEMENTED` de gRPC en `__cause__`),
  `SandboxMetrics.mem_cache_bytes`, `SandboxHealth.cpu_count` y
  `memory_total_bytes`, `SandboxInfo.agent_version`/`cpu_count`/`memory_mb`
  en `get_info()`. `Sandbox.paginate(limit=, next_token=, order=,
  started_after=, states=, metadata=)` → `SandboxListPaginator`
  (`next_items()`, `has_next`, `next_token` opaco) y `list(order=,
  started_after=)`.
- **Política de egress** (`m9-egress-policy`, ADR-012): `create(network=,
  allow_internet_access=)`, `update_network()` (instancia y clase),
  `get_network()`, `NetworkPolicy`, `NetworkOptions`, `EgressProxy`,
  `NetworkState`, `EgressEnforcement`, `ALL_TRAFFIC` y
  `SandboxHealth.egress_enforcement`. Se aplica en el guest de
  `rayito-base-caps`; en cualquier otra imagen el SDK termina el VM (también
  con `keep_on_failure`) y lanza `UnimplementedError`.
- **Kernels JavaScript y TypeScript** (`m9-deno-kernels`): `language`
  acepta `typescript` (alias `ts`) además de `javascript` (`js`), servidos
  por el kernel de Deno de `rayito-base-poly`.
- **Git** (`m9-e2b-v2-surface`): `sbx.git` (`Git`/`AsyncGit`) con la API git
  de E2B sobre `commands.run` (`clone`, `status`, `commit`, `push`, `pull`,
  `dangerously_authenticate`, ...), `GitStatus`, `GitBranches`,
  `GitFileStatus`, `GitAuthException` y `GitUpstreamException`; las
  credenciales nunca se registran y se redactan de la salida.
- **CLI** (`rayito[cli]`): `rayito sandbox create [--detach --token-file]`,
  `connect`, `exec -- CMD` y `metrics [--follow]`, con terminal interactiva
  (POSIX y consola de Windows) y el token sólo por fichero o
  `RAYITO_ACCESS_TOKEN`, nunca por argv.
- `create()`/`connect()` aceptan `logger=` (los logs de ese sandbox van al
  `logging.Logger` dado), `is_running(request_timeout=)` y
  `CommandHandle.wait(on_pty=, on_stdout=, on_stderr=)`.
- **Shim `rayito.e2b` 2.x** (`m9-e2b-v2-surface`, contrato `e2b` 2.51.0 y
  `e2b-code-interpreter` 2.10.0): `set_timeout`, `connect(timeout)` y la
  instancia `sbx.connect()`, `lifecycle`, `beta_create(auto_pause=True)`,
  `upload_url`/`download_url`, `get_metrics(start, end)` y la forma de clase
  con el access token, `list(limit, next_token, order)` con
  `SandboxQuery.state/started_after/template`, `allow_internet_access=False`
  y `network` (en `rayito-base-caps`), `update_network`, `git`,
  `run_code(language="typescript")`, `E2B`, `ConnectionConfig`,
  `sbx.connection_config`, `proxy=`, `headers=`, `retries=`, `logger=`,
  `traffic_access_token`, `envd_api_url` y las excepciones
  `FileNotFoundException`, `SandboxNotFoundException`,
  `ServiceBusyException`, `FileUploadException`, `BuildException`.
  `UnimplementedError` explícito (nunca `TypeError` ni `AttributeError`) para
  `fork`, snapshots, `pause(keep_memory=False)`, `network.rules`,
  `mask_request_host`, `allow_public_traffic=True`, `mcp`, `iam`, volúmenes,
  secretos y templates.

### Changed (shim)

- **`rayito.e2b` exige una imagen M9**: `create()` siempre pide un plazo
  lógico; contra una imagen anterior termina el VM y lanza
  `UnimplementedError("lifecycle")`. `timeout` es ahora el plazo lógico y
  `maximumDurationInSeconds` es `max_lifetime` (por defecto
  `max(3600, min(timeout + 60, 28800))`).
- `rayito.e2b.Sandbox.set_timeout` deja de lanzar `UnimplementedError` (lo
  que decía la entrada de 0.2.0): mueve el plazo lógico hasta
  `max_lifetime`.
- `sbx.pause()` devuelve `bool` (antes el id); `files.watch_dir` y
  `pty.create` toman el callback por nombre (el segundo posicional es
  `user`); `proxy=` se honra; `NotEnoughSpaceException` es
  `DiskFullException` y se lanza; `rayito.e2b.UnimplementedError` es la
  misma clase que `rayito.UnimplementedError`.

### Fixed

- **Auto-resume tras una pausa por el plazo con el cliente vivo**: en modo
  `on_timeout="pause"` con `auto_resume`, si este cliente suspendió el
  sandbox al vencer y la suspensión real duró menos de 2 s (el vigilante de
  `rayd` no la reconoce como congelación), el sandbox volvía `expired` y la
  siguiente llamada fallaba con `sandbox_timeout`. Ahora el SDK aplica la
  regla de E2B (`max(timeout, 300 s)`, acotada al tope menos 5 s) con un
  `SetTimeout` en el primer `sandbox_timeout` tras reanudar, una vez por
  suspensión. La marca de un solo uso sólo se consume cuando la
  `resume_generation` ya avanzó y el `SetTimeout` respondió (un
  `sandbox_timeout` que llega antes de la congelación ya no la gasta), y los
  callers concurrentes comparten una sola reapertura (un lock en `Sandbox`,
  una tarea compartida en `AsyncSandbox`): un único `SetTimeout` y todos
  reintentan.
- `rayito doctor`: la tabla de compatibilidad gana la fila `0.3` (`rayd`
  mínimo `0.3.0`); sin ella la comprobación `compatibility` daba `FAIL` con
  el SDK 0.3.0.
- Las URLs y la pata S3 de las transferencias se firman con la sesión del
  plano de control (`LambdaMicrovmsControlPlane.session`) cuando no se pasa
  `session=`, también en un sandbox tomado de un pool (`pool.session`); antes
  caían a la cadena por defecto de boto3 y podían firmar con otro principal.
- `get_info()` devuelve los hechos del guest (`agent_version`, `cpu_count`,
  `memory_mb`) en el valor que retorna sin guardarlos en `sbx.info`.
- `UnimplementedError` de una política de egress en una imagen sin
  `CAP_NET_ADMIN` nombra `allow_internet_access=False` sólo cuando ese flag,
  y no `network=`, es lo que pide la política (la misma regla que TS).
- CLI: al salir de `rayito sandbox connect`/`create` el hilo que lee la
  terminal local se detiene (antes podía seguir consumiendo la entrada y, en
  macOS, bloquear el cierre del descriptor de la PTY).
- `Result.extra` se lee del proto con las claves ordenadas: el orden de un
  mapa protobuf no está garantizado.
- La readiness de `create()`, `connect()` y `resume()` exige además un
  `Health` con `sandbox_id`: el proxy deja pasar `Health` antes de que
  `rayd` reciba `/run`, y ese `Health` (kernel del snapshot sin rotar) daba
  por listo un sandbox cuya rotación de `/run` ponía `kernel_ready=false`
  justo después (AWS_API_NOTES.md Q78, visto en la regresión de M9 con `create()` en 2,35 s).

### Security

- `repr()` de `LaunchPlan` ya no muestra el access token ni el de
  `LaunchRequest` el `runHookPayload` (que lleva los `envs` de `create()`):
  ambos campos quedan fuera de `repr` (`field(repr=False)`).
- **`JsonFilePoolBackend` escribe por un temporal exclusivo**
  (auditoría interna H-01…H-06, fila H-03): el fichero del pool se escribía
  por `<path>.tmp`, un nombre fijo que otro usuario del sistema podía crear
  o apuntar con un enlace antes que el SDK; ahora se crea con
  `tempfile.mkstemp()` (`O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`) y `fchmod`
  sobre el descriptor antes de escribir nada, y se renombra al destino. Sin
  esto la promesa de `0600` de T14 no era cierta.
- **`rayito-mcp --http` pasa siempre `TransportSecuritySettings`** (fila
  H-05): el SDK `mcp` sólo auto-activa la protección anti-DNS-rebinding para
  tres cadenas de host exactas, así que `--host 127.0.0.2` servía sin mirar
  `Host` ni `Origin`. Ahora el servidor exige que ambos sean el `host:puerto`
  con el que arrancó, y un `--host` comodín (`0.0.0.0`, `::`) se rechaza con
  error de uso (exit 2).
- **Aviso único cuando `RAYITO_ACCESS_TOKEN` se hereda del entorno** (fila
  C-08): `create()` también lee la variable, así que exportarla convierte el
  secreto por sandbox en uno de toda la flota del proceso. El SDK lo avisa
  una vez por proceso (`logger.warning`, nunca el valor).
- **Los errores de AWS ya no encadenan el `ClientError` crudo**: botocore no
  cuelga la petición firmada de un `ClientError`, pero lo que AWS devuelve
  repite la firma: un `SignatureDoesNotMatch` de S3 trae `AWSAccessKeyId` y
  `CanonicalRequest` (con el valor de `x-amz-security-token`) en
  `response["Error"]`, y un `InvalidSignatureException`/`SignatureDoesNotMatch`
  de lambda-microvms o STS mete la cadena canónica en el mensaje, que acababa
  en el mensaje de la excepción traducida y, por `raise ... from exc`, en
  cualquier traceback (también el `logger.debug(..., exc_info=True)` de la
  reconexión). Ahora el plano de control y las transferencias por S3 lanzan
  fuera del `except` y desde un `AwsErrorSummary`
  (`rayito._aws_sanitize.sanitize_aws_error`) con sólo `name`, `code`, el
  mensaje redactado, `status_code`, `request_id`, `extended_request_id` y
  `attempts` (S3 sin mensaje: un `EndpointConnectionError` nombra el
  bucket); ni `__cause__` ni `__context__` guardan el error de botocore. El
  mensaje traducido pierde la cadena canónica, las cabeceras de firma, los
  parámetros `X-Amz-*` y los ids de clave. `status_code`, `aws_code`,
  `retry_after` y `quota_code` no cambian.

## [0.2.0] - 2026-09-17

Segunda release: los siete cambios de M7 (`MILESTONES.md`), aceptados
contra AWS real sobre `rayito-base` con `rayd` 0.2.0. Los SDKs y `rayd`
suben `MAJOR.MINOR` en lockstep; `rayito doctor` exige `agent_version` ≥
0.2.0 para el SDK 0.2 (`docs/site/docs/limits.md`).

### Added

- **Persistencia del `HOME` en S3** (`m7-s3-persistence`, ADR-009):
  `S3Prefix(bucket, prefix="rayito", name=None, region=None)`,
  `Sandbox.create(persist=, persist_timeout=600)` (exige
  `execution_role_arn`; con `name` restaura el checkpoint existente en
  `sbx.last_restore`, sin `name` lo fija al `sandbox_id`),
  `connect(persist=)`, `sbx.persist`, `checkpoint_files(target=, exclude=,
  timeout=, on_progress=)` → `CheckpointResult`, `restore_files(source=,
  timeout=, on_progress=)` → `RestoreResult` (`NotFoundException` sin
  checkpoint) y `reincarnate(exclude=, persist_timeout=)` (checkpoint →
  `create(persist=)` con las mismas opciones → `kill()`; el viejo sigue vivo
  si el `create` falla). Nuevos `CheckpointProgress`, `RestoreProgress`,
  `LaunchOptions` y `PersistenceException(code)` (`permission_denied`,
  `internal`, `failed_precondition`, `unimplemented`, `interrupted`,
  `resource_exhausted`); `create(pool=)` rechaza `persist=`. Misma
  superficie en `AsyncSandbox`. `rayito.e2b.Sandbox.set_timeout` sigue
  siendo `UnimplementedError` y su mensaje nombra `reincarnate()`.
- `run_code(code, language=...)` y `create_code_context(language=...)` aceptan
  `python`, `bash` y `javascript` (alias `js`, sin distinguir mayúsculas;
  `m7-poly-kernels`): `language` selecciona el contexto por defecto de ese
  kernel (`default-bash`), que el agente crea en la primera celda, y es
  excluyente con `context` (`InvalidArgumentException`). El kernel `bash`
  sólo lo trae la variante de imagen `rayito-base-poly`; `javascript` es un
  nombre reservado que hoy ninguna imagen trae (`AWS_API_NOTES.md` Q57). Un
  kernel que la imagen no trae es `InvalidArgumentException` con `grpc_code`
  `UNIMPLEMENTED` y `rayito-base-poly` en el mensaje; `envs` por ejecución
  sólo en contextos Python. El shim `rayito.e2b` reenvía `bash`,
  `javascript` y `js` al core y mantiene `UnimplementedError` para `r`,
  `java` y el resto (`docs/site/docs/kernels.md`).
- **Pool de sandboxes suspendidos** (`SandboxPool`, `AsyncSandboxPool`,
  `PoolConfig`, `PoolStats`, `PoolSlotInfo`, `PoolBackend`,
  `InMemoryPoolBackend`, `JsonFilePoolBackend`, `PoolClosedException`;
  ADR-008, `m7-suspended-pool`): N MicroVMs calentados con `create()`,
  asentados con una celda trivial, aparcados con `pause(wait=True)` y
  entregados por `take(wait=...)` con `resume_microvm` explícito y el token
  de la plaza (sin `get_microvm` en la toma); fallback a `create()` sin
  plaza; relleno en un hilo del proceso con backoff 1 s → 60 s por los
  token buckets compartidos; reciclado antes del muro de 8 h y
  reconciliación con `list_microvms`; recuperación desde el backend JSON
  (`rayito.pool/1`, modo `0600`, el mismo fichero que lee TypeScript);
  custodia del secreto por plaza hasta `take()` (`SECURITY.md` T14);
  `Sandbox.create(pool=...)` / `AsyncSandbox.create(pool=...)` como azúcar
  que rechaza todo kwarg de lanzamiento. Documentación en
  `docs/site/docs/pool.md`.
- Servidor MCP (`rayito.mcp`, extra `rayito[mcp]`, script `rayito-mcp`):
  seis herramientas (`run_code`, `run_command`, `read_file`, `write_file`,
  `list_files`, `list_sandboxes`), stdio y streamable HTTP, un sandbox por
  proceso (creado en la primera llamada, suspendido por AWS al quedar ocioso,
  terminado al cerrar el servidor); configuración sólo por entorno
  (`RAYITO_TEMPLATE`, `RAYITO_MCP_TIMEOUT_SECONDS`, `RAYITO_MCP_IDLE_SECONDS`,
  `RAYITO_MCP_LOG_LEVEL`). Documentación en `docs/site/docs/mcp.md`.
- **CLI `rayito`** como extra `rayito[cli]` (`typer`) con el entry point
  `rayito` y `python -m rayito.cli`: `rayito image publish|list|prune|zip`,
  `rayito sandbox list|kill|info|logs` (stream de CloudWatch
  `YYYY/MM/DD[<versión>]<id>` resuelto desde `get-microvm`) y
  `rayito doctor` (diez comprobaciones `OK|WARN|FAIL|SKIP`: credenciales y
  región, imágenes gestionadas, Service Quotas frente a los defaults,
  `iam:SimulatePrincipalPolicy` orientativa, bucket, gate de tres estados de
  la imagen, MicroVMs `RUNNING`, token, `Health` del agente y la tabla de
  compatibilidad SDK ↔ `rayd` de `rayito.cli._compat` (la versión de imagen,
  contador de builds por imagen y cuenta, sólo se informa); `--launch`
  crea y mata un sandbox de 300 s; `--json` en todos los comandos). Ninguna
  salida contiene un JWE, un access token ni un `runHookPayload`.
- `probe_health` / `probe_health_async` (un `Health` sin access token sobre
  un canal dedicado) como base de `probe_metadata`; superficie pública sin
  cambios.

### Fixed

- `rayito image zip` / `copy_sidecar.py`: la lista de exclusión se evalúa
  antes de `is_file()`, así que un symlink de Linux dentro de un `.venv`
  excluido (creado por una sesión Docker) ya no rompe la copia en Windows
  (`WinError 1920`).

### Changed

- `scripts/publish_image.py`, `image_prune.py`, `image_zip.py` y
  `copy_sidecar.py` son shims de `rayito.cli` (`_publish`, `_prune`,
  `_artifact`) con los mismos argumentos; `publish` exige `--bucket` o
  `RAYITO_BUCKET` (la biblioteca ya no trae el bucket del mantenedor) y el
  `Makefile` lo pasa con la variable `BUCKET` corriendo los dos shims de
  AWS con `uv run --project clients/python`.

- Licencia MIT → Apache-2.0 (PEP 639: `license = "Apache-2.0"`,
  `license-files = ["LICENSE", "NOTICE"]`, sin clasificador `License ::`;
  la wheel lleva `License-Expression: Apache-2.0` y `NOTICE` en
  `.dist-info/licenses/`, comprobado por `scripts/check_wheel.py`).

## [0.1.0] - 2026-09-16

Primera versión publicable. Reúne la superficie aceptada contra AWS real en
los hitos M1-M6 (`MILESTONES.md`).

### Added

- **Ciclo de vida (M1)**: `Sandbox.create` (`run-microvm` + token del proxy +
  sondeo de `Health` hasta `agent_ready` y `kernel_ready`), `connect`, `kill`,
  `list`, `get_info`, `is_running`, `get_host(port)` con las cabeceras del
  proxy, `get_health`; `IdlePolicy` (auto-suspensión a los 300 s por
  defecto); `timeout` como vida máxima del MicroVM (tope 8 h).
- **Comandos (M2)**: `sbx.commands.run` en foreground y background,
  `on_stdout`/`on_stderr`, `stdin`, `list`, `kill`, `connect(pid, from_seq)`,
  `send_stdin`, `close_stdin`; `CommandHandle` con `wait`, iteración y
  reconexión; `get_metrics()` (instantánea procfs).
- **Ficheros (M3)**: `sbx.files.read` (`text`/`bytes`/`stream`), `write`,
  `write_files` en un solo stream, `list(depth)`, `exists`, `get_info`,
  `remove`, `rename`, `make_dir`, `watch_dir` con `WatchHandle`.
- **Código (M4)**: `sbx.run_code` sobre kernels Jupyter con estado,
  `Execution` con `results` (mime bundles y charts de E2B), `logs`, `error`
  como dato; contextos (`create_code_context`, `list_code_contexts`,
  `remove_code_context`, `restart_code_context`).
- **PTY y suspend/resume (M5)**: `sbx.pty` (`create`, `connect`,
  `send_input`, `resize`, `kill`; `PtyHandle`), `pause()`/`resume()` con
  procesos, PTYs, watches y kernels vivos al otro lado, el contrato de
  reconexión (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`, `Reattach`) y
  `reconnect_timeout`.
- **Metadatos por sandbox (M6)**: `Sandbox.create(metadata=...)` viaja en el
  `runHookPayload` (4096 caracteres junto a `envs`) y `rayd` lo devuelve en
  `Health`: `sbx.metadata`, `SandboxHealth.metadata`, `SandboxInfo.metadata`
  (`None` = no leído del agente), `Sandbox.get_info(sandbox_id)` con la sonda
  de `Health` sobre sandboxes `RUNNING` y `Sandbox.list(metadata=...)`, un
  filtro en cliente **O(n)** (un `get-microvm` + un JWE + un `Health` por
  sandbox `RUNNING`) que nunca despierta un sandbox suspendido.
- **Shim de E2B (M6)**: `rayito.e2b` con `Sandbox`, `AsyncSandbox` y los
  nombres del SDK de E2B 1.x (`Execution`, `Result`, `CommandHandle`,
  `SandboxInfo`, `SandboxQuery`, `SandboxPaginator`, `PtySize(rows, cols)`,
  las excepciones de `e2b.exceptions`, los charts...). Mapea `template`,
  `timeout` (300 s), `metadata`, `envs`, `request_timeout` y `sandbox_id`;
  ignora con `RayitoCompatWarning` `api_key`, `domain`, `debug`, `proxy` y
  `secure=False`; lanza `UnimplementedError` para `set_timeout`,
  `upload_url`/`download_url`, rangos de `get_metrics`, `connection_config`,
  kernels no Python, `list(next_token=)`, `beta_create(auto_pause=...)`,
  templates/`fork` y `allow_internet_access=False` (medido: un MicroVM sin
  conector de egress hereda el de la imagen y sigue saliendo a internet,
  `AWS_API_NOTES.md` Q44).
- `AsyncSandbox` con la misma superficie que `Sandbox` sobre `grpc.aio`,
  incluidos metadatos y el shim.
- Empaquetado: clasificadores, URLs, `py.typed`, wheel comprobada
  (`scripts/check_wheel.py`, `twine check`), workflow `release.yml` con
  Trusted Publishing de PyPI y sitio de documentación (`docs/site`).

### Requisitos de imagen

- `run_code` necesita `rayito-base` ≥ 7.0 (sidecar de kernels); `pty` y la
  reconexión tras `pause()` necesitan ≥ 10.0 (M5); los **metadatos** necesitan
  la imagen de M6 (un `rayd` que devuelva `HealthResponse.metadata`; la
  aceptación de 0.1.0 corrió sobre `rayito-base-e2b` 1.0, construida del mismo
  árbol que la `rayito-base` de M6). Sobre una imagen anterior el SDK funciona
  y `metadata` se lee vacío.

### Limitaciones conocidas

- La vida de un sandbox no se puede extender (`set_timeout` de E2B es
  `UnimplementedError`: no existe `UpdateMicrovm`); tope 8 h running +
  suspended.
- Ancho de banda del endpoint de un MicroVM de 2 GB: ≈ 0,65 MB/s de escritura
  y ≈ 6,71 MB/s de lectura (medidos); 8 conexiones concurrentes.
- `list(metadata=)` es O(n) sobre los sandboxes `RUNNING` y pospone la
  auto-suspensión de cada uno una ventana de idle.
- Sólo kernels Python; sin URLs firmadas; sin historial de métricas.
- Las URLs del proyecto apuntan a `https://github.com/alejandro-cedeno-10/rayito` hasta
  que el repositorio sea público (placeholder documentado).

### Publicación

Pasos manuales, fuera de CI, antes del primer tag (pasos canónicos en
`docs/RELEASING.md`):

1. Registrar el *Trusted Publisher* en PyPI: proyecto `rayito`, repositorio
   `alejandro-cedeno-10/rayito`, workflow `release.yml`, environment `pypi`.
2. Crear el environment `pypi` en GitHub (Settings → Environments).
3. `git tag python-v0.1.0 && git push origin python-v0.1.0`: el job `build`
   construye y comprueba la wheel (contenido y `twine check`) y verifica que
   el tag coincide con `pyproject.toml`; el job `publish` sube a PyPI sin
   token. `workflow_dispatch` sólo ensaya el `build`.

## [0.0.1] - [0.0.5]

Builds internos de los hitos M1-M5, nunca publicados.

[Unreleased]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.10.0...HEAD
[0.10.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.9.1...python-v0.10.0
[0.9.1]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.9.0...python-v0.9.1
[0.9.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.8.0...python-v0.9.0
[0.8.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.7.1...python-v0.8.0
[0.7.1]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.7.0...python-v0.7.1
[0.7.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.6.1...python-v0.7.0
[0.6.1]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.6.0...python-v0.6.1
[0.6.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.5.1...python-v0.6.0
[0.5.1]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.5.0...python-v0.5.1
[0.5.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.4.0...python-v0.5.0
[0.4.0]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.3.3...python-v0.4.0
[0.3.3]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.3.2...python-v0.3.3
[0.3.2]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.3.1...python-v0.3.2
[0.3.1]: https://github.com/alejandro-cedeno-10/rayito/compare/python-v0.3.0...python-v0.3.1
[0.3.0]: https://github.com/alejandro-cedeno-10/rayito/releases/tag/python-v0.3.0
[0.2.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.2.0.md
[0.1.0]: https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/RELEASE_NOTES_0.1.0.md
