# SPEC — Rayito

> Nombre definitivo: **Rayito**. Agente dentro del MicroVM: `rayd`. Paquete PyPI / CLI: `rayito`.
> Fuentes de verdad por encima de este documento: `proto/rayito/v1/*.proto` y `AWS_API_NOTES.md`.

## 1. Qué construimos

Un SDK open-source que da a un agente de IA un sandbox de ejecución aislado por
hardware, con una API de alto nivel equivalente a la de E2B, corriendo **dentro de
la cuenta AWS del propio usuario** sobre AWS Lambda MicroVMs.

El objetivo de ergonomía es que esto funcione:

```python
from rayito import Sandbox

with Sandbox.create(template="base-2gb", timeout=3600) as sbx:
    sbx.files.write("/home/user/data.csv", csv_bytes)
    r = sbx.run_code("import pandas as pd; df = pd.read_csv('/home/user/data.csv'); df.describe()")
    print(r.text)                      # repr de la última expresión (execute_result)
    r = sbx.run_code("df.plot(); import matplotlib.pyplot as plt; plt.show()")
    print(r.results[0].png[:16])       # PNG en base64
    out = sbx.commands.run("ls -la /home/user")
    print(out.stdout)
```

`template` es el nombre (o ARN) de una imagen de MicroVM; el tamaño va en la
imagen, no en `create()`. `timeout` es el plazo lógico del sandbox, impuesto
por `rayd` y movible con `set_timeout()` / `connect(timeout=)`; `max_lifetime`
es el tope de la plataforma (running + suspendido, ≤ 8 h), fijo tras crear
(ADR-011). Sin `max_lifetime` ni `on_timeout`, `timeout` sigue siendo la vida
máxima e inmutable de ADR-007.

## 2. Por qué existe

Empresas reguladas (fintech, salud, legal) no pueden enviar código de clientes a
un sandbox de terceros. El panorama a 2026-09:

| Categoría | Ejemplos | Qué falta |
|---|---|---|
| SaaS con buena ergonomía, datos fuera de tu cuenta | E2B cloud, Modal, CodeSandbox, Vercel Sandbox, Runloop | compliance |
| Managed BYOC | E2B Enterprise BYOC (~$3k/mes), Northflank BYOC, Daytona (invite-only) | precio de entrada, clúster que opera otro |
| Primitivas self-hosted sin capa de abstracción | Firecracker crudo, Kata, `e2b-dev/infra` sobre Nomad | operar un clúster |
| Ya sobre Lambda MicroVMs | RaitBox (E2B-compatible, `exec()`, "pause" = copiar ficheros a S3), asbox, microvms-agentd, microvm-ctl (0–6 ⭐) | ninguno tiene pause/resume con memoria viva + resultados Jupyter + streaming gRPC |

Rayito ocupa el hueco con estos diferenciales, en este orden:

1. **Pause/resume que conserva el kernel vivo**: una variable definida antes de
   `pause()` sigue existiendo tras `resume()` (snapshot de memoria de AWS).
2. Resultados Jupyter reales (`png`, `html`, `chart`, `data`, …), no texto de
   `exec()`.
3. Agente Rust estático con streaming gRPC y clientes generados desde `.proto`.
4. Serverless en la cuenta del cliente, sin clúster ni servicio intermedio.

El pitch es compliance + cuenta propia, **no precio** (ver §7).

## 3. Alcance del slice ejecutable (M1–M9)

Todo lo siguiente entra en el contrato `.proto` desde el día uno. La
implementación se secuencia en `MILESTONES.md`, no se hace en paralelo. Los
métodos de E2B que no tienen equivalente se declaran con `unimplemented`, no se
emulan.

