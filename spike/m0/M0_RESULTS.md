# M0 — resultados medidos

> Estado: **ejecutado el 2026-09-15** contra una cuenta de pruebas, región
> us-east-1, con `spike/m0/run_m0.py` (pasos `iam → image → run → probe →
> suspend → resume → streams → fleet → cleanup`, más las pasadas `failsuspend` y
> `silentstream`). El crudo (`spike/m0/out/`, con una línea por medida en
> `results.jsonl`) se regenera en local con `run_m0.py` y no se versiona;
> esta tabla conserva las cifras medidas. Imagen `rayito-m0-probe` (2048 MiB, hooks en :9000) con tres
> versiones: 1.0 (probe base), 2.0 (`PROBE_FAIL_SUSPEND=1`, dejada INACTIVE) y
> 3.0 (stream silencioso). VM principal con `executionRoleArn`, `idlePolicy`
> {autoResume, 120 s, 3600 s}, `maximumDurationInSeconds` 7200, `runHookPayload`
> JSON con un sha256, `logging.cloudWatch.logGroup=/rayito/rayito-m0-probe`.
> Todos los MicroVMs (45) quedaron TERMINATED; la imagen sigue en la cuenta.

| # | Pregunta | Esperado desde docs | Medido | Veredicto |
|---|---|---|---|---|
| 1 | Credenciales del execution role tras suspend→resume (AccessKeyId, Expiration antes/después) | IMDSv2 vivo; rotación no documentada | IMDSv2 (`PUT /latest/api/token` + `GET …/iam/security-credentials/` → rol `execution_role`). Antes: AccessKeyId …72TY, LastUpdated 14:39:02Z, Expiration 15:34:02Z (TTL 55 min). Tras 313 s suspendido: **las mismas credenciales**, no expiradas, misma Expiration; legibles también como uid 1000. Sin `AWS_ACCESS_KEY_ID` en el entorno. Rotación al vencer el TTL: no medida (requiere > 55 min) | **PASS** (criterio de parada superado) |
| 2 | `run-microvm` → primer 200 (p50/p95, 20 secuenciales) | 2.7–4.5 s | 20 lanzamientos secuenciales sin execution role: **p50 1.85 s, p95 2.13 s**, min 1.78, max 2.40 (la llamada API tarda 0.2–0.75 s; el resto es arranque + proxy). VM principal con execution role y hooks: 2.37 s | Mejor que lo publicado |
| 3 | 20 lanzamientos en paralelo | ThrottlingException > 5 TPS | 20 `run-microvm` simultáneos (cliente sin reintentos): **20/20 OK, 0 ThrottlingException**, 3.9 s de pared. La latencia de la API sube a p50 1.37 s (×2–6) en vez de rechazar; primer 200 p50 2.53 s, p95 3.59 s | La cuota de 5 TPS no se manifestó como 429 en una ráfaga de 20; mantener token bucket por prudencia |
| 4 | `resume-microvm` → primer 200; auto-resume primer request | 0.7–2.6 s | `resume-microvm` API 0.60 s; primer 200 a **1.21 s** desde la llamada. Auto-resume (`autoResumeEnabled`, primer request tras 20 s suspendida): 200 en **1.56 s**; tras suspensión por idle: 1.13 s | Como docs, en el extremo rápido |
| 5 | Proceso en background y `sleep 120` tras resume (gaps wall/mono) | sobreviven; mono no avanza en pausa | Ticker en background vivo (38 líneas por pipe y por fichero); gap máximo entre ticks **313.7 s en wall y 313.7 s en mono**. `sleep 120` terminó con elapsed 350 s wall = 350 s mono | Sobreviven. **`CLOCK_MONOTONIC` sí avanza durante la pausa** (contradice la nota de §15) |
| 6 | TCP loopback / socketpair Unix / TCP saliente tras resume | loopback y Unix sí; saliente no | loopback 127.0.0.1: eco OK; `socketpair`: eco OK; TCP saliente a example.com:80 (abierto antes de suspender): `ConnectionAbortedError [Errno 103]` | Como docs |
| 7 | ipykernel con `x=42` tras resume (cliente existente y cliente nuevo) | sobrevive | Kernel `ipc://` vivo (`kernel_info` 1.9 ms); `x` → `42` desde el cliente existente **y** desde un cliente nuevo cargado del connection file | **PASS** (criterio de parada superado) |
| 8 | Coste 1 h activa + 7 h suspendido (Cost Explorer al día siguiente) | ≈ $0.142 | No medible en M0 (Cost Explorer tarda ~24 h). La pasada completa consumió ≈ 25 min de una VM de 2 GB, 44 VMs de < 1 min, 4 suspend/resume y 3 builds; estimación por precios publicados ≈ $0.3–0.5 incluyendo el mínimo de storage de 3 versiones | Pendiente (revisar el 2026-09-16) |
| 9 | Cuotas reales de la cuenta (Service Quotas) | 400 GB, 5 TPS Run | Aplicadas: memoria ARM_64 **1024 GB**; RunMicrovm 5/5 (rate/burst), ResumeMicrovm 5/5, SuspendMicrovm 2/2, TerminateMicrovm 10/10, GetMicrovm 100/100, CreateMicrovmAuthToken 50/50, CreateMicrovmShellAuthToken 5/5; builds concurrentes 10; imágenes 100; versiones/imagen 50; duración máx 8 h (no ajustable); conexiones 8/16/32/64/128 (1/2/4/8/16 vCPU, no ajustables); RPS 40 (4 vCPU) y 160 (16 vCPU), **sin cuota publicada para 1–2 vCPU** | Como docs para la región grande |
| 10 | `/suspend` devolviendo 500 (versión 2.0 con `PROBE_FAIL_SUSPEND=1`) | la VM sigue RUNNING | `suspend-microvm` acepta; en < 5 s la VM está **TERMINATED**, endpoint 502, `stateReason`: "Suspend lifecycle hook returned HTTP status 500. Please check your hook endpoint and application logs for more details." Sin reintentos del hook observables | **Contradice** troubleshooting: un `/suspend` fallido mata la VM |
| 11 | `runHookPayload` 4097 chars | rechazado en cliente; servidor ? | Con la validación de botocore desactivada, el **servidor** responde `ValidationException` 400 "Member must have length less than or equal to 4096" | 4096 (la prosa "16 KB" es falsa) |
| 12 | Log group por defecto | `/aws/lambda-microvms/<img>` o `/aws/lambda/microvms/<img>` | VMs lanzadas sin `logging` (con execution role) escriben en **`/aws/lambda-microvms/rayito-m0-probe`**; `run-microvm` **no hereda** el `logGroup` de la imagen. Streams: `YYYY/MM/DD[<version>]<microvmId>`. Los logs de build van al grupo de la imagen con el mismo formato de stream | `/aws/lambda-microvms/<image>`; seguir pasando `logging` explícito |
| 13 | `list-microvms` tras terminate | ? | TERMINATED sigue listado a los 5 s, 65 s y ~20 min (45 entradas TERMINATED de la imagen). `get-microvm` tras terminate: `state=TERMINATED`, `stateReason="Success."`, `terminatedAt` | El SDK filtra por estado |
| 14 | Prefijo del `microvmId` | `mvm-` o `ai-` | **`microvm-<uuid>`** (p. ej. `microvm-00000000-0000-0000-0000-000000000001`); `endpoint` = `<uuid-hex>.lambda-microvm.us-east-1.on.aws` sin esquema; `state` al volver `PENDING` | Ninguno de los dos documentados |
| 15 | Stream SSE abierto vs idle 120 s; qué ve el cliente | ? | Stream **con datos** (1 tick/s): la VM sigue RUNNING 260 s (> 120 s). Stream abierto **sin datos** (cabeceras y nada más): **SUSPENDED a ~130 s**; el cliente ve `ChunkedEncodingError: Response ended prematurely` ~57 s después de la suspensión (186 s); el siguiente request auto-reanuda en 1.13 s | Sólo cuentan bytes; un stream ocioso no mantiene viva la VM |
| 16 | 9 conexiones HTTP/1.1 concurrentes a 2 GB | la 9ª → 429 | 9 streams de 30 s simultáneos: **9 × 200**, ningún 429 | El cap de 8 no se aplicó como 429 en esta prueba |
| 17 | `curl --http2` + `X-aws-proxy-force-h2` (gRPC real en M1 con tonic) | h2 | (`httpx[http2]`, ningún curl de Windows trae h2) Sin la cabecera: cliente↔proxy HTTP/2, la app recibe **HTTP/1.1** (200). Con `x-aws-proxy-force-h2: true`: **502** porque la app stdlib sólo habla HTTP/1.1 (el proxy pasó a h2c) | La cabecera es efectiva y necesaria para h2c; bidi/trailers pendientes M1 |
| 18 | Listener en 127.0.0.1:3000 vía `X-aws-proxy-port: 3000` | ? | **200** `{"loopback_listener": true}`. El proxy corre dentro de la VM: la app ve `client=127.0.0.1` | Alcanzable |
| 19 | Δ reloj de pared vs monotónico tras resume; `date` dentro vs fuera | wall congelado? | Entre `/suspend` y `/resume`: Δwall **313.15 s**, Δmono **313.14 s**; hora UTC en la VM vs real: −0.98 s | AWS corrige el reloj de pared **y** el monotónico avanza: los timeouts monotónicos vencen tras resume |
| 20 | uid por defecto, CapEff, cgroup escribible, setpriv/iptables/useradd, IMDS como uid 1000, IP origen de hooks, versión HTTP de hooks | root; caps restringidas | uid 0; CapEff = chown, dac_override, fowner, fsetid, kill, setgid, setuid, setpcap, net_bind_service, net_raw, sys_chroot, mknod, audit_write, setfcap (**sin** sys_admin, net_admin, sys_ptrace); `/sys/fs/cgroup` no montado ni escribible; setpriv, runuser, su, useradd, iptables presentes; sudo y nft ausentes; `iptables -L` falla ("Permission denied") sin net_admin; IMDS OK como uid 1000; hooks desde **127.0.0.1, HTTP/1.1** (`/ready` con `user-agent: smithy-java/1.6.0` y `x-amzn-requestid`; `/run`, `/suspend`, `/resume` con `host: localhost:9000`, sin user-agent); RLIMIT_NOFILE 1024; kernel 6.1.166 amzn2023 aarch64; `/` ext4 8 GB en `/dev/vdc`; cgroup de PID 1 `0::/system.slice/app` | Root capado; sin `additionalOsCapabilities` no hay iptables |
| 21 | Tamaños de snapshot y tiempo de build de la imagen probe | build 3–6 min | Build **135 s** (v1.0, `chipsetGeneration` 3) y **145 s** (v3.0, generación 4). `snapshotBuild`: memoria 672/678 MB, `codeInstallSizeInBytes` 644 MB, disco 33/35 MB. Imagen con pandas/matplotlib: no medida | Más rápido que lo publicado |
| 22 | ¿Se factura el build? ¿Mínimo 1 semana en storage de suspend? | ? | No medible en M0 (facturación); revisar Cost Explorer el 2026-09-16 | Pendiente |
| 23 | `endpoint` con `https://` o hostname pelado | pelado | Hostname pelado (`<uuid-hex>.lambda-microvm.us-east-1.on.aws`) | Pelado |
| 24 | Hooks alcanzables vía proxy con token `allPorts` en puerto 9000 | ? (riesgo T2) | `POST :9000/aws/lambda-microvms/runtime/v1/ready` con `X-aws-proxy-port: 9000` → **200** | **Riesgo confirmado**: nunca incluir el puerto de hooks en `allowedPorts` |
| 25 | RNG clonado entre dos VMs de la misma imagen (`random_at_import`) | idéntico | `random_at_import` = 0.5693094545201111 en **ambas** VMs; `random_now` distinto porque `/run` hace `random.seed()` | Clonado; reseed en `/run` obligatorio |
| 26 | Token de 61 min | ValidationException | `ValidationException` 400 "ExpirationMinutes 61 exceeded max allowed of 60." | 60 máx (servidor) |
| 27 | Ancho de banda: 16 MiB a 2 GB | ≈ 4 s | 16 MiB en 3.69 s = **4.54 MB/s** | ≈ 4 MB/s como docs |
| 28 | Token de 1 min con stream abierto en el minuto 2 | ? | Stream abierto con token de 1 min siguió recibiendo ticks **150 s** (hasta que el servidor cerró) | La expiración sólo se evalúa al abrir la conexión |

