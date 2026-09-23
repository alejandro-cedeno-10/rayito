# Paridad con E2B

La tabla de alcance de Rayito 0.3.0 (M9) frente a E2B 2.51 (`e2b` 2.51.0 y
`e2b-code-interpreter` 2.10.0), fila a fila: 113 features del SDK, la CLI y
las recetas de la documentación de E2B. Cómo migrar un programa y qué se
comporta distinto está en [Compatibilidad con E2B](e2b-compat.md); esta
página sólo dice **qué hay** y dónde está documentado.

!!! note "Aceptado contra AWS real"
    Los seis cambios de M9 se aceptaron contra AWS real el 2026-09-24
    (`MILESTONES.md`, M9: regresión e2e de Python 62/62, corpus de E2B
    18/18, TypeScript 26/26). Las filas que dependen de una medida citan su
    fila de `AWS_API_NOTES.md` §16 (Q63–Q78).

## Estados

| Estado | Qué significa | Filas |
|---|---|---|
| implementado | la feature de E2B funciona con el mismo contrato; "antes de M9" si ya estaba en 0.2.0 | 72 (24 antes de M9, 48 en M9) |
| **divergente** | funciona, con una diferencia escrita en la nota (una imagen concreta, un bucket, el access token, un tope distinto) | 17 |
| fuera por SPEC | se podría construir, pero `SPEC.md` §4 lo deja fuera (plano de control, montajes compartidos, templates, escritorio); lanza `UnimplementedError` o no existe | 13 |
| imposible en la plataforma | Lambda MicroVMs no tiene la primitiva; lanza `UnimplementedError` con el motivo (o se ignora con `RayitoCompatWarning`) | 11 |

