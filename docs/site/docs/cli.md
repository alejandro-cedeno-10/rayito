# CLI

`rayito` es la herramienta de línea de comandos del SDK: publica y lista la
imagen, prepara la cuenta (`rayito setup`), borra versiones antiguas, lista,
inspecciona y mata sandboxes, lee sus logs de CloudWatch y diagnostica una
cuenta antes del primer `Sandbox.create()`. También despliega las pilas opcionales
(`rayito stack`, `rayito events`, `rayito domain`), construye templates
declarativos (`rayito template`) y el agente de IA como proyecto
(`rayito agent`). No hay `rayito.toml`: la configuración son
opciones y variables de entorno.

| Grupo | Comandos |
|---|---|
| [`rayito image`](#rayito-image) | `publish`, `list`, `sizes`, `prune`, `zip` |
| [`rayito sandbox`](#rayito-sandbox) | `list`, `info`, `kill`, `logs`, `create`, `connect`, `proxy`, `exec`, `metrics` |
| [`rayito stack`](#rayito-stack) | `list`, `status`, `deploy`, `destroy` |
| [`rayito domain`](#rayito-domain) (experimental) | `deploy`, `status`, `destroy` |
| [`rayito events`](#rayito-events) | `deploy`, `status`, `destroy`, `list`, `webhook add`, `webhook list`, `webhook remove` |
| [`rayito template`](#rayito-template) | `build`, `status`, `logs` |
| [`rayito agent`](#rayito-agent) | `init`, `build`, `run`, `detect`, `deploy`, `export`, `invoke`, `status`, `stop`, `list`, `delete`, `template build` |
| [`rayito dashboard`](#rayito-dashboard) | — |
| [`rayito setup`](#rayito-setup) | — |
| [`rayito doctor`](#rayito-doctor) | — |

Hay dos CLI con la misma superficie, salida y códigos de salida: la de
Python (extra `cli`) y la de TypeScript (ejecutable `rayito` del paquete npm).
Lo que sigue vale para las dos salvo donde se diga lo contrario; los dos
árboles de órdenes se comparan con `testdata/cli/surface.json` y
`scripts/check_sdk_parity.py --cli`. El servidor MCP es otro ejecutable del
mismo paquete, `rayito-mcp` ([Servidor MCP](mcp.md)).

## Instalación

=== "Python"

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

=== "TypeScript"

    El paquete npm `rayito` declara el ejecutable `rayito` (Node 20 o
    superior, sólo ESM, sin dependencias de CLI: usa `node:util`):

    ```bash
    npx rayito --help                # sin instalar nada
    npm install --global rayito      # o: pnpm add --global rayito
    rayito --help
    ```

    Los clientes de AWS que no todas las órdenes necesitan son **peers
    opcionales** del paquete (`@aws-sdk/client-cloudformation` para `stack`,
    `events` y `domain`, `@aws-sdk/client-cloudwatch-logs` para
    `sandbox logs`, `@aws-sdk/client-iam` para la simulación de IAM de
    `doctor`, `@aws-sdk/client-service-quotas` y
    `@aws-sdk/client-cost-explorer` para el panel, entre otros). Una orden que
    necesita uno que falta falla y dice qué paquete instalar. La API
    de TypeScript es sólo asíncrona: la CLI no cambia por eso.

!!! warning "Dos instalaciones globales"
    Si instalas las dos CLI, gana la primera del `PATH`.
    `rayito --version` dice cuál responde (`(python)` o `(node)`).

## Opciones globales y credenciales

```text
rayito [--profile P] [--region R] [--json] [--verbose] [--version] <grupo> <comando> …
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
- `--verbose`: logs `INFO` del SDK (reconexiones, sondas) y de la CLI. Sin
  él, los avisos y errores del SDK salen igual por stderr (por ejemplo
  `receta de la flota: recipe_renewal_ignored`), en las dos CLI.
- `--version`: imprime `rayito <versión> (python)`. El runtime entre paréntesis
  dice cuál de las dos CLI responde cuando conviven la de Python y la del
  paquete npm (`rayito <versión> (node)`): manda la primera del `PATH`.

Códigos de salida: `0` éxito; `1` la operación falló (build no lanzable, una
versión que `prune` no pudo borrar, un id desconocido en `kill`, un `FAIL`
del `doctor`, un error de AWS impreso como `AWS error <Code>: <Message>`);
`2` uso o entorno (argumentos, sin región, sin credenciales, sin extra).
Si el lector cierra la salida (`rayito sandbox list | head`), la CLI sale con
`1` sin escribir ninguna traza.

## `rayito image`

### `image publish`

```bash
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 \
    --bucket <bucket> [--variant full|slim|poly] [--image-name N] \
    [--os-capabilities ALL] [--build-role-arn ARN | --stack-name rayito-m0-iam] \
    [--memory-mib 2048] [--timeout-seconds 1800] [--force] \
    [--sizes 512mb,1gb,4gb,8gb] [--env K=V]... [--with-efs] \
    [--artifact-run-id RUN_ID]
```

`--base-image-version` es obligatoria y fija la versión de la base gestionada
de AWS (`imageVersion` de `list-managed-microvm-image-versions`) sobre la que
se construye la imagen. No hace falta buscarla a mano: con `latest` se usa la
más reciente que lista AWS y se imprime (`base_image_version=<versión>`); un
número explícito fija una build reproducible.

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
   Con `--artifact-run-id RUN_ID` la clave es
   `rayito/images/runs/<RUN_ID>/rayd-<12 hex del sha256>.zip`: el objeto es
   sólo de esa ejecución (ver más abajo).
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

`--artifact-run-id` es para quien publica imágenes desechables en paralelo
(la aceptación de una release, el e2e): dos ejecuciones del mismo commit
compartirían la clave por contenido, y la limpieza de la primera borraría el
zip al que todavía apunta la imagen de la segunda. `RUN_ID` es un único
segmento de clave (letras, dígitos y guiones, de 1 a 64, empezando por letra
o dígito); otro valor es un error de uso (2) antes de llamar a AWS. La clave
sigue bajo `rayito/images/`, así que los permisos de abajo no cambian. El
resumen de `--json` dice en `artifactUploaded` si esta invocación subió el
objeto (`true`) o ya existía (`false`): una limpieza borra sólo los objetos
que su ejecución subió. Con `--sizes`, la raíz (el baseline) es la que sube;
los tamaños reutilizan ese objeto y dicen `false`. Sin el flag la clave es
la compartida de siempre.

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
rayito agent init [DIR] [--runtime R] [--language python|typescript] [--provider P] [--model ID]
                  [--region R] [--force]
rayito agent build --bucket B [--file PATH] [--force] [--timeout 1800]
rayito agent run PROMPT [--file PATH] [--session-id ID] [--model ID] [--stream]
rayito agent detect [PATH] [--framework F] [--language L] [--entry E] [--protocol P]
                    [--model-id ID] [--model-provider P] [--model-base-url URL] [--write]
rayito agent deploy [PATH] [--file PATH] [--timeout S] [--port N] [--token-file F] [--keep-on-failure]
                    [--framework F] [--language L] [--entry E] [--protocol P]
                    [--model-id ID] [--model-provider P] [--model-base-url URL]
                    [--memory MIB] [--max-sessions N] [--sessions shared|dedicated]
                    [--min-vms N] [--max-vms N] [--warm N] [--idle S] [--queue N] [--queue-timeout S]
                    [--env K=V]... [--allow-out DEST]... [--bucket B] [--secret PROVEEDOR=NOMBRE]...
                    [--create-secret] [--secret-env NOMBRE=SECRETO]... [--secret-env-from SECRETO]...
                    [--create-secrets-from FICHERO] [--check-env]
                    [--base-artifact ZIP] [--yes] [--dry-run]
                    [--target microvm|agentcore] [--tag K=V]... [--bedrock-model ID]...
                    [--readable-secret NOMBRE]... [--idle-session-timeout S] [--max-session-lifetime S]
                    [--external-model] [--scale-in-after S] [--no-save-recipe]
                    [--renew-gateway-keys]
rayito agent export --target docker|agentcore --out DIR [--file PATH] [--overwrite]
rayito agent invoke SANDBOX_ID [PROMPT] [--session-id ID] [--input JSON | --input-file F]
                    [--stream] [--token-file F] [--target microvm|agentcore]
                    [--renew-gateway-keys]
rayito agent status SANDBOX_ID [--token-file F] [--target microvm|agentcore]
rayito agent stop SANDBOX_ID [--target microvm|agentcore] [--session-id ID] [--keep-recipe]
rayito agent list --target agentcore
rayito agent delete NOMBRE --target agentcore [--yes]
rayito agent template build --bucket B [--name rayito-agent] [--base rayito-base-caps] [--memory-mb 2048]
                            [--no-deepagents] [--no-prefetch] [--force] [--timeout 1800]
```

Desde 0.14.0 los comandos `init`, `build`, `run`, `deploy`, `export`, `invoke`,
`status` y `stop` trabajan con un proyecto de agente (`rayito-agent.toml`,
`--file` por defecto es el directorio actual): ver
[Despliega tu agente](guias/despliega-tu-agente.md). `init` y `export` no
llaman a AWS; `init` no sobrescribe nada sin `--force` (salida 2), `deploy`
imprime el id del sandbox, la URL y el puerto (con `--json`, `sandboxId`,
`url` y `port`) y nunca las cabeceras de acceso, y `--stream` imprime los
eventos a medida que llegan (con `--json`, una línea JSON por evento).
`deploy` guarda el access token del sandbox en `--token-file` (o usa
`RAYITO_ACCESS_TOKEN`) y sin ninguno de los dos sale con 2 sin crear nada;
`invoke` y `status` lo leen igual, y `stop` no lo necesita. Si el arranque
falla, `deploy` imprime la causa (el código del fallo y el final de la salida
de error de la app, sin valores de entorno ni credenciales) y termina el
sandbox, salvo con `--keep-on-failure`, que lo deja vivo para depurarlo
(factura hasta `rayito agent stop`). Con una `HttpApp` que declara rutas
propias sin `protocol = "raw"`, `deploy` avisa de que no se alcanzan desde
fuera. `run` e `invoke` imprimen `tokens=desconocido` (y `usageReported:
false` con `--json`) cuando el agente no informó de tokens. `init --runtime
custom` escribe una app HTTP (`[runtime.app]` con `protocol = "raw"`,
`requirements`, `install` y un `serve.py`), y `init --runtime langgraph`, un
grafo de LangGraph a secas.

`rayito agent detect` y `rayito agent deploy` (desde 0.14.0) trabajan con un
agente que ya tienes, sin `rayito-agent.toml`: detectan el lenguaje, el
framework, el punto de entrada, la instalación y el proveedor leyendo los
ficheros (no ejecutan tu código), y las banderas fijan lo que la detección no
acierta. `detect` no llama a AWS y con `--write` guarda lo detectado como
`rayito-agent.json`; `deploy --dry-run` imprime la detección, el plan y el coste
sin llamar a AWS. `deploy` despliega una **flota**
([Varias sesiones y varias VMs](guias/flota-de-agentes.md)): `--min-vms` VMs
vivos y `--warm` plazas aparcadas; con `--secret proveedor=nombre` usa un
secreto que ya existe y con `--create-secret` crea el que falte desde la
variable estándar del preset de la pasarela (`AWS_BEARER_TOKEN_BEDROCK` para un
`[gateways.model] preset = "bedrock"`), con el nombre exacto que lee la pasarela
(cuesta $0,40 al mes); no crea buckets (eso lo
hace `rayito setup`; usa `--bucket`, `RAYITO_BUCKET`, el de `cdk bootstrap` y, si
no existe, la salida `BucketName` de la pila `rayito-m0-artifacts` que creó
`rayito setup`), y sólo
publica la imagen base con `--base-artifact` y `--yes`. `invoke`, `status` y
`stop` aceptan el nombre de la flota (`rayito agent stop <nombre>` termina todos
sus VMs); el `session_id` que devuelve `invoke` en una flota con varios VMs
identifica el VM, y pasarlo con `--session-id` sigue la conversación. Los
mensajes y las ayudas van en español. Ver
[Despliega tu agente](guias/despliega-tu-agente.md#sin-escribir-nada).

Desde 0.17.0, `deploy` guarda la
[receta de la flota](guias/flota-de-agentes.md#la-receta-de-la-flota) en
Parameter Store tras arrancarla (`--no-save-recipe` no la guarda): con ella,
`invoke`, `status` y `stop` con el nombre de la flota la reconstruyen como se
desplegó, y `status` imprime el escalado real o, sin proyecto ni receta,
`members: N (escalado desconocido: sin proyecto ni receta)` (con `--json`, la
clave `scalingSource`). `stop <nombre>` borra la receta (`--keep-recipe` la
conserva). Un aviso de la receta (`recipe_access_denied`, `recipe_not_saved`,
`recipe_renewal_ignored`, `recipe_too_large`) va a stderr y, con `--json`, en `warnings`; nunca hace
fallar el despliegue. `--scale-in-after S` fija los segundos sin uso tras los
que se termina un VM por encima de `--min-vms` (por defecto 900, mínimo 60).
`--renew-gateway-keys` renueva la clave de corta duración de Bedrock de las
pasarelas `bedrock` antes de crear cada VM
([Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md#renovar-la-clave-de-bedrock)).
Con `--target agentcore`, esas tres banderas salen con 2
(`option_not_supported`). `--env` rechaza, antes de llamar a nadie, un nombre
que parece un secreto (`*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`..., con
un segmento `PASS`, `PASSWD`, `PAT`, `CREDENTIAL` o `CREDENTIALS`, como
`DB_PASS`, o con `AUTH` o `BEARER` como último segmento o junto a `TOKEN`,
`KEY` o `HEADER`, como `X_AUTH` o `AUTH_HEADER`; sin distinguir mayúsculas):
usa `--secret-env` o `--secret-env-from`. `AUTH_MODE`, `AUTH_URL` o
`AUTHOR_NAME` pasan.

`invoke` con el nombre de una flota acepta `--renew-gateway-keys`: renueva la
clave de Bedrock de las pasarelas antes de cada VM nuevo que cree la flota,
firmada con tus credenciales, porque la receta no la activa por su cuenta
(sin la bandera avisa `recipe_renewal_ignored` si la receta la pedía). Con el
id de un sandbox sale con 2 sin llamar a nadie, y con `--target agentcore`,
con 2 (`option_not_supported`).

Con varios grafos en `langgraph.json`, `detect` y `deploy` aceptan
`--graph NOMBRE` (excluyente con `--entry`). `invoke` acepta, en lugar de
`PROMPT`, `--input JSON` o `--input-file F` (`-` lee la entrada estándar): el
objeto de entrada de un grafo con esquema propio; exactamente uno de los tres,
o sale con 2 antes de llamar a nadie. La salida del grafo (`output`) se imprime
como JSON, y con `--json` va en la clave `output`. Ver
[Varios grafos y entrada estructurada](guias/despliega-tu-agente.md#varios-grafos-y-entrada-estructurada).

Con `--secret-env NOMBRE=<secreto>[#clave]` y `--secret-env-from
<secreto>[,prefix=P][,keys=A|B]` (repetibles, en los dos destinos) el agente
recibe valores de Secrets Manager como variables de entorno, sumados a los de
`[secret_env]` y `[[secret_env_from]]` del fichero; `--create-secrets-from
FICHERO.env` crea (o actualiza) un secreto JSON con los valores de ese `.env`,
enseña sólo los nombres y pide `--yes` (cuesta $0,40 al mes), y `--check-env`
avisa de las variables de `.env.example` que no reciben valor. Ver
[Secretos como variables de entorno](guias/secretos-como-entorno.md).

`--protocol` acepta `agentcore`, `json`, `raw` y, desde esta versión,
`langgraph-api` (experimental): sobre un proyecto con `langgraph.json` sirve
el grafo con `langgraph dev` y su LangGraph Platform API en vez de importarlo;
sin `langgraph.json` falla con `invalid_value`. Ver
[LangGraph Platform API](guias/despliega-tu-agente.md#langgraph-platform-api).

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

`--model-id`, `--model-provider` y `--model-base-url` (en `detect` y
`deploy`) fijan el modelo cuando tu código lo
elige por configuración: el id, la pasarela (`openai-compatible`, `litellm`…)
y la URL `https` de tu pasarela de LLM, que pasa a ser su `upstream`. Con un
fichero de proyecto se comportan como `--entry`: lo que el fichero dice gana.
Un `--model-provider` desconocido da `invalid_value` en `model.provider`. Ver
[Modelo elegido por tu código](guias/despliega-tu-agente.md#modelo-desde-tu-codigo).

### `agent` con `--target agentcore` { #agent-agentcore }

Desde 0.15.0, `rayito agent deploy --target agentcore`
despliega el agente en Amazon Bedrock AgentCore Runtime en vez de en una flota de
MicroVMs, y `invoke`, `status`, `stop`, `list` y `delete` lo operan. Sin
`--target`, o con `--target microvm`, todo es como en 0.14. Guía completa:
[Desplegar en AgentCore](guias/desplegar-en-agentcore.md).

!!! info "Coste y activación"
    - **Activa:** `--target agentcore` en `rayito agent deploy`. `--dry-run` no
      llama a AWS.
    - **Recursos y llamadas AWS:** una pila de CloudFormation por agente
      (`rayito-agentcore-<nombre>`), un build de CodeBuild por cada contenido
      nuevo y un runtime de AgentCore.
    - **Coste aproximado** (us-east-1, precios de lista, consultados el
      2026-10-19): runtime a $0,0895 por vCPU-hora y $0,00945 por GB-hora de uso
      real; build a $0,0034 por minuto (100 minutos al mes gratis); imagen en
      ECR a $0,10 por GB-mes. Más en [Precios](cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** [Antes de empezar](guias/desplegar-en-agentcore.md#antes-de-empezar).
    - **Cómo apagarla:** `rayito agent delete <nombre> --target agentcore`.
    - **Ejemplo:** `rayito agent deploy ./mi-agente --target agentcore --yes`.

| Opción | Órdenes | Qué hace |
|---|---|---|
| `--target` (`microvm` o `agentcore`) | `deploy`, `invoke`, `status`, `stop` (en `list` y `delete` es obligatoria y sólo vale `agentcore`) | Dónde se despliega. Por defecto, `microvm` |
| `--tag K=V` | `deploy` | Etiqueta de la pila `rayito-agentcore-<nombre>` y del runtime; repetible; útil si la organización exige etiquetas |
| `--bedrock-model ID` | `deploy` | Modelo o perfil de inferencia de Bedrock que el rol del runtime puede invocar; repetible. Por defecto, el modelo del proyecto |
| `--readable-secret NOMBRE` | `deploy` | Secreto de Secrets Manager que el rol del runtime puede leer; repetible. Por defecto, ninguno |
| `--idle-session-timeout S` | `deploy` | Segundos sin uso tras los que AgentCore termina la microVM de una sesión ({{agentcoreLifecycleMinSeconds}}-{{agentcoreLifecycleMaxSeconds}}, por defecto {{agentcoreIdleSessionTimeoutDefaultSeconds}}) |
| `--max-session-lifetime S` | `deploy` | Vida máxima de la microVM de una sesión ({{agentcoreLifecycleMinSeconds}}-{{agentcoreLifecycleMaxSeconds}}, por defecto {{agentcoreLifecycleMaxSeconds}}) |
| `--external-model` | `deploy` | Permite un modelo fuera de Bedrock: el rol no recibe permisos de Bedrock que no pidas y tu código obtiene la clave (`--readable-secret`). Sin ella, un proveedor que no es `bedrock` da `provider_unsupported` ([guía](guias/desplegar-en-agentcore.md#modelos-fuera-de-bedrock)) |
| `--session-id ID` | `stop` | La sesión cuya microVM se para (`StopRuntimeSession`); con `--target agentcore` es obligatoria |
| `--yes` | `deploy`, `delete` | No pedir confirmación (hace falta sin terminal o con `--json`) |

Las opciones nuevas, salvo `--target`, sólo valen con `--target agentcore`: sin
él dan `option_requires_agentcore` (salida 2). Con `--target agentcore`, las
opciones de MicroVMs que no tienen equivalente (`--min-vms`, `--max-vms`,
`--warm`, `--memory`, `--port`, `--timeout`, `--idle`, `--secret`,
`--create-secret`, `--token-file`, `--keep-on-failure`, `--base-artifact`,
`--allow-out`) dan `option_not_supported` (salida 2) antes de llamar a AWS; la
[tabla completa](guias/desplegar-en-agentcore.md#opciones-que-no-aplican) dice
qué hace cada una y por qué.

- **`deploy --target agentcore`** imprime el plan (tres pasos: pila, imagen y
  runtime), la sección `Seguridad:` (AgentCore no tiene la pasarela de secretos
  de Rayito: el agente obtiene las credenciales del rol del runtime), los
  avisos y el coste aproximado, y pide confirmación
  (`¿Desplegar en AgentCore?`). Con `--dry-run` termina ahí sin llamar a AWS; sin
  terminal o con `--json` hace falta `--yes` (`needs_confirmation`, salida 2).
  Al terminar imprime `target=agentcore`, `name`, `runtime_id`, `runtime_arn`,
  `version`, `region` y `logs`, y cómo invocarlo.
- **`invoke NOMBRE PROMPT --target agentcore`**: el argumento es el nombre del
  agente; `--session-id` y `--stream` van tal cual. `--token-file` da
  `option_not_supported`: AgentCore autentica con SigV4.
- **`status NOMBRE --target agentcore`** imprime el estado del runtime, su
  versión, la imagen, la región y el grupo de logs.
- **`stop NOMBRE --target agentcore --session-id ID`** termina esa sesión; sin
  `--session-id` da `session_required` (salida 2).
- **`list --target agentcore`** lista los agentes de Rayito de la región
  (`<nombre> runtime=… id=… version=… status=…`) o dice que no hay ninguno.
- **`delete NOMBRE --target agentcore`** borra el runtime, la pila
  `rayito-agentcore-<nombre>` (repositorio de ECR con sus imágenes, proyecto y
  logs de CodeBuild y los dos roles) y el grupo de logs del runtime; sólo lo que
  creó Rayito para ese nombre. Pide confirmación (`--yes` la salta; sin terminal
  o con `--json`, `delete_needs_confirmation`, salida 2). Sin nada que borrar,
  sale con 0.

**Salida 2** (uso o entorno): `unsupported_region`, `option_not_supported`,
`option_requires_agentcore`, `target_unsupported`, `provider_unsupported`,
`network_policy_unsupported`, `name_invalid`, `name_conflict`, `model_required` (reservado: hoy no se lanza),
`image_missing`, `lifecycle_invalid`, `lifecycle_order`, `tags_invalid`,
`env_invalid`, `iam_resource_invalid`, `raw_protocol`, `no_bucket`, `bucket_region`, `needs_confirmation`,
`delete_needs_confirmation` y `session_required`. **Salida 1** (lo que falla en
AWS): `stack_blocked`, `stack_failed`, `image_build_failed`,
`image_build_timeout`, `build_quota`, `image_too_large`, `runtime_blocked`,
`runtime_failed`, `runtime_timeout`, `runtime_not_found`, `delete_failed`,
`access_denied` y `aws_error`. Qué significa cada uno:
[Errores](referencia/errores.md#agentcore).

Con `--json`, cada orden imprime un único documento:

| Orden | Claves |
|---|---|
| `deploy --dry-run` | `dryRun`, `target`, `detection`, `plan` (`target`, `name`, `region`, `runtimeName`, `stackName`, `repositoryName`, `buildProjectName`, `imageTag`, `bucket`, `bucketSource`, `network`, `bedrockModels`, `readableSecrets`, `environment`, `lifecycle`, `tags`, `steps`, `security`, `warnings`, `cost`) |
| `deploy` | `target`, `name`, `region`, `runtimeName`, `runtimeId`, `runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `stackName`, `logGroup`, `steps` |
| `status` | `target`, `name`, `region`, `runtimeName`, `runtimeId`, `runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `logGroup` |
| `list` | `target`, `region`, `agents` (`name`, `runtimeName`, `runtimeId`, `runtimeVersion`, `status`) |
| `delete` | `target`, `name`, `region`, `runtimeDeleted`, `stackDeleted`, `logGroupDeleted` |
| `stop` | `target`, `name`, `sessionId`, `stopped` |
| un error | `{"error": {"code", "message"}, "exitCode"}` por stdout y la línea `rayito: <mensaje>` por stderr, como `rayito setup` |

Cada paso (`steps`) lleva `resource` (`stack`, `image` o `runtime`), `name`,
`action` y `status`; en el plan, `action` es siempre `planned`.

## `rayito dashboard`

```bash
rayito dashboard [--port N] [--allow-actions] [--cost-explorer]
                 [--events-stack NOMBRE] [--log-group GRUPO]
                 [--theme FICHERO] [--logo FICHERO]
                 [--account NOMBRE=ARN_DEL_ROL ...] [--external-id ID] [--open]
rayito [--region R] dashboard cloudwatch [--log-group GRUPO ...]
                 [--log-group-prefix PREFIJO] [--otlp] [--quota] [--output FICHERO]
```

El panel de gestión local, idéntico en las dos CLI (misma página, misma API
`/api/v1`, mismas cabeceras y mismos controles de `Host`, `Origin` y CSRF): un servidor en `127.0.0.1` (puerto al azar salvo
`--port`) con una página y la API `/api/v1` sobre tus credenciales de AWS,
hasta que pulses Ctrl+C. Imprime **una sola vez** la URL de arranque con un
token de un solo uso. Por defecto es de **sólo lectura**: `--allow-actions`
habilita matar, suspender y reanudar con confirmación y una cola que respeta
el TPS; `--cost-explorer` permite consultar el coste facturado (cada consulta
cuesta $0,01, también «Ignorar la caché», y el arranque lo anuncia);
`--events-stack` activa la vista de eventos (lecturas de DynamoDB, de pago
aunque mínimo); reanudar con `--allow-actions` vuelve a facturar cómputo; `--theme` (`.json` o `.css`,
 o `RAYITO_DASHBOARD_THEME`) y `--logo` (PNG o SVG, o `RAYITO_DASHBOARD_LOGO`)
cambian la paleta y el logo, y se validan antes de abrir el puerto. No admite
`--json`. Salida 0 al cerrar y 2 si el puerto es inválido u ocupado o falta
región o credenciales. `--account` (repetible, opt-in) añade otras cuentas de
sólo lectura por roles que se asumen, con `--external-id` si los roles lo
exigen; `rayito dashboard cloudwatch` imprime el JSON de un dashboard de
CloudWatch (logs de `/rayito/*`, `--otlp`, `--quota`) sin llamar a AWS ni
necesitar credenciales (generarlo es gratis; desplegarlo cuesta $3 al mes por
dashboard pasados los 3 gratuitos y $0,005 por GB escaneado en cada widget de
Logs Insights, y `--quota` requiere la pila `quota-alerts`, ~$0,30 al mes); ver
[Panel: CloudWatch, cuentas y avisos](funciones-opcionales/panel-extras.md).
Guía completa en
[Panel de gestión](funciones-opcionales/panel-de-gestion.md#panel-web-local),
con el [recorrido paso a paso y capturas](funciones-opcionales/panel-de-gestion.md#recorrido)
(el panel lo resume en su entrada **Ayuda**),
defensas en [Seguridad](security.md#panel-de-gestion-local). Para tenerlo
siempre disponible dentro de tu cuenta, con inicio de sesión y auditoría:
`rayito stack deploy dashboard --artifact-bucket B`
([Panel en tu cuenta](funciones-opcionales/panel-en-tu-cuenta.md)).

## `rayito setup`

```bash
rayito [--profile P] [--region R] [--json] [--verbose] setup
       [--bucket BUCKET] [--artifact PATH] [--tag K=V ...] [--yes] [--dry-run]
       [--caps-image]
```

Prepara la cuenta y la región para Rayito en una orden idempotente, como
`cdk bootstrap`. Tutorial paso a paso: [Configurar AWS](primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup).

| Opción | Qué hace |
|---|---|
| `--bucket BUCKET` | Bucket de artefactos en la misma región; por defecto `RAYITO_BUCKET`, el de la pila de IAM, el del bootstrap de CDK o uno nuevo `rayito-artifacts-<cuenta>-<región>`. Si no existe, se crea con ese nombre (pila `rayito-m0-artifacts`). |
| `--artifact PATH` | Zip de la imagen ya verificado (p. ej. con cosign); sin él se descarga `rayito-image.zip` de la release `rayd-v<versión del SDK>` y se comprueba contra su `SHA256SUMS`. Sólo vale el zip de la variante `full`, sin EFS. |
| `--tag K=V` | Etiqueta K=V; repetible. Se aplica a las pilas que crea setup (rayito-m0-artifacts y rayito-m0-iam) y CloudFormation la propaga a sus recursos; útil si la organización exige etiquetas. Una pila reutilizada no se reetiqueta y la imagen no recibe etiquetas. |
| `--yes` | No pedir confirmación (hace falta sin terminal o con `--json` si hay algo que crear). |
| `--dry-run` | Sólo lee la cuenta y muestra el plan; no crea ni cambia nada. |
| `--caps-image` | Publica también `rayito-base-caps`, la base del agente en MicroVM (la misma imagen con `additionalOsCapabilities` `ALL`, desde el mismo zip). Es otra versión de imagen que cuesta, así que va apagada: sin la opción el paso sale como `skip` y no lee ni escribe esa imagen. Hace falta para `rayito agent deploy` en MicroVM. |

`RAYITO_BUCKET` lo lee la orden, no el parser, igual que `rayito doctor`.

Con etiquetas, si tu organización las exige:

```bash
rayito setup --tag Owner=platform --tag Project=agents
```

**El plan** son siempre cuatro pasos, en este orden:

| Recurso | Acciones posibles | Escribe | Pide confirmación |
|---|---|---|---|
| `bucket` (pila `rayito-m0-artifacts` si se crea) | `create`, `reuse`, `wait` | `create` | `create` |
| `iamStack` (pila `rayito-m0-iam`) | `create`, `update`, `reuse`, `wait` | `create`, `update` | `create`, `update` |
| `baseImage` (imagen `rayito-base`) | `publish`, `reuse`, `wait` | `publish` | `publish` |
| `capsImage` (imagen `rayito-base-caps`) | `publish`, `reuse`, `wait`, `skip` | `publish` | `publish` |

- **Bucket**, en dos fases. *Reutilización:* el primero que exista de
  `--bucket` o `RAYITO_BUCKET` (si das uno explícito, es el único candidato),
  el parámetro `ArtifactBucket` de la pila de IAM o el bucket de `cdk bootstrap`
  de esa cuenta y región (calificador por defecto), cada uno con un
  `HeadBucket`. Existe en la región: `reuse` (origen `argument`, `environment`,
  `iamStack` o `cdkBootstrap`). Existe en otra región: `bucket_region`. Existe
  pero no es accesible (403): `bucket_unavailable`, salvo el de `cdk bootstrap`,
  que se salta. No existe: con `--bucket` o `RAYITO_BUCKET`, se crea con ese
  nombre; si es el de la pila de IAM o el de `cdk bootstrap`, se prueba el
  siguiente. Una cuenta con un bucket reutilizable nunca pasa a la segunda
  fase ni lee nada más. *Creación:* sin bucket reutilizable, `create` con la
  pila `rayito-m0-artifacts` (plantilla `infra/artifact-bucket.yaml`), con el
  nombre explícito o, si no, `rayito-artifacts-<cuenta>-<región>` (origen
  `rayitoDefault`). Si esa pila ya existe y está completa, `reuse` (origen
  `artifactStack`); si está en curso, `wait`; si está bloqueada,
  `stack_blocked`. El bucket creado está cifrado (SSE-S3), con todo el acceso
  público bloqueado, ACL desactivadas, sólo TLS y con los objetos de
  `rayito/images/` caducando a los 7 días; borrar la pila lo conserva. Un
  bucket que ya existe con el nombre por defecto y no es de esa pila no se
  adopta (`bucket_taken`): si es tuyo, pásalo con `--bucket`.
- **Pila de IAM:** si no existe, `create` con `ArtifactBucket=<bucket>` y
  `TemplateFingerprint`, el sha256 del `infra/iam.yaml` que lleva el SDK. Si
  está en `CREATE_COMPLETE`, `UPDATE_COMPLETE` o `UPDATE_ROLLBACK_COMPLETE` y
  tiene la salida `BuildRoleArn`, `reuse` sólo si su `ArtifactBucket` es el
  del plan y su `TemplateFingerprint` es el de este SDK; si no, `update` con la
  plantilla de este SDK (los demás parámetros conservan su valor). Desde
  0.17.1 una pila de una versión anterior, o desplegada a mano, se actualiza
  así aunque el bucket no cambie: el plan dice «actualizar a la plantilla
  infra/iam.yaml de este SDK» y `previousBucket` es `null`. Una pila creada
  con la plantilla de 0.6.1 o anterior recibe `LegacyCallerPolicy=true`, que
  deja a `CallerPolicy` la descripción con la que se creó: IAM no la puede
  cambiar y CloudFormation sustituiría la política, cuyo nombre fijo choca
  consigo mismo (la actualización se revertiría). Una pila que
  desplegó un Rayito **más nuevo** (su etiqueta `rayito:sdk-version`) nunca
  vuelve a la plantilla de este SDK: con el mismo bucket es `reuse` («ya
  existe (…) con la plantilla de Rayito X, más nuevo que este SDK: se
  conserva», `stackSdkVersion` en `--json`) y, si el plan le cambia el
  bucket, `stack_newer` (salida 2). Así un CI con una versión anterior no
  deshace los permisos de una posterior. Al actualizar la pila, `setup` manda
  de nuevo las etiquetas que ya tenía, con las de `--tag` por encima: una
  etiqueta de una ejecución anterior se conserva aunque no repitas `--tag`
  (para quitarla, `rayito stack deploy` o la consola). En un estado
  `*_IN_PROGRESS`, `wait` (y al terminar, `update` si sigue con otro bucket u
  otra plantilla); en `DELETE_IN_PROGRESS`, `ROLLBACK_COMPLETE` o `*_FAILED`,
  `stack_blocked`.
- **Imagen:** si no existe `rayito-base`, `publish`; con una versión
  `PENDING` o `IN_PROGRESS`, `wait`; con una versión `SUCCESSFUL` y `ACTIVE`,
  `reuse` (sea cual sea el `rayd` con que se construyó; `rayito doctor
  --launch` avisa de uno viejo y `rayito image publish` lo actualiza); con
  sólo versiones fallidas o inactivas, `publish`. El `publish` usa `--artifact`
  o la release `rayd-v<versión del SDK>` y el `latest` de la base gestionada
  de AWS.
- **Imagen con capacidades** (desde 0.17): `rayito-base-caps`, la base que usa
  `rayito agent deploy` en MicroVM. Se decide igual que la imagen y se publica
  desde **el mismo zip** (una sola descarga) con
  `additionalOsCapabilities=["ALL"]`, como
  `rayito image publish --os-capabilities ALL`. Sólo con `--caps-image`: sin
  la opción el paso es `skip` y no se lee ni se publica. Si después
  `rayito agent deploy` (MicroVM) no la encuentra, falla con un mensaje que
  pide `rayito setup --caps-image`.

El plan está `ready` cuando todos los pasos son `reuse` (o `skip`). Pide confirmación si
algún paso es `create`, `update` o `publish`: en una terminal, con
`¿Preparar la cuenta? [y/N]:`; sin terminal o con `--json`, hace falta
`--yes` (`needs_confirmation`, salida 2). Un `wait` no cuesta nada nuevo y no
pide confirmación. Después de aplicar, ejecuta `rayito doctor` (sin `--launch`)
sobre `rayito-base` y el bucket elegido, y termina con
`RAYITO_TEMPLATE=<arn>` y `RAYITO_BUCKET=<bucket>`. La primera ejecución tarda
unos 10–12 minutos, casi todo el snapshot de la imagen en AWS; una cuenta ya
preparada termina en segundos.

```bash
rayito setup --dry-run
```

En una cuenta sin ningún bucket (con uno reutilizable, la primera línea dice
`reutilizar` y no hay línea de coste del bucket):

<!-- noqa: example: salida de consola, no es código -->
```text
rayito setup: cuenta 123456789012, región us-east-1
  bucket rayito-artifacts-<cuenta>-<región>: crear con la pila rayito-m0-artifacts (nombre por defecto de Rayito)
  pila de IAM rayito-m0-iam: crear
  imagen rayito-base: publicar desde rayd-v0.17.0 (≈ 10 min)
  imagen con capacidades rayito-base-caps: omitida (sin --caps-image)
Coste aproximado:
  bucket de artefactos: los zips de la imagen caducan a los 7 días (regla de ciclo de vida)
  pila de IAM: gratis
  imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)
  zip de la imagen en el bucket: céntimos
no se creó ni cambió nada (--dry-run)
```

**Códigos de salida.** `0`: éxito, `--dry-run` o nada que hacer con el doctor
sin `FAIL`. `1`: falla la operación (`stack_blocked`, `stack_incomplete`,
`release_download`, `checksum_missing`, `checksum_mismatch`,
`image_build_failed`, un «no» en la confirmación, un `FAIL` del doctor o un
error de AWS sin traducir). `2`: uso o entorno (`unsupported_region`,
`bucket_unavailable`, `bucket_region`, `bucket_taken`,
`artifact_stack_conflict`, `artifact_missing`, `artifact_variant`,
`needs_confirmation`, `stack_newer`, y los de siempre: sin región, sin
perfil o sin credenciales). Qué hacer con cada uno:
[Solución de problemas](operacion/solucion-de-problemas.md#rayito-setup).

**Con `--json`** sale un único documento por stdout y el progreso por stderr.
Sus claves, en este orden: `rayito` (versión del SDK), `region`, `account`
(`null` si el plan falló), `dryRun`, `ready`, `steps`, `cost`, `result`,
`doctor`, `error` y `exitCode`. Cada elemento de `steps` lleva `resource`
(`bucket`, `iamStack`, `baseImage` o `capsImage`), `name`, `action` (`create`,
`update`, `reuse`, `wait`, `publish` o `skip`), `source` (`argument`, `environment`, `iamStack`,
`cdkBootstrap`, `artifactStack`, `rayitoDefault`, `release`, `artifact` o `null`), `status`, `imageVersion`,
`release`, `previousBucket` y `stackSdkVersion` (la versión de Rayito que
desplegó la pila de IAM, sólo si es más nueva que este SDK y se conserva; si
no, `null`). El `imageVersion` de un paso `publish` es
`null` en el plan y, tras aplicarlo, la versión recién publicada (desde
0.17.1; antes seguía en `null`). `result` (`bucket`, `imageArn`, `imageVersion`,
`artifactUploaded`, `capsImageArn` y `capsImageVersion`, éstos `null` sin
`--caps-image`) es `null` mientras no termine la preparación; `doctor` es
el documento de `rayito --json doctor`, o `null` con `--dry-run` y si la
preparación se detuvo antes; `error` es `{"code", "message"}` o `null`. En un
error el mensaje sale además por stderr como `rayito: <mensaje>`.

```json
{
  "rayito": "0.17.0",
  "region": "us-east-1",
  "account": "123456789012",
  "dryRun": true,
  "ready": false,
  "steps": [
    {"resource": "bucket", "name": "amzn-s3-demo-bucket", "action": "reuse",
     "source": "argument", "status": null, "imageVersion": null,
     "release": null, "previousBucket": null,
     "stackSdkVersion": null},
    {"resource": "iamStack", "name": "rayito-m0-iam", "action": "create",
     "source": null, "status": null, "imageVersion": null,
     "release": null, "previousBucket": null,
     "stackSdkVersion": null},
    {"resource": "baseImage", "name": "rayito-base", "action": "publish",
     "source": "release", "status": null, "imageVersion": null,
     "release": "rayd-v0.17.0", "previousBucket": null,
     "stackSdkVersion": null},
    {"resource": "capsImage", "name": "rayito-base-caps", "action": "skip",
     "source": null, "status": null, "imageVersion": null,
     "release": null, "previousBucket": null,
     "stackSdkVersion": null}
  ],
  "cost": [
    "pila de IAM: gratis",
    "imagen: ≈ $0,04 por semana por versión publicada (mínimo una semana)",
    "zip de la imagen en el bucket: céntimos"
  ],
  "result": null,
  "doctor": null,
  "error": null,
  "exitCode": 0
}
```

!!! info "Coste y activación"
    - **Activa:** ejecutar `rayito setup` y confirmar. `--dry-run` sólo lee la
      cuenta; con una cuenta ya preparada tampoco escribe nada.
    - **Recursos y llamadas AWS:** sin bucket reutilizable, la pila
      `rayito-m0-artifacts` (`CreateStack` sin capacidades: un bucket y su
      política); la pila `rayito-m0-iam`
      (`CreateStack`/`UpdateStack` con `CAPABILITY_NAMED_IAM`); las imágenes
      `rayito-base` y, con `--caps-image`, `rayito-base-caps` (un objeto en el
      bucket, compartido, y `CreateMicrovmImage` o `UpdateMicrovmImage` por
      imagen; la segunda con `additionalOsCapabilities=["ALL"]`); lecturas de `GetCallerIdentity`,
      `DescribeStacks`, `HeadBucket`, `GetMicrovmImage` y
      `ListMicrovmImageVersions`; la descarga HTTPS de `rayito-image.zip` y
      `SHA256SUMS`. El SDK no llama a `CreateBucket`: el bucket lo crea
      CloudFormation con tus credenciales.
    - **Coste aproximado:** pila de IAM, gratis; el bucket no cuesta por existir
      y sus zips caducan a los 7 días; cada versión publicada de cada
      imagen ≈ $0,04 por semana ($0,08 por GB-mes, mínimo una semana; con
      `--caps-image`, una imagen más); el zip en el bucket, céntimos. Ver
      [Precios](cost.md).
    - **IAM:** credenciales de administración o de *bootstrap*, una vez, como
      `cdk bootstrap`: CloudFormation, IAM (roles y políticas), publicar la
      imagen y, si crea el bucket, S3 (`s3:CreateBucket`,
      `s3:PutEncryptionConfiguration`, `s3:PutBucketPublicAccessBlock`,
      `s3:PutBucketOwnershipControls`, `s3:PutLifecycleConfiguration`,
      `s3:PutBucketTagging`, `s3:GetBucketPolicy`, `s3:PutBucketPolicy`). Para
      usar el SDK después, la política mínima de
      [Configurar AWS](primeros-pasos/configurar-aws.md#3-la-pila-de-iam).
    - **Cómo apagarla:** no ejecutarla; sin `--caps-image` (por defecto) no
      publica `rayito-base-caps`; para deshacerla, borra las pilas
      (`rayito-m0-iam` y, si existe, `rayito-m0-artifacts`) y las versiones de la
      imagen. El bucket creado se conserva: vacíalo y bórralo a mano.
    - **Ejemplo:** `rayito setup --dry-run`.

!!! warning "El hash no prueba la autenticidad"
    La comprobación contra `SHA256SUMS` detecta un zip truncado, corrupto o
    equivocado, pero no que sea auténtico (`SHA256SUMS` sale de la misma
    release). Para la firma de cosign, verifica el zip y pásalo con
    `--artifact`: [Verificar una release](verify.md).

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
  "rayito": "0.11.0",
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
    {"sdk_series": "0.8", "min_agent_version": "0.8.0", "note": "…"},
    {"sdk_series": "0.9", "min_agent_version": "0.9.0", "note": "…"},
    {"sdk_series": "0.10", "min_agent_version": "0.10.0", "note": "…"},
    {"sdk_series": "0.11", "min_agent_version": "0.11.0", "note": "…"}
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
- Completado de shell.