## Otros hechos medidos

- `get-microvm-image` / `update-microvm-image` con el nombre pelado → `ValidationException: Invalid ARN format`; `imageIdentifier` es siempre el ARN.
- Versiones de imagen numeradas `1.0`, `2.0`, `3.0`; `baseImageVersion` devuelto `1.0` (la base gestionada lista `0` y `1`).
- `update-microvm-image-version --status INACTIVE` funciona; mientras la imagen está `UPDATING` responde `ConflictException: MicroVM Image is already in state: UPDATING`.
- `authToken` es un map con una única clave, exactamente `X-aws-proxy-auth`; un token acuñado antes de suspender sigue valiendo tras reanudar.
- La app sólo ve `Host: <endpoint>`, `x-amzn-requestid` y las cabeceras del cliente; ninguna `X-aws-proxy-*` ni `X-Forwarded-For`.
- Variables inyectadas: `AWS_LAMBDA_MICROVM_IMAGE_ARN`, `AWS_LAMBDA_MICROVM_IMAGE_NAME`, `AWS_LAMBDA_MICROVM_IMAGE_VERSION`, `AWS_REGION`.
- El body de `/run` es exactamente `{"microvmId": …, "runHookPayload": …}` (196 bytes con un payload de 106); el sha256 del payload coincide con el enviado.
- Suspender tarda 1.4 s hasta `SUSPENDED`; `/validate` corre en una VM desechable (su estado no aparece en el snapshot).

## Criterio de parada (MILESTONES.md)

Preguntas 1 y 7: **superadas**. Las credenciales del execution role siguen
válidas tras suspend→resume y el ipykernel conserva `x = 42` para clientes
viejos y nuevos. Cambios de diseño que sí exige M0: `/suspend` no puede fallar
nunca (500 = VM terminada), `CLOCK_MONOTONIC` avanza en pausa, un stream ocioso
no cuenta como tráfico, y el puerto de hooks es alcanzable con un token
`allPorts`.
