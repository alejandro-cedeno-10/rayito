# Compatibilidad con E2B

Desde M9 (Rayito 0.3.0), `rayito.e2b` (Python) y `rayito/e2b` (TypeScript)
son un drop-in a nivel de import para los SDKs de E2B **2.x** (contrato:
`e2b` 2.51.0 y `e2b-code-interpreter` 2.10.0). Cambia la línea de import y
el resto del programa sigue igual. Qué hay y qué no, fila a fila, está en
[Paridad con E2B](e2b-parity.md); esta página explica cómo migrar y en qué
se comporta distinto.

## Migrar: cambia un import

=== "Python"

    ```python
    # antes
    # from e2b_code_interpreter import Sandbox
    from rayito.e2b import Sandbox

    with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
        print(sbx.run_code("1 + 1").text)            # "2"
        print(sbx.commands.run("echo hola").stdout)  # "hola\n"
        sbx.files.write("/home/user/a.txt", "a")
        sbx.set_timeout(600)                         # rayd mueve el plazo
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito.e2b import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(timeout=300) as sbx:
            print((await sbx.run_code("1 + 1")).text)
            await sbx.set_timeout(600)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    // antes: import { Sandbox } from "@e2b/code-interpreter";
    import { Sandbox } from "rayito/e2b";

    await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
    console.log((await sbx.runCode("1 + 1")).text); // "2"
    console.log((await sbx.commands.run("echo hola")).stdout);
    await sbx.setTimeout(600_000);
    ```

Lo que cambia fuera del código:

1. **Credenciales de AWS en vez de `E2B_API_KEY`.** El SDK usa la cadena
   por defecto de boto3 / AWS SDK v3 (`AWS_PROFILE=<tu-perfil>`,
   `AWS_REGION`); `api_key`, `domain` y compañía se aceptan y se ignoran con
   un `RayitoCompatWarning`.
2. **Una imagen M9 en tu cuenta** en lugar del template de E2B:
   `RAYITO_TEMPLATE=rayito-base` (o `template=`). Para JS/TS hace falta
   `rayito-base-poly` y para `allow_internet_access=False`/`network=`,
   `rayito-base-caps` ([Imágenes e IAM](images.md)).
3. **`upload_url`/`download_url`** necesitan un bucket tuyo:
   `RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket` ([Ficheros](files.md)).

!!! warning "Los shims exigen una imagen M9"
    El shim siempre pide a `rayd` un plazo lógico. Contra una imagen anterior
    a M9, `create()` termina el VM y lanza `UnimplementedError("lifecycle")`
    nombrando la imagen M9. Publica `rayito-base` desde este árbol
    (`rayito image publish`) antes de migrar.

## Tabla de imports

=== "Python"

    | Antes | Después |
    |---|---|
    | `from e2b_code_interpreter import X` | `from rayito.e2b import X` |
    | `from e2b import X` | `from rayito.e2b import X` |
    | `from e2b.exceptions import X` | `from rayito.e2b.exceptions import X` (las excepciones también se exportan desde `rayito.e2b`) |

    Nombres que exporta `rayito.e2b` (su `__all__`):

    | Grupo | Nombres |
    |---|---|
    | Sandbox y cliente | `Sandbox`, `AsyncSandbox`, `E2B`, `ConnectionConfig`, `SandboxInfo`, `SandboxMetrics`, `SandboxQuery`, `SandboxState`, `SandboxPaginator`, `AsyncSandboxPaginator`, `ListedSandbox`, `ALL_TRAFFIC` |
    | Comandos y PTY | `CommandHandle`, `AsyncCommandHandle`, `CommandResult`, `ProcessInfo`, `PtySize`, `PtyOutput`, `Stdout`, `Stderr`, `OutputHandler`, `Username` |
    | Ficheros | `EntryInfo`, `WriteInfo`, `WriteEntry`, `FileType`, `FilesystemEvent`, `FilesystemEventType`, `WatchHandle`, `AsyncWatchHandle`, `UploadTicket`, `DownloadLink` |
    | Git | `Git`, `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode` ([Git](git.md)) |
    | Code interpreter | `Execution`, `Result`, `Logs`, `OutputMessage`, `ExecutionError`, `Context`, `MIMEType`, `RunCodeLanguage` y los charts `ChartType`, `ScaleType`, `Chart`, `Chart2D`, `PointData`, `LineChart`, `ScatterChart`, `BarChart`, `BarData`, `PieChart`, `PieData`, `BoxAndWhiskerChart`, `BoxAndWhiskerData`, `SuperChart` |
    | Excepciones | `SandboxException`, `TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`, `InvalidArgumentException`, `AuthenticationException`, `RateLimitException`, `CommandExitException`, `NotEnoughSpaceException`, `ServiceBusyException`, `FileUploadException`, `GitAuthException`, `GitUpstreamException`, `TemplateException`, `BuildException` |
    | Sin primitiva (importan, pero toda llamada lanza) | `Template`, `AsyncTemplate`, `Volume`, `AsyncVolume`, `Secret`, `AsyncSecret`, `get_signature` |
    | Sólo Rayito | `UnimplementedError`, `RayitoCompatWarning` |

