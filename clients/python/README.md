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
`image/Dockerfile`, publicada con `make image-publish`); 0.1.0 necesita la
imagen de M6 para los metadatos (sobre una anterior se leen vacíos).

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
    print("".join(sbx.run_code("echo hi", language="bash").logs.stdout))  # "hi
"
```

`language` acepta `python`, `bash` y `javascript` (alias `js`); en
`rayito-base` cualquier kernel distinto de Python es
`InvalidArgumentException` con `grpc_code` `UNIMPLEMENTED`, y `javascript`
lo es en toda imagen hasta que `ijavascript` se pueda instalar sin compilador
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

## Shim de E2B

```python
# antes
from e2b_code_interpreter import Sandbox

# después
from rayito.e2b import Sandbox

with Sandbox(timeout=300, metadata={"run": "42"}) as sbx:
    print(sbx.run_code("1 + 1").text)
    print(sbx.commands.run("echo hi").stdout)
    sbx.set_timeout(600)  # UnimplementedError: no existe UpdateMicrovm
```

`rayito.e2b` re-exporta los nombres del SDK de E2B 1.x (`Sandbox`,
`AsyncSandbox`, `Execution`, `CommandHandle`, `SandboxInfo`, `SandboxQuery`,
`SandboxPaginator`, `PtySize(rows, cols)`, las excepciones...), sigue los
valores por defecto de E2B (`timeout=300` sin auto-pausa, endpoint público y
salida a internet), ignora con `RayitoCompatWarning` `api_key`, `domain`,
`debug`, `proxy` y `secure=False`, y lanza `UnimplementedError` (un
`NotImplementedError`, nunca `SandboxException`) para lo que Lambda MicroVMs
no puede hacer: `set_timeout`, `upload_url`/`download_url`, rangos de
`get_metrics`, `connection_config`, kernels R/Java, `list(next_token=)`,
`beta_create(auto_pause=...)`, templates, `fork` y `allow_internet_access=False`
(sin conector de egress el MicroVM sigue saliendo a internet, medido). El
nativo queda en `sbx.native`. Tabla completa: `docs/site/docs/e2b-compat.md`.

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
