# Rayito

[![CI](https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml/badge.svg)](https://github.com/alejandro-cedeno-10/rayito/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/rayito)](https://pypi.org/project/rayito/) [![npm](https://img.shields.io/npm/v/rayito)](https://www.npmjs.com/package/rayito) [![Licencia](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/alejandro-cedeno-10/rayito/badge)](https://scorecard.dev/viewer/?uri=github.com/alejandro-cedeno-10/rayito)

Sandboxes de ejecución para agentes de IA, aislados por hardware, corriendo en tu
propia cuenta de AWS sobre Lambda MicroVMs.

> Sandboxes que aparecen en un destello. Dentro de tu propia cuenta de AWS.

La ergonomía de E2B sin que el código de tus clientes salga de tu cuenta, y sin
clúster que operar: el SDK habla directamente con la API de Lambda MicroVMs.

```bash
pip install rayito          # SDK Python (clients/python); Python >= 3.11
pnpm add rayito             # SDK TypeScript (clients/typescript); Node >= 20
```

```python
from rayito import Sandbox

with Sandbox.create() as sbx:
    sbx.files.write("/work/data.csv", data)
    print(sbx.run_code("import pandas as pd; pd.read_csv('/work/data.csv').head()").text)
```

Para sandboxes en menos de un segundo, un [pool de suspendidos](docs/site/docs/pool.md)
(ADR-008): N MicroVMs calientes aparcados que `take()` reanuda en ≈ 0,8 s
(p95 0,90 s medido frente a 6,5 s de `create()`).

```python
from rayito import PoolConfig, SandboxPool

with SandboxPool(PoolConfig(size=3, template="rayito-base")) as pool:
    sbx = pool.take()                     # < 1 s con plaza lista; create() si no hay
    print(sbx.run_code("1 + 1").text)
    sbx.kill()
```

Si vienes del SDK de E2B, `rayito.e2b` es un drop-in a nivel de import: lo
que Lambda MicroVMs no puede hacer lanza `UnimplementedError` en vez de
aproximarse en silencio (`docs/site/docs/e2b-compat.md`).

```python
from rayito.e2b import Sandbox          # antes: from e2b_code_interpreter import Sandbox

with Sandbox(timeout=300, metadata={"run": "42"}) as sbx:
    print(sbx.run_code("1 + 1").text)
    sbx.set_timeout(600)                # UnimplementedError: no existe UpdateMicrovm
```

## Cómo funciona

```
tu proceso (Python / TypeScript)                AWS, tu cuenta
┌───────────────────────────────┐   HTTPS/2    ┌──────────────────────────────┐
│ SDK rayito                    │ ───────────► │ proxy de Lambda MicroVMs     │
│  · boto3 / SDK JS: run-microvm│              │   ▼ h2c                      │
│  · gRPC: commands, files,     │              │ MicroVM: rayd → sidecar →    │
│    run_code, pty              │ ◄─────────── │   ipykernel (tu código aquí) │
└───────────────────────────────┘   streams    └──────────────────────────────┘
```

- **El SDK nunca corre dentro del sandbox.** Vive en tu proceso (tu app, tu
  agente, tu CI) y sólo envía RPCs: crea el MicroVM con la API
  `lambda-microvms` de tu cuenta y habla gRPC con `rayd`, el agente Rust que
  va dentro de la imagen. Nada del runtime de tu agente se copia a la VM.
- **Dónde corre tu código.** Lo que envías con `run_code` se ejecuta en un
  kernel de Jupyter dentro del MicroVM como uid 1000 (con estado entre
  celdas); `commands` y `pty` lanzan procesos normales ahí mismo, también
  como uid 1000. `rayd` corre como root y es el único que toca los hooks de
  Lambda. Con `run_code(code, language="bash")` la celda va a un kernel bash
  que la variante de imagen `rayito-base-poly` arranca en la primera celda
  (`docs/site/docs/kernels.md`); `javascript` queda reservado como nombre.
- **Desde qué lenguajes.** Hoy, Python y TypeScript. Cualquier otro lenguaje
  con un cliente gRPC puede hablar con `rayd` generando el cliente desde
  `proto/rayito/v1/`: el contrato es la fuente de verdad y los SDKs añaden
  encima el ciclo de vida, los tokens y la reconexión.

Detalle de procesos, usuarios y transportes en `ARCHITECTURE.md` ("Qué corre
dónde"); comparación con E2B, Daytona y Modal en `docs/site/docs/concepts.md`.

**Estado: M0–M7 aceptados contra AWS real (M7 el 2026-09-17; release 0.2.0,
`docs/RELEASE_NOTES_0.2.0.md`).** `rayd` 0.2.0 sirve
`Health`, `ProcessService`, `FilesystemService` (con `Checkpoint`/`Restore`
a S3), `CodeService` (con `Reattach` y `language`) y `PtyService`, y los
hooks `/suspend`/`/resume` reales; los SDKs Python y TypeScript (`rayito`
0.2.0, en lockstep con `rayd`) tienen ciclo de vida, `commands`, `files`,
`run_code` con contextos (`create_code_context`, `list_code_contexts`,
`remove_code_context`, `restart_code_context`) y `language=`, `pty`,
`pause()`/`resume()` y el contrato de reconexión, metadatos por sandbox
(`create(metadata=)`, `list(metadata=)`), el pool de suspendidos
(`SandboxPool`), la persistencia del `HOME` en S3 (`create(persist=)`,
`reincarnate()`), el shim `rayito.e2b`, el servidor MCP (`rayito[mcp]`) y la
CLI `rayito` (`rayito[cli]`: `image`, `sandbox`, `doctor`). Los SDKs 0.2
exigen una imagen construida con `rayd` ≥ 0.2.0 (`agent_version` en
`Health`; `rayito doctor` lo comprueba): sobre un `rayd` anterior
`persist=` responde `UNIMPLEMENTED`, `language=` se ignora y, en imágenes
sin el sidecar de kernels, `create()` falla con `SandboxNotReadyException`.
La versión de imagen (`rayito-base` N.0) es un contador de builds por cuenta,
no un criterio de compatibilidad. Ver `MILESTONES.md`.

```python
from rayito import IdlePolicy, PtySize, Sandbox

with Sandbox.create(idle=IdlePolicy(max_idle_seconds=600), reconnect_timeout=60) as sbx:
    sbx.run_code("x = 42")
    pty = sbx.pty.create(size=PtySize(cols=100, rows=30), on_data=print, timeout=None)
    sbx.pty.send_input(pty.pid, "echo hola\n")
    sbx.pause()                       # suspend-microvm: procesos, PTY y kernel siguen vivos
    sbx.resume()                      # resume-microvm + Health con la generación nueva
    assert sbx.run_code("x").text == "42"
    sbx.pty.send_input(pty.pid, "echo sigo-viva\n")   # el handle se reengancha solo
    print(sbx.get_health().resume_generation)         # 1
```

Un handle en background, una PTY o un `watch_dir` nunca despiertan un sandbox
suspendido (leerlos espera al `resume()` o al auto-resume de otra llamada);
`reconnect_timeout` (60 s por defecto) es cuánto espera un corte a que el
agente vuelva antes de fallar.

El SDK TypeScript (`clients/typescript`, paquete `rayito` en npm, M6) ofrece la
misma superficie en camelCase y milisegundos, sólo async, generado desde el
mismo `.proto` y validando el mismo `limits.json`:

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create({ idle: { maxIdleSeconds: 600 } });
await sbx.runCode("x = 42");
await sbx.pause();
await sbx.resume();
console.log((await sbx.runCode("x")).text); // "42"
```

- **Servidor MCP** (`rayito[mcp]`, `python -m rayito.mcp` / `rayito-mcp`) para
  Claude Code, Claude Desktop, Cursor y VS Code: un sandbox por proceso con
  `run_code`, `run_command`, `read_file`, `write_file`, `list_files` y
  `list_sandboxes`, stdio o streamable HTTP; ver `docs/site/docs/mcp.md`.
- **CLI** (`rayito[cli]`, comando `rayito`): `rayito doctor` diagnostica una
  cuenta antes del primer `create()` (credenciales, cuotas, IAM, bucket,
  imagen, agente, compatibilidad SDK ↔ `rayd` ↔ imagen); `rayito sandbox
  list|info|kill|logs` opera los MicroVMs vivos; `rayito image
  publish|list|prune|zip` es el flujo de `make image-publish` con nombre
  estable (los scripts de `scripts/` son shims suyos); ver
  `docs/site/docs/cli.md`.

## Endurecimiento (M6)

```python
from rayito import DiskFullException, Sandbox

sbx = Sandbox.create(
    "rayito-base-caps",                  # misma imagen con additionalOsCapabilities ALL
    execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
    cpu_time_limit=60,                   # RLIMIT_CPU por proceso: segundos de CPU, no de pared
    egress=["arn:aws:lambda:us-east-1:123456789012:network-connector:mi-allowlist"],
)
health = sbx.get_health()
assert health.imds_blocked              # uid 1000 no alcanza 169.254.169.254; rayd (root) sí
assert health.hook_anomalies == 0       # /run repetidos y suspensiones estancadas
try:
    sbx.files.write("/home/user/grande.bin", b"\0" * (8 << 30))
except DiskFullException:               # reserva de 256 MiB comprobada antes de escribir
    ...
```

- **IMDS**: en la imagen por defecto cualquier proceso del sandbox lee las
  credenciales del `execution_role_arn` (el SDK avisa una vez si hay rol e
  `imds_blocked` sigue en `False` pasados 10 s de `uptime` desde el `Health`
  de readiness: antes `rayd` todavía está verificando). `rayito-base-caps` (`make image-publish-caps`)
  las bloquea para uid 1000-65535 con una ruta de política instalada por
  `rayd` en el arranque y reverificada en cada `/run` y `/resume`
  (`SECURITY.md` T1, `AWS_API_NOTES.md` Q48).
- **Hooks forjados**: un token `allPorts` (que el SDK nunca acuña) puede
  llamar a los hooks de `rayd`; el daño medido es nulo (`/run` → `already_ran`)
  o una interrupción (`/suspend` sin checkpoint: el watchdog reabre la puerta
  en ≤ 20 s; pares `/suspend` + `/resume`: un corte por par) de la que los
  handles se recuperan solos (`Connect`, `Pty.Connect`, `WatchDir` y
  `Reattach` reintentan la puerta cerrada con backoff). `rayd` nunca rechaza
  una transición: rechazarla afectaría también al hook genuino que llega
  detrás de uno forjado. Los `/run` repetidos y las recuperaciones quedan en
  `get_health().hook_anomalies` (`SECURITY.md` T2).
- **Límites**: `cpu_time_limit` (1–28 800 s, `SIGXCPU` y `SIGKILL` 5 s
  después), presupuesto de salida de 128 MiB por sandbox para los replays
  (`Connect(from_seq)` puede responder `NotFoundException` antes que antes;
  la entrega en vivo no cambia) y reserva de disco de 256 MiB
  (`DiskFullException`).
- **Egress allowlist**: `infra/egress-connector.yaml` crea un
  `AWS::Lambda::NetworkConnector` con un security group deny-all y hasta
  cinco CIDRs permitidos; receta en `infra/README.md`; `make infra-lint`
  valida la plantilla. `SandboxInfo.ingress`/`egress` muestran los conectores
  con los que se lanzó el sandbox.
- **Versiones de imagen**: `make image-prune PRUNE_ARGS="--dry-run"` y luego
  `--keep 5` borra las versiones antiguas de `rayito-base` de una en una
  (cada versión cuesta ≈ $0,04/semana de storage).

## Persistencia más allá de las 8 h

Un MicroVM vive como mucho 8 h y `kill()` borra su disco. Con `persist=`,
`rayd` guarda el `HOME` del usuario en tu bucket de S3 (como root, con el
execution role; el código del sandbox sigue sin ver IMDS en `rayito-base-caps`)
y lo restaura en el sandbox siguiente. `reincarnate()` es la respuesta al
`set_timeout` de E2B: checkpoint, VM nueva con las mismas opciones, restore y
`kill()` de la vieja. Sobreviven los ficheros; no las variables del kernel ni
los procesos. Detalles, lista de exclusión e IAM en
[`docs/site/docs/persistence.md`](docs/site/docs/persistence.md).

```python
from rayito import S3Prefix, Sandbox

sbx = Sandbox.create(
    "rayito-base-caps",
    execution_role_arn="arn:aws:iam::123456789012:role/rayito-m0-execution-us-east-1",
    persist=S3Prefix("mi-bucket", name="agente-7"),   # s3://mi-bucket/rayito/agente-7/
)
sbx.checkpoint_files(exclude=["data/raw"])          # home.tar.gz + manifest.json
sbx = sbx.reincarnate()                               # 8 h frescas, mismo HOME
```

## Verificar una release

Los assets de cada release `rayd-v<v>` (`rayd`, `rayito-image.zip`, SBOM
CycloneDX) van firmados keyless con cosign; PyPI y npm publican por OIDC con
attestations. Receta completa en [`docs/site/docs/verify.md`](docs/site/docs/verify.md):

```bash
cosign verify-blob --bundle rayito-image.zip.sigstore.json \
  --certificate-identity-regexp '^https://github.com/alejandro-cedeno-10/rayito/\.github/workflows/release\.yml@refs/tags/rayd-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com rayito-image.zip
```

## Coste y latencia (medido 2026-09)

`rayito-base` 10.0 (2 GB, snapshot de memoria de 0,92 GB), us-east-1, cliente a
≈ 90 ms de RTT, sonda de 100 ms; detalle, crudos y decisión sobre el pool en
[`docs/benchmarks/2026-09-cold-start.md`](docs/benchmarks/2026-09-cold-start.md).

| | p50 | p95 |
|---|---|---|
| `create()` → `kernel_ready` (20 secuenciales) | 5,2 s | 6,1 s |
| `create()` → `kernel_ready` (ráfaga de 20 por el SDK, bucket de 5 TPS) | 5,7 s | 8,6 s |
| `create()` → `agent_ready` | 2,3 s | 3,0 s |
| `resume()` explícito → kernel listo | 0,38 s | 0,40 s |
| auto-resume (primer `commands.run` sobre un sandbox suspendido) | 0,67 s | 0,68 s |
| primera celda (`1+1`) tras crear o reanudar | 0,10 s | 0,11 s |

| Coste (precios verificados en Cost Explorer) | |
|---|---|
| hora activa a 2 GB / 1 vCPU | $0,126 |
| lanzamiento (lectura del snapshot de 0,92 GB) | ≈ $0,0014 |
| ciclo `pause()` + `resume()` (escritura + lectura) | ≈ $0,0049 (≈ 140 s de compute) |
| hora suspendida (storage del snapshot) | ≈ $0,0001 |

Sin el warm-up del kernel (`rayito-base-slim` 2.0, imagen de medición) el
`create()` secuencial baja a 3,0 / 3,4 s y la primera celda con pandas +
matplotlib sube de 0,14 a 0,79 s.

## Documentos

| Fichero | Contenido |
|---|---|
| `SPEC.md` | Qué construimos, alcance, no-objetivos, decisiones |
| `ARCHITECTURE.md` | Las tres capas y los ADRs |
| `AWS_API_NOTES.md` | Superficie verificada de la API de AWS. **Leer antes de tocar `src/`** |
| `docs/aws-api/` | Apéndice crudo: modelo `service-2.json`, `help` de los 25 comandos, resumen de shapes |
| `MILESTONES.md` | Hitos y criterios de aceptación |
| `spike/m0/` | Imagen probe, runbook, IAM y tabla de resultados del hito M0 |
| `CLAUDE.md` | Reglas para trabajar con Claude Code |
| `CONTRIBUTING.md` | Cómo contribuir: flujo OpenSpec, gates en Linux/WSL2 y Windows, convenciones, DCO |
| `SECURITY.md` | Cómo reportar una vulnerabilidad y el modelo de amenazas |
| `GOVERNANCE.md` | Quién decide y cómo (ADRs, cambios OpenSpec, mantenedores) |
| `LICENSE`, `NOTICE`, `CHANGELOG.md` | Apache-2.0, atribuciones de terceros y los changelogs por componente |
| `docs/RELEASING.md` | Pasos manuales de publicación (PyPI, npm, GitHub Release, tags) |
| `proto/rayito/v1/` | El contrato gRPC. Fuente de verdad de toda la API |

## Estructura prevista

```
proto/rayito/v1/         contrato gRPC (package rayito.v1)
crates/rayd/             agente Rust (tonic) que corre dentro del MicroVM
crates/rayito-proto/     código Rust generado desde proto/
kernel-sidecar/          sidecar Python con jupyter_client
clients/python/          SDK Python (paquete `rayito`)
clients/typescript/      SDK TypeScript
image/                   Dockerfile ARM64 de la imagen base
spike/m0/                spike de validación de la plataforma (sin código de producto)
docs/aws-api/            volcado crudo de la API de Lambda MicroVMs
```

## Primer paso

```bash
aws sso login --profile <perfil>
export AWS_PROFILE=<perfil> AWS_REGION=us-east-1 RAYITO_BUCKET=<bucket>
uv run --with "boto3>=1.43.82" --with requests --with "httpx[http2]" python spike/m0/run_m0.py all
```

Rellenar `spike/m0/M0_RESULTS.md` con lo medido. Sin eso, el resto del repo es
especulación.