| Módulo | Superficie | Milestone |
|---|---|---|
| Ciclo de vida | `create(template, timeout, idle, envs, execution_role_arn, allowed_ports, …)`, `connect(sandbox_id, access_token)`, `kill`, `list(template, template_version, states)` (excluye `TERMINATING|TERMINATED` por defecto; los items no traen `endpoint`), `get_info` (`expires_at`, `remaining_seconds`, `state_reason`), `is_running` (vía `Health`), `sbx.access_token`, `sbx.endpoint` | M2 |
| Comandos | `commands.run` (fg/bg, `stdin`, `timeout` impuesto por el servidor), `list` (procesos y PTYs), `kill`, `connect(pid)`, `send_stdin`, `close_stdin`; `CommandHandle` con `wait/kill/disconnect` | M2 |
| Networking | `get_host(port) -> HostAccess` (`url`, `port`, `headers`, `client()`); un solo hostname por MicroVM, puerto vía `x-aws-proxy-port` y token con ese puerto | M2 |
| Filesystem | `read` (`text|bytes|stream`), `write`, `write_files` (varios ficheros en un stream), `list(depth)`, `exists`, `get_info`, `remove(recursive)`, `rename`, `make_dir -> bool` | M3 |
| Filesystem avanzado | `watch_dir(path, recursive, include_entry, on_event) -> WatchHandle` (bloquea hasta `WatchStarted`) | M3 |
| Código | `run_code(code, context, on_stdout, on_stderr, on_result, on_error, envs, timeout=300) -> Execution(results, logs, error, execution_count)`; `.text` = resultado principal; `create_code_context`, `list_code_contexts`, `remove_code_context`, `restart_code_context` | M4 |
| PTY | `pty.create(size, user, cwd, envs, shell, timeout, on_data) -> PtyHandle` (mismo handle que comandos), `send_input`, `resize`, `kill`, `connect(pid)` | M5 |
| Persistencia | `pause(wait)`, `resume(wait)`; hooks `/suspend` y `/resume` reales; reconexión por `resume_generation` | M5 |
| Salud | `is_running`; `get_metrics`; `get_metrics_history(start, end, max_points)` (serie de 5 s que `rayd` muestrea desde `/run`, anillo de 8 h, hueco en pausa; forma de clase con el access token) | M2 / M6 / M9 |
| Ciclo de vida (M9) | `create(max_lifetime=, on_timeout=)` (`'kill'` o `'pause'`), `set_timeout` (`SetTimeout` EXACT), `connect(timeout=)` (AT_LEAST), `SandboxLifecycle` en `get_health()`/`get_info()`, `LifecycleUnsupportedException` en imágenes anteriores a M9 (ADR-011); `paginate(limit, next_token, order, started_after)` con cursor opaco sobre `nextToken` | M9 |
| Transferencias (M9) | `S3Staging` en `create`/`connect`, `files.upload_url`/`download_url` (URLs de S3 prefirmadas con las credenciales del llamante, ADR-010), ficheros grandes por S3, `files.write/read(gzip=)`, `metadata` por fichero (xattrs), `stream_idle_timeout` | M9 |
| Red saliente (M9) | `create(network=, allow_internet_access=)`, `update_network`, `get_network`, `EgressProxy`, `Health.egress_enforcement`: política de E2B aplicada en el guest de `rayito-base-caps`, falla cerrado en otras imágenes (ADR-012) | M9 |
| Git (M9) | `sbx.git` (`Git`/`AsyncGit`, TS `Git`): la superficie git de E2B sobre `commands.run`, con `git-core` en `rayito-base` | M9 |
| Metadatos | `create(metadata=)` en el `runHookPayload` (4096 chars junto a `envs`), `rayd` lo devuelve en `Health`; `sbx.metadata`, `get_info().metadata`, `Sandbox.get_info(id)` (JWE + `Health` sobre `RUNNING`), `list(metadata=)` filtrado en cliente, O(n) y sólo `RUNNING` | M6 |
| Shim E2B | `rayito.e2b`: `Sandbox`/`AsyncSandbox` y los nombres del SDK de E2B 1.x sobre el nativo; kwargs de E2B mapeados o ignorados con aviso; `UnimplementedError` donde AWS no tiene primitiva. M9: superficie de E2B **2.x** en Python y en TypeScript (`rayito/e2b`), con `set_timeout`, `connect(timeout)`, `lifecycle`, `upload_url`/`download_url`, `get_metrics(start, end)`, `list(next_token)`, `allow_internet_access=False` y `network` (en `rayito-base-caps`; falla cerrado en otras imágenes), `update_network`, `git` y `run_code(language="typescript")` mapeados | M6 / M9 |
| Pool de suspendidos | `SandboxPool`/`AsyncSandboxPool` (`PoolConfig`, backends en memoria y JSON `rayito.pool/1`), `take()` con `resume-microvm` y el token de la plaza, `Sandbox.create(pool=)`; TypeScript `SandboxPool` | M7 |
| Persistencia S3 | `FilesystemService.Checkpoint`/`Restore` en `rayd` (tar.gz del `HOME`, credenciales IMDSv2 del execution role como root); `Sandbox.create(persist=S3Prefix)`, `connect(persist=)`, `checkpoint_files`, `restore_files`, `reincarnate()` | M7 |
| Kernels | `run_code(language=)` / `create_code_context(language=)`: `python`; `bash`, `javascript` (alias `js`) y `typescript` (alias `ts`) en la variante `rayito-base-poly` (JS/TS con el kernel Jupyter de Deno 2.9.7, arranque perezoso, ADR-013); en otra imagen `UNIMPLEMENTED` nombrando `rayito-base-poly` | M7 / M9 |
| MCP y CLI | `rayito.mcp` (`rayito[mcp]`: seis herramientas por stdio o streamable HTTP, un sandbox por proceso); CLI `rayito` (`rayito[cli]`: `image`, `sandbox`, `doctor`); M9: `rayito sandbox create`, `connect`, `exec` y `metrics` (fichero de token, terminal interactiva) | M7 / M9 |

