# MILESTONES

Cada hito tiene un criterio de aceptación verificable. **No se empieza el
siguiente hasta que el test del anterior pasa en verde contra AWS real.**

Costes de referencia (us-east-1, 2 GB): un test de aceptación completo ≈
**$0.03** de compute; publicar una versión nueva de imagen añade **$0.037** de
storage mínimo (1 semana). Un MicroVM huérfano de 2 GB cuesta ≈ $1 en sus 8 h
de vida y retiene 2 GB de la cuota de memoria de la región.

---

## M0 — Spike de validación (manual, sin código de producción)

No escribir nada en `crates/` ni `clients/` en este hito. El objetivo es
responder las **22 preguntas de `AWS_API_NOTES.md` §16** con un runbook y una
imagen sonda propios (hito cerrado: el spike ya no está en el árbol y sigue en
el historial de git; la plantilla IAM que creó vive hoy en `infra/iam.yaml`).

Tareas:

1. Tarea 0: credenciales válidas (`aws sso login`, `aws sts get-caller-identity`
   en `us-east-1`), bucket S3 en la región, stack `infra/iam.yaml` (build
   role, execution role sólo-logs, managed policy del caller) y volcado de las
   cuotas **aplicadas** de la cuenta (`servicequotas`), que en cuentas nuevas
   pueden ser menores que las documentadas.
2. Leer los dos samples de AWS sin desplegarlos (`claude-managed-agents`: hooks
   en :9000, `runHookPayload` JSON, token bucket de 5 TPS en el launcher,
   hooks suspend/resume/terminate = 200 vacíos; `multi-tenant`: hooks en :8080
   junto a la app, `/ready` 503 con escape a los 240 s, `/validate` con
   prewarm). El de Claude necesita environment key + webhook secret de Managed
   Agents y no mide nada.
3. Construir y lanzar la imagen sonda (`probe.py`: hooks en :9000, endpoints
   de medición en :8080, IMDS, reloj, procesos, sockets, ipykernel, stream,
   ancho de banda). AWS la construye desde el zip: sin Docker ni ARM64 local.
4. `run → probe → suspend → resume → streams → fleet → cleanup`: medir tiempos,
   supervivencia de proceso en background, socket loopback/Unix/saliente, kernel
   con `x = 42`, reloj de pared vs monotónico, RNG clonado entre dos VMs de la
   misma imagen, 9 conexiones concurrentes, `runHookPayload` de 4097, token de
   61 min, hooks alcanzables vía proxy con token `allPorts`, log group real,
   `list-microvms` tras terminate.
5. Segunda pasada con `PROBE_FAIL_SUSPEND=1` para Q10 (`/suspend` → 500).

**Criterio de aceptación:** las 22 filas de la tabla de resultados del spike (`M0_RESULTS.md`, historial de git) están
rellenas con el valor medido, o con "no medible en M0" y el motivo (8 y 22
dependen de Cost Explorer con ~24 h de retraso). Coste de la pasada completa
< $3.

**Criterio de parada:** si la pregunta 1 (credenciales tras resume) o la 7
(kernel en memoria) salen mal, parar y replantear el diseño de persistencia
antes de M1. Son los supuestos de los que cuelga todo lo demás.

Lo que M0 **no** cubre: gRPC real a través del proxy (Q17, trailers, streams
largos). Necesita un binario tonic ARM64 → es el primer test de M1.

---

## M1 — Toolchain, andamiaje, contrato y "hello rayd"

Toolchain (un solo entorno Rust, bajo WSL2 Ubuntu 24.04; en D:, nunca en C:):

- `rust-toolchain.toml`: `channel = "1.98.1"`, componentes `clippy`, `rustfmt`,
  target `aarch64-unknown-linux-musl`. Sin `.cargo/config.toml` (`crt-static`
  ya es el default en ese target).
- `cargo-zigbuild` 0.23.4 + `zig` 0.16.0 (`cargo zigbuild --release --target
  aarch64-unknown-linux-musl -p rayd`; `cargo build` a secas no tiene linker
  aarch64-musl). Smoke build de un hello-world con `mimalloc` antes de fijar el
  Makefile.
- `buf` (`buf lint`, `buf breaking --against '.git#branch=main,subdir=proto'`,
  `buf generate`). Requiere `git init` + primer push antes que nada.

Workspace y codegen:

- `crates/rayito-proto` (`build.rs`: `protox::compile` de los seis `.proto` →
  `tonic_prost_build`, sin `protoc`; `tonic` 0.14.6, `tonic-prost` 0.14.6,
  `prost` 0.14.4, `protox` 0.9.1), `crates/rayd-core`, `crates/rayd` (ver
  `ARCHITECTURE.md`, "Diseño hexagonal"). Lints de workspace:
  `unwrap_used`/`expect_used = deny`, `clippy.toml` con
  `allow-unwrap-in-tests = true`.
- `buf.gen.yaml` con plugins pinneados; Python a `clients/python/src`
  (importable como `rayito.v1.*`), TypeScript a `clients/typescript/src/gen`
  (`protoc-gen-es` v2, `target=ts`, `import_extension=js`).
  `clients/python/pyproject.toml`: `grpcio>=1.84,<2`, `protobuf>=7.36.1,<8`,
  `boto3>=1.43.82,<2`, `uv_build`, `>= 3.11`, `py.typed`.
- El contrato ya está en `proto/rayito/v1/` (PTY server-stream + `Connect` +
  `SendInput`, `KeepAlive`, `WatchDirResponse`, `EndEvent` sint32/status/signal,
  `CloseStdin`, `ProcessKind`, `DataEvent.seq`, `OutputChunk`,
  `ExecutionStarted`, `Reattach`, `resume_generation`, `Write` multi-fichero).
  Desde el primer `buf generate`, `buf breaking FILE` lo congela.

Pipeline de imagen (`image/`, `Makefile`):

- `image-zip`: copia `target/aarch64-unknown-linux-musl/release/rayd` y
  `kernel-sidecar/` a `image/` y comprime con el Dockerfile en la raíz.
- `image-publish` (`scripts/publish_image.py`): sube a
  `s3://<bucket>/rayito/images/rayd-<sha256[:12]>.zip` (no-op si la clave
  existe), `create-microvm-image` la primera vez o `update-microvm-image`
  después (PUT con `baseImageArn`, `buildRoleArn` del stack `rayito-m0-iam`,
  `codeArtifact`; `--base-image-version` opcional), reutiliza la versión si el
  artefacto y la configuración ya coinciden (`--force` para rehacerla), hooks
  con timeouts explícitos (`port` 9000, ready 600 s, validate 600 s, run 30 s,
  resume 30 s, suspend 30 s, terminate 10 s), `logging.cloudWatch.logGroup`
  explícito, y sondea el **gate de tres estados** (imagen `CREATED|UPDATED`,
  versión `SUCCESSFUL`, status `ACTIVE`) imprimiendo los tamaños de
  `snapshotBuild`. Enums según el modelo (`CREATE_FAILED`, `--status`).
- `image-prune`: conserva las 5 versiones más recientes.
- `image-local`: `docker buildx --platform linux/arm64`, opcional, fuera del
  camino crítico.
- `scripts/hooks-sim.py` para el bucle interno: hace POST a los seis hooks de un
  `rayd` local en el orden de la plataforma (`ready` con reintento en 503 →
  `validate` → `run` con body `{"microvmId","runHookPayload"}` → `suspend` →
  `resume` → `terminate`; los demás sin body) respetando los timeouts
  declarados. `make dev-run` arranca `rayd` + sidecar en WSL2.

CI (`.github/workflows/ci.yml`, `ubuntu-24.04` x86, **sin runner ARM, sin QEMU,
sin Docker**): `buf lint`, `buf breaking`, `cargo fmt --check`, `cargo clippy
--workspace --all-targets -- -D warnings`, `cargo test --workspace`,
`uv run pytest tests/unit`, `uv run ruff check`; job de build con
`mlugg/setup-zig@v2` + `cargo install --locked cargo-zigbuild@0.23.4` +
`cargo zigbuild` + `make image-zip`, subiendo `rayd` e `image.zip` como
artefactos; `Swatinem/rust-cache@v2`. `.github/workflows/e2e.yml` sólo
`workflow_dispatch` + nightly, con rol OIDC, `make image-publish` y `pytest -m
e2e`; pre-flight que falla si hay > 10 MicroVMs de la imagen de test (M7:
sin `make image-publish`, ver `m7-supply-chain` D8: el workflow corre contra
la última versión ACTIVE publicada por el mantenedor y nunca publica una
imagen).

`rayd` en M1 implementa **sólo** `HealthService` y los seis hooks: `/ready`
responde 200 de inmediato, `/run` parsea el envelope e instala el hash del
token, los demás ACK 200.

**Aceptación ("hello rayd"):**

1. `make build` produce `target/aarch64-unknown-linux-musl/release/rayd` y
   `file` reporta `statically linked`.
2. `make proto` regenera Python y TS sin errores; `cargo build` regenera Rust;
   `cd clients/python && uv run python -c "import rayito.v1.health_pb2_grpc"`
   pasa.
3. `make image-publish` deja una versión `SUCCESSFUL` + `ACTIVE` con
   `hooks.port` 9000 y los seis hooks `ENABLED`.
4. `clients/python/tests/e2e/test_m1_hello.py`:
   `run_microvm(imageIdentifier=<ARN>, maximumDurationInSeconds=900,
   runHookPayload='{"v":1,"token_sha256":…}',
   logging.cloudWatch.logGroup=/rayito/<image-name>, clientToken=uuid4)` →
   `create_microvm_auth_token(expirationInMinutes=60, allowedPorts=[{port:8080}])`
   → `HealthService.Health` sobre `https://<endpoint>:443` con metadata
   `x-aws-proxy-auth` / `x-aws-proxy-port: 8080` / `x-aws-proxy-force-h2: true`
   / `x-access-token` devuelve `agent_ready=true` en < 90 s; la misma llamada
   **sin** `x-access-token` devuelve `UNAUTHENTICATED`; `terminate_microvm`.
   Con esto queda respondida Q17 (trailers + h2c a través del proxy).
5. CI verde (lint + test + build).

**Estado de aceptación (medido 2026-09-15, cuenta de pruebas, us-east-1):**
1–4 en verde contra AWS real; 5 pendiente del primer push (el repo aún no está
en git). `cargo zigbuild` → `rayd` 3 162 040 B, `ELF 64-bit ARM aarch64,
statically linked, stripped`; `cargo test --workspace` 60 tests, clippy
pedantic limpio; `pytest tests/unit` 113 tests. `image-publish` →
`rayito-base` versión `1.0` (`CREATED` + `SUCCESSFUL` + `ACTIVE`, hooks
`port` 9000, seis hooks `ENABLED`) en **113.7 s** desde `create-microvm-image`
(artefacto `rayito/images/rayd-7e23ac22a3a3.zip`, 3 163 061 B;
`snapshotBuild`: memoria **577 499 136 B**, code install 431 906 816 B, disco
23 502 848 B; `chipsetGeneration` 4). `test_m1_hello.py` PASSED (6.9 s):
`run-microvm` → `Health.agent_ready` en **2.09 s y 2.12 s** (dos
ejecuciones; 3.61 s en la sonda de Q17), `ProcessService.List` →
`UNAUTHENTICATED` sin `x-access-token` y `UNIMPLEMENTED` con él, JWE inválido
→ 403 del proxy (`PERMISSION_DENIED`), `connect()` desde otro `Sandbox`,
`terminate-microvm` visible en `get-microvm` a los 0.14 s. Q17 medida: el
unario funciona con y sin `x-aws-proxy-force-h2` (`AWS_API_NOTES.md` §16).
Coste de la pasada: 1 versión de imagen + 3 MicroVMs de < 1 min.

Guardrails de e2e (`clients/python/tests/e2e/conftest.py`, desde M1 y para
siempre):

- `make test-e2e` se niega a correr sin `RAYITO_E2E=1` y `RAYITO_TEMPLATE`.
- Todo `run_microvm` de test pasa `maximumDurationInSeconds=900` (sin
  `idlePolicy`); los tests de pause/resume de M5 pasan 1800 y su propio
  `idlePolicy`.
- La fixture de sandbox llama siempre a `terminate_microvm` en teardown
  (idempotente).
- Sweeper al final de la sesión: pagina `list_microvms(imageIdentifier=<imagen
  de test>, maxResults=50)` y termina todo lo que no esté en
  `TERMINATING|TERMINATED`, a ≤ 10/s.
- Versiones de imagen por hash de artefacto; `image-publish` no-op si existe;
  `image-prune` mantiene 5.

---

## M2 — Procesos, ciclo de vida, red y seguridad estructural

Agente: `ProcessService` completo (`Start`, `Connect(pid, from_seq)`,
`SendInput`, `CloseStdin`, `SendSignal`, `List`), `Health` con `sandbox_id`,
hooks `/run` reales (una vez por arranque). Defaults de seguridad **que viven en
el camino de spawn y no se pueden añadir después** (movidos de M6 aquí):

- Usuario por defecto `user` (uid 1000, creado en el Dockerfile); root sólo si
  `username == "root"` **y** la imagen tiene `RAYITO_ALLOW_ROOT=1`.
- Entorno del hijo construido desde cero: `PATH`, `HOME`, `USER`, `LOGNAME` +
  `envs` de la petición (el último gana). Nada del entorno de `rayd` se hereda.
