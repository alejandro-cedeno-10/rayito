# rayito (SDK Python)

Cliente Python de Rayito: sandboxes de ejecución para agentes de IA sobre AWS
Lambda MicroVMs, dentro de tu propia cuenta. La ergonomía del SDK de E2B sin
que el código de tus clientes salga de tu cuenta y sin clúster que operar.

```python
from rayito import Sandbox

with Sandbox.create(template="rayito-base", timeout=900) as sbx:
    print(sbx.run_code("x = 40; x + 2").text)  # "42"
```

Documentación: `docs/site` (quickstart, conceptos, referencia de API,
seguridad, límites, compatibilidad con E2B y modelo de costes; `make docs`
la construye). Cambios por versión: `CHANGELOG.md`.

## Novedades de 0.3.0 (M9)

Paridad con E2B 2.x; exige una imagen publicada con el `rayd` de M9 y está
aceptada contra AWS real (2026-09-24). Cada bloque enlaza su página de
`docs/site/docs/`; qué imagen necesita cada cosa, en `images.md`.

```python
import requests
from rayito import ALL_TRAFFIC, AsyncSandbox, S3Staging, Sandbox

# Plazo del servidor (lifecycle.md): rayd lo impone aunque tu proceso muera
sbx = Sandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
sbx.set_timeout(1800)  # exacto, hasta max_lifetime
sbx.connect(timeout=900)  # sólo alarga

# Historial de métricas y listado (observability.md)
samples = sbx.get_metrics_history(max_points=60)
pages = Sandbox.paginate(limit=20, order="desc")
first, cursor = pages.next_items(), pages.next_token

# Git (git.md)
sbx.git.clone("https://github.com/octo/demo.git", path="/home/user/demo", depth=1)
print(sbx.git.status("/home/user/demo").current_branch)
sbx.kill()

# Transferencias por S3 (files.md): URLs firmadas con tus credenciales
with Sandbox.create(transfer=S3Staging("amzn-s3-demo-bucket")) as sbx:
    ticket = sbx.files.upload_url("/home/user/in.csv", expires_in=900)
    requests.put(ticket, data=b"a,b\n1,2\n", headers=ticket.headers).raise_for_status()
    ticket.wait()
    link = sbx.files.download_url("/home/user/in.csv")
    sbx.files.write("/home/user/log.txt", "texto", gzip=True, metadata={"origen": "ci"})

# JavaScript y TypeScript con Deno (kernels.md), sólo rayito-base-poly
with Sandbox.create("rayito-base-poly") as sbx:
    print(sbx.run_code("const x: number = 40 + 2; x", language="typescript").text)

# Red saliente de E2B (network.md), sólo rayito-base-caps
with Sandbox.create(
    "rayito-base-caps", network={"deny_out": [ALL_TRAFFIC], "allow_out": ["api.example.com"]}
) as sbx:
    sbx.update_network({"deny_out": [ALL_TRAFFIC]})


# Todo lo anterior también en asyncio
async def main() -> None:
    async with await AsyncSandbox.create(timeout=600, max_lifetime=3600) as sbx:
        await sbx.set_timeout(1200)
        print(len(await sbx.get_metrics_history()))
```

```bash
# CLI (cli.md): create/connect/exec/metrics; el token nunca por argv
rayito sandbox create rayito-base --detach --token-file ~/.rayito/demo.token
rayito sandbox exec microvm-<id> --token-file ~/.rayito/demo.token -- python3 -c 'print(42)'
```