## 4. No-objetivos (explícitos)

Se rechazan en review si aparecen antes de M6:

- Desktop / GUI / streaming de escritorio (el `e2b-desktop` equivalente).
- Templates declarativos con CLI propia (`rayito template build`). En M1–M5 la
  imagen se construye con un Dockerfile a mano y `create-microvm-image`. M7
  (`m7-cli`) entrega una CLI **operativa** (`rayito image|sandbox|doctor`)
  sobre el flujo Dockerfile existente; los templates declarativos
  (`rayito.toml`) siguen fuera. M9 añade `rayito sandbox
  create/connect/exec/metrics`, también operativos; `auth`, `template`,
  `snapshots` y `fork` de la CLI de E2B siguen fuera.
- Soporte multi-cloud. AWS-only por diseño (ADR-003).
- Kernels que no sean Python en `rayito-base`. `bash` llega en M7
  (`m7-poly-kernels`) como variante `rayito-base-poly` con arranque perezoso;
  `javascript` y `typescript` llegan en M9 (`m9-deno-kernels`) a esa misma
  variante con el kernel de Deno (`ijavascript` necesita compilador en al2023
  ARM64, AWS_API_NOTES.md Q57 y Q61). R y Java siguen fuera, con números
  medidos: R por conda-forge (`r-base` + `r-irkernel`) ocupa 1,4 GB
  instalado, y R-core por `dnf` 127 MB más cairo/pango/harfbuzz/tk/fuentes;
  Java serían 262 MB de Corretto 21 headless más IJava sin mantenimiento (una
  release desde 2023, no compila en JDK 17/18).
- Pool de MicroVMs **pre-calentados** (`RUNNING`, $0,126/h cada uno). Decidido en
  M6 con datos (`docs/benchmarks/2026-09-cold-start.md`, ADR-008): el p95 de
  `kernel_ready` en una ráfaga de 20 por el SDK fue 8,57 s (≥ 8 s, regla D11)
  pero el resume tarda 0,40 s, así que lo que entró en el plan es un **pool de
  suspendidos** (coste = storage del snapshot), no un pool de VMs corriendo.
  El pool de suspendidos se entregó en M7 (`m7-suspended-pool`:
  `SandboxPool`/`AsyncSandboxPool`, `Sandbox.create(pool=)`, `take()` →
  primera celda p95 0,90 s; `docs/site/docs/pool.md`); el pre-calentado
  sigue fuera.
- Billing, dashboard o cualquier superficie de producto SaaS.
- ~~**Metadatos por sandbox**~~: entregados en M6 vía `runHookPayload` +
  `Health` (los MicroVMs no admiten tags y `list-microvms` no filtra por
  ellos); `list(metadata=)` es O(n) sobre los sandboxes `RUNNING` y no es
  secreto. Sin almacén del lado cliente ni actualización tras `create()`.