- `pre_exec`: `setrlimit(RLIMIT_NPROC 512, RLIMIT_NOFILE 4096, RLIMIT_CORE 0)`;
  si la plataforma rechaza subir un hard limit se recorta al heredado (en
  Lambda MicroVMs root no tiene `CAP_SYS_RESOURCE` y `NOFILE` queda en
  1024/1024, medido 2026-09-15).
- Grupos de procesos (`process_group(0)`) y `killpg` en timeout y `SendSignal`.
- Canales de salida acotados por suscriptor (64 eventos × 32 KiB) con
  backpressure; `output_truncated` si el cliente no consume.
- Máximo 256 procesos/PTYs vivos por sandbox.
- `Health` es el único RPC sin `x-access-token`.

Cliente Python: `Sandbox.create()` (`template` nombre o ARN, resuelto una vez a
ARN con `get_microvm_image`; `timeout`; `IdlePolicy(max_idle_seconds=300,
suspended_duration_seconds=None, auto_resume=True)` o `idle=None`; `envs`;
`execution_role_arn=None`; `allowed_ports`; `logging`; `access_token`),
`.connect(sandbox_id, access_token)`, `.kill()`, `.list()` (paginador, filtra
`TERMINATING|TERMINATED` por defecto), `.get_info()`, `.is_running()`,
`.get_host(port) -> HostAccess`, `.commands.run()` en foreground y background,
`.commands.list()`, `.commands.kill()`, `.commands.connect()`,
`.commands.send_stdin()`, `.commands.close_stdin()`. Token buckets, mapeo de
errores y `_limits.py` de `ARCHITECTURE.md` "Plano de control".

Incluye el **refresher de token JWE** (45 min, reacuñado en `connect()`, manejo
del 403 del proxy) y la entrega del token del agente: el SDK genera el secreto,
envía su sha256 en `runHookPayload`, `rayd` lo instala en `/run`.

**Aceptación:**

```python
with Sandbox.create() as sbx:
    assert sbx.commands.run("echo hola").stdout.strip() == "hola"
    assert sbx.commands.run("id -u").stdout.strip() == "1000"
    h = sbx.commands.run("sleep 30", background=True)
    assert any(p.pid == h.pid for p in sbx.commands.list())
    assert sbx.commands.kill(h.pid)
    with pytest.raises(TimeoutException):                 # el servidor lo mata
        sbx.commands.run("sleep 30", timeout=2)            # (SIGTERM, SIGKILL +5 s)
    sbx.commands.run("python3 -m http.server 3000 --bind 0.0.0.0", background=True)
    host = sbx.get_host(3000)
    assert requests.get(host.url, headers=host.headers, timeout=10).status_code == 200
    assert requests.get(host.url, timeout=10).status_code == 403   # sin x-aws-proxy-auth
    assert sbx.get_metrics().cpu_count >= 1                          # Metrics real (procfs)
```

Y `Sandbox.connect(sbx.sandbox_id, access_token=sbx.access_token)` desde otro
proceso ejecuta un comando; sin token → `AuthenticationException`.

**Estado de aceptación (medido 2026-09-15, cuenta de pruebas, us-east-1):**
verde contra AWS real: `clients/python/tests/e2e` (`test_m1_hello.py` +
`test_m2_processes.py`, las 20 aserciones de
`openspec/changes/archive/2026-09-15-m2-processes-lifecycle/design.md`) **2 passed en
71.45 s**. Host: `cargo test --workspace` 120 tests, clippy pedantic y `cargo
fmt --check` limpios; `pytest tests/unit` 201 tests, `ruff check`, `ruff format
--check` y `mypy src` limpios; la suite `cfg(unix)` `m2_process.rs` 25/25 bajo
Docker. `cargo zigbuild` → `rayd` 3 428 088 B, ARM64 estático. `image-publish`:
versión `2.0` (`rayd-3ee429d6a78e.zip`, 123.8 s; memoria 581 709 824 B, code
install 435 101 696 B, disco 23 502 848 B) y, tras corregir la capa de token,
versión **`3.0`** (`rayd-59ab4c9ba9cd.zip`, 3 429 831 B, **113.4 s**; memoria
**579 080 192 B**, code install 433 504 256 B, disco 22 970 368 B;
`chipsetGeneration` 3; hooks `port` 9000, seis hooks `ENABLED`). Medidas de
`test_m2_processes.py` (un MicroVM de 3.0): `run-microvm` → `Health.agent_ready`
**2.07 s** (2.27 s en el de M1); `timeout=2` sobre `sleep 10` →
`TimeoutException` a los **2.10 s** (`duration_ms` 2001 en la VM, SIGTERM,
exit 143); **3 000 000 B** de stdout en **0.75 s** (≈ 4 MB/s, el tope a 2 GB);
**30 comandos secuenciales en 3.12 s** (≈ 104 ms por `echo`, un canal, sin
429); primer `bash -l` de la VM 971 ms y ≈ 8 ms los siguientes (log de `rayd`);
`sleep 35` en foreground con `KeepAlive` a los 30 s atraviesa el proxy; `kill`
→ `signaled` 137; `get_host(3000)` 200 con cabeceras y 403 sin ellas;
`terminate-microvm` → `TERMINATING` a los 0.12 s. Dos hechos de plataforma
nuevos (`AWS_API_NOTES.md` §16 Q29–Q30): la respuesta temprana
`UNAUTHENTICATED` sin leer el body llega como `CANCELLED` a través del proxy
(la primera pasada, imagen 2.0, falló ahí; `rayd` drena el body de las
peticiones rechazadas desde 3.0) y `RLIMIT_NOFILE` no se puede subir de 1024
(root sin `CAP_SYS_RESOURCE`): el test acepta el recorte. Coste de la pasada:
2 versiones de imagen + 4 MicroVMs de < 2 min. Cambio OpenSpec
`m2-processes-lifecycle` archivado.

---

## M3 — Filesystem

Agente: `FilesystemService` completo: `Read` (256 KiB), `Write` multi-fichero
(chunks ≤ 1 MiB, temporal + fsync + rename, padres creados, `chown` al usuario),
`Stat`, `ListDir(depth)`, `MakeDir` (`ALREADY_EXISTS` / `INVALID_ARGUMENT`),
`Move`, `Remove(recursive)`, `WatchDir` con `notify` (`WatchStarted` sólo con el
watch instalado; `NOT_FOUND`/`INVALID_ARGUMENT` como status antes de cualquier
mensaje; sin debounce).

Cliente: `.files.read/write/write_files/list/exists/get_info/remove/rename/
make_dir/watch_dir`. `write_files` manda N ficheros en **un** stream. Deadline
por petición = 60 s + 1 s por MB. `watch_dir` bloquea hasta `WatchStarted` y
devuelve un `WatchHandle` (`get_new_events()`, `stop()`); los iteradores ignoran
`KeepAlive`.

**Aceptación** (`clients/python/tests/e2e/test_m3_filesystem.py`, los 18
bloques de `openspec/changes/m3-filesystem/design.md`):

```python
with Sandbox.create() as sbx:
    payload = os.urandom(8_000_000)
    info = sbx.files.write("/home/user/m3/big.bin", payload)        # EntryInfo, deadline 60 s + 1 s/MB
    assert (info.size, info.owner, info.mode) == (8_000_000, "user", 0o644)
    data = sbx.files.read("/home/user/m3/big.bin", format="bytes")
    assert sha256(data) == sha256(payload)                            # ≤ 10 s por sentido
    entries = sbx.files.write_files([WriteEntry(f".../f{i:02}.txt", b"...") for i in range(50)])
    assert [e.name for e in entries] == [f"f{i:02}.txt" for i in range(50)]   # un solo stream
    assert sbx.files.make_dir("/home/user/m3/dir") is True
    assert sbx.files.make_dir("/home/user/m3/dir") is False
    assert sbx.files.exists("/home/user/m3/missing") is False
    with pytest.raises(AuthenticationException):                      # deny list, proxy_rejected=False
        sbx.files.read("/etc/passwd")
    h = sbx.files.watch_dir("/home/user/m3/watch")                    # bloquea hasta WatchStarted
    sbx.commands.run("echo hola > .../a.txt; echo mas >> .../a.txt; rm .../a.txt")
    # get_new_events(): CREATE, WRITE, REMOVE de "a.txt" en ese orden, sin ".rayito-tmp-*"
    # files.write(...) sobre un directorio observado llega como RENAME (temporal + rename)
```

- Funcional: sha256 igual tras 8 MB de ida y vuelta con el deadline por
  defecto; `write_files` de 50 ficheros en una sola llamada devuelve 50
  `EntryInfo` en orden; `list(depth=2)` ordenado; `make_dir` dos veces →
  `True`, `False`; `exists` de una ruta inexistente → `False`; symlinks
  reportados y nunca seguidos; `/etc`, `/usr`, `/proc` y el binario de `rayd`
  denegados; `..` rechazado; rutas relativas al `HOME`; `mode=0o600`
  respetado; `user="root"` rechazado.
- Benchmark (marker `bench`, nightly): 50 MB escritura + lectura; tiempo total
  ≤ 120 s y MB/s en el log. A 2 GB el endpoint limita a **4 MB/s por sentido**:
  ≥ 12,5 s por trayecto, ≥ 25 s ida y vuelta (o usar la imagen de 4 GB a 8 MB/s).
- `watch_dir` detecta creación, escritura y borrado (y `CHMOD` con
  `include_entry=True`, recursivo con `recursive=True`); un evento producido
  antes de `WatchStarted` no se pierde; `timeout=` termina el handle con
  `TimeoutException`; paridad sync/async.

**Estado de aceptación (medido 2026-09-15, cuenta de pruebas, us-east-1):**
verde contra AWS real: `clients/python/tests/e2e` (`test_m1_hello.py` +
`test_m2_processes.py` + `test_m3_filesystem.py`, los 18 bloques de
`openspec/changes/archive/2026-09-15-m3-filesystem/design.md`) **3 passed en
99.95 s**, y `test_m3_bench.py` (`-m bench`) **1 passed en 90.40 s**. Host:
`cargo test --workspace` 186 tests (23 + 3 + 12 `rayd`, 148 `rayd-core`;
suites `cfg(unix)` compiladas fuera), clippy pedantic y `cargo fmt --check`
limpios; `pytest tests/unit` 261 tests, `ruff check`, `ruff format --check` y
`mypy src` limpios. `cargo zigbuild` → `rayd` **3 868 664 B** (M2: 3 429 831 B),
ARM64 estático (`e_machine` 183, sin `PT_INTERP`), reproducible: el mismo
hash tras rebuild. `image-publish`: versión **`4.0`** (`rayd-6c2ce20c53bf.zip`,
3 870 613 B, `update-microvm-image` → `UPDATED` + `SUCCESSFUL` + `ACTIVE` en
**123.9 s**; memoria **572 235 776 B**, code install 436 166 656 B, disco
23 502 848 B; `chipsetGeneration` 3; hooks `port` 9000, seis hooks
`ENABLED`); la versión `5.0` (experimento de ventana HTTP/2 en tonic, Q32)
quedó `INACTIVE`. Medidas de `test_m3_filesystem.py` (un MicroVM de 4.0,
cliente a ≈ 93 ms de RTT): `run-microvm` → `Health.agent_ready` **2.36 s**
(1.98 s y 1.97 s en los de M1 y M2); `write` de 8 000 000 B **12.37 s (0.65
MB/s**, `duration_ms` 12 280 en el agente: la subida HTTP/2 va a una ventana
de 64 KiB por RTT, `AWS_API_NOTES.md` §16 Q32, por eso el presupuesto de
subida del test es 20 s); `read` de 8 000 000 B **1.19 s (6.71 MB/s)**, 31
chunks ≤ 256 KiB; `write_files` × 50 en un stream **0.11 s** (19 ms en el
agente); `list(depth=2)` de 53 entradas **0.09 s**; `watch_dir` →
`WatchStarted` **0.38 s**; `echo > a.txt; >> a.txt; rm a.txt` → `CREATE`,
`WRITE`, `REMOVE` recibidos en **0.91 s**; `files.write` sobre el directorio
observado llega como `RENAME` y `chmod 600` como `CHMOD` con `entry.mode`
0o600; `timeout=2` → `TimeoutException` en el handle; watch recursivo ve
`sub` y `sub/n.txt`; 30 `exists` secuenciales **2.84 s (p95 0.096 s)**, sin
429; `/etc/passwd`, `/usr/local/bin/rayd`, `/proc` y `/root` →
`AuthenticationException(proxy_rejected=False)`; `..` → `InvalidArgument`;
`user="root"` rechazado; `stat -c %U:%G` = `user:user`; async parity;
`terminate-microvm` → `TERMINATING` a los 0.76 s. Bench: `write` de 50 MB
**74.01 s (0.68 MB/s)**, `read` de 50 MB **11.71 s (4.27 MB/s)**, total
**85.72 s** (≤ 120 s), sha256 igual. Hecho de plataforma nuevo
(`AWS_API_NOTES.md` §16 Q33): el drenaje de D6 entrega `PERMISSION_DENIED`
cuando el resto del body cabe en 1 MiB / 2 s (1 B: 6/6), pero un `Write`
denegado de 4 MB sigue llegando como `RST_STREAM(CANCEL)` →
`SandboxException` (6/6): mitigación pendiente para M6 (drenar hasta el
`grpc-timeout` del cliente o pre-validar la ruta en el SDK). Coste de la
pasada: 0 versiones nuevas (4.0 reutilizada por hash) + 5 MicroVMs de < 2 min.
Cambio OpenSpec `m3-filesystem` archivado.

---

## M4 — Ejecución de código

El hito con más riesgo. Empezar por el sidecar, no por el agente.

