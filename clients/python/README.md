# rayito (SDK Python)

Cliente Python de Rayito: sandboxes de ejecución para agentes de IA sobre AWS
Lambda MicroVMs, dentro de tu propia cuenta. La ergonomía del SDK de E2B sin
que el código de tus clientes salga de tu cuenta y sin clúster que operar.
No hay servidor de terceros ni API key: el SDK llama a la API de AWS con tus
credenciales.

Documentación: **https://alejandro-cedeno-10.github.io/rayito/** ·
[Primeros pasos](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/) ·
[Referencia de Python](https://alejandro-cedeno-10.github.io/rayito/api/) ·
[Changelog](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/CHANGELOG.md)

## Instalación

```bash
pip install "rayito[cli]"      # o: uv add "rayito[cli]"
```

Python ≥ 3.11. Dependencias de runtime: `grpcio`, `protobuf` y `boto3`. El
SDK trae un árbol síncrono (`Sandbox`) y uno asíncrono (`AsyncSandbox`) con
la misma superficie.

| Extra | Para qué |
|---|---|
| `rayito[cli]` | la CLI `rayito`: publicar la imagen, `rayito doctor`, operar sandboxes y pilas opcionales ([CLI](https://alejandro-cedeno-10.github.io/rayito/cli/)) |
| `rayito[mcp]` | el servidor MCP `rayito-mcp` ([Servidor MCP](https://alejandro-cedeno-10.github.io/rayito/mcp/)) |
| `rayito[otel]` | spans OpenTelemetry del lado del SDK ([OpenTelemetry](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/opentelemetry/)) |
| `rayito[custom-domain]` | dominio propio, experimental ([Dominio propio](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/dominio-propio/)) |

## Antes del primer sandbox

Tu cuenta necesita una pila de IAM, un bucket y la imagen `rayito-base`
publicada con el `rayd` de la misma serie que el SDK. Se hace una vez por
cuenta y región: [Configurar AWS](https://alejandro-cedeno-10.github.io/rayito/primeros-pasos/configurar-aws/).
Después:

```bash
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 RAYITO_TEMPLATE=rayito-base
rayito doctor --template rayito-base     # credenciales, cuotas, IAM, imagen, versión del agente
```

## Primer sandbox

```python
from rayito import Sandbox

with Sandbox.create() as sbx:  # `with` mata el sandbox al salir
    print(sbx.commands.run("echo hola").stdout)  # "hola\n"
    sbx.files.write("/home/user/a.txt", "contenido")
    print(sbx.files.read("/home/user/a.txt"))  # "contenido"
    print(sbx.run_code("x = 40; x + 2").text)  # "42"
```

En `asyncio`:

```python
import asyncio

from rayito import AsyncSandbox


async def main() -> None:
    async with await AsyncSandbox.create() as sbx:
        print((await sbx.run_code("x = 40; x + 2")).text)  # "42"


asyncio.run(main())
```

Recorrido completo (comandos, ficheros, código y reconexión desde otro
proceso): [Primer sandbox](https://alejandro-cedeno-10.github.io/rayito/quickstart/).

## Si vienes de E2B

`rayito.e2b` es un drop-in a nivel de import del SDK de E2B 2.x. Lo que
Lambda MicroVMs no puede hacer lanza `UnimplementedError` en vez de
aproximarse en silencio. Exige una imagen 0.3.0 o posterior.

```python
from rayito.e2b import Sandbox  # antes: from e2b_code_interpreter import Sandbox

with Sandbox.create(timeout=300, metadata={"run": "42"}) as sbx:
    print(sbx.run_code("1 + 1").text)  # "2"
    sbx.set_timeout(600)
```

[Migrar desde E2B](https://alejandro-cedeno-10.github.io/rayito/migrar-desde-e2b/) ·
[Diferencias](https://alejandro-cedeno-10.github.io/rayito/e2b-compat/) ·
[Paridad](https://alejandro-cedeno-10.github.io/rayito/e2b-parity/)

## Un agente de código dentro del sandbox

`sbx.agent` corre un agente de código (OpenCode, o deepagents) **dentro**
del sandbox, sobre la imagen `rayito-agent` (`rayito agent template build`).
El agente llama a su modelo sólo a través de la pasarela de secretos: usa la
credencial, pero nunca puede leerla.

```python
from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>")
spec = AgentSpec(
    model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1")
)

with Sandbox.create(
    template="rayito-agent",
    allow_internet_access=False,
    gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
) as sbx:
    result = sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", spec=spec)
    print(result.text, result.usage.total)
```

El arranque rápido (`PoolConfig(warmup=agent_pool_warmup())`, servidor
residente) es opcional:
[¿Qué uso?](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/#que-uso).
Guía: [Agente en el sandbox](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/).

## Qué incluye

| Superficie | Guía |
|---|---|
| `Sandbox.create / connect / kill / list / get_info`, metadatos | [Ciclo de vida](https://alejandro-cedeno-10.github.io/rayito/guias/ciclo-de-vida/) |
| `pause()` / `resume()`, `IdlePolicy` | [Pausar y reanudar](https://alejandro-cedeno-10.github.io/rayito/guias/pausar-reanudar/) |
| `timeout`, `max_lifetime`, `on_timeout`, `set_timeout()` | [Plazo del servidor](https://alejandro-cedeno-10.github.io/rayito/lifecycle/) |
| `sbx.commands` | [Comandos](https://alejandro-cedeno-10.github.io/rayito/guias/comandos/) |
| `sbx.run_code`, contextos de código | [Ejecutar código](https://alejandro-cedeno-10.github.io/rayito/guias/ejecutar-codigo/) |
| `run_code(language="bash" / "javascript" / "typescript")` | [Lenguajes y kernels](https://alejandro-cedeno-10.github.io/rayito/kernels/) |
| `sbx.pty` | [Terminal (PTY)](https://alejandro-cedeno-10.github.io/rayito/guias/terminal-pty/) |
| `sbx.files`, `transfer=S3Staging(...)`, URLs firmadas | [Ficheros y S3](https://alejandro-cedeno-10.github.io/rayito/files/) |
| `sbx.get_host(port)` | [Puertos y host](https://alejandro-cedeno-10.github.io/rayito/guias/puertos-y-host/) |
| `network=`, `allow_internet_access=False` | [Red saliente](https://alejandro-cedeno-10.github.io/rayito/network/) |
| `SandboxPool`, `PoolConfig` | [Pool](https://alejandro-cedeno-10.github.io/rayito/pool/) |
| `persist=S3Prefix(...)`, `reincarnate()` | [Persistencia](https://alejandro-cedeno-10.github.io/rayito/persistence/) |
| `get_metrics_history()`, `Sandbox.paginate()` | [Métricas y listado](https://alejandro-cedeno-10.github.io/rayito/observability/) |
| `sbx.git` | [Git](https://alejandro-cedeno-10.github.io/rayito/git/) |
| `sbx.agent`, `AgentSpec`, presets de pasarela | [Agente en el sandbox](https://alejandro-cedeno-10.github.io/rayito/guias/agente-en-el-sandbox/) |
| `rayito-mcp` | [Servidor MCP](https://alejandro-cedeno-10.github.io/rayito/mcp/) |
| LangChain y Vercel AI | [LangChain y Vercel AI](https://alejandro-cedeno-10.github.io/rayito/guias/langchain-y-vercel-ai/) |

## Funciones opcionales, apagadas por defecto

Cada una crea recursos o hace llamadas facturables en tu cuenta, así que sólo
se activa con una opción explícita del SDK o de la CLI; sin ella no se
construye ningún cliente de ese servicio. Qué activa, qué cuesta, qué IAM
necesita y cómo apagarla: [Funciones opcionales](https://alejandro-cedeno-10.github.io/rayito/optional-features/).

| Opción | Función |
|---|---|
| `secrets=`, `SecretStore` | [Secretos](https://alejandro-cedeno-10.github.io/rayito/secrets/) de Secrets Manager como variables de entorno |
| `gateways=`, `SecretGateway` | [Pasarela de secretos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/pasarela-de-secretos/): usar un secreto sin poder leerlo |
| `mounts=`, `S3Mount` | [Montajes S3](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/montajes-s3/) |
| `size=` | [Tamaños](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/tamanos/) de 512 MiB a 8 GiB |
| `events=`, `LifecycleEvents` | [Eventos y webhooks](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/eventos-y-webhooks/) |
| `telemetry=`, `TelemetryExport` | [Exportación OTLP](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/exportacion-otlp/) |
| `tracer_provider=` | [OpenTelemetry (SDK)](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/opentelemetry/) |
| `index=DynamoDbIndex(...)` | [Índice de metadatos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/indice-de-metadatos/) |
| `Template.build()` | [Templates declarativos](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/templates/) |
| `AgentTemplate` | [Templates de agente](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/templates-de-agente/) |
| `OptionalStacks`, `rayito stack` | [Pilas opcionales](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/pilas-opcionales/) |
| `rayito sandbox proxy` | [Proxy local](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/proxy-local/) |
| `volumes=`, `VolumeStore` (experimental) | [Volúmenes EFS](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/volumenes-efs/) |
| `domain=`, `CustomDomain` (experimental) | [Dominio propio](https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/dominio-propio/) |

## Costes y límites

Un sandbox factura el cómputo de Lambda MicroVMs mientras corre, más el
almacenamiento de cada versión de imagen; Rayito no cobra nada. Precios,
ejemplos y tiempos medidos: [Precios](https://alejandro-cedeno-10.github.io/rayito/cost/).
Vida máxima, tamaños de payload y la tabla de compatibilidad SDK ↔ `rayd`:
[Límites](https://alejandro-cedeno-10.github.io/rayito/limits/).

## Desarrollo

```bash
cd clients/python
uv sync
uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv build && python ../../scripts/check_wheel.py dist/*.whl   # y `make wheel` desde la raíz: twine con --hash
RAYITO_E2E=1 RAYITO_TEMPLATE=<arn-o-nombre> uv run pytest tests/e2e -m e2e -v -s
```

## Licencia y marcas

[Apache-2.0](https://github.com/alejandro-cedeno-10/rayito/blob/main/LICENSE);
las atribuciones de terceros están en el fichero `NOTICE` incluido en el
paquete. E2B es una marca de su titular: Rayito es un proyecto independiente,
no afiliado, patrocinado ni respaldado por E2B, y usa el nombre sólo para
describir la compatibilidad de API del shim. Contribuir:
[`CONTRIBUTING.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/CONTRIBUTING.md);
seguridad: [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
