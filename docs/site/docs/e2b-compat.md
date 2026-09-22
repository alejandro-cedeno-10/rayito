# Compatibilidad con E2B

`rayito.e2b` es un drop-in a nivel de import para el SDK Python de E2B (1.x).
Cambia la línea de import y el resto del programa sigue igual:

```python
# antes
from e2b_code_interpreter import Sandbox
from e2b import Sandbox, SandboxQuery
from e2b.exceptions import CommandExitException

# después
from rayito.e2b import Sandbox
from rayito.e2b import Sandbox, SandboxQuery
from rayito.e2b.exceptions import CommandExitException
```

El shim sigue los valores por defecto de **E2B**, no los de `rayito.Sandbox`:
`timeout=300` es la vida del sandbox (sin auto-pausa), endpoint público
(`ALL_INGRESS`) y salida a internet (`INTERNET_EGRESS`). Si quieres los
defaults de Rayito (3600 s con auto-suspensión a los 300 s de inactividad,
conectores explícitos), usa `rayito.Sandbox`; el nativo siempre está en
`sbx.native`. Lo que Lambda MicroVMs no puede hacer lanza
`UnimplementedError` (un `NotImplementedError`, nunca `SandboxException`) con
la feature y el motivo: nada se aproxima en silencio.

## Funciona sin cambios

| E2B | Rayito |
|---|---|
| `with Sandbox() as sbx:` / `Sandbox.create()` / `async with await AsyncSandbox.create()` | igual (`RAYITO_TEMPLATE` en lugar del template por defecto de E2B) |
| `sbx.run_code(code)` → `Execution` con `text`, `results`, `logs.stdout`, `error` | igual; los charts (`Result.chart`, `png`) también |
| `sbx.commands.run(cmd)`, `background=True`, `on_stdout`, `CommandExitException.exit_code`, `commands.list()` | el `Commands` nativo tal cual |
| `sbx.files.write(path, data)`, `files.write([WriteEntry, ...])`, `read`, `list`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | igual |
| `sbx.files.watch_dir(path, on_event)` con 60 s de vida por defecto | igual |
| `sbx.pty.create(PtySize(rows, cols), on_data)`, `send_stdin`, `resize`, `kill` | igual (`PtySize` en el orden de E2B) |
| `sbx.get_host(port)` | un `str` con el hostname más `.headers` con las cabeceras del proxy |
| `sbx.get_info()`, `Sandbox.get_info(id)` → `SandboxInfo` con `metadata`, `state`, `end_at` | igual; `raw_state` conserva el estado de AWS |
| `Sandbox.list(query=SandboxQuery(metadata=...), state=..., limit=...)` → `SandboxPaginator` | igual; `next_token` siempre `None` |
| `sbx.beta_pause()` / `sbx.pause()` y `Sandbox.connect(id)` sobre un sandbox pausado | `suspend-microvm` y `resume-microvm` |
| `sbx.get_metrics()` → `[SandboxMetrics]` en bytes | una instantánea |
| `sbx.kill()`, `Sandbox.kill(id)`, `sbx.is_running()`, `sbx.sandbox_id`, `sbx.sandbox_domain` | igual |
| Las excepciones de `e2b.exceptions` | las mismas clases nativas bajo esos nombres; `NotEnoughSpaceException` y `TemplateException` existen pero nunca se lanzan |

## Se mapea, con una nota

| E2B | Rayito |
|---|---|
| `timeout` (300 s por defecto) | la vida máxima del MicroVM (running + suspended, tope 8 h), sin política de idle. E2B la extiende con `set_timeout`; Rayito **no puede** |
| `metadata` | viaja en `runHookPayload` (4096 caracteres junto a `envs`); no es secreto; necesita una imagen de M6 |
| `api_key`, `domain`, `debug`, `proxy` | ignorados con un `RayitoCompatWarning` cada uno: las credenciales son las de AWS |
| `secure=False` | ignorado con aviso: todo RPC exige `x-access-token` |
| `Sandbox(sandbox_id=...)` (E2B v1) | se conecta; los kwargs de creación que se pasen se ignoran con un aviso |
| `connect()` | necesita el `access_token` del sandbox (o `RAYITO_ACCESS_TOKEN`), que E2B no tiene |
| `Sandbox.list(state=[PAUSED])` | `SUSPENDING|SUSPENDED` de AWS |
| Kwargs nativos (`region`, `session`, `execution_role_arn`, `allowed_ports`, `ingress`, `logging`, `control_plane`, `transport`, ...) | se pasan tal cual; `idle` y `egress` no se aceptan (`TypeError`) |

## Lanza `UnimplementedError`

| Feature de E2B | Motivo |
|---|---|
| `sbx.set_timeout(t)`, `Sandbox.set_timeout(id, t)` | la vida de un MicroVM es inmutable: no existe `UpdateMicrovm`; el camino soportado es `rayito.Sandbox.reincarnate()` (checkpoint del `HOME` en S3 + VM nueva, [Persistencia](persistence.md)) |
| `sbx.upload_url()`, `sbx.download_url()` | el proxy exige cabeceras firmadas por petición; usa `files` o `get_host(port).headers` |
| `sbx.get_metrics(start=, end=)` | no hay historial: `Metrics` es una instantánea procfs |
| `Sandbox.get_metrics(id)` | necesita el access token: usa `connect()` |
| `sbx.connection_config` | no hay `api_key` ni `domain` |
| `run_code(language=...)`, `create_code_context(language=...)` con algo distinto de `python`, `bash`, `javascript` (o `js`) | kernels R y Java no incluidos (SPEC.md §4); `bash` vive en la variante `rayito-base-poly` y `javascript` es UNIMPLEMENTED en toda imagen hasta que `ijavascript` se pueda instalar sin compilador (`kernels.md`) |
| `Sandbox.list(next_token=...)` | no hay cursor reanudable |
| `Sandbox.list(query=SandboxQuery(metadata=...), state=[PAUSED])` | los metadatos viven en el agente; leerlos despertaría el sandbox |
| `Sandbox.beta_create(auto_pause=, network=, mcp=)` | sin primitiva en Lambda MicroVMs |
| `allow_internet_access=False` | medido (`AWS_API_NOTES.md` Q44): un MicroVM lanzado sin `egressNetworkConnectors` hereda el conector de la versión de imagen y sigue saliendo a internet; fingir que no sería mentir. El allowlist de egress llega con el conector VPC del track de endurecimiento |
| Templates, `fork`, snapshots, `cpu`/`memory` por sandbox, la CLI de E2B | fuera del alcance (ver `SPEC.md` §4) |

## El coste de `list(query=SandboxQuery(metadata=...))`

`list-microvms` no conoce los metadatos. El filtro es O(n) sobre los sandboxes
`RUNNING`: por cada uno, `get-microvm` + `create-microvm-auth-token` + un
`Health` (≈ 0,6-0,8 s por sandbox, medido: `AWS_API_NOTES.md` Q45), y cada
sonda cuenta como tráfico para la política de idle de ese sandbox. Filtra por
`template` antes si tienes muchos.