- **Tamaño por sandbox** (`cpu=`/`memory=`): el tamaño es propiedad de la imagen
  (`resources[0].minimumMemoryInMiB`) tanto en Rayito como, de hecho, en E2B.
  Un template = un tamaño (`base-2gb`, `base-4gb`).
- ~~Persistencia de filesystem entre sesiones (S3/EFS)~~ (era candidata a M6):
  entregada en M7 (`m7-s3-persistence`, ADR-009): checkpoint/restore de
  `/home/user` en S3 desde `rayd` (`Sandbox.create(persist=)`,
  `checkpoint_files()`, `restore_files()`, `reincarnate()`); EFS sigue fuera.
- Servicio de plano de control: en M1–M5 el SDK llama a AWS directamente.

## 5. Decisiones ya tomadas

| Decisión | Elección | Razón |
|---|---|---|
| Lenguaje del agente | Rust, `aarch64-unknown-linux-musl`, estático | MicroVMs es ARM64/Graviton-only; binario mínimo sin runtime ni crate TLS (el proxy termina TLS) |
| Protocolo | gRPC h2c vía `tonic` 0.14 + `x-aws-proxy-force-h2` (ADR-001) | Connect en Rust es inmaduro; el proxy soporta gRPC inbound |
| Forma de los streams | sólo server-stream + unarios; PTY sin bidi (ADR-005) | bidi a través del proxy no está verificado; server-stream es la forma probada de `envd` y encaja con `grpcio` sync |
| Codegen Rust | `tonic-prost-build` + `protox` en `crates/rayito-proto/build.rs`, sin `protoc` | tonic ≥ 0.14 separó prost; no hay `protoc` en la máquina de desarrollo |
| Codegen Python / TS | `buf` con plugins pinneados (`protocolbuffers/python:v36.1`, `grpc/python:v1.84.0`, `bufbuild/es:v2.15.0`) | reproducible; el suelo de `protobuf` en `pyproject` = versión del plugin |
| Stack Python | `grpcio` + `protobuf` + `boto3`, `uv_build`, `>= 3.11`, sync y async con la misma superficie | mínimo de dependencias; `grpc.aio` reutiliza los stubs |
| Cliente TypeScript | Connect-ES v2 (`@connectrpc/connect-node` `createGrpcTransport`) + `protoc-gen-es` | un solo plugin genera tipos y servicios; el interceptor inyecta las cabeceras del proxy |
| Kernel Jupyter | sidecar Python con `jupyter_client` (`AsyncKernelManager`, transporte `ipc`; TCP de loopback con HMAC sólo para Deno, ADR-013), JSON lines por stdio (ADR-002) | el trabajo real (formatters, warm-up, supervisión) es Python de todas formas; consolidar en Rust es opción M6 |
| PTY | `nix::pty::openpty` + `tokio::process::Command` (ADR-005) | cambio de uid nativo, lectura async, una sola versión de `nix`; `portable-pty` descartado |
| Hooks | puerto dedicado 9000, `axum` HTTP/1.1, nunca en `allowedPorts` (ADR-006) | no mezclar HTTP/1.1 con h2c ni exponer los hooks a tokens del 8080 |
| Token del agente | sha256 del secreto en `runHookPayload` → `POST /run`; `x-access-token` en cada RPC salvo `Health` (ADR-004) | `run-microvm` no tiene env vars; es el único canal per-VM |
| Vida del sandbox | plazo lógico en `rayd` + `maximumDurationInSeconds` ≤ 28800 como tope (ADR-011) | no existe `UpdateMicrovm`; `rayd` es PID 1 y su salida termina la VM sin IAM (Q58) |
| Estructura de `rayd` | hexagonal: `rayito-proto` (generado) / `rayd-core` (dominio + puertos, sin tonic ni axum) / `rayd` (adaptadores + main) | el dominio se testea en Windows con puertos falsos; los adaptadores sólo en Linux |
| Clientes | Python primero, TypeScript después | generados desde `.proto`, no escritos a mano |
| Plano de control | librería en el cliente (boto3), no servicio | cada MicroVM tiene su endpoint propio; nada que un servicio añada en M1–M5 |
| Readiness | `HealthService.Health` a través del endpoint, nunca `get-microvm.state` | el estado es eventualmente consistente por documentación |
| Imagen | zip (Dockerfile + `rayd` precompilado + sidecar) → S3 → `create/update-microvm-image` → gate de tres estados | AWS construye la imagen; Docker local fuera del camino crítico |
| Compatibilidad E2B | misma superficie; lo que no existe se declara `unimplemented` | patrón Dormice; sin promesas falsas de paridad |
| Shim `rayito.e2b` (M6; 2.x y `rayito/e2b` en TypeScript desde M9) | drop-in a nivel de import por composición sobre `rayito.Sandbox`: defaults de E2B (`timeout=300` como plazo lógico con `on_timeout='kill'`, sin idle, `ALL_INGRESS` + `INTERNET_EGRESS`), `RayitoCompatWarning` por kwarg ignorado, `UnimplementedError(NotImplementedError)` (el nativo re-exportado) para lo que no tiene primitiva: `fork`/snapshots, `pause(keep_memory=False)`, `network.rules`, `mask_request_host`, `allow_public_traffic=True`, MCP, volúmenes, templates, kernels R/Java; `allow_internet_access=False` y `network` sólo en `rayito-base-caps` (en otras imágenes: `UnimplementedError` tras terminar el VM, porque sin conector de egress el MicroVM sigue saliendo, Q44/Q60) | nunca sombrear la distribución `e2b`; un `except SandboxException` de E2B no debe tragarse una feature ausente |