=== "TypeScript"

    | Antes | Después |
    |---|---|
    | `import { Sandbox } from "@e2b/code-interpreter"` | `import { Sandbox } from "rayito/e2b"` |
    | `import Sandbox from "e2b"` | `import Sandbox from "rayito/e2b"` (export por defecto) |
    | `import { Sandbox, X } from "e2b"` | `import { Sandbox, X } from "rayito/e2b"` |

    Nombres que exporta `rayito/e2b`:

    | Grupo | Nombres |
    |---|---|
    | Sandbox y cliente | `Sandbox` (también por defecto), `SandboxPaginator`, `E2B`, `ConnectionConfig`, `ALL_TRAFFIC`; tipos `SandboxOpts`, `SandboxConnectOpts`, `SandboxInstanceConnectOpts`, `SandboxInfo`, `SandboxMetrics`, `SandboxMetricsOpts`, `SandboxLifecycle`, `SandboxNetworkOpts`, `SandboxNetworkUpdate`, `SandboxListOpts`, `SandboxPauseOpts`, `SandboxUrlOpts`, `SandboxState`, `ConnectionOpts`, `E2BClientOpts`, `Username`, `Logger` |
    | Comandos, ficheros y PTY | `Filesystem`, `Pty`, `FileType`, `FilesystemEventType`, `DEFAULT_WATCH_TIMEOUT_MS`; tipos `CommandHandle`, `CommandResult`, `EntryInfo`, `WriteInfo`, `FilesystemEvent`, `WatchOpts`, `PtySize`, `PtyCreateOpts`, `PtyConnectOpts` |
    | Git | `Git`; tipos `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode` ([Git](git.md)) |
    | Code interpreter | `Execution`, `Result`; tipos `Context`, `Logs`, `OutputMessage`, `ExecutionError` |
    | Errores (las clases nativas de `rayito`: `instanceof` vale entre los dos) | `SandboxError`, `TimeoutError`, `NotFoundError`, `FileNotFoundError`, `SandboxNotFoundError`, `InvalidArgumentError`, `AuthenticationError`, `RateLimitError`, `CommandExitError`, `NotEnoughSpaceError` (= `DiskFullError`), `ServiceBusyError` (= `CapacityError`), `FileUploadError`, `GitAuthError`, `GitUpstreamError`, `TemplateError`, `BuildError`, `UnimplementedError` |
    | Sin primitiva (importan, pero toda llamada lanza) | `Template`, `Volume`, `Secret`, `getSignature` |

    `rayito/e2b` es una entrada del mismo paquete npm `rayito` (ESM y
    CommonJS): no hay que instalar nada más.

## Valores por defecto

