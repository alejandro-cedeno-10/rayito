# Seguridad

Resumen de [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(el modelo de amenazas completo; los identificadores T*n* de esta página son
sus filas). Tres principales: el operador del SDK (confianza
total), el código que corre dentro del sandbox (**ninguna**) y AWS (proxy,
hooks, snapshot).

## Reportar una vulnerabilidad

En privado, con [GitHub Private Vulnerability Reporting](https://github.com/alejandro-cedeno-10/rayito/security/advisories/new);
nunca en un issue público, y nunca pegando un JWE (`x-aws-proxy-auth`), un
access token (`x-access-token`) ni un `runHookPayload`. Incluye la versión
del SDK, el `agent_version` de `get_health()`, la imagen y su versión, la
región y una reproducción mínima sin datos de clientes. Acuse de recibo en
7 días y corrección o mitigación en 90; no hay programa de recompensas.

**Versiones soportadas**: sólo la última línea `MAJOR.MINOR`, hoy la
**0.15.x** (SDK de Python y de TypeScript, y una imagen con `rayd` 0.15.x);
mientras el proyecto esté en 0.x no hay ramas de mantenimiento de líneas
anteriores ([Versionado y soporte](limits.md#versionado-y-soporte),
[Compatibilidad SDK ↔ rayd ↔ imagen](limits.md#compatibilidad-sdk-rayd-imagen)).

## Lo que protege el SDK por defecto

| Amenaza | Mitigación |
|---|---|
| El código del sandbox lee las credenciales del execution role por IMDS | `create(execution_role_arn=None)` **por defecto**: sin rol no hay credenciales dentro (ni logs de runtime). Con rol, mínimo privilegio |
| Robo del JWE del proxy | Todo RPC salvo `Health` exige además `x-access-token`; TTL 60 min; el SDK nunca loguea cabeceras |
| El sha256 del secreto viaja en `runHookPayload` (CloudTrail) | Sólo el hash sale del cliente; `rayd` compara en tiempo constante y nunca escribe el body de `/run` |
| Escalada a root dentro del guest | Procesos, PTYs y kernels como uid 1000; otra cuenta sólo si su uid y gid están entre 1000 y 65535 (el rango que cubren el bloqueo de IMDS y las reglas de red) y no está en el grupo 0; root sólo con `user="root"` **y** `RAYITO_ALLOW_ROOT=1` en la imagen |
| Agotamiento de recursos desde el sandbox | rlimits, grupos de procesos, timeouts de servidor, canales de salida acotados, máx. 256 procesos/PTYs, máx. 8 kernels; en el cliente, la salida guardada de un comando o una PTY está acotada a 64 MiB por descriptor (`max_output_bytes`/`maxOutputBytes`, `truncated` lo indica); los puertos de `rayd` (8080 y 9000) atienden un número máximo de conexiones a la vez y los hooks cortan una cabecera que no termina de llegar, así que conexiones ociosas no agotan sus descriptores (T7) |
| Pids falsos en el protocolo del sidecar (T12) | `rayd` sólo registra y señala el kernel que `/proc` confirma como hijo del sidecar, líder de su grupo y de su mismo uid, fijado por su hora de arranque; nunca señala el grupo 0, el 1 ni el suyo propio |
| Credenciales de git en el sandbox | `clone`/`push`/`pull` con `username`/`password` corren sin hooks ni credential helpers, se niegan si la configuración de git reescribe URLs y siempre intentan quitar el token de `.git/config` al acabar; **riesgo residual**: el código del sandbox con el mismo usuario puede capturarlo igualmente: trátalo como revelado y usa tokens de vida corta y de un solo repositorio ([Git](git.md#credenciales)) |
| Contenido de terceros o secretos en un build de template | los enlaces simbólicos dentro de un directorio copiado nunca se siguen (como Docker), así que un enlace a un fichero de tu máquina no acaba en el artefacto ni en la imagen; `.dockerignore` sigue la semántica de Docker (`**/.env` excluye también el `.env` de la raíz) y el SDK avisa si va a empaquetar `.env`, `.git/`, `.aws/`, `.ssh/` o claves `*.pem`/`*.key`, que cualquier código del sandbox podría leer ([Templates](funciones-opcionales/templates.md#contexto-de-build)) |
| Logs de CloudWatch o de build con secuencias de escape | la CLI muestra los caracteres de control como `\xNN` visibles cuando escribe en una terminal; con un execution role, el código de un sandbox puede escribir en cualquier stream de `/rayito/*`, así que los logs de runtime no prueban integridad |
| `rayd` (root) como *confused deputy* en el filesystem | lista de denegación sobre rutas canónicas, `setfsuid` del usuario en cada operación, sin `..`; cada ruta se abre componente a componente con `O_NOFOLLOW` y se actúa sobre el descriptor, así que un componente que el código del sandbox cambie por un enlace entre la comprobación y el uso se rechaza en vez de seguirse, y un directorio en `proc`, `sysfs` o `devpts` se rechaza llegue por donde llegue (T11) |
| Pasarela de secretos como *confused deputy* | la ruta de cada petición pasa una lista de permitidos (RFC 3986 sin `;`, decodificada una sola vez, sin segmentos `.`/`..`) antes de la allowlist, así que un upstream que normalice `..;` o decodifique dos veces no la saca de `allow` ([Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md)) |
| Estado clonado del snapshot compartido entre sandboxes | nada único antes de `/ready`; `/run` reinicia el kernel por defecto; `/resume` reseed de `random`/`numpy.random` |
| Exfiltración por red saliente | `egress=` explícito en `create()`; allowlist vía conector VPC (fuera del guest); desde 0.3.0, `network=` / `allow_internet_access=False` aplicados dentro del guest en `rayito-base-caps` (rutas por uid + proxy local, [Red saliente](network.md)); en otra imagen fallan cerrados |
| URLs prefirmadas y SSRF de `rayd` (T16) | las firmas el SDK con tus credenciales, `rayd` no guarda ninguna; nunca se loguean; cada URL cubre una clave ligada al sandbox, un método y una caducidad; `rayd` sólo acepta `https` al host regional exacto del bucket y su resolvedor descarta loopback, link-local e IMDS ([Ficheros](files.md)) |
| Proxy de egress de `rayd` como SSRF (T17) | guardia después de resolver (loopback, IMDS, las direcciones propias del guest), el proxy no resuelve nombres denegados, credenciales del proxy del operador sólo por RPC y nunca en logs; la cabecera `Host` se reescribe sólo en la forma absoluta `http://`: un túnel `CONNECT`/SOCKS5 a un nombre permitido (80 o 443) comparte el riesgo de las IPs compartidas; bajo deny-all en `rayito-base-caps`, desde 0.3.2 el DNS de uid ≥ 1000 también se bloquea (una regla `ip rule` del puerto 53 por delante de los resolvedores de la plataforma, que escuchan dentro del guest), así que ni las conexiones ni las consultas DNS de los procesos del sandbox salen del VM; **riesgo residual**: las capas del guest son de mejor esfuerzo y no resisten a root en el guest ni a un exploit del kernel: el conector VPC sigue siendo el control duro de plataforma |
| Secretos inyectados (T18) | apagado por defecto; con `secrets=` el valor viaja sólo en los `envs` por llamada (nunca en `runHookPayload`, `metadata`, logs ni errores) y se cachea sólo en la memoria del SDK; **riesgo residual**: el código del sandbox puede leerlo (fase 1) y queda en el snapshot si se suspende ([Secretos](secrets.md)) |
| Índice de metadatos (T19) | apagado por defecto; con `index=DynamoDbIndex(...)` se copia en tu tabla DynamoDB sólo la `metadata` (no secreta) más la imagen, `startedAt` y el TTL, nunca tokens, `envs` ni secretos; una fila falsa nunca crea un sandbox fantasma (el listado parte de `list-microvms` y exige misma imagen y `startedAt`); rol escritor (`PutItem`) separado del lector (`BatchGetItem`) ([Índice de metadatos](funciones-opcionales/indice-de-metadatos.md)) |
| Hooks de ciclo de vida forjados desde dentro de la VM (T2) | el puerto 9000 nunca va en un token; `rayd` comprueba qué uid es dueño de cada conexión a los hooks: un `/terminate` o un `/validate` de un proceso del sandbox responde 200 sin hacer nada, un `/run` suyo no consume el `/run` del arranque (así el `start_cmd` de una plantilla, que llega antes que la plataforma, no instala su token), y tras el primer `/run` un `/ready` o un `/validate` no reinicia ningún kernel; todo queda en `hook_audit` y en `get_health().hook_anomalies`; **riesgo residual**: `/suspend` y `/resume` forjados se aceptan (cuentan como anomalía), y un `/terminate` que esquiva la búsqueda se acepta pero también cuenta |
| Pasarela de secretos (T24) | apagada por defecto; con `gateways=` el valor vive sólo en `rayd`, el `upstream` es fijo (`https://host`) y la allowlist decide antes de conectar; de la respuesta se quitan las cabeceras que llevan el valor; **riesgo residual**: el cuerpo vuelve sin inspeccionar, así que una regla de `allow` hacia un endpoint que refleje las cabeceras de la petición entrega el secreto ([Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md#lo-que-la-pasarela-no-puede-impedir)) |
| Dominio propio (T25, experimental) | apagado por defecto; con `CustomDomain` la CloudFront Function borra toda cabecera `x-aws-proxy-*` del viewer antes de poner las suyas, una ruta exige token de tráfico salvo `public=True` explícito (se guarda sólo su sha256) y una ruta caducada responde 404; **riesgo residual**: la ruta no se borra sola al matar el sandbox (llama a `unregister()`) y el JWE de cada ruta vive en el KeyValueStore de tu cuenta ([Dominio propio](funciones-opcionales/dominio-propio.md)) |
| Agente en AgentCore sin pasarela de secretos (T36) | `--target agentcore` es opt-in; el agente lee las credenciales del rol del runtime, así que ese rol es de mínimo privilegio (Bedrock sólo en los modelos de `--bedrock-model`, Secrets Manager sólo en los de `--readable-secret`), con MMDSv2 siempre activo y sin copiar secretos al runtime ([detalle](#agentcore)) |

## Qué no poner en `envs` ni en `metadata`

`envs` y `metadata` viajan juntos en `runHookPayload`. AWS puede registrar el
payload en eventos de datos de CloudTrail y cualquier principal con
`lambda:CreateMicrovmAuthToken` sobre la imagen puede leer los metadatos por
`Health`; y el propio código que corre dentro del sandbox también, sin
ninguna credencial: `Health` es el único RPC anónimo y `rayd` lo sirve en
`0.0.0.0:8080`, así que un proceso uid 1000 del sandbox lee su `sandbox_id`
y el mapa `metadata` completo. Son etiquetas y configuración, **no
secretos**: las credenciales van por `sbx.files.write`, por `envs=` de un
comando concreto o, desde 0.5.0, por `secrets=` (desde Secrets Manager, con
caché; ver [Secretos](secrets.md)), nunca en el payload de creación. Ni `rayd` ni el SDK escriben claves ni valores de
`metadata` en logs (sólo el número de claves).

## El shim de E2B y la red

`rayito.e2b.Sandbox` reproduce los valores por defecto de E2B: endpoint
público (`ingress=["ALL_INGRESS"]`) y salida a internet
(`egress=["INTERNET_EGRESS"]`). Un MicroVM sin conector de egress en
`run-microvm` hereda el de la versión de imagen y sigue saliendo a internet
(medido, `AWS_API_NOTES.md` Q44 y Q60), así que desde 0.3.0
`allow_internet_access=False` y `network=` se aplican **dentro del guest**
en `rayito-base-caps` y, en cualquier otra imagen, el SDK termina el VM y
lanza `UnimplementedError`: nunca te devuelve un sandbox con la red abierta
([Red saliente](network.md), `SECURITY.md` T17). Para cerrar la entrada:
`ingress=["NO_INGRESS"]`; fuera del guest, el allowlist vía conector VPC con
`rayito.Sandbox(egress=[...])`.

## Cadena de suministro

`rayd` corre como root dentro de cada MicroVM, así que lo que se construye y
publica está controlado de punta a punta (detalle en `SECURITY.md` T10 y en
la tabla "Cadena de suministro"):

- La imagen base va fijada por digest en `image/Dockerfile`
  (`FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38…`) y
  cada publish pasa `--base-image-version` de forma explícita. Cuando AWS
  mueva el tag, refrescar el digest es un PR de Dependabot (`docker`) o
  `docker buildx imagetools inspect public.ecr.aws/lambda/microvms:al2023-minimal`
  (sin Docker: la receta del propio `Dockerfile` con el token de
  `public.ecr.aws` y `Docker-Content-Digest`); un digest nuevo es una versión
  de imagen nueva, publicada y aceptada con el e2e como cualquier otra.
- Dependencias auditadas en CI y cada semana: `cargo deny` (licencias,
  advisories, fuentes), `pip-audit` sobre los pins de la imagen y de los SDK,
  `pnpm audit --prod`.
- `rayd` se compila con `cargo auditable` (grafo de crates embebido) y lleva
  un SBOM CycloneDX; los assets de cada release van firmados keyless con
  cosign, y PyPI y npm publican por OIDC con attestations. Cómo comprobarlo:
  [Verificar una release](verify.md). La receta liga la firma a la versión
  exacta que instalas (`release.yml@refs/tags/rayd-v<versión>`), así que un
  asset antiguo o firmado bajo otro tag no la pasa.
- El job que compila no tiene ninguna credencial: firmar (token OIDC) y subir
  a la release (`contents: write`) son jobs aparte que no hacen checkout ni
  ejecutan herramientas de build, y comprueban con `sha256sum -c` lo que
  produjo el build. Los dos esperan la aprobación del mantenedor (environment
  `release`): sin ella no hay token OIDC ni certificado de Sigstore, y un
  ensayo no firma nada. Ningún job de la release, del sitio de documentación
  ni del e2e restaura una caché de Actions; zig, `cargo-deny` y uv se
  descargan contra un sha256 fijado; el workflow nunca reemplaza un asset
  publicado, y publicar exige que el commit del tag esté en `main`.
- Un token del repositorio con `contents: write` sí podría reemplazar un
  asset de una release (las releases inmutables siguen apagadas): por eso la
  receta de [Configurar AWS](primeros-pasos/configurar-aws.md) comprueba la
  firma y `SHA256SUMS` **antes** de `rayito image publish`, que no verifica
  nada por sí mismo.
- Las herramientas de CI con dependencias (twine, pip-audit, cfn-lint) se
  instalan desde requisitos con `--hash`, nunca con `uvx`, y `uv` nunca
  re-bloquea un `uv.lock` desfasado (`UV_LOCKED=1`).
- Dependabot espera 7 días (14 para un major) antes de proponer una versión
  recién publicada; las actualizaciones de seguridad no esperan.
- El constructor del artefacto de imagen (`rayito image zip`,
  `make image-publish`) rechaza cualquier enlace simbólico en `image/` y en
  `kernel-sidecar/`: un enlace metería en la imagen el contenido de un
  fichero de la máquina que construye.

## Persistencia en S3 (T15)

Con `Sandbox.create(persist=S3Prefix(...))` el `HOME` del usuario viaja a tu
bucket. Lo hace `rayd` como root con el execution role (que `persist=` exige
explícitamente), no el código del sandbox: en `rayito-base-caps` uid 1000
sigue sin alcanzar IMDS. El rol sólo puede escribir y leer bajo
`<bucket>/<prefix>/*` (`infra/iam.yaml`), nunca borrar. `persist=` liga
además el sandbox a su bucket y a su `prefix`: `rayd` rechaza con
`permission_denied` cualquier checkpoint o restore fuera de ellos, así que un
`prefix` por inquilino separa inquilinos aunque compartan rol. Un sandbox
creado sin `persist=` no liga nada y su access token alcanza cualquier
destino que su rol alcance: no le des un rol que llegue al prefijo de
persistencia. El restore corre con
la identidad del usuario y sólo extrae ficheros regulares, directorios y
symlinks (nada de dispositivos ni hard links), rechaza `..`, rutas absolutas y
padres que salgan del `HOME`, descarta los bits setuid/setgid/sticky y
verifica el sha256 del archivo. Quien pueda escribir bajo tu prefijo puede
plantar ficheros en el `HOME` de la siguiente encarnación: trata el prefijo
como parte del sandbox. Añade al bucket una regla
`AbortIncompleteMultipartUpload` a 1 día. Detalles en
[Persistencia](persistence.md) y en `SECURITY.md` T15.

## Custodia de secretos del usuario (T18)

`secrets=` ([Secretos](secrets.md)) está **apagado por defecto** y
sólo lo enciende la propia opción. Cuando se usa:

- **Canal**: el valor viaja sólo en los `envs` por llamada de comandos,
  PTY, celdas Python y contextos de código, por el mismo canal autenticado
  que los ficheros (TLS hasta el proxy de AWS, `x-access-token`); nunca en
  el `runHookPayload`, `metadata`, etiquetas, el entorno de la imagen, los
  registros del pool, logs, `repr` ni errores.
- **En reposo**: en Secrets Manager (KMS); en el cliente, sólo en la
  memoria del proceso del SDK (`SecretCache`, TTL 300 s por defecto: nunca
  una lectura por comando); en el VM, en el entorno de los procesos que lo
  recibieron y en el snapshot de memoria si el sandbox se suspende con
  ellos vivos (pregunta abierta SEC-5).
- **Quién lee qué**: IAM de lector en las credenciales del **llamante**
  (`infra/secrets-access.yaml`), nunca el execution role. **Fase 1: el
  código del sandbox puede leer un secreto inyectado.** Con código no
  confiable, inyecta sólo tokens de vida corta y mínimo privilegio, nunca
  credenciales de larga duración.
- **Logs**: Rayito nunca escribe el valor ni el nombre en sus logs. El log
  DEBUG del SDK de AWS que usa por debajo (botocore/urllib3 en Python, un
  `logger` del `SecretsManagerClient` en TypeScript) **sí** imprime los
  cuerpos de Secrets Manager con el valor en claro: no lo actives en
  procesos que manejan secretos ([Secretos](secrets.md#que-activa-y-que-cuesta)).
- `Secret.fill()` del shim de E2B devuelve el placeholder, pero Rayito no lo
  resuelve en ninguna parte.

Detalle en `SECURITY.md` T18.

## Copia de metadatos en reposo (T19)

Con el índice opcional de metadatos (`index=DynamoDbIndex(...)`), la
`metadata` de cada sandbox queda **en reposo** en una tabla DynamoDB de tu
cuenta (`infra/metadata-index.yaml`), cifrada por defecto y con TTL. Es la
misma `metadata` que ya no es secreta (T4: `Health` la devuelve a cualquier
principal que pueda acuñar un JWE y al propio código del sandbox), así que no
metas en ella nada que no pondrías en una etiqueta.

- **Qué se guarda**: `pk` (id del sandbox), ARN y versión de la imagen,
  `startedAt`, `metadata`, la versión del SDK y `expires_at`. Nunca el access
  token ni su hash, `envs`, secretos, el `runHookPayload` ni el JWE.
- **Integridad**: una fila falsa no crea un sandbox fantasma; el listado
  parte de `list-microvms` (de ahí sale el estado) y exige misma imagen y
  `startedAt` (±1 s). Quien pueda escribir en la tabla sí puede cambiar con
  qué metadatos aparece un sandbox real o esconderlo de un listado con
  índice: no uses la `metadata` como control de acceso.
- **IAM**: `RayitoIndexWriter` (`dynamodb:PutItem`) para quien crea
  sandboxes o corre un pool, `RayitoIndexReader` (`dynamodb:BatchGetItem`)
  para quien lista; ambas sobre el ARN de la tabla.
- **Apagarlo**: no pases `index=`; borra el stack para borrar la tabla.

Detalle en `SECURITY.md` T19.

## Eventos y webhooks (T22)

Con `events=`/`LifecycleEvents`, `rayd` emite eventos firmados por stdout y
tres Lambdas de tu cuenta los verifican, guardan y entregan a tus webhooks.

- **Sólo el MAC autentica.** El forwarder deriva la clave del sandbox que
  nombra el *log stream* y verifica el MAC antes de leer el contenido. El
  nombre del stream no prueba identidad: quien tenga el execution role
  (legible desde la imagen por defecto, T1) o el rol de build puede escribir
  en el stream de otro sandbox, pero sin su clave no pasa el MAC.
- **Sólo lo que emite `rayd`, y reciente.** Tipos estrictos, el mismo
  `sandbox_id` que el stream, campos con la forma de `rayd` y un
  `occurred_at_ms` de menos de 24 h: una línea repetida días después no
  vuelve a entregarse.
- **`paused` y `resumed` son orientativos.** El código del sandbox (uid
  1000, sin root) puede llamar por loopback a los hooks `/suspend` y
  `/resume` de `rayd` (T2), y `rayd` emite entonces un `paused`/`resumed`
  con firma válida. El forwarder sólo admite eventos que hagan avanzar el
  ciclo de vida del sandbox y limita `paused`/`resumed` a 20 seguidos y
  uno cada 30 s por sandbox; lo demás se cuenta y se descarta. No factures
  ni limpies recursos sólo por un `paused`.
- **Un sandbox no degrada a los demás.** El reconciliador consulta un
  índice con sólo los sandboxes abiertos (nunca la tabla entera) y el
  deliverer sólo se invoca por eventos nuevos.
- **Una línea o un webhook malos no bloquean a los demás.** La línea mala
  se cuenta y se descarta; un webhook con URL rota o lento falla solo. Lo
  que no se pudo escribir o entregar tras los reintentos queda en las
  colas SQS de la pila.
- **Dos firmas.** `e2b-signature` (la de E2B, byte a byte) no lleva marca
  de tiempo; `rayito-signature` es un HMAC con la hora del intento y el
  `webhook_id`. En el receptor, verifica `rayito-signature` con una
  ventana de 5 minutos, deduplica por `(sandbox_id, event_id)` y usa un
  secreto aleatorio de 32 bytes o más.
- **Webhooks de toda la pila.** Cada webhook recibe los eventos de todos
  los sandboxes de sus tipos: no des `register_webhook` a tus clientes.
- **Secretos**: los de firma de webhooks (`rayito/webhooks/`) nunca llegan
  a un sandbox (`RayitoSecretsReader` los niega y el SDK los rechaza en
  `secrets=`). Las URLs de webhook se guardan en claro en tu tabla. Una
  rotación del secreto de un webhook llega al deliverer en 5 minutos como
  mucho (antes si tu receptor responde `401`/`403`).
- **Mínimo privilegio**: cada Lambda escribe sólo sus filas y sólo en su
  propio log group. Las políticas del llamante van por tarea:
  `EventsLauncherPolicy` para `events=`, `EventsReaderPolicy` para
  `get_events` (no ve webhooks ni la clave del stack) y
  `EventsWebhookAdminPolicy` para los webhooks.

Detalle en `SECURITY.md` T22.

## Quién publica imágenes

`infra/iam.yaml` separa `SandboxLauncherPolicy` (lanzar y manejar sandboxes)
de `ImagePublisherPolicy` (publicar imágenes); `CallerPolicy` es la unión de
las dos. Al rol de un servicio en producción vincúlale sólo la del
lanzador: si lo comprometen, no puede publicar una versión con puerta
trasera de la imagen que usarán todos tus sandboxes. Ver
[IAM](operacion/iam.md).

`RayitoTemplateBuilder` (`Template.build()`) también es una política aparte
y sólo lee `rayito/*` del bucket de artefactos, nunca los checkpoints de
`HOME` ni las transferencias que compartan bucket (T26). El guardarraíles
de tamaño opcional (`sizes-guard`, T27) niega lanzar imágenes fuera de su
lista **y** publicar imágenes, para que nadie con esa política reconstruya
una imagen permitida a un tamaño mayor; una versión antigua y mayor que
siga existiendo bajo un nombre permitido sí se puede lanzar, así que
mantén cada nombre en un solo tamaño. Ver
[Tamaños](funciones-opcionales/tamanos.md).

## Montajes S3 y telemetría (T20, T23)

- `mounts=` (T20) sólo existe en `rayito-base-caps`: `mount-s3` corre como
  un usuario propio (uid 990), nunca recibe una credencial por argumento ni
  por entorno, y sólo monta buckets de `RAYITO_ALLOWED_MOUNT_BUCKETS` en
  rutas bajo `/mnt/` o `/home/user/`. El nombre del bucket y el prefijo no
  son secretos: el código del sandbox puede verlos.
- `telemetry=` con `OtlpAuth.execution_role()` (T23) da al guest las
  credenciales del execution role para `PutMetricData`, que no se puede
  acotar por namespace: un sandbox comprometido puede escribir métricas
  arbitrarias. Si no confías en el código del sandbox, usa
  `OtlpAuth.bearer(...)`.

## Qué nunca se loguea

Contenido de ficheros, código ejecutado, bytes de PTY, tokens, cabeceras del
proxy, `envs`, `metadata`, el body de los hooks, ni el mensaje crudo de un
error de AWS (un error de firma lleva la cadena canónica con el token de
sesión: el SDK, los stacks, `Template.build` y la CLI sólo muestran su
resumen saneado); tampoco URLs
prefirmadas, buckets, claves o rutas de una transferencia, metadatos de
fichero, entradas de la política de egress, destinos del proxy ni
credenciales de git, ni el JWE, las cabeceras, los cuerpos ni las rutas de
lo que pasa por `rayito sandbox proxy`, ni valores ni nombres de secretos,
ni el prompt, las instrucciones o el texto de un agente de `sbx.agent` (sus
errores llevan un `reason` cerrado y un mensaje de tabla fija, y el span
opcional `rayito.agent.run` sólo atributos `gen_ai.*` y de tokens). Sólo
ids, códigos de estado, recuentos y duraciones.

<a id="rayito-sandbox-proxy-m12"></a>

## `rayito sandbox proxy`

El proxy local (`rayito sandbox proxy <id> --port N`, [CLI](cli.md#proxy))
acuña el mismo JWE que usa el canal gRPC del SDK, pero con alcance a **un
solo puerto** (`PortSpec.single(N)`, nunca `allPorts`) y **nunca al 9000**
de los lifecycle hooks (ADR-006): `validate_proxy_port` lo rechaza antes de
tocar AWS. El listener se enlaza a `127.0.0.1` por defecto; `--bind` fuera
de loopback exige `--allow-remote` y avisa por stderr. Quita cualquier
cabecera `x-aws-proxy-*` que traiga el cliente antes de reenviar la
petición, así que un cliente local no puede suplantar la autenticación del
proxy de AWS.

**Webs abiertas en el navegador del operador**: como el proxy añade el JWE a
todo lo que reenvía y el guest sólo ve `Host: <endpoint>`, una web
cualquiera podía usar el servicio del sandbox por DNS rebinding, con un POST
entre sitios o con un `WebSocket` de otro origen (con Jupyter, code-server o
una terminal web, eso es ejecutar código en el sandbox). Desde 0.7.0 el
proxy sólo reenvía peticiones cuyo `Host` es de loopback con el puerto
local, la dirección de `--bind`, `<id>.localhost` o un `--allowed-host`
(si no, `421`) y cuyo `Origin`, si lo trae, es uno de esos orígenes o un
`--allow-origin` (si no, `403`); como mucho `--max-connections` (8)
conexiones a la vez (`503` después). Por cada conexión sólo reenvía **un
mensaje**: la primera petición reescrita, con su cuerpo delimitado por
`Content-Length` o `chunked` (una delimitación ambigua es `400`); nada de lo
que el cliente mande después llega al upstream, y un upgrade que el guest
no acepta con `101` se responde con `Connection: close` y se cierra, en vez
de quedar como túnel sin filtrar. **Cookies**: no se separan por puerto,
así que lo que el sandbox sirve en `http://127.0.0.1:<puerto>` recibe las
cookies de tus otras apps locales y comparte "sitio" con ellas; ábrelo en
`http://<id>.localhost:<puerto>` (tarro propio, la CLI lo anuncia) o en un
perfil de navegador dedicado ([Proxy local](funciones-opcionales/proxy-local.md#navegador-y-cookies)).

**Las dos CLI, la misma garantía**: el proxy de la CLI de TypeScript
(`npx rayito sandbox proxy`) ejecuta los mismos vectores que el de Python
(`testdata/cli/proxy-vectors.json`: cabeceras con CR o LF suelto, `obs-fold`,
nombres fuera de los `tchar`, delimitación del cuerpo, `Host`/`Origin`,
puertos) y comparten los topes y plazos de `limits.json`. Los espacios que se
recortan alrededor de un valor de cabecera son sólo `OWS` (espacio y
tabulador), no todo lo que un lenguaje llame espacio en blanco: un
`Content-Length: 12` seguido de un `\x85` es inválido en las dos. Un `Content-Length` o un tamaño de trozo que no cabe en un
entero seguro de JavaScript es `400` en la CLI de TypeScript.

Riesgo residual (`SECURITY.md` T2/T3): mientras el proxy está en marcha,
cualquier proceso que alcance el puerto local reenviado —de la máquina del
operador, o de otra si se usó `--allow-remote`— tiene el mismo acceso al
sandbox que el operador. Un sandbox `SUSPENDED` con auto-resume se despierta con la
primera petición que le llega (factura cómputo, como cualquier reanudación).

## Panel de gestión local (`rayito dashboard`) { #panel-de-gestion-local }

`rayito dashboard` ([guía](funciones-opcionales/panel-de-gestion.md#panel-web-local))
sirve una página y una API JSON en `127.0.0.1` con **tus** credenciales de
AWS. Cualquier web que abras en el mismo navegador podría intentar usarlas,
así que el servidor acota el cuerpo (64 KiB como máximo, `413` si lo supera) y
aplica después, en este orden y antes de interpretarlo:

- **`Host` y `Origin`**: la misma lista que el proxy local
  (`127.0.0.1`, `localhost` y `[::1]` con el puerto del panel). Un `Host`
  ajeno es `421` (DNS rebinding); un `Origin` presente y ajeno, incluido
  `null`, es `403`.
- **Sesión**: la URL de arranque lleva un token de un solo uso (con
  caducidad) que el servidor cambia por una cookie `HttpOnly`,
  `SameSite=Strict`, `Path=/` y redirige a la URL limpia con
  `Referrer-Policy: no-referrer`. El nombre de la cookie incluye el puerto,
  porque las cookies no se aíslan por puerto. Sin la cookie no responde
  nada, ni la página ni los ficheros estáticos.
- **`Sec-Fetch-Site`**: en `/api/v1`, si el navegador manda la cabecera y no
  vale `same-origin` ni `none`, la petición es `403`. Cubre lo que `Origin`
  no ve: un `<img>` o una navegación desde la web que sirve un sandbox con
  `rayito sandbox proxy` en `127.0.0.1:<otro puerto>` es `same-site`,
  comparte el tarro de cookies y no manda `Origin`. Además, el proxy no
  reenvía al sandbox las cookies `rayito_dashboard_*`.
- **CSRF**: todo `POST` exige `Content-Type: application/json` (una web
  ajena no lo puede mandar sin preflight) y la cabecera `X-Rayito-CSRF` con
  el token de la sesión, que sólo se entrega a quien ya tiene la cookie. La
  consulta de coste facturado (`GET /api/v1/cost?billed=true`) también exige
  el token, porque aunque sea un `GET` cuesta dinero cada vez.
- **Sólo lectura por defecto**: sin `--allow-actions` el servidor rechaza
  cualquier acción con `403` antes de tocar AWS. Con la opción, cada acción
  exige `"confirm": true`, se valida (ids, tope por petición) y entra en una
  cola con un solo trabajador que respeta el TPS.
- **Navegador**: CSP sin `unsafe-inline` ni `unsafe-eval` (la página son
  módulos y hojas de estilo del propio origen), `nosniff`, `no-store`,
  `X-Frame-Options: DENY` y `Cross-Origin-Resource-Policy: same-origin`. La
  página escribe todo con `textContent`: ningún dato de AWS ni ninguna línea
  de log se interpreta como HTML.
- **Tema y logo de usuario**: se validan antes de arrancar (claves cerradas,
  valores de color, longitud y fuente con gramática estricta, SVG sin código
  ni referencias externas), el CSS lo genera el panel desde los valores
  validados y se sirve del mismo origen bajo la misma CSP; el logo, además,
  con una CSP propia (`default-src 'none'`, `sandbox`).
- **Qué no sale**: ni tokens, JWE, `envs` o `runHookPayload` (no existen en
  el modelo), ni el mensaje de AWS en los errores (sólo su código), ni la
  URL con el token en ningún log. El servidor no registra peticiones.
- **Coste**: el coste facturado (Cost Explorer, de pago) sólo se consulta
  con `--cost-explorer`, al pulsar el botón y con el token CSRF.

Riesgo residual: mientras el panel corre, cualquier proceso **de tu máquina**
que lea la URL impresa (por ejemplo desde el historial del terminal antes de
gastarla) o que obtenga la cookie tiene el mismo acceso que tú; el token se
gasta en el primer uso. Con `--allow-actions` eso incluye matar sandboxes.
Los logs de un sandbox son lo que ese sandbox escribió y se muestran tal
cual (como en `rayito sandbox logs`): si tu código imprime secretos, el panel
los mostrará.

## Panel de gestión, fase 3: varias cuentas y avisos de cuota { #panel-fase-3 }

Resumen de **T33** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md);
la [guía](funciones-opcionales/panel-extras.md) tiene el uso.

- **Roles de lectura, no de acciones.** `rayito dashboard --account` asume el
  rol que creaste con `dashboard-read-role` en la cuenta destino: sólo cinco
  lecturas (MicroVMs, imágenes, versiones y cuotas), nada de escritura, ni
  Cost Explorer ni logs. Confía sólo en el rol o usuario que indicas (nunca en
  una raíz de cuenta) y, con `ExternalId`, sólo si se presenta.
- **Siempre de sólo lectura.** Las cuentas adicionales no tienen acciones
  aunque arranques con `--allow-actions`; un `?account=` desconocido es un `400`
  sin llamada a AWS.
- **Sin ARN en la página.** La API y los mensajes sólo conocen la etiqueta que
  elegiste; un rechazo de STS se muestra con su código y sin su mensaje.
- **Sesión corta y visible.** La sesión asumida no se pide hasta usarla, dura
  {{dashboardAssumeRoleSessionSeconds}} s y deja `rayito-dashboard` como nombre
  de sesión en CloudTrail de la cuenta destino.
- **Avisos de cuota**: la función sólo lee y escribe en su tema, su fila de
  estado y su espacio de nombres de métricas; el mensaje no lleva ids ni
  nombres, y un fallo de SNS no se da por enviado.
- **Dashboard de CloudWatch**: el prefijo y los grupos se validan contra una
  lista cerrada de caracteres antes de entrar en una consulta.

## Panel de gestión en tu cuenta (la pila `dashboard`) { #panel-de-gestion-en-tu-cuenta }

La pila `dashboard` ([guía](funciones-opcionales/panel-en-tu-cuenta.md)) publica
una Function URL sin autenticación de AWS (`AuthType NONE`, porque un navegador
no firma con SigV4), así que **cualquiera en Internet puede llamarla**: la
protección es que la función valida la identidad antes de hacer nada. Es el
resumen de **T32** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).

- **Identidad antes que nada.** Cada petición a `/api/v1` lleva un token de
  acceso de Cognito en `Authorization`. Antes de enrutar o de llamar a AWS, la
  función comprueba el formato y el tamaño, que el algoritmo sea `RS256` (`none`
  y los demás se rechazan), la firma contra el JWKS del grupo (con la
  biblioteca estándar, comparando el mensaje codificado entero), el emisor,
  `token_use=access`, el `client_id`, la caducidad, `nbf` y `sub`. Sin token,
  `401`. Si Cognito no se puede consultar, `503`, nunca «pasa». Sólo la página
  estática y `/config.json` (el dominio del Hosted UI y el id del cliente,
  públicos) no piden token.
- **Dos roles.** La función corre con un rol de **sólo lectura**. Matar,
  suspender y reanudar los hace un **rol de acciones** que sólo confía en el rol
  de la función, que lo asume **después** de comprobar el grupo del usuario y
  con su `sub` en el nombre de la sesión. Sin `AllowActions=true` ese rol no
  existe.
- **Auditoría que no se puede reescribir desde la aplicación.** Una fila por
  sandbox y acción, escrita **antes** de ejecutar (si falla, la acción no
  corre), con quién, qué, cuándo, id de petición e IP. Escrituras condicionales
  y sin `UpdateItem` ni `DeleteItem` (la plantilla los deniega).
- **Sin sesión ambiente.** El token va en la cabecera, en memoria, no en una
  cookie: una web ajena no tiene nada que reutilizar. Cada `POST` comprueba
  `Origin` y `application/json`, y la URL no habilita CORS.
- **Tema y logo**: los mismos validadores que el panel local; uno inválido se
  descarta y se sirve el de fábrica.
- **Coste**: el coste facturado sólo con `EnableCostExplorer=true`. La caché de
  24 h en DynamoDB no basta (su clave lleva la ventana y la región, que elige
  quien pregunta), así que la función acepta sólo ventanas de meses completos
  de los últimos {{dashboardStackCostLookbackMonths}} meses y regiones conocidas,
  y gasta un presupuesto de {{dashboardStackCostMissesPerDay}} consultas de pago
  por día UTC para toda la pila (no por usuario) que, agotado o sin poder
  comprobarse, deja de facturar. `ReservedConcurrency` acota una avalancha de
  peticiones sin autenticar.
- **Acciones con identidad en vivo**: antes de asumir el rol de acciones, la
  función comprueba en Cognito que el usuario sigue habilitado y en el grupo;
  si Cognito no responde, no se actúa.
- **Nada secreto en logs**: ni el token, ni `Authorization`, ni el cuerpo.

Riesgo residual: un token de acceso ya emitido no se puede revocar (la función
lo valida sin conexión): vale hasta que caduca ({{dashboardStackAccessTokenHours}}
hora) aunque desactives al usuario, lo quites del grupo o cierres sus sesiones
(`admin-user-global-sign-out` sólo impide renovarlo). Hasta entonces puede
leer, pero no actuar, porque cada acción se comprueba en Cognito; quien
administra la cuenta puede cambiar la tabla de auditoría; y la dirección es
pública, así que lo que la protege es la validación del token, no la red.

## Agente de código dentro del sandbox

Resumen de **T29** y **T30** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`ai-agent-core`), el modelo de amenazas de
[`sbx.agent`](guias/agente-en-el-sandbox.md).

**El agente en sí mismo (T29).** `opencode run --auto` responde "sí" a
cualquier permiso que el modelo pida, y el backend por defecto de
deepagents (`LocalShellBackend`) ejecuta lo que el modelo pida sin
preguntar: **los permisos de `AgentPermissions`/`AgentSpec` no son una
frontera de seguridad**, son una preferencia que un modelo que alucine, o
un *prompt injection* desde un fichero del propio workdir (un `AGENTS.md`
o un repositorio clonado con instrucciones ocultas), puede saltarse pidiendo
otra herramienta o reformulando el pedido. La frontera real es la que ya
protege cualquier otro código que corra dentro del sandbox: el MicroVM y,
el egress cerrado, que exige pasar `allow_internet_access=False` de forma
explícita (`Sandbox.create` lo deja abierto por defecto; las recetas de la
guía lo pasan). La exfiltración sólo puede salir
por el `upstream` que la pasarela de secretos permite, nunca directamente.
**Riesgo residual**: código dentro del sandbox puede llamar a la propia
pasarela por su cuenta, fuera del presupuesto de tokens del SDK (no hay
forma de distinguir, desde la pasarela, una llamada que hizo el runtime del
agente de una que hizo el modelo pidiéndole ejecutar `curl`). Mitigaciones:
restringir `allow` a los `(método, ruta)` de los modelos exactos que uses
(`bedrock_gateway` nunca abre `/model/*`; `gemini_gateway` sólo abre los
`models` dados) y `rate_per_minute`. En las APIs al estilo de OpenAI
(OpenAI, Azure OpenAI, xAI, OpenRouter, Groq, Mistral, DeepSeek, LiteLLM)
el modelo va en el cuerpo y la pasarela **no puede** limitarlo: fija el
tope de gasto en el proveedor (ver
[Proveedores del agente](guias/agente-proveedores.md#limites-del-lado-del-proveedor)).
Las suscripciones de consumo (plan de ChatGPT, Claude Pro/Max, Copilot,
SuperGrok) no se admiten: ver
[Proveedores no admitidos](guias/agente-proveedores.md#proveedores-no-admitidos).

**Cadena de suministro del runtime (T30).** OpenCode y ripgrep se instalan
por versión y sha256 fijados en `limits.json`, que el build de la plantilla
comprueba con `sha256sum -c` antes de instalar;
el venv de deepagents se instala con `--require-hashes`; el sha256 del
runner de deepagents (`runner_sha256`) queda registrado en el manifiesto
del template (`/opt/agents/rayito-agent.json`); `AgentSpec.runtime_version`
hoy sólo se valida como texto y **no** se compara con ese manifiesto. Autoupdate, la descarga de
modelos, de LSPs, los plugins por defecto y la lectura de un `.claude/` del
workdir están apagados (`autoupdate: false`, `share: "disabled"`, `OPENCODE_DISABLE_CLAUDE_CODE=1`); nada de eso
sale a buscar algo a Internet dentro de un sandbox con egress cerrado, y
si lo intentara, fallaría igual que cualquier otra conexión saliente no
permitida.

## AgentCore: sin pasarela de secretos (T36) { #agentcore }

Resumen de **T36** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`agentcore-deploy`, desde 0.15.0): el modelo de amenazas
de [Desplegar en AgentCore](guias/desplegar-en-agentcore.md).

**La diferencia.** En un MicroVM de Rayito el agente nunca ve la credencial del
modelo: la pasarela de secretos la añade al reenviar la petición. En AgentCore
no hay pasarela de secretos de Rayito: el agente obtiene las credenciales del rol
del runtime por el servicio de metadatos de la microVM, y **cualquier código que
corra en la sesión puede leerlas**: el tuyo, el del framework y una herramienta
que el modelo decida ejecutar. La garantía de «el agente no puede leer la
credencial» no existe aquí; lo que la sustituye es el mínimo privilegio.

**Qué puede hacer el rol del runtime** (el que crea `infra/agentcore-runtime.yaml`,
uno por agente, que sólo AgentCore puede asumir en tu cuenta y tu región):

- extraer **su** imagen de ECR y escribir en **su** grupo de logs
  (`/aws/bedrock-agentcore/runtimes/<runtime>-*`);
- enviar trazas a X-Ray y métricas al espacio de nombres `bedrock-agentcore`;
- obtener tokens de identidad de carga de trabajo (`GetWorkloadAccessToken` y
  `GetWorkloadAccessTokenForJWT`) sobre el directorio y las identidades de su
  runtime, **sin** `GetWorkloadAccessTokenForUserId`;
- invocar en Bedrock (`InvokeModel` e `InvokeModelWithResponseStream`) **sólo**
  los ARNs de `--bedrock-model` (por defecto, el modelo del proyecto; sin
  modelo, ningún permiso de Bedrock);
- leer (`secretsmanager:GetSecretValue`) **sólo** los secretos de
  `--readable-secret` (por defecto, ninguno).

No puede nada más. Lo que cree el agente con esas credenciales, como llamar a
otro modelo o leer otro secreto, lo deniega IAM.

**Lo que no cambia con el rol.**

- **Red `PUBLIC`.** El runtime tiene salida a Internet sin filtro: la política de
  salida de Rayito (`egress`, `network`) no se aplica, y por eso un proyecto que
  la declara no se despliega (`network_policy_unsupported`). Un código que lea las
  credenciales del rol puede enviarlas a donde quiera; si no quieres ese riesgo,
  no des al rol nada que no puedas perder.
- **MMDSv2 siempre activo.** AgentCore exige la versión 2 del servicio de
  metadatos (`requireMMDSV2`) desde el 2026-06-30 y rechaza invocar un runtime
  sin ella. Rayito la activa justo después de crear el runtime y en cada
  actualización.
- **Sin secretos en el runtime.** Las variables de entorno del runtime se leen
  con `GetAgentRuntime`: Rayito nunca copia valores de secretos allí, ni la clave
  de un proveedor que no sea Bedrock (`provider_unsupported`). El agente lee lo
  que necesite con el rol, en ejecución.
- **Sesiones y usuarios.** Cada sesión corre en su propia microVM, aislada de las
  demás, pero AgentCore no asocia sesiones a usuarios: quién puede continuar qué
  sesión (qué `session_id` puede usar cada persona) lo decide tu backend.
- **Autenticación.** Invocar un runtime exige `bedrock-agentcore:InvokeAgentRuntime`
  y firma SigV4: no hay access token ni `RAYITO_AGENT_SERVICE_TOKEN` (no lo fijes
  en este destino).

**El build.** El zip del contexto se sube a `rayito/images/agentcore/<nombre>/` del
bucket de artefactos, se borra en cuanto acaba el build (con éxito o no) y, si
queda alguno, el bucket de `rayito setup` lo caduca a los 7 días. El rol de
CodeBuild sólo lee ese prefijo, sólo escribe en su grupo de logs y sólo sube
imágenes a su repositorio, que tiene etiquetas inmutables y análisis al subir.
La imagen base va fijada por digest.

**Qué borra `rayito agent delete`.** Sólo lo que creó Rayito para ese nombre: un
runtime que no tiene la marca `rayito.agentcore/1` en su descripción nunca se toca
(`name_conflict`).

**Riesgo residual.** Todo lo que el rol permita, cualquier código de la sesión
puede usarlo; una organización con SCP que exigen etiquetas necesita `--tag`, y
las etiquetas de un runtime ya creado no se actualizan.

**Modelo fuera de Bedrock (`--external-model`).** Es opt-in: sin la opción, un
proveedor que no es `bedrock` sigue siendo `provider_unsupported`. Con ella, el
agente llama a su pasarela de LLM por la red `PUBLIC` con una clave que obtiene
su código (`--readable-secret` o `--secret-env`): el rol no recibe permisos de
Bedrock que no se pidan, la clave nunca va a las variables de entorno del
runtime (sólo la URL base, que no es secreta) y Rayito no la ve ni filtra esa
salida. El plan lo dice con la línea `externalModel`.

## Constructos CDK: roles de los recursos personalizados y del build de la imagen (T37) { #cdk }

Resumen de **T37** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`agentcore-cdk`, ADR-032, desde 0.15.0): lo que añaden
los constructos de [Desplegar con CDK](guias/desplegar-con-cdk.md) a lo que ya
crea la CLI.

**El rol del agente no cambia.** `RayitoAgent` despliega la misma plantilla
`infra/agentcore-runtime.yaml` que `rayito agent deploy`, así que el rol del
runtime es el mismo, con el mismo mínimo privilegio de [AgentCore](#agentcore).
Lo nuevo son dos piezas que la CLI hacía con las credenciales de quien
despliega y que aquí hace CloudFormation:

- **Las funciones de los recursos personalizados** (`Custom::RayitoImageBuild` y
  `Custom::RayitoRuntimeMmds`, código en `infra/agentcore-cdk-handler.py`). Su
  rol sólo puede `codebuild:StartBuild` y `BatchGetBuilds` sobre **el** proyecto
  de CodeBuild de ese agente, `ecr:DescribeImages` sobre **su** repositorio,
  `bedrock-agentcore:GetAgentRuntime` y `UpdateAgentRuntime` sobre **su**
  runtime, e `iam:PassRole` **sólo** sobre el rol del runtime (lo exige
  `UpdateAgentRuntime`). No puede crear ni borrar runtimes, ni pasar otro rol, ni
  leer imágenes, secretos ni modelos.
- **El rol de build de la imagen de MicroVMs** (`RayitoMicrovmImage`). Sólo lo
  puede asumir Lambda en tu cuenta (`aws:SourceAccount`), lee **sólo** el objeto
  del asset del zip y escribe **sólo** en los grupos de logs `/rayito/<nombre>` de
  sus imágenes. No usa el rol compartido de `rayito-m0-iam`.

**El asset del código.** El rol de build de CodeBuild del agente puede leer,
además de su prefijo de siempre, el objeto exacto del asset del código en el
bucket del bootstrap de CDK; nada más del bucket.

**Quién puede cambiar qué.** El runtime lleva la marca `rayito.agentcore/1+cdk`
y la imagen la etiqueta `rayito:managed-by=rayito-cdk`: `rayito agent deploy`,
`rayito agent delete` y `rayito image publish` se niegan a modificarlos antes de
escribir nada (ni siquiera el artefacto en S3), así que la CLI no puede deshacer
en silencio lo que declara la pila.

**Riesgo residual.** El despliegue lo hace el rol de ejecución del bootstrap de
CDK, que por defecto tiene `AdministratorAccess`: quien puede ejecutar
`cdk deploy` en la cuenta puede cambiar el rol del runtime. Si eso no vale,
restringe el bootstrap ([Bootstrap restringido](guias/desplegar-con-cdk.md#bootstrap-restringido)).
El recurso de CloudFormation no tiene la propiedad de MMDSv2, pero AgentCore ya
crea cada versión con MMDSv2 activo; `Mmds` lo comprueba en cada versión nueva
y sólo lo activaría fuera de CloudFormation (y la detección de deriva podría
marcarlo) si alguna llegara sin él. Los assets (el código del agente, el zip de la imagen) se
quedan en el bucket del bootstrap hasta `cdk gc`.

## Secretos como entorno (T38) { #secretos-como-entorno }

Resumen de **T38** de
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(`agent-secret-env`, ADR-033 D4): lo que cambia cuando un agente recibe valores
de Secrets Manager como variables de entorno ([guía](guias/secretos-como-entorno.md)).

**Sólo nombres en el proyecto, en el plan y en la imagen.** `[secret_env]` y
`[[secret_env_from]]` llevan el nombre de la variable y el nombre o ARN del
secreto, nunca el valor. En AgentCore el runtime sólo recibe
`RAYITO_SECRET_ENV` (nombres y ARNs) y el host la borra de su entorno al
arrancar; en MicroVMs los valores viajan sólo en el entorno del proceso del
host, nunca en un fichero ni en la imagen. Ningún log, error, plan ni `repr`
lleva un valor ni el nombre de un secreto: el host sólo registra
`secret_env_loaded count=N`, falla en cerrado con un código (`secret_env_*`) y
redacta del diagnóstico de arranque los valores inyectados, también los cortos.

**Mínimo privilegio.** En AgentCore los secretos referenciados se suman a los de
`--readable-secret` en la misma sentencia `ReadSecrets`: el rol sólo puede
`secretsmanager:GetSecretValue` sobre esos ARNs (con el sufijo de seis
caracteres de Secrets Manager), sin `kms:Decrypt`, sin `ListSecrets` y sin
`secretsmanager:*`; un test falla si se amplía. Un ARN de otra cuenta o región
es `iam_resource_invalid`. En MicroVMs el SDK los lee con tus credenciales.

**Nombres reservados.** Un secreto no puede fijar `PATH`, `HOME`, `PYTHONPATH`,
`NODE_OPTIONS`, nada que empiece por `AWS_`, `LD_` o `RAYITO_`, ni las variables
que el runtime ya pone: se rechaza en el plan y otra vez en el host.

**Riesgo residual.** Una variable de entorno la puede leer todo el código del
agente: el proceso y lo que lance (`/proc/self/environ`, un volcado). En
AgentCore, además, cualquier código de la sesión puede pedir las credenciales
del rol al servicio de metadatos y leer esos secretos. En MicroVMs, quien tenga
el token de acceso del sandbox ve el entorno del proceso con `commands.list()`,
y una pausa guarda los valores en la instantánea de memoria. Para la clave de un
modelo en MicroVMs, la [pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md)
sigue siendo mejor: el agente la usa sin poder leerla.
