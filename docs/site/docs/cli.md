# CLI

`rayito` es la herramienta de línea de comandos del SDK: publica y lista la
imagen, borra versiones antiguas, lista, inspecciona y mata sandboxes, lee
sus logs de CloudWatch y diagnostica una cuenta antes del primer
`Sandbox.create()`. También despliega las pilas opcionales
(`rayito stack`, `rayito events`, `rayito domain`), construye templates
declarativos (`rayito template`) y la imagen del agente de IA
(`rayito agent template build`). No hay `rayito.toml`: la configuración son
opciones y variables de entorno.

| Grupo | Comandos |
|---|---|
| [`rayito image`](#rayito-image) | `publish`, `list`, `sizes`, `prune`, `zip` |
| [`rayito sandbox`](#rayito-sandbox) | `list`, `info`, `kill`, `logs`, `create`, `connect`, `proxy`, `exec`, `metrics` |
| [`rayito stack`](#rayito-stack) | `list`, `status`, `deploy`, `destroy` |
| [`rayito domain`](#rayito-domain) (experimental) | `deploy`, `status`, `destroy` |
| [`rayito events`](#rayito-events) | `deploy`, `status`, `destroy`, `list`, `webhook add`, `webhook list`, `webhook remove` |
| [`rayito template`](#rayito-template) | `build`, `status`, `logs` |
| [`rayito agent`](#rayito-agent) | `template build` |
| [`rayito doctor`](#rayito-doctor) | — |

El servidor MCP es otro ejecutable del mismo paquete, `rayito-mcp`
([Servidor MCP](mcp.md)).

## Instalación

La CLI es un extra del paquete `rayito` (añade `typer`):

```bash
pip install "rayito[cli]"        # o: uv pip install "rayito[cli]"
rayito --help
```

Desde un checkout del repositorio, sin instalar nada:

```bash
uv run --project clients/python rayito --help
```

Sin el extra, `rayito` imprime cómo instalarlo y sale con 2.

## Opciones globales y credenciales

```text
rayito [--profile P] [--region R] [--json] [--verbose] <grupo> <comando> …
```

- `--profile` y `--region` construyen la sesión de `boto3`; sin ellos se usa
  `AWS_REGION` y, si falta, la cadena habitual (`AWS_PROFILE`,
  `AWS_DEFAULT_REGION`, el fichero de configuración, el rol de la máquina).
  No hay API key de Rayito.
- Sin región resoluble: `sin región: pasa --region o exporta AWS_REGION` y
  salida 2, antes de cualquier llamada. Sin credenciales o con un perfil
  desconocido, lo mismo.
- `--json`: cada comando escribe **un único documento JSON** por stdout y el
  progreso por stderr, así `rayito --json sandbox list | jq` funciona.
- `--verbose`: logs `INFO` del SDK (reconexiones, sondas) y de la CLI.

Códigos de salida: `0` éxito; `1` la operación falló (build no lanzable, una
versión que `prune` no pudo borrar, un id desconocido en `kill`, un `FAIL`
del `doctor`, un error de AWS impreso como `AWS error <Code>: <Message>`);
`2` uso o entorno (argumentos, sin región, sin credenciales, sin extra).

## `rayito image`

### `image publish`

```bash
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 \
    --bucket <bucket> [--variant full|slim|poly] [--image-name N] \
    [--os-capabilities ALL] [--build-role-arn ARN | --stack-name rayito-m0-iam] \
    [--memory-mib 2048] [--timeout-seconds 1800] [--force] \
    [--sizes 512mb,1gb,4gb,8gb] [--env K=V]... [--with-efs]
```

`--with-efs` publica la imagen con `amazon-efs-utils` que necesita
`volumes=` ([Volúmenes EFS](funciones-opcionales/volumenes-efs.md)): exige
un zip hecho con `rayito image zip --with-efs`, `--os-capabilities ALL` (`rayd`
sólo monta con `CAP_SYS_ADMIN`) y la variante `full`, y su nombre por defecto
es `rayito-base-caps-efs`. Un zip con el marcador de efs sin `--with-efs`, o
`--with-efs` con un zip sin él, se rechaza antes de llamar a AWS.

`--sizes` publica, además del baseline (2048 MiB), una imagen con sufijo de
tamaño por cada nombre listado desde el mismo artefacto; `--env KEY=VALUE`
(repetible) hornea variables de imagen adicionales en todas las que publique
la invocación. Con `--sizes`, `--memory-mib` sólo admite el valor por
defecto (2048): el baseline de `--sizes` es siempre la imagen sin sufijo.
Ver [Tamaños](funciones-opcionales/tamanos.md).

Reproduce el pipeline de `make image-publish`:

1. Comprueba, antes de llamar a AWS, que el zip existe y que su marcador de
   variante (`warmup_variant` para `slim`, `kernels_variant` para `poly`)
   coincide con `--variant`, y que el marcador `efs_variant` aparece si y
   sólo si se pasa `--with-efs`.
2. Sube el zip a `s3://<bucket>/rayito/images/rayd-<12 hex del sha256>.zip`,
   salvo que la clave ya exista (clave por contenido: mismo zip, misma clave).
3. Toma el build role de `--build-role-arn` o de la salida `BuildRoleArn` del
   stack `--stack-name`.
4. Si ya hay una versión `SUCCESSFUL`/`ACTIVE` construida desde ese zip con
   la misma configuración (hooks, memoria, log group, `additionalOsCapabilities`
   y `baseImageVersion`, esta última comparada numéricamente porque la API
   acepta `1` y devuelve `1.0`), la **reutiliza** y no construye nada;
   `--force` construye igualmente. Cada versión cuesta una semana de storage
   del snapshot.
5. Si no, `create-microvm-image` la primera vez o `update-microvm-image`
   después, y sondea cada 10 s el **gate de tres estados**: imagen
   `CREATED|UPDATED`, versión `SUCCESSFUL`, status `ACTIVE`. Sólo entonces la
   versión es lanzable.
6. Imprime el resumen del `snapshotBuild` y, en la última línea,
   `RAYITO_TEMPLATE=<arn>`. Si el build falla, imprime los `stateReason` y la
   cola de los build logs del grupo `/rayito/<imagen>` y sale con 1.

`--bucket` se resuelve del flag o de `RAYITO_BUCKET`; sin ninguno de los dos
es un error de uso (2). La biblioteca no trae ningún bucket por defecto: el
bucket es de la cuenta que publica.

Permisos S3 del principal que publica (la `ImagePublisherPolicy` o la
`CallerPolicy` de `infra/iam.yaml` los conceden tal cual): `s3:PutObject` y `s3:GetObject`
sobre `arn:aws:s3:::<bucket>/rayito/*` y `s3:ListBucket` sobre
`arn:aws:s3:::<bucket>`. `s3:ListBucket` es lo que hace que el `head-object`
de un artefacto nuevo responda `404` en vez de `403` (sin él la CLI sube el
artefacto igualmente y un permiso ausente de verdad aflora en `put-object`)
y lo único que necesita el `head-bucket` de `rayito doctor`.

### `image list`

```bash
rayito image list [--name-filter F]      # imágenes: name, state, latestActive/FailedImageVersion, createdAt
rayito image list rayito-base            # versiones de una imagen, la más nueva primero
```

Con un nombre o ARN lista `imageVersion`, `state`, `status`,
`baseImageVersion`, el nombre del artefacto en S3 y `createdAt`.

### `image sizes`

```bash
rayito image sizes [--variant full|slim|poly] [--image-name <nombre>]
```

Por cada tamaño del catálogo cerrado (`512mb`/`1gb`/`2gb`/`4gb`/`8gb`,
[Tamaños](funciones-opcionales/tamanos.md)), qué imagen de la variante ya
publicó `rayito image publish --sizes` (`name`, `published`, `imageArn`,
`state`, `sameArtifact`, `createdAt`) o si ninguna. Siempre una
`list-microvm-images` filtrada por el nombre base; si hay algún tamaño
adicional publicado, además una `GetMicrovmImageVersion` (sin cuota propia)
por imagen publicada para `sameArtifact` — con sólo el baseline publicado,
ninguna llamada adicional. Nunca construye, publica ni lanza nada.
`sameArtifact` compara el `codeArtifact.uri` de la versión activa de cada
tamaño contra la del baseline: `false` detecta un tamaño publicado desde un
zip distinto, sin lanzar ningún sandbox. `--image-name` lista la familia
publicada con `rayito image publish --image-name <nombre> --sizes ...`
(`<nombre>`, `<nombre>-4gb`, ...) en lugar de la de la variante.

### `image prune`

```bash
rayito image prune --dry-run                     # primero: el plan, sin borrar nada
rayito image prune [--image-name rayito-base] [--keep 5] [--wait-timeout 600]
```

Conserva las `--keep` versiones lanzables más nuevas, toda versión que un
MicroVM no terminado esté usando (leído justo antes de cada borrado) y las
que están `PENDING`, `IN_PROGRESS`, `DELETING` o `DELETED`; borra el resto de
la más antigua a la más nueva, **de una en una** (la API responde
`ConflictException` mientras la imagen está `UPDATING`/`DELETING`), con
backoff 5/10/20/40/80 s y desactivando primero una versión `ACTIVE` que la
API se niegue a borrar. Imprime la tabla del plan y un resumen JSON; sale con
1 si alguna candidata sigue existiendo. Nunca borra la imagen ni su grupo de
logs `/rayito/<imagen>`; al retirar una imagen entera, borra los dos a mano
([Templates declarativos](funciones-opcionales/templates.md)).

### `image zip`

```bash
rayito image zip image image/rayito-image.zip [--variant full|slim|poly] [--sidecar kernel-sidecar] [--with-efs]
```

`--with-efs` añade el marcador `kernel-sidecar/efs_variant`, que activa la
capa condicional de `amazon-efs-utils` del `Dockerfile` (sin él, ninguna
imagen cambia). El resumen `--json` lo indica en `withEfs`.

Zip determinista (fechas y modos fijos; sin `__pycache__`, `tests`, `.venv`,
cachés, `uv.lock` ni otros zips) con el `Dockerfile` en la raíz. Con
`--sidecar` copia antes `kernel-sidecar/` a `image/kernel-sidecar/` con las
mismas exclusiones. Imprime ficheros, bytes, variante y el sha256 del zip
(con él se predice la clave S3). Es el único comando que no necesita AWS.

Si el directorio lleva `rayd`, tiene que llevar también sus avisos de
licencia en `licenses/` (`LICENSE`, `NOTICE` y `THIRD_PARTY_LICENSES.md`):
el `Dockerfile` los copia a `/usr/share/doc/rayd/` y el zip se niega a
empaquetar `rayd` sin ellos. Desde el repositorio los deja ahí
`make image-licenses`, que genera `THIRD_PARTY_LICENSES.md` con cargo-about
desde `Cargo.lock` (los objetivos `make image-zip*` ya lo hacen).

## `rayito sandbox`

```bash
rayito sandbox list [--template T] [--template-version V] [--all-states] [--state S]… [--metadata K=V]… [--index-table TABLA]
rayito sandbox info ID [--no-metadata]
rayito sandbox kill ID… | rayito sandbox kill --all [--template T] [--yes]
rayito sandbox logs ID [--log-group G] [--limit 1000] [--since 30m|2h|1d|ISO]
rayito sandbox create [TEMPLATE] [--timeout S] [--metadata K=V]… [--env K=V]… [--detach] [--token-file F] [--user U] [--cwd D]
rayito sandbox connect ID [--user U] [--cwd D] [--env K=V]… [--token-file F]
rayito sandbox exec ID [--background] [--cwd D] [--user U] [--env K=V]… [--timeout 0] [--token-file F] -- CMD…
rayito sandbox metrics ID [--follow] [--interval 5] [--token-file F]
rayito sandbox proxy ID --port N [--local-port M] [--bind 127.0.0.1] [--allow-remote] [--allowed-host H]… [--allow-origin O]… [--max-connections 8]
```

- `list` omite `TERMINATING` y `TERMINATED` (AWS los sigue listando unos 20
  minutos) salvo con `--all-states`; no sondea ningún endpoint. `--state`
  (repetible, sin distinguir mayúsculas: `--state suspended`) filtra por
  estado. `--metadata K=V` (repetible) filtra por metadatos: sin
  `--index-table` es la sonda O(n) de `Sandbox.list(metadata=)` (un `Health`
  por sandbox `RUNNING`); con `--index-table TABLA` (opcional, **apagado
  por defecto**) usa el índice de metadatos de esa tabla DynamoDB
  (`infra/metadata-index.yaml`): un `dynamodb:BatchGetItem` por página, sin
  sondas, y también sobre sandboxes en pausa
  (`rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes`).
  Sólo aparecen los sandboxes creados con `index=`; con `--json` cada fila
  lleva sus `metadata`. Coste e IAM en
  [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md).
  `--index-table` sin `--metadata` es un error de uso (salida 2) y no
  llama a DynamoDB: el índice sólo sirve para filtrar por metadatos.
  `--all-states` no se combina con `--state`, `--metadata` ni
  `--index-table` (salida 2).
- `info` hace `get-microvm` y, sobre un sandbox `RUNNING`, lee sus metadatos
  con un `Health` (un JWE de un solo uso, canal dedicado); `--no-metadata`
  lo evita. Los metadatos no son secretos; el endpoint es un hostname; no hay
  ningún token que imprimir.
- `kill` termina cada id e imprime `terminated` o `not found` (salida 1 si
  alguno no existía). `--all` lista los vivos, pide confirmación (salvo
  `--yes`) y los termina; es excluyente con los ids.
- `logs` resuelve el grupo `/rayito/<imagen>` (o `--log-group`) y el stream
  `YYYY/MM/DD[<versión>]<id>` del sandbox; si el nombre exacto no existe
  recorre hasta 10 páginas de streams por último evento buscando los que
  terminan en `]<id>`. Imprime `<timestamp ISO> <mensaje>` (`--json`: lista
  de `{timestamp, message, stream}`). **Sólo hay logs de runtime si el
  sandbox se lanzó con `execution_role_arn` y `logging="cloudwatch"`**; si no
  hay stream ni grupo: `sin logs: el sandbox se lanzó con logging disabled,
  sin executionRoleArn, o en otro grupo (--log-group)` y salida 1.

<a id="create-connect-exec-y-metrics-m9"></a>

### `create`, `connect`, `exec` y `metrics`

Los cuatro comandos operativos de la CLI de E2B que Lambda MicroVMs puede
servir. Los que necesitan hablar con `rayd` usan el **access token** del
sandbox, que nunca viaja por argv (se vería en `ps`) ni se imprime:

- `--token-file F` (el contenido del fichero, sin espacios alrededor) o
  `RAYITO_ACCESS_TOKEN`; si están los dos, gana el fichero. Sin ninguno,
  salida 2 con `falta el access token: --token-file o RAYITO_ACCESS_TOKEN`.
- `create --detach` escribe el token en `--token-file` **antes** de
  `run-microvm` (con `O_CREAT | O_EXCL` y modo `0600`: un fichero existente
  es salida 2), así un fallo al escribirlo nunca deja un VM huérfano. Sin
  `RAYITO_ACCESS_TOKEN` ni `--token-file`, `--detach` es salida 2. En Windows
  el modo lo ignora el sistema: deja el fichero en tu perfil de usuario.

Comportamiento:

- `create` sin `--detach` abre una terminal interactiva en el sandbox y lo
  termina al salir (como `e2b sandbox create`); con `--detach` imprime el
  `sandbox_id` (`--json`: `sandbox_id`, `endpoint`, `template`,
  `template_version` y `expires_at`, nunca el token) y sale con 0.
  `--timeout` es el `timeout` nativo (3600 por defecto).
- `connect` reanuda un sandbox suspendido y abre la terminal; no lo termina.
- `exec` corre `CMD…` (unido con `shlex.join`) y reenvía stdout y stderr a
  medida que llegan; sale con el código remoto, o 124 si venció el
  `--timeout` del servidor (`timeout del comando` por stderr). Con
  `--background` imprime el `pid` y sale con 0.
- `metrics` imprime la instantánea de `get_metrics()` (tabla, o un objeto
  JSON con `--json`); `--follow` repite cada `--interval` segundos hasta
  Ctrl-C (salida 0) o hasta que el sandbox desaparezca (salida 1). Conectar
  despierta un sandbox suspendido.

Un recorrido completo, con el token en un fichero que sólo lee tu usuario:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
mkdir -p ~/.rayito && chmod 700 ~/.rayito           # --detach crea el fichero, no el directorio
rayito sandbox create rayito-base --detach --timeout 1800 \
    --metadata equipo=datos --token-file ~/.rayito/demo.token      # imprime microvm-<id>
rayito sandbox exec microvm-<id> --token-file ~/.rayito/demo.token -- python3 -c 'print(40 + 2)'
rayito sandbox exec microvm-<id> --background --token-file ~/.rayito/demo.token -- sleep 600
rayito --json sandbox metrics microvm-<id> --token-file ~/.rayito/demo.token
rayito sandbox connect microvm-<id> --token-file ~/.rayito/demo.token   # terminal; Ctrl-D para salir
rayito sandbox kill microvm-<id>
```

El mismo token sirve en los SDKs: `Sandbox.connect("microvm-<id>",
access_token=open(ruta).read().strip())`.

La terminal pone la tuya en modo raw en POSIX (restaurado al salir; Ctrl-C
viaja al sandbox como `\x03`, nunca como SIGINT local) y reenvía los cambios
de tamaño (`SIGWINCH`); en una consola de Windows traduce las flechas y
sondea el tamaño cada segundo; con stdin por tubería reenvía los bytes tal
cual y manda `\x04` al acabar. El código de salida es el del shell remoto.

### `proxy`

Expone un puerto del guest en `http://<bind>:<local-port>` (por defecto
`--local-port` es igual a `--port`), para desarrollo local: curl, un
navegador o cualquier cliente HTTP/1.1 contra un servidor que el sandbox
ya sirve. El paso de WebSocket (upgrade) está implementado, no medido
contra AWS. Nunca hace falta el access token del
sandbox, sólo el JWE del proxy.

```bash
rayito sandbox proxy microvm-<id> --port 8000
# http://127.0.0.1:8000 → microvm-<id>:8000
#   con cookies aisladas de otras apps locales: http://microvm-<id>.localhost:8000
curl http://127.0.0.1:8000/
```

- Rechaza `--port 9000` (el puerto de los lifecycle hooks) y
  cualquier valor fuera de 1-65535, sin llamar a AWS.
- `--bind` fuera de `127.0.0.1`/`::1`/`localhost` necesita `--allow-remote`
  (si no, salida de uso): cualquiera que llegue a ese puerto usa el sandbox
  con el mismo acceso que quien lanzó el proxy. `--bind 0.0.0.0`/`::`
  necesita además `--allowed-host`.
- `Host` y `Origin`: sólo reenvía si `Host` es `127.0.0.1`, `localhost` o
  `[::1]` con el puerto local, la dirección de `--bind`, `<id>.localhost` o
  un `--allowed-host` (si no, `421`), y si el `Origin`, cuando viene, es uno
  de esos mismos orígenes o un `--allow-origin` (si no, `403`). Así una web
  abierta en tu navegador no llega al sandbox por DNS rebinding, con un POST
  entre sitios ni con un `WebSocket` de otro origen
  ([Proxy local](funciones-opcionales/proxy-local.md#que-peticiones-reenvia)).
- Como mucho `--max-connections` (8) conexiones reenviadas a la vez; la
  siguiente recibe `503`.
- Cabeceras: quita cualquier `x-aws-proxy-*` que traiga el cliente, fija
  `Host` al endpoint del sandbox, añade `X-aws-proxy-auth` (el JWE vigente)
  y `X-aws-proxy-port`, y fuerza `Connection: close` salvo en una petición
  de upgrade (`Upgrade` + `Connection: upgrade`, WebSocket). Sólo reenvía
  la primera petición de cada conexión, con su cuerpo delimitado por
  `Content-Length` o `chunked` (`400` si es ambiguo); un upgrade al que el
  sandbox no responde `101` se reenvía con `Connection: close` y se
  cierra. Sin JWE
  vigente o sin conexión al sandbox responde `502` y escribe el motivo en
  stderr; la cabecera tiene que llegar en 30 s y la conexión al sandbox
  abrirse en 30 s. Nunca registra el JWE, las cabeceras, los cuerpos
  ni las rutas de lo que pasa por el proxy ([Seguridad](security.md#rayito-sandbox-proxy)).
- El JWE se renueva a los 45 min con el mismo `TokenRefresher` que usa el
  canal gRPC del SDK: no se acuña uno nuevo en cada petición.
- Un sandbox `SUSPENDED` con auto-resume se despierta con la primera
  petición que llega al proxy (eso sí factura cómputo, más una lectura de
  snapshot al reanudar: [Límites](limits.md#tamano-cpuram) y
  [Precios](cost.md) para el precio por GB).
- $0 de AWS más allá de `GetMicrovm` + `CreateMicrovmAuthToken` (gratuitos,
  cuota de 50 TPS por cuenta/región; ~1 acuñación cada 45 min por proxy en
  marcha); sin recursos nuevos. Ctrl-C para el refresher y cierra el
  listener.
- IAM: `lambda:GetMicrovm` y `lambda:CreateMicrovmAuthToken` sobre el
  MicroVM (ya en la `SandboxLauncherPolicy` y en la `CallerPolicy` de
  `infra/iam.yaml`; nada nuevo que desplegar para usar el proxy).

## `rayito stack`

```bash
rayito stack list
rayito stack status <componente> [--stack-name N]
rayito stack deploy <componente> [--param K=V]... [--artifact-bucket B] [--tag K=V]... [--stack-name N] [--yes]
rayito stack destroy <componente> [--stack-name N] [--yes]
```

El convenio [`OptionalStack`](funciones-opcionales/pilas-opcionales.md):
cada función opcional con infraestructura propia es una pila de
CloudFormation en tu cuenta, y nada se despliega sin que lo pidas.

- `list` imprime el catálogo (nombre, si está implementado y coste en
  reposo) **sin llamar a AWS**.
- `deploy` imprime el bloque "Coste y activación" del componente y los
  parámetros que cambian, y pide confirmación salvo `--yes` (o `--json`).
  Sobre una pila que ya existe, los parámetros que no repites conservan su
  valor actual; los valores por defecto sólo se aplican al crearla.
  `--artifact-bucket` es obligatorio para un componente con código Lambda
  (`events-webhooks`): ahí se sube el código, en
  `rayito/stacks/<componente>/<sha256>.zip`, sólo si el bucket es de tu
  cuenta y comparando el contenido de un objeto que ya exista.
- `status` es un `DescribeStacks`: estado y salidas de la pila.
- `destroy` dice qué se conserva y pide confirmación salvo `--yes`.

## `rayito domain`

```bash
rayito domain deploy --public-domain D --certificate-arn ARN [--alternate-domain-name H]... [--stack-name N] [--yes]
rayito domain status | destroy [--stack-name N] [--yes]
```

[Dominio propio](funciones-opcionales/dominio-propio.md) (**experimental**,
sin verificar aún de punta a punta en AWS real): una fachada de
`rayito stack deploy custom-domain` (y `status`/`destroy`) con los nombres de
`CustomDomain`. `deploy` valida `--public-domain` y cada
`--alternate-domain-name` antes de llamar a AWS, pide confirmación salvo
`--yes` e imprime a qué apuntar el `CNAME`. El certificado ACM tiene que
estar en `us-east-1`.

## `rayito events`

```bash
rayito events deploy --artifact-bucket B --log-group-name G [--reconciler-interval-minutes 5] [--tag K=V]... [--yes]
rayito events status | destroy [--yes]
rayito events list [--sandbox-id ID] [--type T]... [--limit 100] [--order desc|asc]
rayito events webhook add <url> --secret-name S --type T [--type T]...
rayito events webhook list
rayito events webhook remove <webhook-id>
```

Atajo de `rayito stack deploy events-webhooks` más la gestión de webhooks y
la consulta de eventos ([Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md)).
Todos aceptan `--stack-name` (por defecto `rayito-events-webhooks`).
`deploy` imprime el coste (≈ $0,40/mes en reposo, el secreto HMAC) y pide
confirmación como `rayito stack deploy`.

## `rayito template`

```bash
rayito template build <spec.py> --name N --bucket B [--memory-mb 2048] [--force] [--timeout 1800]
rayito template status <name> [--version V]
rayito template logs <name> [--limit 500]
```

Construye un [template declarativo](funciones-opcionales/templates.md):
`build` ejecuta `spec.py` (que debe definir una variable de módulo
`template` con un `rayito.Template`), sube el contexto a `--bucket` bajo
`rayito/templates/` y crea o actualiza la imagen `--name`, imprimiendo el
log del build. Cada versión nueva cuesta almacenamiento de snapshot, igual
que `rayito image publish`; las antiguas se borran con `rayito image prune --image-name <name>`. `status` y
`logs` sólo leen: sin `--version`, la versión más reciente.

## `rayito agent`

```bash
rayito agent template build --bucket B [--name rayito-agent] [--base rayito-base-caps] [--memory-mb 2048]
                            [--no-deepagents] [--no-prefetch] [--force] [--timeout 1800]
```

Construye la imagen de `AgentTemplate`
([Templates de agente](funciones-opcionales/templates-de-agente.md)): parte
de `--base` (una imagen ya publicada con `rayito image publish`) e instala
OpenCode, ripgrep y, salvo `--no-deepagents`, el entorno de deepagents, todo
fijado por hash. `--no-prefetch` omite el demonio que precarga los binarios
tras cada restauración del snapshot. Sin fichero de spec: la receta es la
del SDK. Imprime `template_id=` y `build_id=` (con `--json`, `templateId`,
`buildId` y `alias`); un build fallido es `build fallido: …` y salida 1.

Se construye una vez: el arranque normal del agente es
`Sandbox.create(template="rayito-agent", ...)` + `sbx.agent.run(...)`, sin
pool. El prefetch y los pools calientes son opcionales:
[¿Qué uso?](guias/agente-en-el-sandbox.md#que-uso). Cada versión nueva cuesta
almacenamiento de snapshot ([Precios](cost.md#cuanto-cuesta-con-ejemplos));
las antiguas se borran con `rayito image prune --image-name <name>`.

## `rayito doctor`

```bash
rayito [--json] doctor [--template rayito-base] [--template-version V] [--bucket B] [--launch]
                       [--efs-vpc-id V --efs-subnet-ids S1,S2]
```

Diez comprobaciones, en orden, cada una con `OK`, `WARN`, `FAIL` o `SKIP`.
Una excepción dentro de una comprobación es su `FAIL` (o `SKIP` cuando un
`AccessDeniedException` sólo impide un diagnóstico) nombrando la operación
que AWS rechazó (`details.operation`) junto al permiso IAM que la
comprobación necesita (`details.action`), y las demás siguen corriendo. Sale
con 1 sólo si alguna es `FAIL`.

| # | Comprobación | Qué mira | `WARN` | `FAIL` | `SKIP` | Qué hacer |
|---|---|---|---|---|---|---|
| 1 | `credentials` | `sts:GetCallerIdentity` y la región de la sesión | región fuera de las diez con Lambda MicroVMs | sin credenciales, token caducado, sin región | — | `aws login` / exporta `AWS_PROFILE` y `AWS_REGION` |
| 2 | `managed-images` | `list-managed-microvm-images` y la versión más nueva de `al2023-1` | la llamada responde pero `al2023-1` no está | `AccessDenied`, endpoint desconocido (botocore antiguo), región sin el servicio | — | actualiza `boto3`; cambia de región |
| 3 | `quotas` | Service Quotas de `lambda` con `microvm` en el nombre frente a los defaults publicados (`L-CD1C0CC4` memoria, `L-535CA9B6` RunMicrovm…) | alguna cuota ajustable por debajo del default | — | `servicequotas:ListServiceQuotas` denegado | pide aumento de cuota en la consola |
| 4 | `iam-simulation` | `iam:SimulatePrincipalPolicy` sobre las acciones que usan el SDK y la CLI (imagen, listas, `PassNetworkConnector`, S3 del bucket, logs, quotas), una llamada por recurso | algún `implicitDeny` | `explicitDeny` o denegación de una SCP | root, principal no simulable, `iam:GetRole`/`SimulatePrincipalPolicy` denegados | orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan; `iam:PassRole` no se simula. Medido 2026-09-16: el simulador devuelve `implicitDeny` para `lambda:PassNetworkConnector` incluso con `AdministratorAccess`, mientras `run-microvm` funciona: ese `WARN` no bloquea nada |
| 5 | `bucket` | un `head-bucket` del bucket de artefactos (`s3:ListBucket`, el mismo permiso que `publish` necesita) y la región de su cabecera `x-amz-bucket-region` | bucket en otra región (`S3_CROSS_REGION_ACCESS_DENIED` al construir); sin cabecera de región | 403/404 (el resumen nombra la operación rechazada y los permisos S3 de `publish`) | sin `--bucket` ni `RAYITO_BUCKET` | concede `s3:ListBucket` sobre el bucket; usa un bucket de la misma región |
| 6 | `image-gate` | `get-microvm-image` + la versión (`--template-version` o la última activa) por el gate de tres estados y su `snapshotBuild` | lanzable pero con un build fallido listado | la imagen no existe, no está `CREATED|UPDATED` o la versión no es `SUCCESSFUL`/`ACTIVE` | — | `rayito image publish …`; `rayito image prune` para el build fallido |
| 7 | `sandboxes` | MicroVMs no terminados de la imagen (`list-microvms`) | 1–10 `RUNNING` (facturan) | más de 10 `RUNNING` | — | `rayito sandbox list`, `rayito sandbox kill --all` |
| 8 | `token` | `create-microvm-auth-token` (puerto 8080) para el sandbox de `--launch` o el `RUNNING` más nuevo | — | `AccessDenied`, `ValidationException` | sin sandbox `RUNNING` (usa `--launch`) | revisa `lambda:CreateMicrovmAuthToken` |
| 9 | `agent` | un `Health` de `rayd`: `agent_version`, `kernel_ready`, `imds_blocked`, `hook_anomalies` | `kernel_ready=false`, `hook_anomalies>0`, `imds_blocked=false` en una imagen `-caps` | `Health` falla (`UNAVAILABLE`, `UNAUTHENTICATED`, 403 del proxy) | sin token | espera al kernel; revisa quién tiene un token `allPorts`; republica la imagen |
| 10 | `compatibility` | la tabla SDK ↔ `rayd` de [Límites](limits.md); la versión de imagen (contador de builds por imagen y cuenta) sólo se informa | `rayd` más nuevo que el SDK en `MAJOR.MINOR` | `rayd` por debajo del mínimo del SDK | sin `agent_version` | actualiza el SDK o publica una imagen desde el tag del `rayd` mínimo |

Con `--efs-vpc-id` y `--efs-subnet-ids` (volúmenes EFS en una VPC que ya
existe) se añade, tras la 7, la comprobación **`efs-network`**: la misma que
`EfsVolumes.check()`, de sólo lectura (`ec2:DescribeVpcs`,
`DescribeVpcAttribute`, `DescribeSubnets`, `DescribeRouteTables`). `FAIL`
si la VPC o una subred no existe, una subred es de otra VPC, dos comparten
AZ o a una le quedan menos de 2 IPs libres; `WARN` con una sola AZ o sin DNS
en la VPC. El resumen dice qué crearía `rayito stack deploy efs-volumes` y
su coste en reposo; no crea nada. Ver
[Volúmenes EFS en tu VPC](funciones-opcionales/volumenes-efs-vpc.md).

Sin `--launch` el doctor **nunca crea un MicroVM**: las comprobaciones 8–10
usan el sandbox `RUNNING` más nuevo de la imagen o quedan en `SKIP`. Con
`--launch` crea uno de 300 s (`idle=None`, `logging="disabled"`,
`metadata={"rayito": "doctor"}`), lo usa para 8–10 y lo mata en un `finally`
pase lo que pase (también con Ctrl-C); cuesta la lectura del snapshot más
unos segundos de cómputo ([Precios](cost.md#cuanto-cuesta-con-ejemplos)) y su
id aparece como `launched_sandbox_id`.

Salida humana: una línea por comprobación (`OK   credentials  cuenta …`), los
detalles indentados en `WARN`/`FAIL`, la tabla de compatibilidad y
`rayito doctor: N OK, N WARN, N FAIL, N SKIP`. Con `--json`:

```json
{
  "rayito": "0.8.0",
  "region": "us-east-1",
  "account": "123456789012",
  "principal_kind": "assumed-role",
  "checks": [{"name": "credentials", "status": "OK", "summary": "…", "details": {}}],
  "compatibility": [
    {"sdk_series": "0.1", "min_agent_version": "0.1.0", "note": "…"},
    {"sdk_series": "0.2", "min_agent_version": "0.2.0", "note": "…"},
    {"sdk_series": "0.3", "min_agent_version": "0.3.0", "note": "…"},
    {"sdk_series": "0.4", "min_agent_version": "0.4.0", "note": "…"},
    {"sdk_series": "0.5", "min_agent_version": "0.5.0", "note": "…"},
    {"sdk_series": "0.6", "min_agent_version": "0.6.0", "note": "…"},
    {"sdk_series": "0.7", "min_agent_version": "0.7.0", "note": "…"},
    {"sdk_series": "0.8", "min_agent_version": "0.8.0", "note": "…"}
  ],
  "launched_sandbox_id": null,
  "exit_code": 0
}
```

Nada de lo que imprime la CLI es un secreto: ni el JWE del proxy, ni el
access token, ni el `runHookPayload` aparecen en ninguna salida (los tests
lo comprueban con canarios).

## Equivalencias con `make`

Los cuatro scripts de `scripts/` siguen existiendo con los mismos argumentos
y son *shims* de la CLI, así que los targets del `Makefile` no cambian:

| `make` | Script | Equivalente |
|---|---|---|
| `make image-zip` | `python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar`, `make image-licenses` y `python scripts/image_zip.py image image/rayito-image.zip` | `rayito image zip image image/rayito-image.zip --sidecar kernel-sidecar` |
| `make image-zip-efs` | `python scripts/image_zip.py image image/rayito-image-efs.zip --with-efs` | `rayito image zip image image/rayito-image-efs.zip --sidecar kernel-sidecar --with-efs` |
| `make image-publish-caps-efs` | `… publish_image.py --artifact image/rayito-image-efs.zip --with-efs --os-capabilities ALL …` | `rayito image publish --artifact image/rayito-image-efs.zip --with-efs --os-capabilities ALL …` |
| `make image-publish` (`-slim`, `-poly`, `-caps`) | `uv run --project clients/python python scripts/publish_image.py --artifact … --bucket $(BUCKET) --base-image-version 1` | `rayito image publish --artifact … --bucket … --base-image-version 1` |
| `make image-publish-caps MOUNT_BUCKETS=b1,b2` (también `-caps-efs`) | `… publish_image.py … --env RAYITO_ALLOWED_MOUNT_BUCKETS=b1,b2` | `rayito image publish … --env RAYITO_ALLOWED_MOUNT_BUCKETS=b1,b2` |
| `make image-prune PRUNE_ARGS="--keep 5 --dry-run"` | `uv run --project clients/python python scripts/image_prune.py --image-name rayito-base --keep 5 --dry-run` | `rayito image prune --keep 5 --dry-run` |

`image_zip.py` y `copy_sidecar.py` sólo usan la biblioteca estándar (cargan
`rayito/cli/_artifact.py` por ruta de fichero) porque CI y la release los
ejecutan con un `python3` pelado sin instalar el SDK. `publish_image.py` e
`image_prune.py` necesitan el entorno del cliente (`typer` y el SDK) y sin él
imprimen el comando `uv run --project clients/python …` y salen con 2.

## Lo que la CLI no hace

- `rayito.toml` ni ningún fichero de configuración propio.
- Construir la imagen más allá del zip: el Dockerfile lo construye AWS.
- `sandbox logs --follow`: para eso están los SDKs y el
  [servidor MCP](mcp.md).
- `auth`, `snapshots` y `fork` de la CLI de E2B: no tienen
  primitiva o quedan fuera por `SPEC.md` §4 ([Compatibilidad con E2B](e2b-compat.md)).
- Envolver las herramientas de desarrollo (`bench_cold_start.py`,
  `hooks-sim.py`, `gen_limits.py`, `check_*.py`).
- Una CLI en TypeScript ni completado de shell.