Los shims siguen los valores por defecto de **E2B**, no los del SDK nativo:
`timeout=300` es el plazo lógico del sandbox con `on_timeout='kill'` (lo
impone `rayd`, [más abajo](#plazo-y-ciclo-de-vida-m9-server-timeout)), sin
auto-suspensión por inactividad, endpoint público (`ALL_INGRESS`) y salida a
internet (`INTERNET_EGRESS`). El tope de la plataforma (`max_lifetime`,
kwarg sólo de Rayito; TS `maxLifetimeMs`) es `max(3600, min(timeout + 60,
28800))` si no lo pasas. Si quieres los defaults de Rayito (auto-suspensión a
los 300 s de inactividad, conectores explícitos), usa `rayito.Sandbox`; el
nativo siempre está en `sbx.native`. Lo que Lambda MicroVMs no puede hacer
lanza `UnimplementedError` (un `NotImplementedError`, nunca `SandboxException`;
es la misma clase que `rayito.UnimplementedError`) con la feature y el motivo:
nada se aproxima en silencio.

## Funciona sin cambios

| E2B | Rayito |
|---|---|
| `with Sandbox.create() as sbx:` / `async with await AsyncSandbox.create()` / `await Sandbox.create()` | igual (`RAYITO_TEMPLATE` en lugar del template por defecto de E2B) |
| `sbx.run_code(code)` → `Execution` con `text`, `results`, `logs.stdout`, `error`; charts (`Result.chart`, `png`) | igual |
| `run_code(language="python" / "bash")`, `create_code_context`, `list_code_contexts`, `remove_code_context`, `restart_code_context` | igual; `bash` sólo en `rayito-base-poly` |
| `sbx.commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, stdin, timeout, request_timeout)` (también posicional), `commands.list/kill/connect/send_stdin/close_stdin`, `CommandHandle.wait(on_pty, on_stdout, on_stderr)` | el `Commands` nativo |
| `sbx.files.read/write/write_files/list/exists/get_info/remove/rename/make_dir`, `files.watch_dir(path, on_event=...)` | igual |
| `sbx.pty.create(PtySize(rows, cols), on_data=...)`, `send_stdin`, `resize`, `kill`, `connect(pid)` | igual (`PtySize` en el orden de E2B) |
| `sbx.get_host(port)` | un `str` con el hostname más `.headers` con las cabeceras del proxy (TS: `getHost(port)` síncrono devuelve el hostname) |
| `sbx.kill()`, `Sandbox.kill(id)`, `sbx.is_running(request_timeout=)`, `sbx.sandbox_id`, `sbx.sandbox_domain` | igual |
| `sbx.pause()` / `beta_pause()` → `bool`, `Sandbox.connect(id)` sobre un sandbox pausado | `suspend-microvm` y `resume-microvm` |
| `Sandbox.list(query=SandboxQuery(metadata=...))` sobre sandboxes `RUNNING` | filtro en cliente, O(n) ([coste](#el-coste-de-listquerysandboxquerymetadata)) |
| Las excepciones de `e2b.exceptions` (`TimeoutException`, `NotFoundException`, `FileNotFoundException`, `SandboxNotFoundException`, `CommandExitException`, `GitAuthException`, ...) | las mismas clases nativas bajo esos nombres; `NotEnoughSpaceException` es `DiskFullException` y se lanza con el disco lleno; `ServiceBusyException` se lanza ante `InsufficientCapacityException`; `TemplateException` y `BuildException` existen pero nunca se lanzan |

## Se mapea, con una nota

| E2B | Rayito |
|---|---|
| `timeout` (300 s por defecto) | plazo lógico impuesto por `rayd` bajo `max_lifetime` (3600 por defecto, ≤ 28800; kwarg sólo de Rayito): `max(3600, min(timeout + 60, 28800))` si no lo pasas. Al vencer, `rayd` sale y la VM pasa a `TERMINATED` ≈ 15 s después (Q58) |
| `sbx.set_timeout(t)` / `Sandbox.set_timeout(id, t)` | `SetTimeout` EXACT: alarga o acorta el plazo, hasta `max_lifetime`; más allá, `InvalidArgumentException` con el plazo intacto. La forma de clase necesita el access token (`access_token=` o `RAYITO_ACCESS_TOKEN`) |
| `Sandbox.connect(id, timeout)` / `sbx.connect(timeout)` | reanuda si hace falta y **sólo alarga** el plazo (`AT_LEAST`, como E2B); la forma de clase necesita el access token, que E2B no tiene |
| `lifecycle={"on_timeout": ..., "auto_resume": ...}` | `on_timeout='kill'` exacto; `'pause'` suspende al vencer con un cliente vivo y, sin cliente, lo hace la política de idle en `max_idle` (300 s); `auto_resume=True` reanuda con un plazo nuevo de `max(timeout, 300 s)`, `False` lo hace cumplir `rayd` |
| `Sandbox.beta_create(auto_pause=True)` | `lifecycle={"on_timeout": "pause"}` (E2B lo retiró en 2.23.0; se conserva) |
| `TimeoutException` | también cuando una llamada choca con el plazo (`sandbox_timeout`) |
| `sbx.upload_url(path, user, use_signature_expiration)` / `uploadUrl` | URL de S3 prefirmada con **tus** credenciales ([Ficheros](files.md)): `PUT` con el cuerpo en crudo (`form=True` da los campos de un `POST`); el fichero aterriza de forma asíncrona (barrera en `rayd` y `ticket.wait()`); de un solo uso; caducidad siempre fijada (≤ 7 días y ≤ la vida de tus credenciales); `use_signature_expiration <= 0` lanza; exige un bucket de transferencias: el shim no acepta `transfer=`, así que se configura con `RAYITO_TRANSFER_BUCKET` (y `RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`); sin él `UnimplementedError`; `upload_url(path=None)` lanza. Python devuelve el `UploadTicket` nativo (un `str` con `.headers` y `wait()`); TS devuelve la URL como `string` (`sbx.native.files.uploadUrl()` da el ticket con `wait()`) |
| `sbx.download_url(...)` / `downloadUrl` | URL `GET` prefirmada (admite `Range`) sobre una foto tomada al llamar; un fichero que no existe lanza al llamar; en el shim async ambos métodos son corrutinas |
| `files.read(gzip=)`, `files.write(gzip=)`, `metadata=`, `stream_idle_timeout=`, `use_octet_stream=`; JS `format: "blob"` | gzip de gRPC (las respuestas lo piden con la cabecera `rayito-compress`); `metadata` como xattrs `user.rayito.*` (claves en minúsculas, sobrescribir reemplaza el conjunto); `use_octet_stream` se acepta sin efecto (gRPC no tiene formulario multipart) |
| `sbx.get_metrics(start, end)` → `[SandboxMetrics]` en bytes, con `mem_cache` | el historial de `rayd` (una muestra cada 5 s, anillo de 8 h, **hueco** mientras está suspendido; [Observabilidad](observability.md)); sin `start`/`end`, un historial vacío o una imagen anterior a M9 dan la instantánea de `get_metrics()` como único punto; con rango en una imagen anterior a M9, `UnimplementedError` |
| `Sandbox.get_metrics(id, start, end)` | necesita el access token del sandbox; sin él, `UnimplementedError` sin llamar a nada |
| `Sandbox.list(limit=, next_token=, query=SandboxQuery(state=, started_after=, template=), order=)` → `SandboxPaginator` | `next_token` es un cursor opaco sobre el `nextToken` de `list-microvms`; `order` se calcula en cliente (recorre todas las páginas antes del primer item); un token reanudado salta por identidad; `state=[PAUSED]` = `SUSPENDING`/`SUSPENDED` de AWS |
| `allow_internet_access=False` / `allowInternetAccess: false` | política deny-all **dentro del guest** en `rayito-base-caps` ([Red saliente](network.md)): ninguna conexión sale del VM, aunque los nombres pueden seguir resolviéndose por los resolvedores de la plataforma en el guest; en cualquier otra imagen el shim termina el VM y lanza `UnimplementedError` (sin conector de egress el MicroVM sigue saliendo: `AWS_API_NOTES.md` Q44, Q60) |
| `network={"allow_out", "deny_out", "egress_proxy"}` (CIDR, IP, `ALL_TRAFFIC`, `*.dominio`, selectores invocables) | la semántica de E2B en `rayito-base-caps`: rutas por uid y, para nombres de host o `egress_proxy`, un proxy local de `rayd`; los clientes que no honran `HTTPS_PROXY` fallan cerrados |
| `sbx.update_network(network)` / `Sandbox.update_network(id, network)` | `NetworkService.UpdateNetwork`: cambia la política del guest de forma atómica para las conexiones nuevas; devuelve `None` |
| `network.https_ports` | se valida (1–65535); una lista no vacía es `UnimplementedError`: medido, el proxy no reenvía TLS extremo a extremo a un puerto del guest (fila Q67 de `AWS_API_NOTES.md`); `get_host(puerto)` sirve HTTP en claro |
| `network.allow_public_traffic=False` | se acepta sin efecto: es el comportamiento permanente |
| `run_code(language="javascript" / "js" / "typescript" / "ts")` | el kernel de Deno de `rayito-base-poly` ([Kernels](kernels.md)); en otra imagen `UnimplementedError` nombrando `rayito-base-poly` |
| `sbx.git` (`clone`, `status`, `commit`, `push`, `pull`, `dangerously_authenticate`, ...) | la API git de E2B sobre `commands.run` con `git-core` de la imagen ([Git](git.md)); las credenciales de `dangerously_authenticate` quedan en `~/.git-credentials`, **visibles para el código del sandbox** |
| `sbx.get_info()` / `Sandbox.get_info(id)` → `SandboxInfo` 2.x | `sandbox_domain` = endpoint; `cpu_count`/`memory_mb` como los ve el guest; `envd_version` = `agent_version`; `end_at` = el plazo lógico; `lifecycle`, `network` y `allow_internet_access` rellenos; `volume_mounts` siempre vacío |
| `sbx.traffic_access_token` (TS `trafficAccessToken`), `envd_api_url`, `envd_direct_url` (sólo Python) | el JWE vigente de `x-aws-proxy-auth` (rota, TTL ≤ 60 min: es una credencial al portador) y `https://<endpoint>`; toda petición necesita las cabeceras del proxy |
| `metadata`, `envs` | viajan en `runHookPayload` (4096 caracteres entre los dos); no son secretos |
| `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`, `validate_api_key`, `api_headers`, `secure=False` | ignorados con un `RayitoCompatWarning` cada uno: las credenciales son las de AWS |
| `proxy=` | se honra (sólo `http://host:puerto`) en los canales gRPC y en botocore |
| `headers=`, `retries=`, `ConnectionConfig.set_integration`, `sbx.connection_config` | cabeceras extra en cada RPC (las del proxy, del token y de gRPC están reservadas), reintentos de botocore y `user_agent_extra` |
| `logger=` | los logs del SDK de ese sandbox van al `logging.Logger` dado (TS: un `Logger` con `debug`/`info`/`warn`/`error`), distinto del `logging=` nativo, que es CloudWatch |
| JS `sbx.getHost(port)` | síncrono, devuelve el hostname como E2B; las cabeceras del proxy que toda petición necesita salen de `await sbx.getHostHeaders(port)` |
| JS `signal` (`AbortSignal`) en `ConnectionOpts` | cancela las llamadas del plano de control y los RPC en curso; rechaza con `signal.reason` |
| `E2B(...)` (cliente ligado) | liga `region`, `session` y `control_plane`; `.Template`, `.Volume` y `.Secret` lanzan |
| Kwargs nativos (`region`, `session`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `control_plane`, `transport`, ...) | se pasan tal cual; `idle`, `egress` y `pool` no se aceptan (`TypeError`) |

## Lanza `UnimplementedError`

El mensaje es siempre `<feature> no está disponible en Rayito: <motivo>. Ver
docs/site/docs/e2b-compat.md`, y el error lleva `feature`, `reason` y `doc`.
Python lanza al llamar; en TypeScript un método asíncrono devuelve la promesa
rechazada. Las claves y los motivos de esta tabla son los de
`clients/python/src/rayito/e2b/_unimplemented.py` y
`clients/typescript/src/e2b/unimplemented.ts`, idénticos en los dos SDKs:

| Feature (Python) | Feature (TypeScript) | Motivo, literal |
|---|---|---|
| `fork`, `create_snapshot`, `list_snapshots`, `delete_snapshot` | `fork`, `createSnapshot`, `listSnapshots`, `deleteSnapshot` | ninguna operación de Lambda MicroVMs copia la memoria de un MicroVM en marcha (AWS_API_NOTES.md §1 y §15); el análogo de sólo ficheros es checkpoint_files() + create(persist=) |
| `connect(on_resume='reboot')` | `connect({ onResume: 'reboot' })` | resume-microvm siempre restaura memoria y disco (AWS_API_NOTES.md §5); el análogo es reincarnate(), con un id nuevo |
| `pause(keep_memory=False)`, `lifecycle.on_timeout.keep_memory=False` | `pause({ keepMemory: false })`, `lifecycle.onTimeout.keepMemory=false` | suspend-microvm siempre guarda memoria y disco (AWS_API_NOTES.md §5); el análogo es checkpoint_files() + kill() |
| `network.rules` | `network.rules` | no hay un proxy de egress fuera del VM donde inyectar cabeceras: el proxy de Lambda MicroVMs sólo gestiona el ingress (AWS_API_NOTES.md §7) |
| `network.mask_request_host` | `network.maskRequestHost` | el proxy de Lambda MicroVMs siempre reenvía Host: &lt;endpoint&gt; y no lo reescribe (AWS_API_NOTES.md §7) |
| `network.allow_public_traffic=True` | `network.allowPublicTraffic=true` | no existe acceso sin autenticar: toda petición al endpoint exige X-aws-proxy-auth (AWS_API_NOTES.md §3 y §7); allow_public_traffic=False es el comportamiento permanente |
| `iam` | `iam` | los MicroVMs no emiten tokens con audiencia: la única identidad es el execution role por IMDSv2 (AWS_API_NOTES.md §9) |
| `mcp`, `get_mcp_url`, `get_mcp_token` | `mcp`, `getMcpUrl`, `getMcpToken` | cada petición al endpoint necesita además un JWE en cabecera con TTL de 60 min como máximo (AWS_API_NOTES.md §3 y §7), así que una URL con token fijo no sirve; usa el servidor rayito-mcp |
| `volume_mounts`, `Volume` (y `AsyncVolume`) | `volumeMounts`, `Volume` | SPEC.md §4 deja fuera EFS y los montajes compartidos; usa persist= (S3) o upload_url/download_url |
| `get_signature` | `getSignature` | una firma de envd no autentica en el proxy: el JWE sólo viaja en cabecera o en el subprotocolo WebSocket (AWS_API_NOTES.md §7); usa upload_url/download_url, que firman en S3 |
| `Secret` (y `AsyncSecret`) | `Secret` | necesita un almacén de secretos en un plano de control y un inyector de egress fuera del VM (SPEC.md §4; AWS_API_NOTES.md §7) |
| `Template` (y `AsyncTemplate`) | `Template` | SPEC.md §4 deja fuera los templates declarativos; construye la imagen con un Dockerfile y rayito image publish |

`E2B(...).Template`, `.Volume` y `.Secret` lanzan lo mismo. Los casos
siguientes dependen de la imagen o de la configuración y llevan su propio
motivo (texto de Python; TypeScript usa el mismo motivo con los nombres de la
feature en camelCase, salvo las diferencias que se listan tras la tabla):

| Caso | Feature | Motivo, literal |
|---|---|---|
| `run_code(language=...)` / `create_code_context(language=...)` con algo distinto de `python`, `bash`, `javascript`/`js`, `typescript`/`ts` (`r`, `java`) | `run_code(language='r')` | kernels disponibles: python en toda imagen; bash, javascript y typescript en la variante rayito-base-poly; R y Java no (SPEC.md §4) |
| un kernel que la imagen no trae (`bash`, `javascript` o `typescript` fuera de `rayito-base-poly`) | `run_code(language='typescript')` | este kernel sólo existe en la variante rayito-base-poly (publícala con make image-publish-poly y úsala como template) |
| `Sandbox.list(query=SandboxQuery(metadata=...), state=[PAUSED])` | `list(state=PAUSED, query.metadata)` | los metadatos viven en el agente; leerlos despertaría el sandbox |
| `allow_internet_access=False` o `network=` fuera de `rayito-base-caps` (el VM se termina antes de lanzar) | `allow_internet_access=False` / `network` | la imagen no aplica política de egress en el guest (Health.egress_enforcement=NONE): usa una imagen M9 de rayito-base-caps (additionalOsCapabilities ALL) o, a nivel de plataforma, rayito.Sandbox.create(egress=[&lt;ConnectorArn de infra/egress-connector.yaml&gt;]); el sandbox se ha terminado |
| `update_network` fuera de `rayito-base-caps` | `update_network` | la imagen no tiene CAP_NET_ADMIN: la política de egress exige una imagen M9 de rayito-base-caps (additionalOsCapabilities ALL) |
| `network.https_ports` no vacío | `network.https_ports` | el proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del guest (medido, fila 67 QE2 de AWS_API_NOTES.md §16); get_host(puerto) sirve HTTP en claro |
| `upload_url`/`download_url` sin bucket de transferencias | `upload_url` / `download_url` | configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET (TS: configura transfer: { bucket } (S3Staging) o RAYITO_TRANSFER_BUCKET) |
| una URL de subida o de bajada contra un `rayd` anterior a M9 (sin transferencias) | `upload_url` / `download_url` | actualiza la imagen: este rayd no tiene transferencias |
| el historial de métricas por la forma de clase, sin access token ni `RAYITO_ACCESS_TOKEN` | `Sandbox.get_metrics(sandbox_id)` | rayd exige el access token del sandbox (x-access-token): pásalo con access_token= o define RAYITO_ACCESS_TOKEN |
| el historial de métricas con rango (`start`/`end`) o por la forma de clase, contra una imagen anterior a M9 | `get_metrics(start=, end=)` / `Sandbox.get_metrics(sandbox_id)` (TS: `getMetrics({ start, end })` / `Sandbox.getMetrics(sandboxId)`) | la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9 |
| cualquier `create()` contra una imagen anterior a M9 (el VM se termina antes de lanzar) | `lifecycle` | la imagen no impone el timeout del servidor: publica una imagen M9 (ADR-011) |
| `cpu`/`memory` por sandbox, la CLI de templates/snapshots/fork de E2B | — | no existen: fuera del alcance (`SPEC.md` §4); el tamaño es de la imagen |

El SDK nativo sigue la misma regla: `get_metrics_history()` (TS
`getMetricsHistory()`), en instancia y en la forma de clase, contra un `rayd`
anterior a M9 lanza `UnimplementedError` con el mismo motivo (feature
`get_metrics_history` / `Sandbox.get_metrics_history(sandbox_id)`; TS
`getMetricsHistory` / `Sandbox.getMetricsHistory(sandboxId)`), como las
transferencias y el plazo del servidor; el error gRPC `UNIMPLEMENTED` queda en
`__cause__` (TS `cause`). Sin rango, el `get_metrics()` del shim sigue cayendo
en la instantánea.

Diferencias reales entre los dos shims:

- **`list` con `paused` y `metadata`.** Los dos lanzan `UnimplementedError`
  con el mismo motivo antes de tocar AWS; la feature es
  `list(state=PAUSED, query.metadata)` en Python y
  `list(query.state=paused, query.metadata)` en TS.
- **`getMetrics` de clase sin token.** Sin `accessToken` ni
  `RAYITO_ACCESS_TOKEN`, TS lanza `AuthenticationError` (el mismo error que
  `Sandbox.getMetricsHistory(sandboxId)` del SDK nativo), no
  `UnimplementedError`: en TS la falta de credencial es un error de
  autenticación a propósito. Python lanza `UnimplementedError` con el motivo
  de la tabla. En los dos casos no se llama a AWS.
- **Mensaje de `UnimplementedError`.** `feature` y `reason` coinciden; el
  texto de `str(error)` / `error.message` difiere en la frase de enlace
  (Python: «no está disponible en Rayito»; TS: «no está disponible»), así que
  compara `feature` y `reason`, no el mensaje.

## Diferencias por cambio de M9

### Plazo y ciclo de vida (`m9-server-timeout`)

- El tope `max_lifetime` cuenta el tiempo suspendido: 8 h como mucho desde el
  arranque. E2B guarda sandboxes pausados sin límite.
- En modo `pause` los procesos siguen corriendo entre el vencimiento y la
  suspensión (≈ 3 s con cliente vivo, hasta `max_idle` sin él), y la política
  de idle puede suspender antes del plazo: un trabajo desenganchado se congela
  tras `max_idle` sin tráfico.
- Tras una salida en modo `kill` se facturan ≈ 15 s de 502 (Q58).
- El plazo nunca pasa de `max_lifetime − 60 s` desde el arranque.
- Una suspensión real de menos de 2 s que cruza el plazo no se reconoce como
  congelación.
- Más allá de `max_lifetime` sólo queda `rayito.Sandbox.reincarnate()`
  ([Persistencia](persistence.md)).

### Ficheros y URLs (`m9-file-transfer`)

- Subida por `PUT` con el cuerpo en crudo en vez del `POST` multipart de envd
  (`form=True` da los campos de un `POST` para navegadores).
- La subida aterriza de forma asíncrona: una lectura, un comando o una celda
  posterior la esperan hasta 2 s (barrera de `rayd`); `ticket.wait()` la espera
  entera.
- Tickets de un solo uso; la descarga es una foto tomada al llamar (una
  exportación que se reencola tras una suspensión vuelve a leer el fichero).
- Caducidad siempre fijada; hace falta un bucket de transferencias.

### Red saliente (`m9-egress-policy`)

- Sólo en `rayito-base-caps`; en otras imágenes falla cerrado.
- Las reglas por nombre de host sólo valen en 80/443 y a través del proxy
  local; los clientes que no lo honran fallan cerrados.
- Bajo deny-all (`allow_internet_access=False`) los nombres pueden seguir
  resolviéndose por los resolvedores de la plataforma dentro del guest, pero
  ninguna conexión sale del VM (riesgo residual de exfiltración por DNS,
  T17); UDP/QUIC no pasan por el proxy; un cambio afecta a las conexiones nuevas; root no se
  filtra. Detalle en [Red saliente](network.md).

### Kernels (`m9-deno-kernels`)

- `javascript` y `typescript` son el kernel de Deno 2.9.7 en
  `rayito-base-poly`, con arranque perezoso; `r` y `java` siguen fuera.

### Observabilidad (`m9-sandbox-observability`)

- El historial de métricas tiene un hueco mientras el sandbox está suspendido y
  `memory_mb` es la vista del guest.
- `order` y los filtros de `list` se calculan en cliente.

### Superficie 2.x (`m9-e2b-v2-surface`)

- `git` es un envoltorio en cliente sobre `commands.run` (E2B lo marca como
  obsoleto).
- `traffic_access_token` es el JWE del proxy, que rota cada 45 min.

## Migrar desde el shim 1.x

Rayito 0.2.0 traía un shim de E2B 1.x. Estos cambios **rompen** código
escrito contra él:

| Antes (shim 1.x) | Ahora (shim 2.x) |
|---|---|
| `sbx.pause()` devolvía el id del sandbox | devuelve `bool` (`Sandbox.pause(id)` igual) |
| `files.watch_dir(path, on_event)` síncrono | `watch_dir(path, on_event=cb)`; el segundo posicional es `user`; vive hasta `stop()` |
| `pty.create(size, on_data)` síncrono | `pty.create(size, on_data=cb)`; el segundo posicional es `user` |
| `proxy=` avisaba y se ignoraba | se honra (sólo `http://`) |
| `NotEnoughSpaceException` nunca se lanzaba | alias de `DiskFullException`, se lanza con el disco lleno |
| `sbx.connection_config` lanzaba `UnimplementedError` | devuelve un `ConnectionConfig` |
| `set_timeout`, `upload_url`/`download_url`, `get_metrics(start=, end=)`, `list(next_token=)`, `allow_internet_access=False` lanzaban `UnimplementedError` | se mapean (tabla de arriba) |
| `mcp=`, `network=`, `lifecycle=` eran `TypeError` | se mapean o lanzan `UnimplementedError` |
| `timeout` era la vida inmutable del MicroVM | es el plazo lógico de `rayd`; `max_lifetime` es el tope, y hace falta una imagen M9 |
| TS: el disco lleno era `RateLimitError` | `DiskFullError` (`NotEnoughSpaceError` en `rayito/e2b`) |

## Paridad

La tabla completa (113 filas: implementado, divergente, fuera por SPEC,
imposible en la plataforma, con la página de cada una) está en
[Paridad con E2B](e2b-parity.md).

## El coste de `list(query=SandboxQuery(metadata=...))`

`list-microvms` no conoce los metadatos. El filtro es O(n) sobre los sandboxes
`RUNNING`: por cada uno, `get-microvm` + `create-microvm-auth-token` + un
`Health` (≈ 0,6-0,8 s por sandbox, medido: `AWS_API_NOTES.md` Q45), y cada
sonda cuenta como tráfico para la política de idle de ese sandbox. Filtra por
`template` antes si tienes muchos.