- `kernel-sidecar` (Python 3.12, `jupyter_client` `AsyncKernelManager` con
  transporte `ipc`, un kernel por contexto como uid 1000, un `asyncio.Lock` por
  contexto), protocolo JSON lines por stdio definido por `rayd`. Pins:
  `ipykernel==6.31.0`, `jupyter_client==8.10.0`, `ipython==9.15.0`,
  `pyzmq==27.2.0`, `matplotlib==3.10.9`, `pandas==2.2.3`, `numpy==2.3.5`.
  `ipython_kernel_config.py` con `NoColor` y `max_seq_length = 0`; formatters de
  arranque y `e2b_charts` vendorizado (MIT) para `chart` y `data`.
- `CodeService` en el agente como proxy: `Execute` con `ExecutionStarted`,
  `OutputChunk`, `ExecutionResult`, `ExecutionError` (nunca terminal),
  `ExecutionEnd`, `KeepAlive` cada 5 s; `timeout_ms` impuesto por el agente
  (interrupt → `RestartContext` a los 5 s + `ExecutionTimeout`); cancelar el
  stream interrumpe; `Reattach` → `UNIMPLEMENTED`.
- Warm-up (`import pandas, numpy, matplotlib.pyplot`) **antes** de que `/ready`
  devuelva 200 (503 inmediato hasta entonces, escape a los 300 s), para que el
  snapshot lo capture. `/validate` ejecuta una celda real con pandas +
  matplotlib. En `/run` y `/resume`: reseed de `random`/`numpy.random` y
  rotación de la clave HMAC (reinicio del kernel precalentado) para no compartir
  estado entre sandboxes. Medir aquí el coste de la precarga en tamaño de
  snapshot y latencia de arranque (`snapshotBuild`).
- Cliente: `run_code()`, `create_code_context()`, `list_code_contexts()`,
  `remove_code_context()`, `restart_code_context()`; `Execution`, `Result` con
  todos los mime types, `Logs`, `ExecutionError`; `Health.kernel_ready` entra en
  el criterio de readiness de `create()`.

**Aceptación:**

```python
sbx.run_code("x = 42")
assert sbx.run_code("x").text == "42"                            # execute_result
assert "42" in "".join(sbx.run_code("print(x)").logs.stdout)      # stream, no .text
r = sbx.run_code("import matplotlib.pyplot as plt; plt.plot([1,2,3]); plt.show()")
assert r.results[0].png is not None                               # resultado enriquecido
assert r.results[0].chart is not None                             # e2b_charts
e = sbx.run_code("1/0")
assert e.error.name == "ZeroDivisionError"                        # errores estructurados
t = sbx.run_code("import time; time.sleep(10)", timeout=2)
assert t.error.name == "ExecutionTimeout"                         # timeout de servidor
assert sbx.run_code("x").text == "42"                             # interrumpido, no reiniciado
assert sbx.run_code("import time; time.sleep(12); 'done'", timeout=60).text == "'done'"  # KeepAlive cada 5 s
ctx = sbx.create_code_context()                                   # kernel nuevo, aislado
assert sbx.run_code("x", context=ctx).error.name == "NameError"
assert sbx.run_code("import os; os._exit(3)").error.name == "KernelDied"
assert sbx.run_code("1+1").text == "2"                            # el sidecar lo reinicia
```

Y dos sandboxes de la misma imagen devuelven secuencias distintas para
`random.random()` y `numpy.random.default_rng()` tras `/run`. Los 19 bloques
completos: `openspec/changes/archive/2026-09-16-m4-code-execution/design.md`
"Acceptance test list".