## 6. Definición de "hecho" para el slice

Un test de integración que, contra una cuenta AWS real (`RAYITO_E2E=1`):

1. Publica la imagen y espera al gate de tres estados (`CREATED|UPDATED` +
   `SUCCESSFUL` + `ACTIVE`).
2. Lanza un MicroVM con `maximumDurationInSeconds=1800` y obtiene un `Sandbox`
   con `Health.agent_ready` y `kernel_ready`.
3. Escribe un CSV, ejecuta código Python que lo lee, y recibe un resultado con
   texto (`.text`) y un PNG de matplotlib (`results[0].png`). `print(x)` aparece
   en `logs.stdout`, no en `.text`.
4. Abre una PTY, manda `echo hola\n`, y recibe `hola` en el stream de salida.
5. Con un `sleep 4000` en background y la PTY abiertos, suspende el sandbox
   (`pause()`), lo reanuda (`resume()`), y verifica que una variable definida en
   el contexto de código **sigue viva**, que el proceso y la PTY siguen vivos,
   que `commands.connect(pid)` devuelve el exit code correcto y que
   `Health.resume_generation` ha incrementado.
6. Mata el sandbox y verifica que `get_info()` reporta `state == TERMINATED` y
   que `list()` (que filtra `TERMINATED` por defecto) ya no lo devuelve.

Si el punto 5 no pasa, el valor diferencial del producto no existe. Es el test
más importante del repo. Coste aproximado por ejecución: $0.03 (+$0.037 si
publica una versión nueva de imagen).

## 7. Riesgos abiertos que el spec no puede cerrar

Se resuelven midiendo, no diseñando. Ver `MILESTONES.md` M0 y
`AWS_API_NOTES.md` §16.

- **Credenciales a través de suspend/resume** (Q1). El mecanismo documentado es
  IMDSv2 en el guest; rotación y TTL tras `/resume` no están documentados. Si
  no rotan bien, el diseño de persistencia cambia. Además, cualquier código del
  sandbox puede leerlas: por defecto se lanza **sin** execution role.
- **Kernel en memoria tras resume** (Q7). E2B y aws-samples mantienen sesiones
  vivas con hooks vacíos; hay que medirlo con ipykernel y ZMQ `ipc://`.