Ninguna fila se aproxima en silencio: lo que no está implementado lanza
`UnimplementedError` (un `NotImplementedError`, nunca `TypeError` ni
`AttributeError`) con la feature y el motivo; los textos exactos están en
[Compatibilidad con E2B](e2b-compat.md#lanza-unimplementederror).

## Qué imagen necesita cada fila

La mayoría de las filas M9 funcionan en cualquier imagen publicada desde
este árbol. Tres familias no:

- política de egress (filas 10–13, 19): `rayito-base-caps`;
- kernels `bash`, `javascript` y `typescript` (filas 94–96): `rayito-base-poly`;
- todo el shim de E2B: una imagen M9 (el shim siempre pide el plazo del
  servidor).

Detalle en [Imágenes e IAM](images.md).

## La tabla

La columna **Cambio** es el cambio OpenSpec de `openspec/changes/` que lo
implementa (— si ya estaba o no hay cambio). **Doc** enlaza la página donde
se explica cómo usarlo.

| # | API de E2B | Estado | Cambio | Nota | Doc |
|---|---|---|---|---|---|
| 1 | `Sandbox.create(template=)` | implementado (antes de M9) | — | nombre o ARN de imagen; `RAYITO_TEMPLATE` sustituye al template por defecto de E2B | [Compatibilidad](e2b-compat.md) |
| 2 | `Sandbox.create(timeout=)` / JS `timeoutMs` | divergente (M9) | `m9-server-timeout` | `rayd` impone un plazo movible: sale y la VM pasa a `TERMINATED` ≈ 15 s después (Q58); techo `max_lifetime` ≤ 28800 s; las 86400 s de E2B Pro son imposibles (el tope cuenta el tiempo suspendido) | [Plazo](lifecycle.md) |
| 3 | `Sandbox.create(metadata=, envs=)` | implementado (antes de M9) | — | en `runHookPayload`, 4096 caracteres entre los dos, no secretos | [Compatibilidad](e2b-compat.md) |
| 4 | `Sandbox.create(secure=)` | implementado (antes de M9) | — | E2B también lo ignora; todo RPC exige `x-access-token`; aviso con `secure=False` | [Compatibilidad](e2b-compat.md) |
| 5 | `ApiParams` `api_key` / `domain` / `debug` / `api_url` / `sandbox_url` / `validate_api_key` / `api_headers` | imposible en la plataforma | `m9-e2b-v2-surface` | no hay API de E2B que configurar; las credenciales son las de AWS; se aceptan y se ignoran con `RayitoCompatWarning` | [Compatibilidad](e2b-compat.md) |
| 6 | `ApiParams` `proxy=` | implementado (M9) | `m9-e2b-v2-surface` | se honra en el canal gRPC (`grpc.http_proxy`) y en botocore | [Compatibilidad](e2b-compat.md) |
| 7 | `ConnectionConfig` `request_timeout` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 8 | `ConnectionConfig` `headers` / `retries` / `set_integration` y `sbx.connection_config` | implementado (M9) | `m9-e2b-v2-surface` | `headers` = metadata gRPC extra (claves reservadas rechazadas); `retries` a la `Config` de botocore; `set_integration` a `user_agent_extra` | [Compatibilidad](e2b-compat.md) |
| 9 | `Sandbox.create(logger=)` / `ApiParamsWithLogger` | implementado (M9) | `m9-e2b-v2-surface` | los logs del SDK van al `logging.Logger` dado; el `logging=` nativo (CloudWatch) es otra cosa | [Compatibilidad](e2b-compat.md) |
| 10 | `allow_internet_access=False` / JS `allowInternetAccess: false` | divergente (M9) | `m9-egress-policy` | en el guest de `rayito-base-caps`, o fuera con `egress=[conector VPC propio]`; en la imagen por defecto el VM se termina y se lanza `UnimplementedError` (Q60: no existe un conector gestionado sin egress) | [Red saliente](network.md) |
| 11 | `network.allow_out` / `deny_out` con CIDR, IP, `ALL_TRAFFIC` o selectores invocables | divergente (M9) | `m9-egress-policy` | semántica exacta de E2B por rutas de política `uidrange`, sólo `rayito-base-caps` | [Red saliente](network.md) |
| 12 | `network.allow_out` / `deny_out` con nombres de host | divergente (M9) | `m9-egress-policy` | modo sólo-proxy: el proxy local de `rayd` filtra por nombre y por IP resuelta y bloquea el egress directo; un cliente que ignora el proxy falla cerrado, mientras que E2B filtra de forma transparente | [Red saliente](network.md) |
| 13 | `network.egress_proxy` (SOCKS5) | divergente (M9) | `m9-egress-policy` | el proxy local encadena al SOCKS5 del operador tras filtrar; sólo caps; falla cerrado para clientes sin proxy | [Red saliente](network.md) |
| 14 | `network.rules` / `SandboxNetworkRule` (inyección de cabeceras, placeholders de IAM) | imposible en la plataforma | `m9-e2b-v2-surface` | su razón de ser es inyectar credenciales fuera del sandbox; Lambda MicroVMs no tiene gancho de egress en el host, así que el inyector correría en el mismo VM que el código no confiable y necesitaría interceptar TLS | [Red saliente](network.md) |
| 15 | `network.allow_public_traffic=True` (el valor por defecto de E2B) | imposible en la plataforma | `m9-e2b-v2-surface` | no hay modo sin autenticar: `X-aws-proxy-auth` es obligatorio (§3, §7); `False` es el comportamiento permanente de Rayito | [Red saliente](network.md) |
| 16 | `Sandbox.traffic_access_token` | divergente (M9) | `m9-e2b-v2-surface` | devuelve el JWE vigente de `x-aws-proxy-auth`; rota (TTL ≤ 60 min) y la cabecera se llama distinto | [Compatibilidad](e2b-compat.md) |
| 17 | `network.mask_request_host` | imposible en la plataforma | `m9-e2b-v2-surface` | el proxy de AWS siempre reenvía `Host: <endpoint>` (§7) | [Red saliente](network.md) |
| 18 | `network.https_ports` | divergente (M9) | `m9-egress-policy` | medido (Q67, QE2): el proxy de Lambda MicroVMs no reenvía TLS extremo a extremo a un puerto del guest, así que una lista no vacía es `UnimplementedError`; `get_host(puerto)` sirve HTTP en claro | [Red saliente](network.md) |
| 19 | `Sandbox.update_network(network)` / `update_network(id, network)` / JS `updateNetwork` | divergente (M9) | `m9-egress-policy` | `NetworkService.UpdateNetwork` cambia la política del guest de forma atómica en caps; los conectores de plataforma siguen inmutables (no hay `UpdateMicrovm`) | [Red saliente](network.md) |
| 20 | `lifecycle.on_timeout='kill'` | implementado (M9) | `m9-server-timeout` | `rayd` sale al vencer y la VM termina sin IAM (Q58) | [Plazo](lifecycle.md) |
| 21 | `lifecycle.on_timeout='pause'` | divergente (M9) | `m9-server-timeout` | un SDK vivo suspende al vencer; sin cliente, la política de idle lo hace en `max_idle`; la carga no se congela entretanto; aplica la suspensión por idle antes del plazo; los pausados siguen muriendo en el tope de 8 h | [Plazo](lifecycle.md) |
| 22 | `lifecycle.auto_resume` | implementado (M9) | `m9-server-timeout` | `True` = `autoResumeEnabled` más el mínimo de 5 min de E2B tras reanudar; `False` lo hace cumplir `rayd` (un resume suelto sigue vencido hasta `connect()`) | [Plazo](lifecycle.md) |
| 23 | `lifecycle.on_timeout={"action": "pause", "keep_memory": False}` y `pause(keep_memory=False)` | imposible en la plataforma | `m9-e2b-v2-surface` | `suspend-microvm` siempre guarda la memoria y un resume de sólo ficheros exigiría el mismo id con arranque en frío (§5); el análogo es `checkpoint_files()` + `kill()`, con un id nuevo | [Plazo](lifecycle.md) |
| 24 | `Sandbox.create(iam=)` + `Secret.iam_token` (identidad JWT-SVID) | imposible en la plataforma | `m9-e2b-v2-surface` | los MicroVMs no emiten tokens con audiencia; la única identidad es el execution role por IMDSv2 (§9) y un emisor propio sería un servicio de plano de control (`SPEC.md` §4) | [Seguridad](security.md) |
| 25 | `Sandbox.create(mcp=)` + `get_mcp_url()` / `get_mcp_token()` | imposible en la plataforma | `m9-e2b-v2-surface` | el contrato (URL, token fijo) no se sostiene: cada petición necesita además un JWE en cabecera con TTL ≤ 60 min (§3, §7) y no hay Docker para los servidores del catálogo; Rayito trae `rayito-mcp` en el cliente | [MCP](mcp.md) |
| 26 | `Sandbox.create(volume_mounts=)` + API `Volume`/`AsyncVolume` | fuera por SPEC | `m9-e2b-v2-surface` | `SPEC.md` §4 deja fuera EFS y los montajes compartidos; análogos: persistencia en S3 y transferencias de M9; `UnimplementedError` explícito | [Ficheros](files.md) |
| 27 | `Sandbox.connect(id, timeout=)` (forma de clase) | divergente (M9) | `m9-server-timeout` | `timeout` alarga el plazo (`AT_LEAST`, semántica de E2B); sigue haciendo falta el access token del sandbox | [Plazo](lifecycle.md) |
| 28 | `sbx.connect()` (forma de instancia) | implementado (M9) | `m9-e2b-v2-surface` | — | [Compatibilidad](e2b-compat.md) |
| 29 | `connect(on_resume='reboot')` | imposible en la plataforma | `m9-e2b-v2-surface` | `resume-microvm` siempre restaura memoria y disco (§5); el análogo es `reincarnate()`, con un id nuevo | [Persistencia](persistence.md) |
| 30 | `Sandbox.kill()` / `Sandbox.kill(id)` → `bool` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 31 | `sbx.is_running(request_timeout=)` | implementado (antes de M9) | `m9-e2b-v2-surface` | ahora acepta `request_timeout` | [Compatibilidad](e2b-compat.md) |
| 32 | `sbx.set_timeout(timeout)` | implementado (M9) | `m9-server-timeout` | fija el plazo exacto, puede acortarlo, hasta `max_lifetime`; más allá, `InvalidArgumentException`; sustituye a ADR-007 | [Plazo](lifecycle.md) |
| 33 | `Sandbox.set_timeout(id, timeout)` / JS estático `setTimeout` | divergente (M9) | `m9-server-timeout` | necesita el access token (`access_token=` o `RAYITO_ACCESS_TOKEN`) | [Plazo](lifecycle.md) |
| 34 | `sbx.get_info()` / `Sandbox.get_info(id)` → `SandboxInfo` 2.x | implementado (M9) | `m9-e2b-v2-surface` | `sandbox_domain` = endpoint; `cpu_count`/`memory_mb` según el guest; `envd_version` = `agent_version`; `lifecycle`, `network` y `allow_internet_access` rellenos; `volume_mounts` siempre vacío; `end_at` = el plazo lógico | [Compatibilidad](e2b-compat.md) |
| 35 | JS `Sandbox.getFullInfo` | implementado (M9) | `m9-e2b-v2-surface` | — | [Compatibilidad](e2b-compat.md) |
| 36 | `Sandbox.list(limit, next_token)` + `SandboxPaginator` `has_next` / `next_items` / `next_token` | implementado (M9) | `m9-sandbox-observability` | token opaco sobre el `nextToken` de `list-microvms` | [Observabilidad](observability.md) |
| 37 | `Sandbox.list(order="asc" / "desc")` | implementado (M9) | `m9-sandbox-observability` | ordenado en cliente por `startedAt` sobre todas las páginas: AWS no ordena | [Observabilidad](observability.md) |
| 38 | `SandboxQuery.state` / `started_after` / `template` | implementado (M9) | `m9-sandbox-observability` | filtros en cliente sobre los items | [Observabilidad](observability.md) |
| 39 | `SandboxQuery.metadata` sobre sandboxes `RUNNING` | implementado (antes de M9) | — | una sonda `Health` por sandbox `RUNNING`, O(n) (Q45) | [Observabilidad](observability.md) |
| 40 | `SandboxQuery.metadata` sobre sandboxes `PAUSED` | fuera por SPEC | — | `SPEC.md` §4 excluye un almacén de metadatos en cliente y leerlos del agente despertaría el VM; sigue siendo `UnimplementedError` | [Observabilidad](observability.md) |
| 41 | `sbx.get_metrics(start, end)` → `List[SandboxMetrics]` con `mem_cache` | implementado (M9) | `m9-sandbox-observability` | anillo de 5 s en `rayd` (8 h) con hueco mientras está suspendido | [Observabilidad](observability.md) |
| 42 | `Sandbox.get_metrics(id)` / JS estático `getMetrics` | divergente (M9) | `m9-sandbox-observability` | necesita el access token del sandbox | [Observabilidad](observability.md) |
| 43 | `sbx.pause()` → `bool` / `Sandbox.pause(id)` | implementado (M9) | `m9-e2b-v2-surface` | devuelve `bool` como E2B 2.x (antes `str`) | [Compatibilidad](e2b-compat.md) |
| 44 | `sbx.beta_pause()` / JS `betaPause` | implementado (M9) | `m9-e2b-v2-surface` | Python ya lo tenía; JS lo gana con el shim de TS | [Compatibilidad](e2b-compat.md) |
| 45 | `Sandbox.beta_create(auto_pause=, network=, mcp=)` | implementado (M9) | `m9-server-timeout` | E2B lo retiró en 2.23.0; se conserva: `auto_pause` es `lifecycle` pause y `network`/`mcp` siguen sus filas; sólo Python (el shim de TS no tiene `betaCreate`) | [Plazo](lifecycle.md) |
| 46 | `create_snapshot` / `list_snapshots` / `delete_snapshot` / `SnapshotInfo` / `SnapshotPaginator` / `create(template=<snapshot_id>)` | imposible en la plataforma | `m9-e2b-v2-surface` | ninguna operación copia la memoria de un MicroVM en marcha (§1, §15); el análogo de sólo ficheros es `checkpoint_files`; `UnimplementedError` explícito | [Persistencia](persistence.md) |
| 47 | `Sandbox.fork(timeout, count)` / `Sandbox.fork(id)` | imposible en la plataforma | `m9-e2b-v2-surface` | un fork del estado en memoria no tiene primitiva (§1, §15) | [Persistencia](persistence.md) |
| 48 | `sbx.upload_url(path, user, use_signature_expiration)` / JS `uploadUrl` | divergente (M9) | `m9-file-transfer` | `PUT` prefirmado de S3 (`form=True` da un `POST`); aterriza de forma asíncrona con la barrera de `rayd` y `ticket.wait()`; un solo uso; caducidad siempre fijada; exige un bucket de transferencias; en el shim de TS devuelve la URL como `string` (sin `wait()`) | [Ficheros](files.md) |
| 49 | `sbx.download_url(path, user, use_signature_expiration)` / JS `downloadUrl` | divergente (M9) | `m9-file-transfer` | `GET` prefirmado de S3 con `Range`; sirve una foto tomada al llamar; caducidad siempre fijada; un fichero que falta lanza al momento; exige un bucket de transferencias (`S3Staging` o `RAYITO_TRANSFER_BUCKET`) | [Ficheros](files.md) |
| 50 | `get_signature(path, operation, user, envd_access_token, expiration_in_seconds)` | imposible en la plataforma | `m9-e2b-v2-surface` | una firma de envd no autentica en el proxy (JWE sólo en cabecera, §7); las URLs de Rayito se firman en S3 dentro de `upload_url`/`download_url` | [Ficheros](files.md) |
| 51 | `sbx.get_host(port)` (Python) | implementado (antes de M9) | — | hostname más las cabeceras obligatorias del proxy (`HostAccess`); la plataforma no tiene URL pública sin cabeceras | [Compatibilidad](e2b-compat.md) |
| 52 | JS `sandbox.getHost(port): string` (síncrono) | divergente (M9) | `m9-e2b-v2-surface` | el shim de TS devuelve el hostname de forma síncrona; las peticiones siguen necesitando las cabeceras del proxy | [Compatibilidad](e2b-compat.md) |
| 53 | `sbx.sandbox_id` / `sbx.sandbox_domain` | implementado (antes de M9) | — | `sandbox_domain` es el hostname del endpoint | [Compatibilidad](e2b-compat.md) |
| 54 | `sbx.envd_api_url` / `sbx.envd_direct_url` | implementado (M9) | `m9-e2b-v2-surface` | `https://<endpoint>`; las peticiones necesitan las cabeceras del proxy; sólo Python | [Compatibilidad](e2b-compat.md) |
| 55 | `with Sandbox() as sbx` / `async with` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 56 | cliente ligado `E2B(api_key, domain, ...)` con `.Sandbox` / `.AsyncSandbox` / `.Template` / `.Volume` / `.Secret` | implementado (M9) | `m9-e2b-v2-surface` | liga `region`/`session`/`control_plane`; `.Template`, `.Volume` y `.Secret` lanzan `UnimplementedError` | [Compatibilidad](e2b-compat.md) |
| 57 | `commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, stdin, timeout, request_timeout)` con argumentos posicionales | implementado (M9) | `m9-e2b-v2-surface` | la llamada nativa ya funcionaba; el shim acepta ahora el orden posicional de E2B | [Compatibilidad](e2b-compat.md) |
| 58 | `commands.connect` / `commands.list` / `ProcessInfo` / `commands.kill` / `send_stdin` / `close_stdin` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 59 | `CommandHandle.wait(on_pty, on_stdout, on_stderr)` (sync) | implementado (M9) | `m9-e2b-v2-surface` | — | [Compatibilidad](e2b-compat.md) |
| 60 | `AsyncCommandHandle` / `CommandResult` / `CommandExitException` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 61 | tipos `Stdout`, `Stderr`, `PtyOutput`, `OutputHandler`, `AsyncCommandHandle`, `AsyncWatchHandle`, `Username` | implementado (M9) | `m9-e2b-v2-surface` | — | [Compatibilidad](e2b-compat.md) |
| 62 | `pty.create` / `pty.send_stdin` / `pty.resize` / `pty.kill` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 63 | `pty.connect(pid)` | implementado (M9) | `m9-e2b-v2-surface` | el nativo ya existía; ahora lo expone el shim | [Compatibilidad](e2b-compat.md) |
| 64 | JS `pty.create({cols, rows, onData, user, cwd, envs, timeoutMs})` | implementado (M9) | `m9-e2b-v2-surface` | `cols`/`rows` de primer nivel en el shim de TS | [Compatibilidad](e2b-compat.md) |
| 65 | `files.read(format text / bytes / stream, user, request_timeout)` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 66 | `files.read(gzip=)` / `files.write(gzip=)` | implementado (M9) | `m9-file-transfer` | gzip estándar de gRPC; las respuestas lo piden con la cabecera `rayito-compress`; cruza el proxy de AWS en los dos sentidos: 20 MB de texto pasan de 0,80 a 52,34 MB/s (Q74) | [Ficheros](files.md) |
| 67 | `files.read(stream_idle_timeout=)` | implementado (M9) | `m9-file-transfer` | — | [Ficheros](files.md) |
| 68 | `files.write(metadata=)` / `write_files(metadata=)` + `WriteInfo.metadata` / `EntryInfo.metadata` | implementado (M9) | `m9-file-transfer` | xattrs `user.rayito.*`; claves en minúsculas; sobrescribir reemplaza el conjunto (semántica de envd) | [Ficheros](files.md) |
| 69 | `files.write(use_octet_stream=)` | implementado (M9) | `m9-file-transfer` | se acepta sin efecto: los streams gRPC no tienen formulario multipart | [Ficheros](files.md) |
| 70 | `files.write`/`files.read` de ficheros grandes (rendimiento) | implementado (M9) | `m9-file-transfer` | van por S3 automáticamente desde un umbral cuando hay `S3Staging` (Q59: 55–106 MB/s frente a 0,68 MB/s por el proxy); sin `S3Staging` siguen por gRPC, igual que antes | [Ficheros](files.md) |
| 71 | `files.write_files(List[WriteEntry])` → `List[WriteInfo]` | implementado (M9) | `m9-e2b-v2-surface` | el nativo ya existía; ahora lo expone el shim | [Compatibilidad](e2b-compat.md) |
| 72 | `files.list(depth)` / `exists` / `get_info` / `remove` / `rename` / `make_dir` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 73 | `FileType.FILE` / `DIR` / `SYMLINK` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 74 | `files.watch_dir(include_entry=)` | implementado (M9) | `m9-e2b-v2-surface` | se pasa a la llamada nativa | [Compatibilidad](e2b-compat.md) |
| 75 | `files.watch_dir(allow_network_mounts=)` | implementado (M9) | `m9-e2b-v2-surface` | se acepta; el sandbox no tiene montajes de red | [Compatibilidad](e2b-compat.md) |
| 76 | `WatchHandle.get_new_events` / `stop`, `AsyncWatchHandle.stop`, `FilesystemEvent`, `FilesystemEventType` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 77 | JS `files.read(format: "blob")` | implementado (M9) | `m9-file-transfer` | — | [Ficheros](files.md) |
| 78 | JS `files.watchDir(path, onEvent, opts)` | implementado (M9) | `m9-e2b-v2-surface` | `onEvent` posicional en el shim de TS | [Compatibilidad](e2b-compat.md) |
| 79 | `sandbox.git` (`clone`, `init`, `remote_add`, `remote_get`, `status`, `branches`, `create_branch`, `checkout_branch`, `delete_branch`, `add`, `commit`, `reset`, `restore`, `push`, `pull`, `set_config`, `get_config`, `dangerously_authenticate`, `configure_user`; `GitStatus`, `GitBranches`, `GitFileStatus`, `GitResetMode`) | implementado (M9) | `m9-e2b-v2-surface` | envoltorio en cliente sobre `commands.run`, con `git-core` en `rayito-base`; E2B marca el módulo como obsoleto | [Git](git.md) |
| 80 | `Secret` / `AsyncSecret` (`create`, `update`, `get_info`, `list`, `exists`, `destroy`, `fill`; `SecretInfo`, `SecretPaginator`) | fuera por SPEC | `m9-e2b-v2-surface` | necesita un almacén de secretos en un plano de control y un inyector de egress en el host (`SPEC.md` §4); `UnimplementedError` explícito | [Compatibilidad](e2b-compat.md) |
| 81 | API de build de templates (`TemplateBase`, `TemplateBuilder`, `Template.build`, ...) | fuera por SPEC | `m9-e2b-v2-surface` | `SPEC.md` §4 excluye los templates declarativos; el análogo es el Dockerfile más `rayito image publish` | [Compatibilidad](e2b-compat.md) |
| 82 | `cpu_count` / `memory_mb` por sandbox | fuera por SPEC | — | el tamaño es de la imagen (`SPEC.md` §4, `AWS_API_NOTES.md` §2) | [Límites](limits.md) |
| 83 | excepciones `AuthenticationException`, `CommandExitException`, `InvalidArgumentException`, `NotFoundException`, `RateLimitException`, `SandboxException`, `TimeoutException` | implementado (antes de M9) | — | `TimeoutException` cubre ahora también `sandbox_timeout` (`m9-server-timeout`) | [Compatibilidad](e2b-compat.md) |
| 84 | `FileNotFoundException` / `SandboxNotFoundException` exportadas desde `rayito.e2b` | implementado (M9) | `m9-e2b-v2-surface` | — | [Compatibilidad](e2b-compat.md) |
| 85 | `NotEnoughSpaceException` | implementado (M9) | `m9-e2b-v2-surface` | alias de `DiskFullException`, que sí se lanza | [Compatibilidad](e2b-compat.md) |
| 86 | `ServiceBusyException` | implementado (M9) | `m9-e2b-v2-surface` | se lanza ante `InsufficientCapacityException` | [Compatibilidad](e2b-compat.md) |
| 87 | `FileUploadException` | implementado (M9) | `m9-e2b-v2-surface` | se lanza cuando falla una importación (`m9-file-transfer`) | [Compatibilidad](e2b-compat.md) |
| 88 | `GitAuthException` / `GitUpstreamException` | implementado (M9) | `m9-e2b-v2-surface` | — | [Git](git.md) |
| 89 | `TemplateException` / `BuildException` | fuera por SPEC | `m9-e2b-v2-surface` | las clases existen y nunca se lanzan (no hay API de templates) | [Compatibilidad](e2b-compat.md) |
| 90 | `Volume*Exception` / `Secret*Exception` | fuera por SPEC | — | sus APIs están fuera de alcance | [Compatibilidad](e2b-compat.md) |
| 91 | `SandboxState` `RUNNING` / `PAUSED` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 92 | paridad completa de `AsyncSandbox` | implementado (M9) | `m9-e2b-v2-surface` | los mismos huecos cerrados que en el shim síncrono | [Compatibilidad](e2b-compat.md) |
| 93 | `run_code(code, language / context, on_stdout, on_stderr, on_result, on_error, envs, timeout, request_timeout)` → `Execution` | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 94 | `run_code(language="python" / "bash")` | implementado (antes de M9) | — | `bash` sólo en `rayito-base-poly` | [Kernels](kernels.md) |
| 95 | `run_code(language="javascript")` | implementado (M9) | `m9-deno-kernels` | kernel de Deno en `rayito-base-poly` con arranque perezoso (Q61); otras imágenes lanzan `UnimplementedError` nombrando poly | [Kernels](kernels.md) |
| 96 | `run_code(language="typescript")` | implementado (M9) | `m9-deno-kernels` | el mismo kernel de Deno, sólo `rayito-base-poly` | [Kernels](kernels.md) |
| 97 | `run_code(language="r" / "java")` | fuera por SPEC | `m9-deno-kernels` | `SPEC.md` §4; medido: R por conda 1,4 GB, R-core 127 MB más dependencias gráficas; Corretto 21 262 MB más un IJava sin mantenimiento | [Kernels](kernels.md) |
| 98 | `create_code_context(cwd, language, request_timeout)` → `Context` | implementado (antes de M9) | `m9-deno-kernels` | `javascript`/`typescript` añadidos en poly | [Kernels](kernels.md) |
| 99 | `list_code_contexts` / `remove_code_context` / `restart_code_context` | implementado (M9) | `m9-e2b-v2-surface` | el nativo ya existía; ahora lo expone el shim | [Compatibilidad](e2b-compat.md) |
| 100 | `Execution` / `Logs` / `OutputMessage` / `ExecutionError` / `Result` / charts (`ChartType`, `ScaleType`, `Chart`, `PointChart`, `LineChart`, `ScatterChart`, `BarChart`, `PieChart`, `BoxAndWhiskerChart`, `SuperChart`, clases de datos) | implementado (antes de M9) | — | — | [Compatibilidad](e2b-compat.md) |
| 101 | `Logs.to_json` / `ExecutionError.to_json` / `Context.from_json` / `Chart2D` / `MIMEType` / `RunCodeLanguage` / `OutputHandler` (code-interpreter) | implementado (M9) | `m9-e2b-v2-surface` | Python (`rayito.e2b`) | [Compatibilidad](e2b-compat.md) |
| 102 | import drop-in de JS (`import { Sandbox } from 'e2b'` / `'@e2b/code-interpreter'`) | implementado (M9) | `m9-e2b-v2-surface` | nueva ruta `rayito/e2b` en `exports` | [Compatibilidad](e2b-compat.md) |
| 103 | JS `Sandbox.create(template?, opts)` con `ConnectionOpts` (`requestTimeoutMs`, `retries`, `logger`, `headers`, `proxy`, `signal`) | implementado (M9) | `m9-e2b-v2-surface` | `apiKey`/`domain` ignorados con aviso; el `AbortSignal` va a las opciones de llamada de Connect | [Compatibilidad](e2b-compat.md) |
| 104 | JS estáticos `Sandbox.kill` / `getInfo` / `isRunning` / `connect` / `pause` | implementado (M9) | `m9-e2b-v2-surface` | formas estáticas en el shim de TS (también `getFullInfo`, `betaPause`, `setTimeout`, `getMetrics`, `list` y `updateNetwork`); el nativo tiene las de instancia | [Compatibilidad](e2b-compat.md) |
| 105 | JS `Sandbox.list(opts)` → `SandboxPaginator` | implementado (M9) | `m9-sandbox-observability` | la forma del paginador más el filtro de metadatos, como en Python | [Observabilidad](observability.md) |
| 106 | superficie camelCase de JS `commands` / `files` / `runCode` / contextos | implementado (antes de M9) | — | el espejo nativo de TS ya coincidía; el shim de TS la reexpone | [Compatibilidad](e2b-compat.md) |
| 107 | API REST y webhooks de eventos de ciclo de vida (docs) | fuera por SPEC | — | necesita un servicio de plano de control con almacén y entrega (`SPEC.md` §4); el análogo en tu cuenta son los eventos de datos de CloudTrail (§10) | [Compatibilidad](e2b-compat.md) |
| 108 | exportación de telemetría OTel (docs, Enterprise) | fuera por SPEC | — | superficie de producto SaaS (`SPEC.md` §4); análogos en tu cuenta: logs de runtime en CloudWatch y el historial de `get_metrics` | [Observabilidad](observability.md) |
| 109 | receta de acceso SSH (sshd + websocat, docs) | divergente (M9) | `m9-e2b-v2-surface` | `rayito sandbox connect` da una terminal PTY interactiva; una receta con sshd necesita una imagen propia y un cliente que hable la autenticación por subprotocolo WebSocket | [CLI](cli.md) |
| 110 | dominio propio vía proxy inverso (docs) | fuera por SPEC | — | el proxy tendría que guardar credenciales IAM para acuñar JWEs por petición: sería un servicio de plano de control (`SPEC.md` §4) | [Compatibilidad](e2b-compat.md) |
| 111 | montajes de buckets s3fs/gcsfuse (docs) | fuera por SPEC | — | receta de template más montajes compartidos en vivo, fuera por `SPEC.md` §4; FUSE en `rayito-base-caps` no está medido; análogos: persistencia en S3 y transferencias de M9 | [Ficheros](files.md) |
| 112 | CLI de E2B (`auth`, `sandbox list/create/connect/exec/kill/metrics`, `template`, `snapshots`, `fork`) | divergente (M9) | `m9-e2b-v2-surface` | añade `sandbox create/connect/exec/metrics` a los `list/kill/logs` que ya había ([CLI](cli.md)); `auth`, `template`, `snapshots` y `fork` siguen fuera | [CLI](cli.md) |
| 113 | SDK de escritorio (`e2b-desktop`) | fuera por SPEC | — | `SPEC.md` §4 (Desktop/GUI) | [Compatibilidad](e2b-compat.md) |

## Qué hacer con lo que no está

| Si usabas | En Rayito |
|---|---|
| `fork`, snapshots, `pause(keep_memory=False)` | `checkpoint_files()` + `create(persist=)` o `reincarnate()`: sobreviven los ficheros, con un id nuevo ([Persistencia](persistence.md)) |
| `Volume`, `volume_mounts`, montajes s3fs | `persist=` (S3) y `upload_url`/`download_url` ([Ficheros](files.md)) |
| `Template.build` | un `Dockerfile` y `rayito image publish` ([CLI](cli.md)) |
| `Secret`, `network.rules` | pasa las credenciales en `envs` de un comando o usa un proxy tuyo fuera del VM; no hay inyector de egress fuera del guest |
| `mcp=` | el servidor [`rayito-mcp`](mcp.md), en el cliente |
| `iam=` | `execution_role_arn` (IMDSv2); en `rayito-base-caps` el código del sandbox no ve IMDS ([Seguridad](security.md)) |
| webhooks de ciclo de vida, OTel | eventos de datos de CloudTrail, logs de runtime en CloudWatch y `get_metrics_history()` ([Observabilidad](observability.md)) |
| cpu/memoria por sandbox | una imagen por tamaño ([Límites](limits.md)) |