Si vienes de E2B, `from rayito.e2b import Sandbox` (shim 2.x, "Shim de
E2B" más abajo, `e2b-compat.md` y `e2b-parity.md`). Cambios que rompen:
`CHANGELOG.md` ("Changed (shim)").

## Instalación

```bash
pip install rayito        # o: uv add rayito
```

Python ≥ 3.11; dependencias de runtime `grpcio`, `protobuf` y `boto3`.

## Credenciales

El SDK usa las credenciales de AWS de la sesión de `boto3` (perfil, variables
de entorno o rol de la máquina); no hay API key.

```bash
export AWS_PROFILE=<perfil> AWS_REGION=us-east-1
export RAYITO_TEMPLATE=rayito-base            # nombre o ARN de la imagen
```

`RAYITO_TEMPLATE` evita pasar `template=` en cada `create()`. Cada sandbox
arranca desde una versión de la imagen `rayito-base` (construida desde
`image/Dockerfile`, publicada con `make image-publish`). El SDK 0.3 necesita
una imagen publicada con el agente 0.3 (M9): sobre una anterior, las
funciones de M9 lanzan `UnimplementedError`; `rayito doctor` lo comprueba.

## CLI

El extra `rayito[cli]` instala el comando `rayito` (`docs/site/docs/cli.md`):

```bash
pip install "rayito[cli]"
rayito doctor --template rayito-base     # diez comprobaciones de la cuenta; --launch prueba un sandbox
rayito sandbox list                      # MicroVMs vivos; también info, kill, logs
rayito image publish --artifact image/rayito-image.zip --base-image-version 1 --bucket <bucket>
```

## Crear y destruir

```python
from rayito import IdlePolicy, Sandbox

sbx = Sandbox.create(
    timeout=1800,  # vida máxima (running + suspended), tope 8 h
    idle=IdlePolicy(max_idle_seconds=600),  # auto-suspensión; None la desactiva
    envs={"APP_ENV": "dev"},
    execution_role_arn=None,  # sin rol no hay credenciales dentro
)
print(sbx.sandbox_id, sbx.access_token)  # guárdalos para connect()
sbx.kill()  # terminate-microvm (o usa `with`)

again = Sandbox.connect(sandbox_id, access_token=token)  # desde otro proceso
```

Plazo del servidor (M9, ADR-011, exige una imagen M9): con `max_lifetime` u
`on_timeout`, `timeout` es un plazo lógico que `rayd` hace cumplir aunque tu
proceso muera, y `max_lifetime` (≤ 28 800 s, running + suspendido) el tope fijo
de la plataforma.

```python
sbx = Sandbox.create(timeout=600, max_lifetime=7200, on_timeout="kill")
sbx.set_timeout(1800)  # SetTimeout EXACT: alarga o acorta, hasta max_lifetime
print(sbx.get_info().expires_at)  # el plazo lógico
Sandbox.connect(sbx.sandbox_id, access_token=sbx.access_token, timeout=900)  # sólo alarga
```

Con `on_timeout="pause"` (necesita `idle`) el sandbox se suspende al vencer
en vez de terminar. Contra una imagen anterior a M9 pedir un ciclo de vida es
`LifecycleUnsupportedException` y el VM se termina.

## Comandos

```python
from rayito import CommandExitException, Sandbox

with Sandbox.create() as sbx:
    result = sbx.commands.run("echo hola")  # foreground
    print(result.stdout, result.exit_code)  # "hola\n" 0

    sbx.commands.run("ls /", on_stdout=lambda line: print(line, end=""))

    server = sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
    print(server.pid, [p.pid for p in sbx.commands.list()])
    server.kill()

    try:
        sbx.commands.run("exit 3")
    except CommandExitException as error:  # exit != 0 es excepción
        print(error.exit_code, error.stderr)
```

## Ficheros

```python
from rayito import Sandbox, WriteEntry

with Sandbox.create() as sbx:
    info = sbx.files.write("/home/user/data.csv", "a,b\n1,2\n")
    print(info.size, sbx.files.read("/home/user/data.csv"))
    raw = sbx.files.read("/home/user/data.csv", format="bytes")

    sbx.files.write_files(
        [WriteEntry("/home/user/a.txt", "a"), WriteEntry("/home/user/b.txt", b"b")]
    )
    print([entry.name for entry in sbx.files.list("/home/user")])

    with sbx.files.watch_dir("/home/user", on_event=print) as watch:
        sbx.commands.run("touch /home/user/new.txt")
        print(watch.get_new_events())
```

URLs de S3 y ficheros grandes (M9, ADR-010): el SDK firma con **tus**
credenciales y `rayd` mueve los bytes; hace falta un bucket de transferencias
(`S3Staging` o `RAYITO_TRANSFER_BUCKET`). Detalle, IAM y bucket en
`docs/site/docs/files.md`.

```python
import requests
from rayito import S3Staging, Sandbox

with Sandbox.create(transfer=S3Staging("amzn-s3-demo-bucket")) as sbx:
    ticket = sbx.files.upload_url("/home/user/in.csv", expires_in=900)
    requests.put(ticket, data=open("in.csv", "rb"), headers=ticket.headers)
    ticket.wait()  # o deja que la barrera espere
    link = sbx.files.download_url("/home/user/in.csv")  # una foto del fichero
    sbx.files.write("/home/user/big.bin", open("big.bin", "rb"))  # > 8 MiB: por S3
    sbx.files.write("/home/user/log.txt", "texto", gzip=True, metadata={"origen": "ci"})
```

## run_code

```python
with Sandbox.create() as sbx:
    sbx.run_code("x = 42")
    print(sbx.run_code("x").text)  # "42": el kernel tiene estado

    plot = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()")
    print(plot.results[0].formats())  # ["png", "chart"]
    png_base64 = plot.results[0].png

    failed = sbx.run_code("1/0")
    print(failed.error.name)  # "ZeroDivisionError", nunca excepción

    ctx = sbx.create_code_context(cwd="/tmp")
    print(sbx.run_code("x", context=ctx).error.name)  # "NameError": otro kernel

    # Kernel bash (variante de imagen rayito-base-poly): arranca en la primera celda
    print("".join(sbx.run_code("echo hi", language="bash").logs.stdout))  # "hi\n"
```

`language` acepta `python`, `bash`, `javascript` (alias `js`) y
`typescript` (alias `ts`); los tres últimos viven en `rayito-base-poly`
(JavaScript y TypeScript con el kernel de Deno, M9). En `rayito-base`
cualquier kernel distinto de Python es `InvalidArgumentException` con
`grpc_code` `UNIMPLEMENTED` nombrando `rayito-base-poly`
(`docs/site/docs/kernels.md`).

## PTY

```python
from rayito import PtySize, Sandbox

with Sandbox.create() as sbx:
    pty = sbx.pty.create(size=PtySize(cols=120, rows=40), on_data=print, timeout=None)
    pty.send_input("echo hola\n")
    for _, _, data in pty:  # (None, None, bytes) por chunk
        if b"hola\r\n" in data:
            break
    pty.resize(PtySize(cols=80, rows=24))
    pty.kill()
```

## pause / resume

```python
with Sandbox.create() as sbx:
    sbx.run_code("x = 42")
    sbx.pause()  # suspend-microvm: todo sigue vivo al otro lado
    sbx.resume()  # resume-microvm + Health con la generación nueva
    print(sbx.run_code("x").text)  # "42": el kernel conservó su estado
    print(sbx.get_health().resume_generation)  # 1
```

Los handles, watches y `run_code` que estaban abiertos se reenganchan solos la
próxima vez que se leen; los unarios cortados se reintentan una vez;
`reconnect_timeout` (60 s) acota cuánto espera el SDK al agente una vez que el
MicroVM vuelve a `RUNNING`. Leer un handle en background nunca despierta un
sandbox suspendido.

## get_host

```python
import httpx

with Sandbox.create() as sbx:
    sbx.commands.run("python3 -m http.server 3000", background=True, timeout=None)
    host = sbx.get_host(3000)  # un str: f"https://{host}" funciona
    response = httpx.get(f"https://{host}/", headers=host.headers)  # cabeceras del proxy de AWS
```

## Metadatos

```python
sbx = Sandbox.create(metadata={"owner": "ana", "run": "42"})
print(sbx.metadata)  # {"owner": "ana", "run": "42"}
print(sbx.get_info().metadata)
print(Sandbox.get_info(sbx.sandbox_id).metadata)  # desde otro proceso: un JWE + un Health

for item in Sandbox.list(metadata={"run": "42"}):
    print(item.sandbox_id, item.metadata)
```

`metadata` viaja en el `runHookPayload` (4096 caracteres junto a `envs`),
es inmutable y **no es secreto** (cualquier principal que pueda acuñar un
JWE para el sandbox lo lee por `Health`). `Sandbox.list(metadata=...)` filtra
en cliente y es **O(n)**: por cada sandbox `RUNNING` hace `get-microvm` +
`create-microvm-auth-token` + un `Health` (≈ 0,5-1 s por sandbox) y cada sonda
cuenta como tráfico para su política de idle; nunca sondea un sandbox
suspendido (`states` sólo admite `RUNNING`). Filtra por `template` antes si
tienes muchos.

Listado reanudable e historial de métricas (M9):

```python
from datetime import datetime, timedelta, timezone

pages = Sandbox.paginate(limit=20, order="desc")
first = pages.next_items()
more = Sandbox.paginate(next_token=pages.next_token).next_items() if pages.has_next else []

with Sandbox.create() as sbx:
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    for sample in sbx.get_metrics_history(start=since, max_points=60):  # una muestra cada 5 s
        print(sample.timestamp, sample.cpu_used_pct, sample.mem_used_bytes)
```

`order` se calcula en cliente (recorre todas las páginas antes del primer
item); el historial tiene un hueco mientras el sandbox está suspendido.

## Shim de E2B

```python
# antes
from e2b_code_interpreter import Sandbox

# después
from rayito.e2b import Sandbox

with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
    print(sbx.run_code("1 + 1").text)
    print(sbx.commands.run("echo hi").stdout)
    sbx.set_timeout(600)  # SetTimeout: rayd mueve el plazo (tope max_lifetime, 3600 s por defecto)
```

`rayito.e2b` re-exporta los nombres del SDK de E2B **2.x** (`Sandbox`,
`AsyncSandbox`, `E2B`, `ConnectionConfig`, `Execution`, `CommandHandle`,
`SandboxInfo`, `SandboxQuery`, `SandboxPaginator`, `PtySize(rows, cols)`,
`Git`, las excepciones...), sigue los valores por defecto de E2B (`timeout=300`
como plazo lógico con `on_timeout='kill'`, sin auto-pausa, endpoint público y
salida a internet) y **exige una imagen M9**. Mapea `set_timeout`,
`connect(timeout)`, `lifecycle`, `beta_create(auto_pause=True)`,
`upload_url`/`download_url`, `get_metrics(start, end)`, `list(next_token=)`,
`allow_internet_access=False` y `network` (en `rayito-base-caps`),
`update_network`, `git` y `run_code(language="typescript")`; ignora con
`RayitoCompatWarning` `api_key`, `domain`, `debug` y `secure=False`; y lanza
`UnimplementedError` (un `NotImplementedError`, nunca `SandboxException`)
para lo que Lambda MicroVMs no puede hacer: `fork`, snapshots,
`pause(keep_memory=False)`, `network.rules`, MCP, `iam`, volúmenes, secretos,
templates y kernels R/Java. El nativo queda en `sbx.native`. Tabla completa:
`docs/site/docs/e2b-compat.md`.

## AsyncSandbox

```python
import asyncio
from rayito import AsyncSandbox


async def main() -> None:
    async with await AsyncSandbox.create(metadata={"run": "42"}) as sbx:
        print((await sbx.run_code("1 + 1")).text)
        print((await sbx.commands.run("echo hola")).stdout)
        await sbx.files.write("/home/user/a.txt", "a")
        print(await AsyncSandbox.list(metadata={"run": "42"}))


asyncio.run(main())
```

La misma superficie que `Sandbox` (incluido `rayito.e2b.AsyncSandbox`) sobre
`grpc.aio`; las llamadas al plano de control van por `asyncio.to_thread`.

## Límites y costes (medidos)

| Concepto | Valor |
|---|---|
| Vida máxima de un sandbox | 28 800 s (8 h) running + suspended, no se puede extender |
| `runHookPayload` (`envs` + `metadata`) | 4096 caracteres |
| Conexiones concurrentes por MicroVM (1 vCPU) | 8; el SDK usa ≤ 2 canales |
| Cómputo a 2 GB / 1 vCPU | $0.126/h |
| Ciclo suspend + resume a 2 GB | ≈ $0.011 (≈ 5 min de cómputo) |
| Storage por versión de imagen | ≈ $0.037/semana (mínimo una semana) |
| Arranque en frío hasta `agent_ready` / `pause()` | 2.36 s / ≈ 1.4 s |
| Escritura / lectura de ficheros a 2 GB | 0.65 MB/s / 6.71 MB/s |
| `list(metadata=)` | ≈ 0.5-1 s por sandbox `RUNNING` |

Fuentes: `AWS_API_NOTES.md` §11-§12, §16 y `MILESTONES.md`; detalle en
`docs/site/docs/limits.md` y `docs/site/docs/cost.md`.

## Desarrollo

```bash
cd clients/python
uv sync
uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv build && python ../../scripts/check_wheel.py dist/*.whl && uvx twine==7.0.0 check dist/*
RAYITO_E2E=1 RAYITO_TEMPLATE=<arn-o-nombre> uv run pytest tests/e2e -m e2e -v -s
```
