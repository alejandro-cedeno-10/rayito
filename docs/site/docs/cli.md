# CLI

`rayito` es la herramienta de línea de comandos del SDK: publica y lista la
imagen, borra versiones antiguas, lista, inspecciona y mata sandboxes, lee
sus logs de CloudWatch y diagnostica una cuenta antes del primer
`Sandbox.create()`. Es una CLI **operativa** sobre el flujo Dockerfile que ya
existe: no hay templates declarativos ni `rayito.toml`.

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
  la cadena habitual (`AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, el
  fichero de configuración, el rol de la máquina). No hay API key de Rayito.
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
    [--memory-mib 2048] [--timeout-seconds 1800] [--force]
```

Reproduce el pipeline de `make image-publish`:

1. Comprueba, antes de llamar a AWS, que el zip existe y que su marcador de
   variante (`warmup_variant` para `slim`, `kernels_variant` para `poly`)
   coincide con `--variant`.
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

Permisos S3 del principal que publica (la `CallerPolicy` de
`infra/iam.yaml` los concede tal cual): `s3:PutObject` y `s3:GetObject`
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
1 si alguna candidata sigue existiendo. Nunca borra la imagen.

### `image zip`

```bash
rayito image zip image image/rayito-image.zip [--variant full|slim|poly] [--sidecar kernel-sidecar]
```

Zip determinista (fechas y modos fijos; sin `__pycache__`, `tests`, `.venv`,
cachés, `uv.lock` ni otros zips) con el `Dockerfile` en la raíz. Con
`--sidecar` copia antes `kernel-sidecar/` a `image/kernel-sidecar/` con las
mismas exclusiones. Imprime ficheros, bytes, variante y el sha256 del zip
(con él se predice la clave S3). Es el único comando que no necesita AWS.

## `rayito sandbox`

```bash
rayito sandbox list [--template T] [--template-version V] [--all-states]
rayito sandbox info ID [--no-metadata]
rayito sandbox kill ID… | rayito sandbox kill --all [--template T] [--yes]
rayito sandbox logs ID [--log-group G] [--limit 1000] [--since 30m|2h|1d|ISO]
rayito sandbox create [TEMPLATE] [--timeout S] [--metadata K=V]… [--env K=V]… [--detach] [--token-file F] [--user U] [--cwd D]
rayito sandbox connect ID [--user U] [--cwd D] [--env K=V]… [--token-file F]
rayito sandbox exec ID [--background] [--cwd D] [--user U] [--env K=V]… [--timeout 0] [--token-file F] -- CMD…
rayito sandbox metrics ID [--follow] [--interval 5] [--token-file F]
```

- `list` omite `TERMINATING` y `TERMINATED` (AWS los sigue listando unos 20
  minutos) salvo con `--all-states`; no sondea ningún endpoint.
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

### `create`, `connect`, `exec` y `metrics` (M9)

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

## `rayito doctor`

```bash
rayito doctor [--template rayito-base] [--template-version V] [--bucket B] [--launch] [--json]
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

Sin `--launch` el doctor **nunca crea un MicroVM**: las comprobaciones 8–10
usan el sandbox `RUNNING` más nuevo de la imagen o quedan en `SKIP`. Con
`--launch` crea uno de 300 s (`idle=None`, `logging="disabled"`,
`metadata={"rayito": "doctor"}`), lo usa para 8–10 y lo mata en un `finally`
pase lo que pase (también con Ctrl-C); cuesta ≈ $0,002 (lectura del snapshot
de ≈ 0,9 GB más unos segundos de cómputo) y su id aparece como
`launched_sandbox_id`.

Salida humana: una línea por comprobación (`OK   credentials  cuenta …`), los
detalles indentados en `WARN`/`FAIL`, la tabla de compatibilidad y
`rayito doctor: N OK, N WARN, N FAIL, N SKIP`. Con `--json`:

```json
{
  "rayito": "0.2.0",
  "region": "us-east-1",
  "account": "123456789012",
  "principal_kind": "assumed-role",
  "checks": [{"name": "credentials", "status": "OK", "summary": "…", "details": {}}],
  "compatibility": [
    {"sdk_series": "0.1", "min_agent_version": "0.1.0", "note": "…"},
    {"sdk_series": "0.2", "min_agent_version": "0.2.0", "note": "…"},
    {"sdk_series": "0.3", "min_agent_version": "0.3.0", "note": "…"}
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
| `make image-zip` | `python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar` y `python scripts/image_zip.py image image/rayito-image.zip` | `rayito image zip image image/rayito-image.zip --sidecar kernel-sidecar` |
| `make image-publish` (`-slim`, `-poly`, `-caps`) | `uv run --project clients/python python scripts/publish_image.py --artifact … --bucket $(BUCKET) --base-image-version 1` | `rayito image publish --artifact … --bucket … --base-image-version 1` |
| `make image-prune PRUNE_ARGS="--keep 5 --dry-run"` | `uv run --project clients/python python scripts/image_prune.py --image-name rayito-base --keep 5 --dry-run` | `rayito image prune --keep 5 --dry-run` |

`image_zip.py` y `copy_sidecar.py` sólo usan la biblioteca estándar (cargan
`rayito/cli/_artifact.py` por ruta de fichero) porque CI y la release los
ejecutan con un `python3` pelado sin instalar el SDK. `publish_image.py` e
`image_prune.py` necesitan el entorno del cliente (`typer` y el SDK) y sin él
imprimen el comando `uv run --project clients/python …` y salen con 2.

## Lo que la CLI no hace

- Templates declarativos (`rayito.toml`, `rayito template build`): la imagen
  se construye desde `image/Dockerfile` con `create-microvm-image`, como
  siempre (`SPEC.md` §4).
- Construir la imagen más allá del zip: el Dockerfile lo construye AWS.
- `sandbox logs --follow`: para eso están los SDKs y el
  [servidor MCP](mcp.md).
- `auth`, `template`, `snapshots` y `fork` de la CLI de E2B: no tienen
  primitiva o quedan fuera por `SPEC.md` §4 ([Compatibilidad con E2B](e2b-compat.md)).
- Envolver las herramientas de desarrollo (`bench_cold_start.py`,
  `hooks-sim.py`, `gen_limits.py`, `check_*.py`).
- Una CLI en TypeScript ni completado de shell.
