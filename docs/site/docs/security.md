# Seguridad

Resumen de [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
(el modelo de amenazas completo; los identificadores T*n* de esta página son
sus filas). Tres principales: el operador del SDK (confianza
total), el código que corre dentro del sandbox (**ninguna**) y AWS (proxy,
hooks, snapshot).

## Lo que protege el SDK por defecto

| Amenaza | Mitigación |
|---|---|
| El código del sandbox lee las credenciales del execution role por IMDS | `create(execution_role_arn=None)` **por defecto**: sin rol no hay credenciales dentro (ni logs de runtime). Con rol, mínimo privilegio |
| Robo del JWE del proxy | Todo RPC salvo `Health` exige además `x-access-token`; TTL 60 min; el SDK nunca loguea cabeceras |
| El sha256 del secreto viaja en `runHookPayload` (CloudTrail) | Sólo el hash sale del cliente; `rayd` compara en tiempo constante y nunca escribe el body de `/run` |
| Escalada a root dentro del guest | Procesos, PTYs y kernels como uid 1000; root sólo con `user="root"` **y** `RAYITO_ALLOW_ROOT=1` en la imagen |
| Agotamiento de recursos desde el sandbox | rlimits, grupos de procesos, timeouts de servidor, canales de salida acotados, máx. 256 procesos/PTYs, máx. 8 kernels; en el cliente, la salida guardada de un comando o una PTY está acotada a 64 MiB por descriptor (`max_output_bytes`/`maxOutputBytes`, `truncated` lo indica) |
| Credenciales de git en el sandbox | `clone`/`push`/`pull` con `username`/`password` corren sin hooks ni credential helpers, se niegan si la configuración de git reescribe URLs y siempre intentan quitar el token de `.git/config` al acabar; **riesgo residual**: el código del sandbox con el mismo usuario puede capturarlo igualmente: trátalo como revelado y usa tokens de vida corta y de un solo repositorio ([Git](git.md#credenciales)) |
| Contenido de terceros o secretos en un build de template | los enlaces simbólicos dentro de un directorio copiado nunca se siguen (como Docker), así que un enlace a un fichero de tu máquina no acaba en el artefacto ni en la imagen; `.dockerignore` sigue la semántica de Docker (`**/.env` excluye también el `.env` de la raíz) y el SDK avisa si va a empaquetar `.env`, `.git/`, `.aws/`, `.ssh/` o claves `*.pem`/`*.key`, que cualquier código del sandbox podría leer ([Templates](funciones-opcionales/templates.md#contexto-de-build)) |
| Logs de CloudWatch o de build con secuencias de escape | la CLI muestra los caracteres de control como `\xNN` visibles cuando escribe en una terminal; con un execution role, el código de un sandbox puede escribir en cualquier stream de `/rayito/*`, así que los logs de runtime no prueban integridad |
| `rayd` (root) como *confused deputy* en el filesystem | lista de denegación sobre rutas canónicas, `setfsuid` del usuario en cada operación, `O_NOFOLLOW`, sin `..` |
| Estado clonado del snapshot compartido entre sandboxes | nada único antes de `/ready`; `/run` reinicia el kernel por defecto; `/resume` reseed de `random`/`numpy.random` |
| Exfiltración por red saliente | `egress=` explícito en `create()`; allowlist vía conector VPC (fuera del guest); desde 0.3.0, `network=` / `allow_internet_access=False` aplicados dentro del guest en `rayito-base-caps` (rutas por uid + proxy local, [Red saliente](network.md)); en otra imagen fallan cerrados |
| URLs prefirmadas y SSRF de `rayd` (T16) | las firmas el SDK con tus credenciales, `rayd` no guarda ninguna; nunca se loguean; cada URL cubre una clave ligada al sandbox, un método y una caducidad; `rayd` sólo acepta `https` al host regional exacto del bucket y su resolvedor descarta loopback, link-local e IMDS ([Ficheros](files.md)) |
| Proxy de egress de `rayd` como SSRF (T17) | guardia después de resolver (loopback, IMDS, las direcciones propias del guest), el proxy no resuelve nombres denegados, credenciales del proxy del operador sólo por RPC y nunca en logs; bajo deny-all en `rayito-base-caps`, desde 0.3.2 el DNS de uid ≥ 1000 también se bloquea (una regla `ip rule` del puerto 53 por delante de los resolvedores de la plataforma, que escuchan dentro del guest), así que ni las conexiones ni las consultas DNS de los procesos del sandbox salen del VM; **riesgo residual**: las capas del guest son de mejor esfuerzo y no resisten a root en el guest ni a un exploit del kernel: el conector VPC sigue siendo el control duro de plataforma |
| Secretos inyectados (T18) | apagado por defecto; con `secrets=` el valor viaja sólo en los `envs` por llamada (nunca en `runHookPayload`, `metadata`, logs ni errores) y se cachea sólo en la memoria del SDK; **riesgo residual**: el código del sandbox puede leerlo (fase 1) y queda en el snapshot si se suspende ([Secretos](secrets.md)) |
| Índice de metadatos (T19) | apagado por defecto; con `index=DynamoDbIndex(...)` se copia en tu tabla DynamoDB sólo la `metadata` (no secreta) más la imagen, `startedAt` y el TTL, nunca tokens, `envs` ni secretos; una fila falsa nunca crea un sandbox fantasma (el listado parte de `list-microvms` y exige misma imagen y `startedAt`); rol escritor (`PutItem`) separado del lector (`BatchGetItem`) ([Índice de metadatos](funciones-opcionales/indice-de-metadatos.md)) |

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
  a la release (`contents: write`, con la aprobación del mantenedor) son jobs
  aparte que no hacen checkout ni ejecutan herramientas de build, y comprueban
  con `sha256sum -c` lo que produjo el build. Ningún job de la release
  restaura una caché de Actions, zig y `cargo-deny` se descargan contra un
  sha256 fijado, un asset publicado no se reemplaza nunca, y publicar exige
  que el commit del tag esté en `main`.
- Dependabot espera 7 días (14 para un major) antes de proponer una versión
  recién publicada; las actualizaciones de seguridad no esperan.

## Persistencia en S3 (T15)

Con `Sandbox.create(persist=S3Prefix(...))` el `HOME` del usuario viaja a tu
bucket. Lo hace `rayd` como root con el execution role (que `persist=` exige
explícitamente), no el código del sandbox: en `rayito-base-caps` uid 1000
sigue sin alcanzar IMDS. El rol sólo puede escribir y leer bajo
`<bucket>/<prefix>/*` (`infra/iam.yaml`), nunca borrar; ese prefijo **no
separa inquilinos**: `rayd` no liga el destino al sandbox que lo pide, así
que quien tenga el access token de un sandbox alcanza cualquier `name` bajo
el mismo prefijo. El restore corre con
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

## Qué nunca se loguea

Contenido de ficheros, código ejecutado, bytes de PTY, tokens, cabeceras del
proxy, `envs`, `metadata`, el body de los hooks, ni el mensaje crudo de un
error de AWS (un error de firma lleva la cadena canónica con el token de
sesión: el SDK, los stacks, `Template.build` y la CLI sólo muestran su
resumen saneado); tampoco URLs
prefirmadas, buckets, claves o rutas de una transferencia, metadatos de
fichero, entradas de la política de egress, destinos del proxy ni
credenciales de git, ni el JWE, las cabeceras, los cuerpos ni las rutas de
lo que pasa por `rayito sandbox proxy`, ni valores ni nombres de secretos. Sólo ids, códigos de
estado, recuentos y duraciones.

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
una terminal web, eso es ejecutar código en el sandbox). Desde 0.6.x el
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

Riesgo residual (`SECURITY.md` T2/T3): mientras el proxy está en marcha,
cualquier proceso que alcance el puerto local reenviado —de la máquina del
operador, o de otra si se usó `--allow-remote`— tiene el mismo acceso al
sandbox que el operador. Un sandbox `SUSPENDED` con auto-resume se despierta con la
primera petición que le llega (factura cómputo, como cualquier reanudación).