- **Coste real.** Precios **verificados en Cost Explorer el 2026-09-16**
  (`AWS_API_NOTES.md` §12; los cinco usage types `Lambda-MicroVM-*` cuadran con
  la página de precios dentro del 0,21 %): $0.0000276944/vCPU-s +
  $0.0000036667/GB-s ⇒ 2 GB / 1 vCPU ≈ **$0.126/h**; snapshot write $0.0038/GB
  (suspend), read $0.00155/GB (run/resume), storage $0.08/GB-mes (mes de 720 h)
  con mínimo de 1 semana por versión de imagen. ≈ **3,2×** Fargate ARM
  on-demand ($0.0395/h), ≈ 10× Fargate Spot, ≈ **1,5×** E2B ($0.083/h) por hora
  activa sin cuota de plan. Con `rayito-base` 10.0 (snapshot de memoria de
  0,919 GB) un lanzamiento lee ≈ $0.0014 de snapshot y un ciclo suspend/resume
  cuesta ≈ **$0.0049** (escritura + lectura de 0,919 GB, no de 2 GB) ≈ 140 s de
  compute ⇒ `max_idle_seconds` por debajo de ≈ 2,5 min no ahorra. El
  benchmark de M6 (142 lanzamientos, 24 ciclos) costó ≈ $0.35 de uso
  (`docs/benchmarks/2026-09-cold-start.md` §7–§8); la lectura de snapshot es
  el 80 % del coste de una VM corta. Pendiente: la línea de Cost Explorer del
  día del bench (GB reales por lanzamiento y por suspend, Q8, Q22).
- **Cold start.** Medido el 2026-09-16 con `rayito-base` 10.0 (0,919 GB de
  memoria) y sonda de 100 ms (`docs/benchmarks/2026-09-cold-start.md`,
  `AWS_API_NOTES.md` §16 Q42/Q43): `run-microvm` → `Health.agent_ready` p50
  **2,3 s** / p95 3,0 s; → `kernel_ready` p50 **5,2 s** / p95 **6,1 s** (20
  secuenciales); en ráfaga de 20 por el SDK p50 5,7 / p95 **8,6 s** (≈ 3 s son
  la cola del bucket de 5 TPS del SDK; sin bucket, 20 `run-microvm`
  simultáneos: p95 5,7 s, 0 `ThrottlingException`, las 20 usables a los 5,9 s);
  `resume()` p50 **0,38 s** / p95 0,40 s, auto-resume 0,67 / 0,68 s; primera
  celda 0,10 s tras crear y tras reanudar. Con el warm-up del kernel
  desactivado (`rayito-base-slim` 2.0, 0,694 GB) el secuencial baja a p50
  **3,0 s** / p95 3,4 s y la primera celda con pandas + matplotlib sube de
  0,14 a 0,79 s: el grueso del cold start es el warm-up que el kernel repite en
  cada `/run`, no el tamaño del snapshot (la diferencia de `agent_ready` entre
  919 y 694 MB, 0,5 s, queda dentro de la resolución de la medida, ≈ 0,4–0,7 s
  por valor). Decisión sobre el pool en §4 y ADR-008 (`B20` supera el umbral
  por menos que esa incertidumbre; la regla se aplicó tal cual).
- **Tope duro de 8 h** (running + suspendido), no ajustable, sin `UpdateMicrovm`.
  Diferencia visible con E2B: `set_timeout` y `connect(timeout=)` mueven el
  plazo lógico, pero nunca más allá de `max_lifetime` (ADR-011); pasado ese
  tope sólo queda `reincarnate()`.
- **TTL del token JWE de 60 min.** Refresh automático a los 45 min, también
  durante la pausa. No es opcional.
- **`runHookPayload`: 4096 caracteres** según el modelo (botocore rechaza más en
  cliente) frente a "16 KB" en la prosa. Es el único canal per-VM (token + envs);
  el SDK asume 4096 hasta que M0 mida (Q11).
- **Ancho de banda 4 MB/s** por sentido a 2 GB y **8 conexiones concurrentes**
  por MicroVM a 1 vCPU, no ajustables. 50 MB tardan ≥ 12,5 s por trayecto; el
  SDK usa como máximo 2 canales y agrupa ficheros en un stream.
- **gRPC a través del proxy** (Q17): trailers y streams largos con
  `x-aws-proxy-force-h2` sobre tonic h2c se confirman en M1 con el primer
  binario; si falla, `rayd` necesitaría TLS y cae la premisa "sin crate TLS".
- **Alcance del puerto de hooks desde fuera** con un token `allPorts` (fila sin numerar de `spike/m0/M0_RESULTS.md`,
  riesgo T2 de `SECURITY.md`).