**Estado de aceptación (medido 2026-09-15, cuenta de pruebas, us-east-1):**
verde contra AWS real: `clients/python/tests/e2e` (`test_m1_hello.py` +
`test_m2_processes.py` + `test_m3_filesystem.py` + `test_m4_code.py`, los 19
bloques del diseño) **4 passed en 157.15 s** a la primera pasada. Host:
`cargo test --workspace` 237 tests en Windows (33 + 4 + 12 `rayd`, 188
`rayd-core`; suites `cfg(unix)` compiladas fuera) y **320** bajo
`rust:1.98-slim-bookworm` como uid 1000 con `python3` (42 + 4 + 12 `rayd`,
`m2_process` 25, `m3_filesystem` 30, `m4_code` **19/19** + 1 ignorado, 188
`rayd-core`), clippy pedantic y `cargo fmt --check` limpios; `kernel-sidecar` 56 tests de
host (10 de kernel real sólo Linux), `ruff`, `mypy` limpios; `pytest
tests/unit` 324 tests, `ruff check`, `ruff format --check` y `mypy src`
limpios. `cargo zigbuild` → `rayd` **4 329 272 B** (M3: 3 868 664 B), ARM64
estático (`e_machine` 183, sin `PT_INTERP`). `image-publish`: las versiones
6.0 y 7.0 del implementador (ver `tasks.md`; 7.0 midió 49 s de `kernel_ready`
antes de que `/validate` reiniciara el kernel para el prefetch, después
7.98/7.67 s) y la versión de aceptación **`8.0`** con las correcciones de
revisión R1–R4 (`rayd-084c93f69ccf.zip`, 4 425 299 B; `update-microvm-image`
→ `UPDATED` + `SUCCESSFUL` + `ACTIVE` en **195.9 s**; memoria **925 466 624 B**
(4.0: 572 235 776 B, +353 MB por el stack científico calentado), code install
1 289 846 784 B (4.0: 436 166 656 B), disco 36 929 536 B; `chipsetGeneration`
3; hooks `port` 9000, seis hooks `ENABLED`; VM de build `warmup_ms` **9 682**,
`/ready` 200 tras dos 503 sin válvula de escape; VMs de `/validate`
`restart_ms` 12 910 / 13 486 y celda `validated` con 2 resultados). Bajo el
tope de 1,2 GB del diseño D14: lista de warm-up intacta. Medidas de
`test_m4_code.py` y de las fixtures (seis MicroVMs de 8.0, cliente a ≈ 93 ms
de RTT): `run-microvm` → `Health.agent_ready and kernel_ready`
(`kernel_ready_s`, incluye la rotación del kernel en `/run`) **3.37 / 6.23 /
6.21 / 6.35 / 6.17 / 8.05 s** (p50 **6.22 s**: bajo la regla de 8 s de D11,
sin ADR; presupuesto asertado 15 s; el sondeo del SDK va de 0,25 s a 2 s, de
ahí el racimo en ≈ 6,2 s) con `restart_ms` del kernel por defecto **2 030 /
2 284 / 3 329 / 3 053 / 3 454 / 4 077** en el log de `rayd`; primera celda
`x = 42` **0.11 s**; `x` → `42`; `print(x)` en `logs.stdout`; matplotlib
(`plot` + `show`, PNG + `LineChart` de `e2b_charts`) **0.32 s**; pandas
(`DataFrame`, `html` + `data` parseado) **0.11 s**; `1/0` →
`ZeroDivisionError` sin ANSI; `timeout=2` sobre `time.sleep(10)` →
`ExecutionTimeout` a los **2.11 s** con `x` intacto (`rayd`: "execution timed
out; interrupting" a los 2 001 ms); celda silenciosa de 12 s **12.11 s** con
**2 `KeepAlive`** (a los 5.10 y 10.10 s, `seq` 0) y `'done'`;
`create_code_context` **2.09 s** (`warmup_ms` 1 882 en el sidecar);
`restart_code_context` **2.10 s** (`restart_ms` 1 997); 20 × `1+1` en
**2.10 s (p95 0.11 s)**; `os._exit(3)` → `KernelDied` y `1+1` de nuevo en
**1.88 s**; `ps -o user= -C python3` = `user`; async parity; dos sandboxes
con `random.random()` y `numpy.random.default_rng()` distintos;
`terminate-microvm` → `TERMINATING` a los 0.78 s. Hechos de plataforma nuevos
(`AWS_API_NOTES.md` §16 Q34–Q36): tamaño y tiempo de build con el stack
científico, el coste de la rotación del kernel tras el restore (y la
necesidad de reiniciarlo también en `/validate` para que Lambda prefetchee
esas páginas) y el server-stream `Execute` a través del proxy (keepalives de
5 s, chunks de 64 KiB: 3 000 001 B de stdout en 46 chunks y 0.77 s, 3.88
MB/s). Coste de la pasada de aceptación: 1 versión de imagen + 6 MicroVMs
de < 3 min. Cambio OpenSpec `m4-code-execution` archivado.

---

## M5 — PTY, suspend/resume y reconexión

- `PtyService` con `nix::pty::openpty` + `tokio::process::Command` (ADR-005):
  `Create` (server-stream, primer mensaje `PtyStarted`), `Connect(pid)`,
  `SendInput`, `Resize`, `Kill`; shell de login del usuario con `-i -l`,
  `TERM=xterm-256color`, `LANG=LC_ALL=C.UTF-8`.
- Hooks `/suspend` y `/resume` **de verdad**, con los checklists de
  `ARCHITECTURE.md`: cierre limpio de los streams de cliente con
  `StreamError{code:"suspending"}` sin matar procesos, kernels ni el enlace con
  el sidecar; `resume_generation`, `clock_offset_ms`, invalidación de
  credenciales, `kernel_info` con timeout 5 s por kernel, reseed,
  `kernel_state_lost`.
- `Code.Reattach` implementado (ring de 4 MiB por ejecución).
- Cliente: `.pty.create/send_input/resize/kill/connect` (`PtyHandle` = mismo
  handle que comandos), `.pause(wait)`, `.resume(wait)`, y el **contrato de
  reconexión**: ante `UNAVAILABLE`/RST/EOF/502 sondear `Health` con backoff con
  jitter durante `resumeTimeoutInSeconds + 30 s`; si `resume_generation`
  cambió, re-suscribir `Process.Connect(from_seq)`, `Pty.Connect`, `WatchDir` y
  `Code.Reattach` antes de propagar el error. El refresher de JWE sigue
  corriendo durante la pausa.

**Aceptación:** el test completo de la sección 6 de `SPEC.md`
(`maximumDurationInSeconds=1800`, `idlePolicy` propio). En particular el paso 5:
una variable definida antes de `pause()` sigue viva después de `resume()`; un
`sleep 4000` en background y una PTY abiertos antes siguen vivos después;
`commands.connect(pid)` devuelve el exit code correcto; `Health.resume_generation`
ha incrementado; un `WatchHandle` re-suscrito sigue recibiendo eventos.
Además: `pause()` sobre un sandbox ya suspendido devuelve `False`
(`ConflictException`), y `SuspendMicrovm` a 2 TPS se respeta con el token
bucket.

**Imagen publicada por el implementador (2026-09-16, cuenta de pruebas,
us-east-1):** `rayito-base` versión **`9.0`** (`rayd-e7fce213dd14.zip`,
4 632 912 B; `rayd` ARM64 estático **4 536 248 B**, M4: 4 329 272 B;
`update-microvm-image` → `UPDATED` + `SUCCESSFUL` + `ACTIVE` en **195.4 s**,
8.0: 195.9 s; memoria **935 469 056 B** (8.0: 925 466 624), code install
1 290 379 264 B (8.0: 1 289 846 784), disco 35 864 576 B (8.0: 36 929 536);
`chipsetGeneration` 4). Sonda en un MicroVM de 9.0 con el SDK 0.0.5: devpts
montado en el guest (`grep -c devpts /proc/mounts` → `1`, `pty.openpty()` →
`/dev/pts/0`, Q37), `pty.create` 0.35 s, `echo hola` 0.14 s, `pause()` →
`SUSPENDED` 0.88 s, `resume()` → `Health` con `resume_generation` 1 en 0.40 s,
la PTY re-suscrita responde 0.18 s después del resume, `kernel_state_lost`
false, `run_code` vivo. Host: 287 tests en Windows y **390** bajo
`rust:1.98-slim-bookworm` como uid 1000 (`m5_pty` 13, `m5_suspend_resume` 7),
clippy pedantic y `cargo fmt --check` limpios en ambos; bucle local 6.3
(`hooks-sim.py` con el drill gRPC y el drill del SDK) verde. El "Estado de
aceptación" con los 15 bloques de `SPEC.md` §6 y el test de auto-resume lo
escribe la aceptación (tarea 7.1).

**Estado de aceptación (medido 2026-09-16, cuenta de pruebas, us-east-1):**
verde contra AWS real: `clients/python/tests/e2e` completo (`test_m1_hello.py`
+ `test_m2_processes.py` + `test_m3_filesystem.py` + `test_m4_code.py` +
`test_m5_pty_suspend_resume.py`, los 15 bloques de
`openspec/changes/archive/2026-09-16-m5-pty-suspend-resume/design.md` y el
test de auto-resume) **6 passed en 333.18 s** en una sola sesión (siete
MicroVMs), tras dos pasadas rojas que sólo corrigieron tests y una regla del
SDK (abajo). Host: `cargo test --workspace` 288 tests en Windows (52 + 4 + 12
`rayd`, 220 `rayd-core`) y **393** bajo `rust:1.98-slim-bookworm` como uid
1000 (61 + 4 `rayd`, 12 `m1_hello`, 25 `m2_process`, 30 `m3_filesystem`, 19 +
1 ignorado `m4_code`, **15 `m5_pty`**, **7 `m5_suspend_resume`**, 220
`rayd-core`), `cargo fmt --check` y clippy pedantic limpios; `pytest
tests/unit` **429** tests, `ruff check`, `ruff format --check` y `mypy src`
limpios. `cargo zigbuild` → `rayd` **4 533 432 B** (9.0: 4 536 248 B; M4:
4 329 272 B), ARM64 estático. `image-publish`: versión **`10.0`**
(`rayd-fc0df3c41a28.zip`, 4 630 096 B; `update-microvm-image` → `UPDATED` +
`SUCCESSFUL` + `ACTIVE` en **215.8 s**; memoria **919 146 496 B** (9.0:
935 469 056; 8.0: 925 466 624), code install 1 290 379 264 B (= 9.0), disco
37 462 016 B (9.0: 35 864 576); `chipsetGeneration` 3; hooks `port` 9000,
seis hooks `ENABLED`), con las correcciones de revisión de `rayd` posteriores
a 9.0. Medidas de `test_pty_suspend_resume` (MicroVMs de 10.0, cliente a
≈ 93 ms de RTT; dos pasadas verdes: 3 y 4): `run-microvm` →
`Health.agent_ready and kernel_ready` **3.96 / 9.46 s** (los siete MicroVMs de
la sesión final: 8.48 / 6.01 / 9.12 / 8.43 / 3.97 / 9.46 / 8.35 s, `restart_ms`
2 183–2 487); **Q37** devpts en el guest (`/dev/ptmx` + `devpts`, `openpty`
→ `/dev/pts/0`); `pty.create` → `started` **0.38 s**, `echo hola` **0.47 /
0.09 s** (0.10 s en la pasada 2), `stty size` `30 100` → `40 120` tras
`resize`, `id -u; tty` → `1000` y `/dev/pts/0`, `$TERM $LANG` →
`xterm-256color C.UTF-8`, `commands.send_stdin` sobre la PTY →
`InvalidArgumentException` (`FAILED_PRECONDITION`), `kind == "pty"` en
`commands.list()`; `pause()` → `SUSPENDED` **1.49 s** (**pause_s**; 1.37 s en
la pasada 2) y el segundo `pause()` → `False`; 30 s después la VM sigue
`SUSPENDED` con el handle en background, la PTY y el watch abiertos (Q38:
`rayd` cerró **3 streams** en `suspend_ms` **10 / 11 ms**); `resume()` →
`Health` con `resume_generation` +1 en **0.37 / 0.73 s** (**resume_s**),
`kernel_state_lost` false, **`clock_offset_ms` 0** (Q41; `rayd`: `probe_ms`
**1 / 3**, `suspended_ms` 31 887 / 32 074, `kernels_alive` 1); primera celda
`x` → `42` **0.10 s** (**kernel_alive_s**) y `len(df)` → `2`;
`commands.connect(sleep 4000)` **0.09 s** (**resubscribe_s**), `kill` →
137; `sleep 60` con `timeout=25` pausado 32 s → `TimeoutException` **22.56 /
22.33 s** después del resume (**rearm_timeout_s**; `duration_ms` **25 000 /
25 001** de reloj de ejecución en `rayd`); `Pty.Connect(from_seq)` + `echo
resumed-42` **0.19 s** (**pty_reattach_s**), el handle original reenganchado
(`reconnects` 1), `pty.kill` → 137 y después `False`; handle vivo: primer
tick tras el resume **0.00 s** (`reconnects` 1, sin excepción); watch
reemitido: evento **0.20 / 0.00 s** (`reconnects` 1); `run_code` de 25 s
pausado a los 3 s → `'slept'` **15.96 / 15.95 s** después del `resume()` por
**`Reattach(from_seq=2)`** (un único registro `rayito.code`; `rayd`:
`streams_closed` 1, `subscribers` 1 desenganchado, `execution reattached …
replayed 0`, Q40) con la generación en +2 y `echo ok` después; paridad async
(`AsyncSandbox.connect`, `pty.create` 80×24 + `echo async-pty`, `pause()`
True, `resume()` **0.37 s**, `x` → `42`, generación +3);
`terminate-microvm` → `TERMINATING` a los 0.74 / 0.12 s, `TERMINATED` y fuera
de `list()`. `test_auto_resume` (`IdlePolicy(60, 600, auto_resume=True)`):
idle → `SUSPENDED` **66.71 / 61.65 s**, primer `commands.run("echo back")`
tras la suspensión **0.67 s** (**auto_resume_s**; el proxy retiene el stream
`Start`, sin reconexión del SDK, Q40), `y` → `7`, generación 1. Tres hechos
nuevos de plataforma (`AWS_API_NOTES.md` §5 y §16 Q37–Q41): (1)
`suspend-microvm` sobre un VM ya `SUSPENDED` responde **200**, no
`ConflictException` (medido tres veces; `resume-microvm` sobre `RUNNING`
también 200 sin subir la generación): `pause()` lee ahora `get-microvm`
antes de llamar y devuelve `False` sobre `SUSPENDING|SUSPENDED` (SDK 0.0.5,
tests unitarios ajustados); (2) `bash` 5.2.15 de AL2023 con `-i -l` activa
el bracketed paste de readline y cada línea llega como `echo
hola\r\n\x1b[?2004l\rhola\r\n`: el e2e casa `[\r\n]hola\r\n` y lee la PTY
con plazo (la primera pasada quedó bloqueada esperando `\r\nhola\r\n`);
(3) un fichero creado en un subdirectorio nuevo antes de que `notify`
instale su watch recursivo sólo aporta `WRITE` (límite de inotify, 1 de cada
2 a 1 vCPU): `test_m3_filesystem.py` espera ahora el `CREATE` de `sub` antes
de crear el fichero. Pendiente para M6: el `reseed` que `/resume` encola tras
una celda larga en curso agota `OpTimeouts` (15 s) y cuenta un timeout hacia
el kill-switch del sidecar aunque el sidecar lo ejecute al terminar la celda
(`reseeded 1`); y la validación de origen de los hooks (T2). Coste de la
aceptación: 1 versión de imagen (10.0) + 26 MicroVMs de < 6 min (cuatro
pasadas e2e más el test de auto-resume suelto, tres sondas; los 26 en
`TERMINATED` al cerrar) + 14 ciclos suspend/resume. Cambio OpenSpec
`m5-pty-suspend-resume` archivado.

---

## M6 — Endurecimiento

Sólo después de que M5 esté verde.

- Aislamiento adicional: slices cgroup2 por proceso, bloqueo de IMDS
  (`169.254.169.254`) para uid 1000 (requiere
  `additionalOsCapabilities: ["ALL"]`), egress allowlist vía conector VPC +
  security group restrictivo + proxy, defensa frente a hooks forjados,
  límites de CPU/salida/disco, poda de versiones de imagen.
  - **Track A (`crates/`, `kernel-sidecar/`, `image/`, `infra/`, `scripts/`,
    cambio OpenSpec `m6-hardening`): implementado y medido contra AWS real
    el 2026-09-16 y aceptado ese mismo día (bloque "Estado de aceptación"
    al final de M6).** Imágenes: `rayito-base`
    **15.0** (memoria 919 146 496 B, code 1 304 760 320 B, disco
    36 933 632 B, build 195 s; 11.0–14.0 por el camino, 8.0 y 10.0 podadas)
    y `rayito-base-caps` **5.0** (`additionalOsCapabilities: ["ALL"]`,
    933 363 712 B; 1.0–4.0 podadas, `rayito-diag-caps` borrada). e2e sobre
    15.0 + caps 5.0: M1–M5 verdes (regresión) y
    `tests/e2e/test_m6_hardening.py` **12 passed, 1 skipped**
    (`test_egress_allowlist`, sin conector desplegable: la puerta exige una
    VPC propia o prestada y la única VPC es de otra carga de trabajo, Q46)
    en 9 min: `/run` forjado → `already_ran` 0,30 s;
    `/suspend` forjado sin checkpoint → primer tick tras el corte **19,6 /
    24,4 s** (`reconnects == 1`, fichero, PTY, proceso y kernel intactos,
    generación intacta); `/resume` forjado → resume real; `hook_anomalies`
    2 y un aviso del SDK por generación; `cpu_time_limit=2` → bucle ocupado muerto con
    exit 152 (`SIGXCPU`) a los 2,18 s, `sleep 3` intacto; dos streams de
    20 MB entregados en 9,6 s y `Connect(from_seq=1)` → `NotFoundException`;
    escritura de 1 MiB con 6 800 MiB libres (reserva 256 MiB);
    `imds_blocked=True` 0,10 s después de `kernel_ready` (11,6 s con rol y
    logs) y `PUT /latest/api/token` como uid 1000 exit 1 en 0,39 s, mientras
    la imagen por defecto responde `imds ok` (fail-open); tres pausas
    durante celdas de 25 s → `resume_s` 0,37–0,38, kernel vivo sin
    reinicio; escalera de RSS (Q50) igual que en 10.0 (scipy + sklearn
    73,3 MB < 100 ⇒ warm-up intacto). **Mecanismo de IMDS ratificado tras
    medir (Q48)**: sin `xt_owner` en el guest, `rayd` usa una ruta de
    política (`ip rule uidrange 1000-65535 lookup 100` + blackhole; el
    agente de la plataforma usa uids 991-994 y su propio canal a IMDS, así
    que "todo uid ≠ 0" dejó el build en `UPDATE_FAILED`). cgroup2: límite
    de plataforma en la imagen por defecto, montado en la variante (Q47).
    Poda (Q49): 6 borrados serializados, `ACTIVE` se borra directo, 5,4 s de
    espera por versión, 0 `ConflictException`. Gates: Windows y Linux
    (Docker uid 1000) `cargo test --workspace` verdes (m6_hooks 7, m6_limits
    5, m6_imds 3; el de NET_ADMIN como root), clippy pedantic limpio en host
    y aarch64-musl, `buf breaking` limpio, sidecar 59 tests + ruff/mypy, SDK
    610 unitarios + ruff/mypy, scripts 11 tests, `cfn-lint` limpio,
    `openspec validate m6-hardening --strict` OK. Coste del track ≈ 9
    versiones de imagen publicadas (≈ $0,04/semana cada una mientras vivan;
    quedan 6) + ≈ 35 MicroVMs de corta vida (< $1 de compute).
    **Correcciones de la revisión (2026-09-16, `rayito-base` 16.0, memoria
    931 258 368 B, code 1 299 968 000 B, build 216 s):** (1) se retira el
    limitador de transiciones de `rayd` — un par forjado `/suspend` +
    `/resume` seguido del `/suspend` real de AWS dentro de los 2 s dejaba
    el checkpoint real sin checklist y el `/resume` real como repetición
    (peor que antes de M6); ahora ninguna transición se rechaza y
    `test_forged_hooks` mide el par forjado + `/suspend` a +1 s →
    `changed` / `changed`, `hook_anomalies` 2, `pause()`/`resume()` real
    0,44 s (`m6_hooks.rs` 7/7 en Docker con el caso nuevo); (2) el aviso
    del SDK por IMDS abierto sólo se evalúa pasados 10 s de `uptime`
    desde el `Health` de readiness (la verificación de `rayd` arranca en
    `/run` con presupuesto de 10 s: en la imagen con capabilities
    `imds_blocked` pasa a `True` 0,10 s *después* de `kernel_ready`, así
    que el aviso en readiness era un falso positivo); (3) `WatchDir` y
    `Reattach` reintentan un `UNAVAILABLE suspending` con el mismo backoff
    que `Connect`/`Pty.Connect` (`GateRetry` compartido, sync y async, 4
    unitarios nuevos: 616 en total); (4) la puerta de despliegue del egress
    (D11, spec) incorpora el criterio real — VPC propia o prestada — y
    deja claro que el deny-all no necesita NAT; T8 queda "validada, no
    medida".
- Cliente TypeScript con paridad de API (Connect-ES v2, mismo JSON de límites).
  - **Track B (`clients/typescript`, paquete `rayito` 0.0.5 en npm, cambio
    OpenSpec `m6-typescript-sdk`): implementado y aceptado contra AWS real el
    2026-09-16.** Gencode `buf.build/bufbuild/es:v2.15.0` commiteado en
    `src/gen`; transporte `createGrpcTransport` (HTTP/2, dos sesiones por
    sandbox) con el interceptor de las cuatro cabeceras del proxy; plano de
    control `@aws-sdk/client-lambda-microvms` 3.1133.0 detrás del puerto
    `ControlPlane` con los mismos buckets; `limits.json` en la raíz rendido por
    `scripts/gen_limits.py` a `_limits.py` y `limits.ts` con test de deriva en
    los dos SDKs y `--check` en `make lint`; superficie completa
    (`Sandbox.create/connect/list/kill/getInfo/pause/resume/isRunning/getHealth/
    getHost/getMetrics`, `commands`, `files`, `pty`, `runCode` + contextos,
    `await using`) y el contrato de reconexión M5 transpuesto a un solo event
    loop. Unit: **282 passed** en 20,7 s (20 ficheros, vitest) sobre un `rayd`
    falso servido por `connectNodeAdapter` + `FakeControlPlane`; `pnpm lint`
    (Biome), `pnpm typecheck` (tsc strict), `pnpm build` (tsdown:
    `dist/index.mjs` 167,6 kB, `dist/index.cjs` 171,6 kB, `.d.mts`/`.d.cts`
    102,2 kB) verdes; `pnpm pack` → `package/LICENSE`, `package/README.md`,
    `package/package.json`, `package/dist/index.{mjs,cjs,d.mts,d.cts}` +
    sourcemaps. Aceptación: `tests/e2e/m6.e2e.test.ts` **2 passed en 193 s**
    (tercera pasada; las dos anteriores fallaron por aserciones del propio
    test, no del SDK) sobre `rayito-base` 10.0 con execution role y logs:
    `kernel_ready_s` 8,18 (main) / 10,58 (idle), `burst_s` 2,82 (30 `echo`),
    `file_1mb_s` 3,17, `watch_event_s` 0,21, `pty_create_s` 0,10,
    `pty_echo_s` 0,10, `pause_s` 1,20, `resume_s` 0,37 (0,74 en la segunda
    pasada), `clockOffsetMs` 0 / 2, `kernel_alive_s` 0,09, `resubscribe_s`
    0,09, `rearm_timeout_s` 24,52 (comando de 25 s pausado 30 s),
    `pty_reattach_s` 0,18, `watch_reissue_s` 0,21, `reattach_s` 10,92
    (`runCode` de 20 s pausado a los 3 s), `Sandbox.connect` desde un segundo
    `Sandbox` con `x == 42`, `kill()` → `TERMINATED` visible en 1,24 s y
    ausente de `list()`; `idle_suspend_s` 71,98 y `auto_resume_s` 0,66 con
    `resumeGeneration == 1`. Sin hechos nuevos de plataforma para
    `AWS_API_NOTES.md` §16. Coste: 0 versiones de imagen + 6 MicroVMs de
    < 4 min (tres pasadas) ≈ $0,20; los 6 `TERMINATED` al cerrar.
    **Corrección en la aceptación de M6 (2026-09-16, tarea 8.5, design D4):**
    la pasada de aceptación sobre `rayito-base` 16.0 pasó los dos tests pero
    el proceso murió 30 s después de cada `close()` con un
    `ERR_HTTP2_INVALID_SESSION` no capturado desde el temporizador de PING de
    connect-node 2.2.0 (`abort()` destruye la sesión y quita sus listeners de
    forma síncrona; el `close` de cada stream, en el tick siguiente, vuelve a
    armar el PING ocioso sobre la sesión destruida). Con un stream vivo en
    `close()` y un proceso que siga vivo >= 30 s es un crash del SDK, no del
    test; el fake unitario siempre pasaba `pingIdleConnection: false` y por
    eso nunca lo vio. Arreglo: `DEFAULT_TRANSPORT_SETTINGS.pingIdleConnection
    = false` (la vida de una sesión callada la sigue comprobando
    `requiresVerify()` antes de la primera petición tras 30 s) + test de
    regresión con temporizadores falsos (rojo con el valor anterior, verde
    con el nuevo); **292 unitarios**, lint, typecheck, build y pack verdes.
    Re-pasada limpia (exit 0, sin errores no capturados) sobre 16.0:
    `kernel_ready_s` 6,04 / 10,58, `burst_s` 3,04, `file_1mb_s` 3,44,
    `watch_event_s` 0,20, `pty_create_s` 0,10, `pty_echo_s` 0,09, `pause_s`
    1,25, `resume_s` 0,46, `kernel_alive_s` 0,10, `resubscribe_s` 0,09,
    `rearm_timeout_s` 24,42, `pty_reattach_s` 0,19, `watch_reissue_s` 0,21,
    `reattach_s` 10,92, `idle_suspend_s` 77,04, `auto_resume_s` 1,29 (2
    passed en 196 s). Una pasada intermedia falló porque el sweeper de fin
    de sesión del conftest Python, corrido a la vez contra la misma imagen,
    terminó el MicroVM del test TypeScript (`/terminate` a los 23 s, error
    del operador, no del SDK): nunca correr dos sesiones e2e sobre el mismo
    template a la vez.
- ~~`get_metrics()` sobre `HealthService.Metrics`~~: adelantado a M2 (procfs).
- Shim de compatibilidad E2B (`rayito.e2b`: re-export con los nombres de E2B y
  `unimplemented` explícito donde no hay equivalente).
  - **Track C (metadatos + `rayito.e2b` + release 0.1.0): implementado y
    aceptado contra AWS real el 2026-09-16** (cambio OpenSpec
    `m6-e2b-compat`). `HealthResponse.metadata` (campo 11) echo del
    `runHookPayload`; SDK 0.1.0 con `create(metadata=)`, `sbx.metadata`,
    `get_info` en sus dos variantes y `list(metadata=)` O(n) sólo sobre
    `RUNNING`; `rayito.e2b` (`Sandbox`/`AsyncSandbox`, 50 nombres de E2B,
    `UnimplementedError`, `RayitoCompatWarning`); `mypy src tests` en el
    lint; wheel comprobada + `release.yml` (Trusted Publishing, sin
    publicar) + `docs/site` (mkdocs-material `--strict`). Unit: **595
    passed** (429 → 595) sobre el `rayd` falso; `cargo test --workspace` 335
    passed; `rayd` ARM64 estático **4 679 032 B** (M5: 4 536 248 B).
    Aceptación: `tests/e2e/test_m6_e2b_compat.py` **2 passed en 27.65 s**
    (dos pasadas) sobre la imagen **`rayito-base-e2b` 1.0** (mismo árbol que
    la `rayito-base` de M6, publicada aparte para no tocar la numeración del
    track de endurecimiento; build 195.5 s, memoria 930 734 080 B): cookbook
    E2B completo a través del shim, `kernel_ready_s` 11.29 / 10.54 s (con
    execution role y logs; 6.09 / 6.16 s sin logs), `get_info_metadata_s`
    0.66 / 0.61 s, `list_metadata_s` 0.77 / 0.76 s con `list_metadata_n` = 1,
    `beta_pause()` 1.01 / 1.04 s, `Sandbox.connect(id)` tras la pausa 0.79 /
    0.78 s con el kernel (`x == 40`) y los metadatos intactos, `kill()` →
    `NotFoundException` 4.27 / 0.13 s. **Q44** (`AWS_API_NOTES.md` §16): un
    MicroVM lanzado sin `egressNetworkConnectors` sigue saliendo a internet
    (hereda el conector de la versión de imagen), así que
    `allow_internet_access=False` es `UnimplementedError` (design D11).
    Coste: 1 versión de imagen ($0.037/semana) + 6 MicroVMs de < 3 min (uno
    de 12 min por un test colgado en la primera pasada, corregido) ≈ $0.10;
    los 6 `TERMINATED` al cerrar. Pendiente fuera del cambio: registrar el
    Trusted Publisher en PyPI y empujar `python-v0.1.0` (manual).
- ~~Benchmark de cold start en ráfaga concurrente con la imagen real. Decidir
  aquí, con datos, si hace falta un pool de MicroVMs pre-calentados.~~ **Medido
  el 2026-09-16** (`docs/benchmarks/2026-09-cold-start.md`,
  `scripts/bench_cold_start.py`, crudos en `docs/benchmarks/raw/`): con
  `rayito-base` 10.0 (memoria 919 146 496 B) `run-microvm` → `agent_ready` p50
  2,33 / p95 3,02 s y → `kernel_ready` p50 **5,20** / p95 **6,07 s** (20
  secuenciales, sonda de 100 ms); ráfaga de 20 por el SDK p50 5,68 / p95
  **8,57 s** (≈ 3 s de cola del bucket de 5 TPS), 20 `run-microvm` simultáneos
  sin bucket p95 5,69 s y **0 `ThrottlingException`** en 5/10/20; `resume()`
  p50 0,38 / p95 0,40 s, auto-resume 0,67 / 0,68 s, primera celda 0,10 s,
  `kernel_alive` 20/20; `rayito-base-slim` 2.0 (sin warm-up, 693 841 920 B)
  secuencial p50 2,99 / p95 3,41 s y celda pandas + matplotlib 0,79 s frente a
  0,14 s. **Decisión (regla D11 fijada antes de medir, `B20` = 8,57 s ≥ 8 s,
  `R` = 0,40 s < 2 s): pool de suspendidos, ADR-008**, implementación en el
  cambio `m7-suspended-pool`; sin pool de VMs corriendo. Precios de §12
  verificados en Cost Explorer (±0,21 %); ciclo suspend/resume ≈ $0,0049.
  Track D — implementado y medido el 2026-09-16 y **aceptado ese mismo día**
  (cambio OpenSpec `m6-benchmark-pool`; run ≈ $0,35 de uso + dos versiones
  slim ≈ $0,08). Queda explícitamente diferida al 2026-09-17 la línea de
  Cost Explorer del día del bench (GB reales por lanzamiento y por suspend,
  VM-segundos, salto de storage: tareas 5.1 y 5.3–5.5 del cambio; la
  recomprobación de las 21:10 UTC del 2026-09-16 seguía sin el bench,
  `docs/benchmarks/raw/2026-09-cost-explorer-2026-09-16T2115Z.json`); la
  DECISIÓN no depende de ella. `rayito-base-slim` (1.0 y 2.0) se borró tras
  la medida: los números viven en el informe y `make image-publish-slim` la
  reconstruye.
- Opciones a evaluar con datos: consolidar el sidecar en Rust
  (`jupyter-zmq-client`, ADR-002), metadatos por sandbox del lado cliente,
  persistencia de filesystem vía S3/EFS.
- Documentación, ejemplos y reserva de nombres (`rayito` en PyPI, npm,
  crates.io; el sidecar nunca se publica en PyPI).
  - Notas de la release 0.1.0 en `docs/RELEASE_NOTES_0.1.0.md`; PyPI y npm
    quedan como paso manual (Trusted Publisher + tag `python-v0.1.0`).

**Estado de aceptación (medido 2026-09-16, cuenta de pruebas, us-east-1):**
verde contra AWS real sobre **`rayito-base` 16.0** (`rayd-0b6ebe73cb60.zip`,
memoria 931 258 368 B, code 1 299 968 000 B, disco 36 933 632 B) y
**`rayito-base-caps` 6.0** (mismo artefacto con `additionalOsCapabilities:
["ALL"]`, build 215,8 s, memoria 924 938 240 B, code 1 305 825 280 B, disco
28 413 952 B). El árbol se recompiló para la aceptación (`cargo zigbuild` ->
`rayd` **4 698 744 B**, sha256 idéntico al de 16.0) y el zip resultó byte a
byte el de 16.0, así que `publish_image.py` reutilizó la versión en vez de
publicar una 17.0. `clients/python/tests/e2e` completo (M1–M5, `test_m6_e2b_compat`
y `test_m6_hardening`) **14 passed, 1 skipped en 564,73 s** en una sesión
(13 MicroVMs; el skip es `test_egress_allowlist`, sin VPC propia, Q46):
`kernel_ready_s` 1,95–10,60 s; forjados: `/run` -> `already_ran` 0,32 s, primer
tick tras el `/suspend` forjado 25,96 s, par forjado + `/suspend` a +1 s ->
`changed`/`changed`, `pause()`/`resume()` real 0,37 s; `cpu_time_limit=2` ->
exit 152 a los 2,16 s; dos streams de 20 MB en 9,54 s y `Connect(from_seq=1)`
-> `NotFoundException`; 6 800 MiB libres tras 1 MiB; `imds_blocked` 0,10 s
después de readiness y `PUT /latest/api/token` como uid 1000 exit 1 en 0,34 s
(caps 5.0) y **0,44 s sobre caps 6.0** (`test_imds_block` repetido a solas:
1 passed en 23,2 s); tres pausas durante celdas de 25 s -> `resume_s`
0,37–0,40, kernel vivo. `clients/typescript` `pnpm test:e2e` **2 passed en
196 s** sobre 16.0 tras la corrección 8.5 (Track B). Host: `cargo test
--workspace` 331 en Windows (62 + 4 `rayd`, 13 `m1_hello`, 252 `rayd-core`),
clippy pedantic, `cargo fmt --check` y `buf lint` limpios; `pytest tests/unit`
**616**, `scripts/tests` 44, `ruff check`/`format`, `mypy src tests`,
`gen_limits.py --check`, `uvx ruff check scripts` y `mkdocs build --strict`
limpios; TypeScript 292 unitarios + lint + typecheck + build + pack. Poda:
`image_prune.py --keep 2` sobre `rayito-base` (11.0–14.0 borradas, 4
borrados serializados, 0 conflictos) y `--keep 1` sobre `rayito-base-caps`
(5.0 borrada); `rayito-base-e2b` y `rayito-base-slim` borradas enteras
(`delete-microvm-image`). Quedan **`rayito-base` 15.0 + 16.0**,
**`rayito-base-caps` 6.0** y `rayito-m0-probe` 3.0 (M0, ≈ $0,04/semana,
candidata a borrar); `list-microvms`: 266 en total, **0 vivos**. Cost
Explorer (21:10 UTC, usage types `Lambda-MicroVM-*`): 2026-09-14 $0,0075,
2026-09-15 $0,3111, 2026-09-16 **$0,0487 (parcial, `Estimated`, sólo las
primeras horas)**, 2026-09-17 $0; el día de la aceptación (≈ 21 MicroVMs
cortos + 1 versión de imagen + 2 consultas CE) se estima en ≈ $0,25 de uso.
Cambios OpenSpec `m6-hardening`, `m6-typescript-sdk`, `m6-e2b-compat` y
`m6-benchmark-pool` archivados. Diferido con razón: allowlist de egress sin
medir (VPC ajena, T8/Q46), slices cgroup2 (variante caps), pool de
suspendidos (`m7-suspended-pool`), Cost Explorer del día del bench
(2026-09-17), publicación en PyPI/npm (manual).

---

## M7 — Preparación open source

Sólo después de que M6 esté verde. Un hito, siete cambios OpenSpec ordenados
por apalancamiento, según `docs/research/2026-09-m7-oss-readiness.md` (§5);
los dos primeros son la puerta para hacer público el repositorio, 3–5 el
apalancamiento de producto, 6–7 si queda capacidad.

| # | Cambio OpenSpec | Alcance | Test de aceptación | Tamaño | Estado |
|---|---|---|---|---|---|
| 1 | `m7-oss-hygiene` | Apache-2.0 en los 5 sitios, `LICENSE`/`NOTICE`/`CONTRIBUTING` (DCO)/`CODE_OF_CONDUCT` (CC 3.0)/sección de reporte en `SECURITY`/`CODEOWNERS`/plantillas/`dependabot.yml`; metadatos PEP 639; insignias; "Qué corre dónde" en `ARCHITECTURE.md`, "Cómo funciona" en el README y tabla de lenguajes en `concepts.md`; `docs/RELEASING.md` (sin reservar nombres ni publicar) | `check_license.py` OK; la wheel de `uv build` lleva `License-Expression: Apache-2.0` + `NOTICE`; `pnpm pack` contiene `LICENSE` + `NOTICE`; `mkdocs --strict` y todos los gates previos verdes (el check DCO en PR es paso manual hasta que el repo esté en GitHub) | S | **aceptado 2026-09-17** (implementado 2026-09-16; evidencia: el conjunto de gates de `design.md` D16 repetido en la aceptación de M7 sobre el árbol 0.2.0 —`check_license.py` OK, wheel `rayito-0.2.0` con `License-Expression: Apache-2.0` + `licenses/NOTICE`, `pnpm pack:check` con `LICENSE` + `NOTICE`, `mkdocs --strict`—; sin superficie AWS) |
| 2 | `m7-supply-chain` | Acciones fijadas por SHA (17 acciones, comentario de versión, gate `grep` + `actionlint`), `permissions: contents: read`, harden-runner (audit), `scorecard.yml`, `deny.toml` + `cargo-deny`, `cargo auditable` (+2 240 B: 4 700 984 B frente a 4 698 744 B, 145 paquetes en `.dep-v0`) + SBOM CycloneDX 1.5 (113 componentes), `pip-audit` ×3 + `pnpm audit --prod` (+ `audit.yml` semanal; vitest 3.2.7 → 4.1.11 cierra los dos moderados de dev), job `ubuntu-24.04-arm`, `e2e.yml` con OIDC (`infra/ci-oidc-role.yaml`, no desplegada) + guardas de coste (≈ $0,03/run), release-please manifest + `linked-versions` (`release-please.yml`), `release.yml` por tag (PyPI attestations, npm trusted publishing con npm ≥ 11.5.1, `rayd` firmado con cosign keyless + `SHA256SUMS`), `Dockerfile` `FROM` por digest + `--base-image-version` obligatoria | CI verde con todos los checks (`deny`, `audit`, `arm`, `actionlint`); `scorecard.yml` corrió en `main`; `e2e.yml` asumió el rol y dejó cero VMs; release-please abre un PR que sube los tres componentes a la vez y `cosign verify-blob` pasa sobre el zip de la release `rayd-v*` desde una máquina limpia | M | **aceptado en local 2026-09-17; aceptación en GitHub pendiente** (gates locales repetidos sobre el árbol 0.2.0: `actionlint`, `cargo deny check`, `pip-audit` ×4, `pnpm audit`, `cfn-lint` 1.56.3 sobre la plantilla IAM (entonces en el spike de M0, hoy `infra/iam.yaml`) + `infra/*.yaml`, `cargo auditable` con `.dep-v0` de 256 crates y SBOM CycloneDX 1.5 de 217 componentes para `rayd` 0.2.0; implementado 2026-09-16 con `rayito-base` **17.0** publicada desde el `Dockerfile` fijado por digest con `--base-image-version 1` (Q52, build 205,7 s) y e2e verde sobre ella: 13 passed, 2 skipped, 537 s, cero VMs vivos; aceptación en GitHub pendiente: CI/Scorecard/`e2e.yml` con el rol OIDC, PR de release-please, `cosign verify-blob` desde una máquina limpia, coste del run en Cost Explorer) |
| 3 | `m7-suspended-pool` | Decidido en ADR-008: pool en cliente de VMs suspendidos con traspaso de token | `Sandbox.create(pool=…)` p95 hasta la primera celda < 1,5 s en 20 tomas; slots reciclados antes de las 8 h | M | **aceptado 2026-09-17** (ver el bloque de aceptación de M7; implementado 2026-09-16: `SandboxPool`/`AsyncSandboxPool`/`PoolConfig` + `Sandbox.create(pool=)` en Python, `SandboxPool` en TypeScript, backends en memoria y JSON `rayito.pool/1`; e2e `test_m7_pool.py` verde en un run de 336 s sobre `rayito-base` 17.0: **`T_take` p50 0,770 / p95 0,897 s** (min 0,739, max 0,922; `hits` 20, `misses` 0) frente a **`T_create` p50 6,151 / p95 6,488 s**, RTT 109 ms, reciclado 121 s tras aparcar con plaza de reemplazo y la primera terminal, recuperación desde JSON con `launched == 0` y toma 0,726 s, cero VMs vivos al terminar, ≈ 46 lanzamientos ≈ $0,25; 88 tests unitarios Python (44 sync + 44 async) y 34 TypeScript sobre un plano de control falso; hallazgo Q56 sobre la ventana `/run` → rotación de `rayd` cerrada con una celda de asentado; e2e TypeScript `pool.e2e.test.ts` verde en 34,5 s: cinco tomas 0,645–0,687 s, `hits` 5, `misses` 0, cero VMs vivos; pendiente sólo la aceptación formal 9.x) |
| 4 | `m7-s3-persistence` | Checkpoint/restore de `/home/user` en S3 nativo en `rayd` (execution role como root, IMDS sigue bloqueado para uid 1000), `Sandbox.create(persist=)`, patrón `reincarnate()` para el muro de 8 h | Escribir 50 MB, `checkpoint_files()`, kill, `create(persist=)` → mismo sha256; `imds_blocked` sigue `true`; delta del binario medido | L | **aceptado 2026-09-17** (ver el bloque de aceptación de M7; ADR-009; escalera D6 resuelta en el primer peldaño: `rustls` + `aws-lc-rs` compilados con `zig cc` sin ningún ajuste; build limpio ARM64 `T0`/`T1` **106 → 222 s**, binario auditable `S0`/`S1` **4 700 984 → 12 524 384 B** (+7,8 MB, ×2,66; 256 paquetes en `.dep-v0`); `rayito-base` **19.0** y `rayito-base-caps` **7.0** publicadas (`snapshotBuild` 19.0: 922 832 896 / 1 321 267 200 / 37 998 592 B); `test_m7_persistence.py` verde contra AWS real sobre caps 7.0 + base 19.0: 50 MB en 21 entradas, checkpoint #1 **1,50 s de agente (31,4 MB/s)** frente a #2 **1,40 s (35,0 MB/s)** —el page-in del código TLS/S3 cuesta ≈ 0,1 s—, `kill()` y `create(persist=)` con restore **0,67 s (78 MB/s)** → mismo sha256 y el directorio excluido ausente, `reincarnate()` 8,85 s de pared con la VM vieja `TERMINATING`, `imds_blocked` `true` antes y después del checkpoint, `NotFoundException` en 0,11 s sin checkpoint, `permission_denied` en 1,35 s sin execution role (repetido dentro de la suite completa: 32,9/36,0 MB/s de subida, 87,9 MB/s de bajada, `reincarnate()` 9,47 s); suite Python M1–M6 + M7 persistencia sobre 19.0/7.0: **17 passed, 2 skipped** (`test_egress_allowlist` sin conector de egress y el test `slow` de 55 min), 1 deselected (`bench`), 626,7 s; e2e TypeScript `m7.e2e.test.ts` (20 MB + `reincarnate()`) y `m6.e2e.test.ts` verdes en 223,5 s; cero VMs vivos y cero objetos ni multipart bajo `rayito-e2e/` al terminar; 283 tests de `rayd-core` + 81 de `rayd` (9 gRPC en loopback) + 11 `cfg(unix)` del tar bajo Docker, 74 unit Python de persistencia (1043 en total) y 48 TypeScript (384 en total); IAM del execution role parametrizado en `infra/iam.yaml` (`PersistenceBucket`/`PersistencePrefix`, stack actualizado y simulado); Q53/Q54/§17 en `AWS_API_NOTES.md`, T15 en `SECURITY.md`; pendiente sólo la aceptación formal 9.3 y el test opcional 8.5) |
| 5 | `m7-mcp-server` | `rayito.mcp` dentro de la wheel `rayito` tras el extra `rayito[mcp]` (SDK oficial `mcp` 2.2): `python -m rayito.mcp` / `rayito-mcp` (stdio) y `--http` (streamable HTTP en loopback, sin auth); seis herramientas `run_code` (JSON + PNG/JPEG como `ImageContent`, SVG como recurso), `run_command`, `read_file`, `write_file`, `list_files`, `list_sandboxes`; un sandbox por proceso (creado en la primera llamada, `idlePolicy` de AWS, `terminate-microvm` al cerrar); configuración sólo por entorno; adaptadores de ejemplo LangChain y Vercel AI SDK en `docs/examples/` | `tests/e2e/test_m7_mcp.py` verde contra AWS (cliente stdio del SDK `mcp` sobre `python -m rayito.mcp`) + Inspector/Claude Code a mano | S | **aceptado 2026-09-17** (ver el bloque de aceptación de M7; implementado 2026-09-16: `test_m7_mcp.py` verde sobre `rayito-base` 17.0, primera llamada con creación 15,9 s, total 19,6 s, VM `TERMINATING` 0,12 s tras cerrar el cliente, cero VMs vivos; 65 tests unitarios nuevos (fake `rayd` + Stubber, HTTP real con uvicorn) en 8,8 s; wheel `OK` con el extra y el entry point; suite e2e completa 16 passed / 2 skipped y 1 fallo ajeno en la PTY de M5 (`DEADLINE_EXCEEDED`, con el pool de M7 lanzando 23 VMs en la misma sesión), `test_m7_mcp.py` verde de nuevo (creación 8,0 s, total 11,3 s); Inspector CLI en modo URL sobre `--http`: seis herramientas y el PNG de matplotlib (20 282 B); Claude Code pendiente de la revisión manual) |
| 6 | `m7-cli` | CLI `rayito` (typer, extra `rayito[cli]`, entry point `rayito`): `image publish|list|prune|zip`, `sandbox list|info|kill|logs`, `doctor` (diez comprobaciones `OK|WARN|FAIL|SKIP`, `--launch` opcional, `--json`); los cuatro scripts de `scripts/` como shims con el mismo argv; tabla de compatibilidad SDK ↔ `rayd` ↔ imagen en `limits.md` con test de deriva | `rayito doctor` informa de todos los checks en una cuenta nueva; `rayito image publish` reproduce `make image-publish` (reuse sin build); `tests/e2e/test_m7_cli.py` verde con cero VMs vivos al final | S | **aceptado 2026-09-16** (`tests/e2e/test_m7_cli.py`: 6 passed en 83 s sobre `rayito-base` 17.0 con execution role; `doctor --launch` 16,2 s con 8 OK + 2 WARN (`lambda:PassNetworkConnector` `implicitDeny` en el simulador aunque `run-microvm` funciona; la fixture RUNNING) y 0 FAIL, `agent_version` 0.1.0, compatibilidad OK, sandbox de `--launch` `microvm-<id>` TERMINATING; `doctor` sin `--launch` 10,2 s contra la fixture sin crear VMs; `image publish` reutilizó 17.0 (`rayd-45037c481630.zip`, 4,1 s, sin build); `sandbox logs` encontró el stream `2026/09/17[17.0]<id>` por nombre exacto (Q55); `sandbox kill` → cero RUNNING de la imagen. Suite M1–M6 + m7-cli: 17 passed, 2 skipped, 2 failed ajenos (`test_auto_resume` pasó al repetirlo solo; `test_e2b_shim_cookbook` exige la imagen `-poly` de `m7-poly-kernels`). Unit: 107 tests de `tests/unit/cli/` + 57 de `scripts/tests`; wheel con `Provides-Extra: cli`; `mkdocs --strict` verde. Hallazgo corregido durante la aceptación: el stack `rayito-m0-iam` había quedado con `LogGroupPrefix=C:/Program Files/Git/rayito` (conversión MSYS) y ningún MicroVM escribía logs desde las 23:18Z; restaurado a `/rayito` con `update-stack --use-previous-template`). Revisión post-aceptación 2026-09-16: la comprobación `compatibility` ya no exige un `imageVersion` mínimo (es el contador de builds por imagen y cuenta: `rayito-base-poly` 3.0 fallaba con el mismo `rayd` 0.1.0) y `bucket` es un solo `head-bucket` (`s3:ListBucket`, concedido ahora por la `CallerPolicy`) con la región de `x-amz-bucket-region`; medido `doctor --template rayito-base-poly --launch`: 7 OK, 3 WARN, 0 FAIL, compatibilidad OK sobre la imagen 3.0 |
| 7 | `m7-poly-kernels` | `language` en `CreateContextRequest` **y en `ExecuteRequest`** (campo 5, contexto por defecto por lenguaje creado perezosamente por `rayd`); kernelspec `bash` (`bash_kernel` 0.10.0) en la variante `rayito-base-poly` (marcador `kernels_variant` + capa condicional del mismo `Dockerfile`); `javascript` reservado como nombre (`UNIMPLEMENTED`: `ijavascript` necesita compilador en al2023 ARM64, Q57); SDKs `run_code(language=)`/`runCode({ language })` + shim E2B | `run_code("echo hi", language="bash")` devuelve `hi` en la variante; `javascript` es `UNIMPLEMENTED` nombrando la variante; el snapshot de `rayito-base` queda en la banda de D5 | M | **aceptado 2026-09-17** (ver el bloque de aceptación de M7; evidencia de la implementación: `rayito-base-poly` 3.0 y `rayito-base` 18.0 publicadas, `test_m7_poly_kernels.py` 6 passed y `poly.e2e.test.ts` 1 passed contra AWS real, `m7_poly.rs` 9/9 + `m4_code` 19/19 bajo Docker, sidecar 80 host + 13 kernel (bash 2,88 s), clientes Python/TS y gates locales verdes; Q57 con tamaños y latencias; e2e Python completo sobre 18.0: 26 passed, 4 skipped, 1 obsoleto corregido —`test_e2b_shim_cookbook` esperaba `UnimplementedError` para `language="js"`, ahora `UNIMPLEMENTED` del agente— y verde al repetirlo; cero VMs vivos) |
| — | Diferido | SDKs cliente Rust/Go (sólo publicar `rayito-proto` en crates.io), kernels R/Java, URLs firmadas, EFS | — | — | — |

**Estado de aceptación (M7, medido 2026-09-17, cuenta de pruebas, us-east-1;
release 0.2.0, `docs/RELEASE_NOTES_0.2.0.md`):** verde contra AWS real sobre las
tres imágenes publicadas desde el árbol 0.2.0 (`rayd` 0.2.0 auditable,
**12 525 664 B**, `.dep-v0` con 256 crates, sha256 `97eee1a9…`, build ARM64
limpio 2 min 23 s): **`rayito-base` 20.0** (`rayd-0a4238505da8.zip`
12 637 801 B, build 195,6 s, memoria 922 832 896 / code install 1 321 267 200 /
disco 33 738 752 B), **`rayito-base-caps` 8.0** (mismo zip con
`additionalOsCapabilities: ["ALL"]`, 195,7 s, 923 885 568 / 1 320 202 240 /
27 881 472 B) y **`rayito-base-poly` 4.0** (`rayd-c699b3064af9.zip`
12 637 942 B, 216,0 s, 928 100 352 / 1 323 397 120 / 39 063 552 B); las tres con
`--base-image-version 1` y `FROM` por digest (Q52). Suite Python completa
(`uv run pytest tests/e2e -m e2e -v -s`, 35 tests de M1–M7 en una sola sesión,
persistencia con `RAYITO_PERSIST_PREFIX=rayito-e2e`, el prefijo que autoriza el
execution role desplegado): **31 passed, 3 skipped, 1 failed, 1 deselected en
2042,2 s** (34 min; skips: `test_egress_allowlist` sin conector, el test `slow`
de 55 min y `test_snapshot_sizes`; deselected: `bench`). El fallo,
`test_output_budget_and_disk_reserve`, fue un **estancamiento de flujo HTTP/2**:
los dos procesos de 20 MB terminaron en el agente en 2,8 y 6,3 s (`process
ended`, `subscribers: 1`, sin `output_truncated`), pero el cliente, que consume
los handles en background perezosamente y esperaba el primero mientras el
segundo llenaba la ventana de la conexión, no recibió el fin del stream hasta
que el tope de vida de 900 s terminó el MicroVM (`RST_STREAM CANCEL`); repetido
a solas **3 de 3 verde** (dos streams de 20 MB en 14,40 / 11,87 / 9,61 s; 30,8 /
26,3 / 24,4 s por test). Queda como punto abierto (M8: consumir los handles en
background en un hilo propio o serializar en el test). Números del run:
`kernel_ready_s` 5,90–11,41 s en `rayito-base` y 11,14–13,60 s en caps con rol
y logs; persistencia sobre caps 8.0 + base 20.0: checkpoint #1 21 ficheros /
52 429 870 B en **1,65 s de pared (1,55 s de agente, 31,9 MB/s)**, #2 1,35 s
(1,25 s, 38,9 MB/s), segunda vida con restore **0,87 s de agente (60,6 MB/s)**
→ mismo sha256, `reincarnate()` 8,88 s con el viejo `TERMINATING`,
`NotFoundException` 0,11 s, `permission_denied` sin rol 1,45 s, `imds_blocked`
`True` a los 0,10 s, 2 objetos borrados en teardown y cero multipart huérfanos;
poly 4.0: primera celda `bash` **4,19 s** (arranque perezoso), segunda 0,19 s,
`timeout=2` vuelve a los 5,54 s, `javascript` → `UNIMPLEMENTED` nombrando
`rayito-base-poly`, `import bash_kernel` sale con 1 en `rayito-base` (capa
inerte); pool dentro de la suite: **`T_take` p50 0,773 / p95 0,805 s** (min
0,716, max 0,807; `hits` 20, `misses` 0) frente a `T_create` p50 6,196 / p95
6,639 s, RTT 111 ms, reciclado 120,9 s tras ready, recuperación desde JSON con
`take()` 0,820 s; MCP dentro de la suite: primera llamada con creación 7,72 s,
total 11,64 s; CLI (`test_m7_cli.py`) 6 passed con `image publish` reutilizando
20.0. Después, en sesiones separadas: **TypeScript `pnpm test:e2e` 4 ficheros /
5 tests passed en 264,3 s** (m6, m7 persistencia con `reincarnate()`, poly y
pool sobre 20.0/8.0/4.0); **pool a solas 3 passed en 362,6 s** (`T_take` p50
0,768 / p95 0,810 s, min 0,715, max 0,831; `T_create` p50 6,200 / p95 6,621 s;
RTT 110 ms; reciclado 119,3 s; recuperación 0,768 s); **MCP a solas 1 passed en
18,5 s** (cliente stdio del SDK `mcp` sobre `python -m rayito.mcp`: primera
llamada con creación 8,20 s, `write_file` 0,11 s, `read_file` 0,21 s,
`list_files` 0,11 s, `run_code` 0,26 s y 0,16 s con `ZeroDivisionError` como
dato, `list_sandboxes` 2,03 s, `TERMINATING` 0,13 s tras cerrar el cliente,
total 12,48 s); **`rayito doctor --launch`: 9 OK, 1 WARN, 0 FAIL, 0 SKIP**
(el WARN es `lambda:PassNetworkConnector` `implicitDeny` en el simulador,
como en M7-6; `agent` `rayd 0.2.0`, `compatibility` OK con la fila 0.2 y la
imagen 20.0 informativa; sandbox de `--launch` terminado); `rayito image list`
muestra las tres imágenes con 4.0 / 8.0 / 20.0 activas. Host (árbol 0.2.0):
`buf lint`, `cargo fmt --all --check`, `cargo clippy --workspace --all-targets
-- -D warnings`, `cargo test --workspace --locked` (81 + 5 + 13 `rayd`, 283
`rayd-core`), `cargo deny check`, `cargo auditable zigbuild` +
`check_auditable.py` (256 crates, root `rayd 0.2.0`; el único aviso es
`linker_messages` de zig: `ignoring deprecated linker optimization setting
'1'`), SBOM CycloneDX 1.5 regenerada (217 componentes); Python `pytest
tests/unit` **1066 passed, 1 skipped**, `ruff check`/`format --check` (143
ficheros), `mypy src tests` (141 ficheros), `scripts/tests` 57, `uvx ruff check
scripts`, `gen_limits.py --check`, `check_license.py` OK, `uv build` →
`rayito-0.2.0` con `License-Expression: Apache-2.0` + `licenses/NOTICE`
(`check_wheel.py` OK, `twine check` PASSED), `mkdocs build --strict`;
TypeScript `pnpm lint` (77), `typecheck`, `test` **386**, `build`, `pack:check`
(`LICENSE` + `NOTICE`), `pnpm audit` limpio; `actionlint`, `pip-audit` ×4,
`cfn-lint` 1.56.3 sobre la plantilla IAM (entonces en el spike de M0, hoy `infra/iam.yaml`) + `infra/*.yaml`, `openspec
validate --all --strict` (23 items). Corregido durante la aceptación:
`rayito.cli._artifact` comprobaba `is_file()` antes de la lista de exclusión y
un symlink de Linux dentro de `kernel-sidecar/.venv` (creado por la sesión
Docker) rompía `copy_sidecar.py` en Windows (`WinError 1920`); ahora la
exclusión se evalúa primero (test de regresión). Poda: `image_prune.py --keep
2` sobre `rayito-base` (15.0–18.0 borradas), `rayito-base-caps` (6.0) y
`rayito-base-poly` (1.0 y 2.0); `rayito-m0-probe` borrada entera
(`delete-microvm-image`; el README del spike de M0, en el historial de git, la reconstruye). Quedan
**`rayito-base` 19.0 + 20.0, `rayito-base-caps` 7.0 + 8.0 y `rayito-base-poly`
3.0 + 4.0**; `list-microvms`: **0 vivos** al terminar; S3 `rayito-e2e/` vacío.
Coste: Cost Explorer (16:15 UTC, `SERVICE = AWS Lambda` por `USAGE_TYPE`,
líneas `Lambda-MicroVM-*`, ambos días `Estimated`) da **$2,93 el 2026-09-16**
(todas las pistas de implementación de M7: 1 117 GB de lectura de snapshot,
172 GB de escritura, 14 312 vCPU-s) y **$1,43 parcial el 2026-09-17** (568 GB
de lectura, 97 GB de escritura, 4 888 vCPU-s; incluye los e2e de madrugada
UTC de las pistas de persistencia y poly); la aceptación de hoy son ≈ 180
lanzamientos cortos + 3 versiones de imagen ≈ $0,6–0,9, y M7 entero queda
en ≈ $4,5–5, muy por debajo del presupuesto de $15. Se recomprueba el
2026-09-18 por el retardo de facturación.
Versiones en lockstep a **0.2.0** (Python, TypeScript desde 0.0.5, `rayd` y
`agent_version`, manifest de release-please, fila 0.2 de la tabla de
compatibilidad). Cambios OpenSpec `m7-oss-hygiene`, `m7-supply-chain`,
`m7-suspended-pool`, `m7-s3-persistence`, `m7-mcp-server` y `m7-poly-kernels`
archivados (con `m7-cli`, los siete de M7). Diferido con razón: la aceptación
de `m7-supply-chain` en GitHub (CI/Scorecard/`e2e.yml` con OIDC, PR de
release-please, `cosign verify-blob` desde una máquina limpia, coste del run),
que exige el repositorio publicado; el test `slow` de credenciales a los 55
min (Q1); la revisión manual del MCP desde Claude Code; la publicación en
PyPI/npm (`docs/RELEASING.md`).

---

## M8 — Correcciones de la auditoría interna (primera tanda, sin AWS)

Primera tanda de M8: cerrar lo que la auditoría interna del 2026-09-22
(`docs/SECURITY_AUDIT.md`) marcó **"arreglar antes de publicar"** en su triaje
(§8), y nada más. El triaje eligió a propósito filas verificables en local, así
que este bloque **no toca AWS**: ni imagen nueva, ni e2e, ni una sola llamada a
la cuenta.

Dos cambios OpenSpec, los dos archivados el 2026-09-22:

| # | Cambio OpenSpec | Alcance | Estado |
|---|---|---|---|
| 1 | `m8-security-fixes` | Código, IAM y CI: H-01 (prefijos disjuntos por construcción y `Deny` explícito del execution role sobre el prefijo de artefactos), H-02 (todo `uvx` clavado + gate de CI), C-05 (`authorize_identity` como comprobación positiva `uid >= 1000 && gid >= 1000` sin grupo 0, y la puerta duplicada de `persistence` borrada), C-13 (`AllowedPattern` de `PersistencePrefix` sin `*`), H-03 y H-04 (escritura del backend JSON del pool por un temporal exclusivo, en Python y TypeScript), H-05 (`TransportSecuritySettings` explícito en `rayito-mcp --http`), C-11 (el gate de pinning pasa a lista blanca) y el aviso de una sola vez de C-08 | **archivado 2026-09-22** |
| 2 | `m8-security-docs` | Las frases publicadas que no coincidían con el código: C-01, C-02 y C-03 (T2 y `ARCHITECTURE.md`: el origen de dentro de la VM, `/terminate` entre los hooks forjables, `/validate` reinicia el kernel, sólo los hooks de **runtime** se auditan), C-04 (T4 y `security.md`: la propia carga del sandbox lee `Health.metadata` sin credencial), C-07 (T15 y `persistence.md`: el prefijo de S3 **no** separa inquilinos), C-08 y C-09 en prosa, y H-06 (`infra/README.md`: el `sub` con environment no lleva rama) | **archivado 2026-09-22** |

**Estado de aceptación (M8 primera tanda, 2026-09-22, local, sin coste):** las
16 filas de §8 marcadas "arreglar ahora" quedan cerradas, cada una con su
`file:line` y el test que la fija en `docs/SECURITY_AUDIT.md` §9. Gates verdes
sobre el árbol corregido: `cargo fmt --all --check`, `cargo clippy --workspace
--all-targets -- -D warnings`, `cargo test --workspace --locked` **388** (base
382), `cargo deny check` (`advisories ok, bans ok, licenses ok, sources ok`);
Python `pytest tests/unit` **1082 passed, 3 skipped** (base 1066), `ruff check`,
`ruff format --check` (143 ficheros), `mypy src tests` (141 ficheros),
`scripts/tests` **77** (incluye el módulo nuevo `test_iam_template.py`);
TypeScript `pnpm lint`, `typecheck`, `test` **388 passed, 2 skipped** (base
386), `build`, `pack:check`; `uvx cfn-lint==1.56.3` sobre la plantilla IAM (entonces en el spike de
M0, hoy `infra/iam.yaml`) + `infra/*.yaml`, `actionlint` sobre los seis workflows, `python
scripts/check_pins.py` (`OK 7 ficheros`), `gen_limits.py --check`,
`check_license.py`, `uv build` + `check_wheel.py` + `uvx twine==7.0.0 check`,
`mkdocs build --strict` y `openspec validate --all --strict` (31 items). Las
versiones no se mueven: 0.2.0 sigue siendo la release publicada y las entradas
van bajo `Unreleased → Security` en los tres changelogs de paquete.

**Diferido con razón escrita (sigue abierto):** las **7 filas marcadas M8** del
triaje —C-10 (separar build y publish del job de npm, que también cierra el
residuo de H-02), C-12 (`--require-hashes` en el build de la imagen), el
binding del `S3Location` al sandbox en el `runHookPayload` (C-07), la
autenticación por uid del par en `/terminate` y `/validate` (C-01), la guarda
por fase de `/validate` y la retirada de `execute_unchecked` (C-02), las dos
llamadas a `audit()` de `/ready` y `/validate` (C-03), el opt-out del servidor
MCP para no compartir `RAYITO_ACCESS_TOKEN` (C-08) y el split
runtime/publicador con el recurso acotado a imágenes nombradas (C-09)— más el
chequeo de digest de `_publish.py` y el versionado del bucket (H-01) y las dos
reglas `uidrange` con su e2e (C-05). **C-06** (TOCTOU entre `realpath` y la
llamada al sistema) queda **aceptado** con razón documentada; `openat2` con
`RESOLVE_BENEATH` está en la lista de endurecimiento. Fuera del triaje quedan
el gemelo TypeScript del aviso de C-08 y la medición del `/etc/passwd` de
`public.ecr.aws/lambda/microvms:al2023-minimal`, de la que depende la mitad de
C-05.

---

## M9 — Paridad con E2B

Seis cambios OpenSpec (`openspec/changes/archive/2026-09-24-m9-*`) que
cierran la tabla de paridad con E2B 2.51 (113 filas,
`docs/site/docs/e2b-parity.md`).

**Estado: M9 cerrado el 2026-09-24.** Aceptado contra AWS real, gates
finales verdes sobre el árbol definitivo y los seis cambios archivados; queda
sólo la release 0.3.0 por release-please al mergear (abajo). Los números
medidos van a `AWS_API_NOTES.md` §16 con marcadores de posición
(`microvm-<id>`, nombres de imagen, sin cuenta, bucket ni ARN).

| # | Cambio OpenSpec | Alcance | Aceptación en AWS real (2026-09-24) | Estado |
|---|---|---|---|---|
| 1 | `m9-deno-kernels` | `javascript` y `typescript` con Deno 2.9.7 en `rayito-base-poly` (ADR-013) | `test_m9_deno_kernels.py` **8/8** y `test_m7_poly_kernels.py` **5/5** (bandas de tamaño revisadas, D12), `poly.e2e.test.ts` **2/2**; `kernel_ready_s` p50 poly 6,62 s frente a base 5,81 s; fila Q77 | cerrado; archivado 2026-09-24 |
| 2 | `m9-file-transfer` | `upload_url`/`download_url` y ficheros grandes por S3 con las credenciales del llamante (ADR-010, T16) | `test_m9_transfer.py` **14/14**, `m9-transfer.e2e.test.ts` **6/6** (regresión de TS en `rayito-base` 22.0); filas Q70–Q75 (Q74: gzip 0,80 → 52,34 MB/s por el proxy) | cerrado; archivado 2026-09-24 |
| 3 | `m9-server-timeout` | plazo lógico en `rayd`, `set_timeout`, `connect(timeout=)`, `on_timeout` (ADR-011, sustituye a ADR-007) | `test_m9_server_timeout.py` **12/12**, `test_set_timeout_beyond_cap` **3/3** aislado, `m9-timeout.e2e.test.ts` **5/5**; filas Q63 (salida 124, se mantiene), Q64 (pausa al vencer) y Q65 (auto-resume suelto) | cerrado; archivado 2026-09-24 |
| 4 | `m9-sandbox-observability` | `MetricsHistory`, `paginate()`, hechos del guest en `Health` | `test_m9_observability.py` **4/4**, `m9-observability.e2e.test.ts` **2/2** (regresión de TS en 22.0); fila Q68 (`memory_mb` = `MemTotal` del guest, 8016 MiB con una imagen de 2048) | cerrado; archivado 2026-09-24 |
| 5 | `m9-egress-policy` | política de egress en el guest de `rayito-base-caps` (ADR-012, T17) | QE1 (Q66) con su regla de parada, resuelta con la adenda de ADR-012 (opción C: bajo deny-all el DNS puede resolver, la conexión falla); QE2 (Q67): el proxy **no** reenvía TLS a un puerto del guest, `https_ports` sigue `UnimplementedError`; `test_m9_egress.py` **14/14**, `m9-egress.e2e.test.ts` verde | cerrado; archivado 2026-09-24 (motivo de `https_ports` citando Q67 en los dos SDK; el job `arm` de CI va al PR) |
| 6 | `m9-e2b-v2-surface` | shims de E2B 2.x en Python y TypeScript (`rayito/e2b`), `git`, CLI `sandbox create\|connect\|exec\|metrics` | corpus E2B **18/18** en Python (9 sync + 9 async) y **8** programas de TS (`m9-e2b.e2e.test.ts` **11/11**), `fork()` vivo → `UnimplementedError`; `test_m9_git.py` **1/1** (`git` 2.50.1), `test_m9_cli_sandbox.py` **4/4** (PTY real), cookbook de M6 **2/2**; fila Q76 (`git-core`) | cerrado; archivado 2026-09-24 |

**Estado de aceptación (M9, 2026-09-24, cuenta de pruebas, us-east-1):**
verde contra AWS real. Regresión e2e de Python sobre **`rayito-base` 23.0,
`rayito-base-caps` 12.0 y `rayito-base-poly` 7.0**: **62 passed de 62**
(server-timeout 12, observability 4, deno 8, egress 14, transfer 14, M4 1, M5
2, M6 2, M7 poly 5); la pasada completa anterior (84 tests sobre 22.0) dejó 73
verdes y 11 rojos, todos en esos ficheros y todos verdes en la de 23.0 (2
skipped opt-in: conector de egress propio y la rotación lenta de
credenciales). Corpus, git y CLI: 23/26 en 23.0 con `watch` en rojo; tras el
arreglo, corpus + M3 **22/22** en **24.0**. TypeScript **26/26** en 24.0 /
caps 13.0 / poly 8.0 (corpus, timeout, M6, egress, poly); transfer y
observability de TS verdes en la regresión completa de TS sobre 22.0.
Imágenes republicadas desde el `rayd` final: **`rayito-base` 25.0** (build
217,6 s; `snapshotBuild` 939 683 840 / 1 362 808 832 / 38 539 264 B),
**`rayito-base-caps` 14.0** (200,1 s; 931 258 368 / 1 361 743 872 /
36 409 344 B) y **`rayito-base-poly` 9.0** (207,9 s; 929 677 312 /
1 500 737 536 / 36 425 728 B), las tres `SUCCESSFUL`/`ACTIVE`; la re-corrida
de las suites afectadas sobre 25.0 (Python: timeout, egress, observability,
corpus E2B y M4; TS: timeout, egress, corpus, observability) estaba en curso
al escribir esto: `test_m9_server_timeout.py` ya dio **12/12** en 25.0
(`terminatedAt − plazo` 14,2–18,8 s, pausa al vencer 2,45 s, auto-resume
1,07 s, cliente muerto 251,2 s). Re-corrida terminada: Python **52/52** de
las suites afectadas y TypeScript **24/24** sobre 25.0 / caps 14.0 / poly 9.0
(con el arreglo tardío del timer del deadline de los streams de TS, en
`clients/typescript/CHANGELOG.md`), más la aceptación de paquete con
instalación limpia (Python y TS: subida y descarga de 50 MB con sha256
coincidente, `set_timeout` → `TERMINATED`).

**Cierre (2026-09-24):**

- Motivo de `network.https_ports` citando Q67 en los dos SDK y en
  `e2b-compat.md` (`m9-egress-policy` 7.2): hecho.
- Gates finales sobre el árbol definitivo, verdes: Rust en la VM Lima
  (`cargo fmt --check`, `clippy -D warnings`, `cargo test --workspace
  --locked` como uid 1500 con 856 passed y 2 ignored, `m9_egress` como root
  en `unshare --net` 1 passed, `cargo deny check`, zigbuild auditable de
  13 764 024 B con `.dep-v0` de 257 paquetes); Python unit 2115 passed, ruff
  y mypy limpios; sidecar 85 passed; TypeScript 863 passed más lint,
  typecheck, build y `pack:check`; `buf lint`,
  `check_pins`/`check_license`/`check_hygiene`, `gen_limits --check`,
  `scripts/tests` 186 passed y `mkdocs build --strict`.
- `openspec/changes/m9-handoff` borrado y los seis cambios archivados en
  orden (deno-kernels, file-transfer, server-timeout, sandbox-observability,
  egress-policy, e2b-v2-surface): 0 escenarios perdidos (635 → 872 en 30 →
  36 capabilities, `e2b-compat` 20 → 59) y `openspec validate --all --strict`
  36/36.

**Tras el merge:** release 0.3.0 en lockstep por release-please
(`docs/RELEASING.md`) y coste de M9 en Cost Explorer (con el retardo de
facturación).

**Diferido con razón escrita (siguiente ciclo):**

- **Egress, opción A**: bloquear el DNS de uid ≥ 1000 bajo deny-all en caps.
  QE1 (Q66) midió que los resolvedores de la plataforma escuchan dentro del
  guest, así que en M9 los nombres pueden resolverse bajo deny-all aunque toda
  conexión fuera del VM falle (adenda de ADR-012, opción C; riesgo residual en
  `SECURITY.md` T17). La opción A es una regla `ip rule` para el puerto 53 de
  uid ≥ 1000 antes de la regla `local`, con cambio atómico y rollback.
- **Fixtures TLS de los tests de `rayd` con `rcgen`**: la clave y el
  certificado autofirmados de prueba de `crates/rayd/tests/fixtures/tls/`
  están fijos en el repositorio y los escáneres los marcan; generarlos en
  tiempo de test no cambia ningún comportamiento publicado.
- **Ventana de rotación del kernel en `rayd` tras `/run`**: hoy la cierran
  los SDK exigiendo `sandbox_id` en la readiness (Q78); cerrarla en el propio
  agente (marcar `Rotating` de forma síncrona, residuo del orden de 100 µs)
  exige republicar las tres imágenes.
- **Reconexión tras un reset de stream del proxy** (`RST_STREAM`, "Stream
  removed" antes del plazo real): tratarlo como corte reconectable
  (`Connect(pid, from_seq)`); visto una vez en la regresión, no reproducido en
  4 intentos.
- **Mensaje amable de `rayito-mcp` sin el extra**: `rayito-mcp` y `python -m
  rayito.mcp` sin `rayito[mcp]` acaban en un `ModuleNotFoundError` de `mcp`;
  la CLI `rayito` ya imprime cómo instalar su extra. Documentado en el
  `README.md`; no afecta a quien instala el extra.
- **`--remap-path-prefix` en `ci.yml` y `release.yml`**: `make build` quita
  del binario `rayd` las rutas del constructor, pero los jobs `rayd` de CI y
  de release compilan sin ese `rustflags`, así que el `rayd` publicado lleva
  rutas del runner. Sólo se puede verificar con un run de GitHub Actions.
- **Job `arm` de CI con el paso de netns de `m9_egress`**
  (`m9-egress-policy` 9.5): necesita el PR de M9 en GitHub; su equivalente
  local (root en `unshare --net` en la VM Lima) está verde. Gate de merge.
- **Salida del sidecar real dentro de la gracia de `SIGTERM`**
  (`m9-server-timeout` 4.1): la secuencia manda `SIGKILL` a los 5 s de todas
  formas y la e2e real termina con código 124; confirmarlo exige leer el log
  de `rayd` en CloudWatch de una VM en modo `kill`.
- Las filas diferidas de `docs/SECURITY_AUDIT.md` §8 siguen diferidas;
  ningún cambio de M9 las empeora.
- Revisión de arquitectura hexagonal/DDD: 32 hallazgos reales diferidos (la mayoría en `rayd`, cuyo cambio obliga a republicar y repetir la aceptación), listados con fichero, principio y arreglo en [`docs/research/2026-09-m9-architecture-review.md`](docs/research/2026-09-m9-architecture-review.md); 12 se corrigieron antes de 0.3.0.
- `rayd` debería marcar close-on-exec los descriptores heredados al arrancar (defensa en profundidad): la CI de GitHub reveló que un proceso padre con pipes abiertos los filtra a las shells de usuario; el harness de tests ya lo hace y dentro del MicroVM no se ha observado.

---

## Orden de trabajo dentro de cada hito

1. Escribir el test de aceptación primero. Debe fallar.
2. Implementar el lado del agente (dominio en `rayd-core` con puertos falsos,
   después adaptadores en `rayd`).
3. Implementar el lado del cliente.
4. Hacer pasar el test en local contra `scripts/hooks-sim.py` y, sólo entonces,
   publicar una versión de imagen (clave S3 por hash de contenido; máximo 5
   builds concurrentes por región; cada versión nueva cuesta $0.037 de storage
   mínimo).
5. Hacer pasar el test contra AWS real, no contra un mock.

Los mocks del agente son útiles para tests unitarios, pero **ningún hito se
cierra con un test que sólo pasa contra un mock.** El valor de este proyecto
está precisamente en el comportamiento real de la infraestructura.
