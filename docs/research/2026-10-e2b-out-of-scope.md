# Funciones de E2B fuera de alcance: arquitecturas posibles sobre Lambda MicroVMs

Investigación de sólo lectura del 2026-09-30, **sin código ni recursos AWS
creados**. Cubre las funciones de `docs/site/docs/e2b-parity.md` marcadas hoy
como "fuera por SPEC" que tienen un camino técnico sobre Lambda MicroVMs:
volúmenes y montajes de buckets (filas 26, 34, 90, 111), secretos (80, 90),
templates declarativos (56, 81, 89, 112), tamaño CPU/RAM (82), consulta de
sandboxes pausados por metadatos (40), eventos y webhooks de ciclo de vida
(107), exportación OpenTelemetry (108) y dominio propio (110, con efectos en
15, 16, 51 y 52). Lo catalogado como **imposible en la plataforma** (fila 24
`iam=`/`Secret.iam_token`, 17 `mask_request_host`, 18 `https_ports`, 46
snapshots de un VM vivo, inyección transparente de `network.rules`) queda
fuera y se revisará después.

Método: siete investigaciones independientes (una por grupo), cada una sobre
el repo (`SPEC.md`, `ARCHITECTURE.md`, `AWS_API_NOTES.md`,
`docs/aws-api/service-2.json`, `SECURITY.md`, el estudio de EFS de la rama
`research/m11-efs`), el código abierto de E2B (`e2b-dev/infra` @ `bfd9179`,
`e2b-dev/E2B` @ `f713914`, ambos del 2026-09-30) y la documentación oficial
de AWS, E2B, Daytona, Modal, Vercel Sandbox, Cloudflare, Fly.io, Runloop y
Northflank. Después, una revisión crítica que verificó las afirmaciones
clave en la web y en el modelo de servicio. Este documento es la síntesis:
donde el crítico y un informe discrepan se dice, y se explica a quién se
cree y por qué.

Convención de etiquetas (la de `AWS_API_NOTES.md` y el estudio de EFS):

- **VERIFICADO**: lo dice la documentación oficial, el código fuente citado o
  el modelo de servicio, o ya está medido en el repo (fila Q).
- **ASUMIDO**: inferencia razonable, no documentada para MicroVMs.
- **A MEDIR**: sólo una medición en Lambda MicroVMs reales lo decide.
- *(tercero)*: artículo o repo no oficial; no cuenta como verificación.

Regla del repo que se mantiene: **ningún parámetro de API de AWS se usa si no
está en `AWS_API_NOTES.md` o `docs/aws-api/`**. Los servicios nuevos que
aparecen aquí (S3 Files, Secrets Manager, DynamoDB, CloudFront KVS,
CloudWatch OTLP, EventBridge) necesitan su contrato en `AWS_API_NOTES.md`
antes de escribir código.

Numeración de mediciones: los informes usaban `Q99…` con significados
distintos en grupos distintos (colisión señalada por el crítico). Aquí cada
grupo usa un prefijo propio (`VOL-`, `SEC-`, `TPL-`, `RES-`, `CP-`, `OT-`,
`DOM-`). Al pasarlas a `AWS_API_NOTES.md` §16 reciben números correlativos a
partir de **Q99** (Q79–Q98 ya los reserva el estudio de EFS, que aún no está
en `main`).

## 0. Resumen ejecutivo

| Función (filas) | Veredicto | Opción recomendada | ¿Plano de control propio? | Esfuerzo | Coste AWS | Medición clave |
|---|---|---|---|---|---|---|
| Montajes de bucket S3 (111) y `Volume` en modo "prefijo S3" (26, 34, 90) | Viable, **divergente** (no POSIX) | Mountpoint for S3 (FUSE) gestionado por `rayd` en `rayito-base-caps` + ops de fichero por boto3 | No | 12–15 d | S3 estándar ($0,023/GB-mes + peticiones); $0 fijo | VOL-1 (¿FUSE en el kernel?), VOL-4 (suspend/resume con mount-s3) |
| `Volume` POSIX compartido (26) | Viable sólo como **experimental** | S3 Files o EFS por conector VPC; ops fuera del VM por pasarela Lambda (D) | Opcional (pasarela) | 20–30 d | EFS $0,30/GB-mes; S3 Files working set $0,30/GB-mes (terceros) | Q46 (conector VPC propio), VOL-8, VOL-11 |
| Secretos: CRUD `Secret`/`AsyncSecret` + excepciones (80, 90) | Viable, paridad completa del CRUD | Secrets Manager desde el SDK | No | 3–4 d | $0,40/secreto-mes + $0,05/10k llamadas | SEC-9 (semántica de borrado/recreación) |
| Secretos: entrega al sandbox | Viable **parcial** (sin inyección transparente) | Push por RPC a env (visible al código) y, después, gateway en loopback (usable pero no legible) | No | 5–7 d + 10–12 d | ≈ $1,50/mes por 100k sandboxes | SEC-3 (uid 1000 no lee la memoria de `rayd`) |
| Templates declarativos (56, 81, 89, 112) | Viable, divergente (sin caché por capa, sólo ARM64) | Compilador DSL → Dockerfile + zip en S3 + `CreateMicrovmImage` en el SDK; CodeBuild opcional | No | 25–35 d | ≈ $0,04/semana por versión de snapshot | TPL-1 (logs de `docker build`), TPL-2 (ECR en `codeArtifact.uri`) |
| Tamaño CPU/RAM (82) | Viable **sin cambiar la semántica de E2B** (E2B también dimensiona por build) | Primero documentación; después catálogo de imágenes por tamaño + resolvedor en el SDK | No | 1–2 d + 10–12 d | ≈ $0,16–0,20 por tamaño/región/mes | RES-1, RES-2, RES-10, RES-13 |
| `SandboxQuery.metadata` sobre pausados (40) | Viable | Índice DynamoDB opt-in escrito por el SDK en `create()` y unido con `list-microvms` | Almacén opcional en la cuenta del cliente (sin servicio) | 5–7 d | < $0,10/mes a 10k sandboxes | CP-9 (e2e sin despertar VMs) |
| Eventos de ciclo de vida y webhooks (107) | Viable con componentes opcionales | Auditoría por CloudTrail→EventBridge; eventos reales por stdout de `rayd` → forwarder con MAC por evento | Sí, opcional (Lambdas en la cuenta del cliente) | 2–3 d + 10–14 d | ≈ $1–2/mes a 10k sandboxes | CP-1, CP-4, CP-5, CP-7 |
| Exportación OpenTelemetry (108) | Viable, divergente (nombres `rayito.*`, alias `e2b.*`) | Spans en el SDK + `traceparent` hasta `rayd`; después exportador OTLP de `rayd` a CloudWatch | No | 6–8 d + 7–9 d | ≈ $0,00014/sandbox-hora (ASUMIDO) | OT1 (OTLP SigV4 desde el guest), OT9 |
| Dominio propio (110; efectos en 15, 16, 51, 52) | Viable con plano de datos opcional | Ya: `rayito sandbox proxy` local. Después: CloudFront + Function + KVS **si el JWE cabe**; si no, gateway Rust en Fargate | Sí, opcional (ingress en la cuenta del cliente) | 2–3 d; 9–13 d o 13–18 d | CloudFront ≈ $20/mes a 10M req; gateway ≈ $45–80/mes fijo | DOM-1 (longitud real del JWE, puede llegar a 8000 caracteres) |

Esfuerzo total realista, con paridad en TypeScript, e2e en AWS y
documentación de `SECURITY.md`: **150–200 días-persona**. La suma bruta de
los informes daba unos 110; el crítico tiene razón en que casi todos omitían
esas tres partidas. Coste de toda la campaña de mediciones: **≈ $8–12**, más
una VPC propia para las de NFS.

Tres decisiones que el mantenedor tiene que tomar antes de escribir código
(detalle en §8 y §9):

1. **Enmendar `SPEC.md` §4** con una sola distinción: sigue prohibido un
   servicio hospedado por Rayito; se permiten componentes **opcionales y
   reconstruibles en la cuenta del cliente** (tabla, Lambdas, plantillas),
   nunca necesarios para `create/connect/kill`. Un único ADR-014 para todo
   (los informes proponían cuatro "ADR-014" distintos; ADR-013 ya existe).
2. Aceptar que varias funciones llegan como **divergentes y declaradas**, no
   como emulaciones silenciosas (regla 7 de `openspec/project.md`).
3. Aprobar las **fundaciones compartidas** (§8.2): RPC `ConfigureSandbox`,
   presupuesto único de `/suspend`, *credential broker* único en `rayd` y la
   política "rol de ejecución ⇒ `rayito-base-caps`".

## 1. Volúmenes y montajes de buckets

Filas 26 (`Volume`/`AsyncVolume`, `Sandbox.create(volume_mounts=)`), 34
(`SandboxInfo.volume_mounts`), 90 (parte de volúmenes:
`VolumeException`, `VolumeNotFoundException`,
`VolumePathNotFoundException`) y 111 (montajes s3fs/gcsfuse/R2 de la
documentación de E2B).

### 1.1 Cómo lo hace E2B

VERIFICADO en código (`e2b-dev/infra` @ `bfd9179`, `e2b-dev/E2B` @ `f713914`):

- **El plano de datos del montaje es un servidor NFSv3 en espacio de usuario
  en el host**, no EFS ni Filestore en el guest. El orchestrator arranca un
  `nfsproxy` (willscott/go-nfs, `packages/orchestrator/pkg/nfsproxy/proxy.go`)
  si `PERSISTENT_VOLUME_MOUNTS` no está vacío (`factories/run.go:961`). Cada
  volumen es un directorio del host `<raíz>/team-<uuid>/vol-<uuid>` servido
  en chroot (`chrooted/builder.go`).
- **La autorización no es por credencial, sino por dirección de origen**: el
  handler identifica el sandbox por la IP:puerto de la conexión TCP
  (`sandboxes.GetByHostPort(remoteAddr)`) y sólo exporta `/<nombre>` si
  figura en `sbx.Config.VolumeMounts` (`nfsproxy/chroot/nfs.go`).
- **En el guest, `envd` monta como root** con
  `nfsvers=3,hard,sync,noac,lookupcache=none,rsize=wsize=1048576` y 10 s de
  plazo (`packages/envd/internal/api/init.go:640-760`). Comentario literal:
  "disable caching so that pause/resume works correctly". Tras un resume con
  otro `LifecycleID` hace `umount --force` (fallback `--lazy`) y remonta
  (`shouldRemountNFS`). NFSv3 no tiene estado (sin leases), por eso la pausa
  no rompe al servidor; `noac`/`lookupcache=none` explican las limitaciones
  publicadas.
- **Operaciones de fichero sin sandbox**: la API emite un JWT por volumen
  (`api/internal/handlers/volume_token.go`) y el SDK habla con una "volume
  content API" (`E2B/spec/openapi-volumecontent.yml`: `GET|PATCH|DELETE
  /path`, `GET|POST /dir` con `depth,uid,gid,mode,force`, `GET|PUT /file`)
  que termina en el `VolumeService` gRPC del orchestrator
  (`orchestrator/pkg/volumes/*.go`). Todo es plano de control propio.
- **SDK** (`e2b/volume/volume_sync.py`): `Volume.create/connect/get_info/
  list/destroy`; instancia `list/make_dir/exists/get_info/update_metadata/
  read_file/write_file/remove`; 404 → `VolumePathNotFoundException`. Nombre
  `^[a-zA-Z0-9_-]+$`.
- **Limitaciones publicadas** ([volumes-beta-limitations](https://docs.e2b.dev/faq/volumes-beta-limitations)):
  flock/fcntl cuelgan, sin caché de metadatos, ESTALE, permisos no aplicados
  entre usuarios, montajes fijos en create, `df` falso, sin snapshots.
- **Buckets** ([connect-bucket](https://docs.e2b.dev/sandbox/connect-bucket)):
  no es una función, es una receta: template con `s3fs`/`gcsfuse`,
  credenciales escritas dentro del sandbox y `sudo s3fs … -o allow_other`.
  Las credenciales quedan legibles por el código del sandbox.

### 1.2 Cómo lo hacen otros

- **Daytona** ([volumes](https://www.daytona.io/docs/en/volumes)): FUSE sobre
  su almacén S3-compatible; el runner (host) ejecuta `mount-s3` y lo
  bind-montea al contenedor ([issue #3273](https://github.com/daytonaio/daytona/issues/3273));
  no apto para bases de datos. Montajes externos con credenciales en env
  ([mount-external-storage](https://www.daytona.io/docs/en/mount-external-storage/)).
- **Modal**: `CloudBucketMount` "built on top of AWS' mountpoint" con
  `read_only`, `key_prefix`, OIDC ([cloud-bucket-mounts](https://modal.com/docs/guide/cloud-bucket-mounts));
  Volumes propios con commit/reload y ops fuera del contenedor
  ([volumes](https://modal.com/docs/guide/volumes)).
- **Vercel Sandbox** (Firecracker): `sudo mount-s3 --allow-other` con
  credenciales en env, o `--no-sign-request` + firewall que reenvía a una
  función firmante ([mount-remote-storage](https://vercel.com/docs/sandbox/mount-remote-storage));
  Drives: un montaje RW a la vez, $0,05/GB-mes, sin ops fuera del sandbox
  ([drives](https://vercel.com/docs/sandbox/concepts/drives)).
- **Cloudflare Sandbox SDK**: `sandbox.mountBucket(bucket, path, {readOnly,
  prefix, credentialProxy})` sobre s3fs; `credentialProxy: true` deja sólo
  credenciales falsas en el contenedor ([mount-buckets](https://developers.cloudflare.com/sandbox/guides/mount-buckets/)).
- **Fly.io**: volumen = trozo de NVMe local, 1 volumen ↔ 1 Machine
  ([volumes](https://docs.fly.io/volumes/overview/)).
- **Sobre Lambda MicroVMs** *(tercero)*: un autor de AWS monta S3 Files con
  `mount -t s3files -o accesspoint=…` (efs-utils + efs-proxy) en `/run`,
  nunca en el snapshot, y **remonta en cada `/resume`** (~2 s)
  ([dev.to/aws](https://dev.to/aws/baking-a-fast-lambda-microvm-lessons-learned-in-the-trenches-745)).
  `beeblastco/lambda-sanbdox` @ `f992ef0` monta `mount-s3 --prefix` en `/run`
  con `additionalOsCapabilities: ["ALL"]` ("without it mount-s3 fails with
  EPERM") y tuvo EIO a la hora hasta que refrescó credenciales
  ([repo](https://github.com/beeblastco/lambda-sanbdox)). Son indicios fuertes
  de que el kernel del guest trae FUSE y NFSv4.1, pero no sustituyen VOL-1.

### 1.3 Opciones

| Opción | Diseño | Servicios | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|---|
| **A. Mountpoint for S3 gestionado por `rayd`** | `rayd` (root, sólo en caps) abre `/dev/fuse`, hace `mount(2)` con `allow_other` y lanza `mount-s3 <bucket> /dev/fd/N --foreground --prefix … --uid 1000 --gid 1000` como uid dedicado (<1000) con entorno vacío. Montaje por spec `S3Mount{bucket, prefix, read_only, allow_delete, allow_overwrite}`. Ops de `Volume` fuera del VM con boto3 sobre el mismo prefijo | S3, IAM | No | **12–15 d** (informe: 7–9) | $0,023/GB-mes + LIST/PUT $0,005/1k, GET $0,0004/1k; egress del conector sin medir |
| B. S3 Files + access points (NFS 4.1 por conector VPC) | `mount -t s3files -o tls,iam,accesspoint=…`, remontar en `/resume`; ops fuera del VM por la API de S3 del bucket enlazado; unicidad de nombre con manifiesto `If-None-Match: *` | S3 Files, S3 (Versioning obligatorio), VPC + conector, IAM | No | 20–24 d | Working set $0,30/GB-mes + $0,03/$0,06 por GB leído/escrito (terceros, A MEDIR) |
| C. EFS + access points (opción A del estudio EFS) | Tal cual el estudio, pero asumiendo remontaje en cada `/resume` y ESTALE en fds abiertos | EFS, VPC + conector, IAM | No | 22–23 d | $0,30/GB-mes |
| D. Pasarela de contenido de volúmenes | Función Lambda **(no MicroVM)** con `FileSystemConfig` a EFS o S3 Files, tras Function URL con `AWS_IAM`, que implementa la volume content API de E2B | Lambda, EFS/S3 Files, VPC | Sí (opcional) | +6–8 d | ≈ $0 en reposo |

Contras relevantes:

- **A no es POSIX** ([SEMANTICS.md](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)):
  sin append ni escrituras aleatorias, sin rename de directorios, sin
  chmod/chown, sin locks. git, SQLite y `pip install` encima fallan. Vale para
  datasets y artefactos, no como disco de trabajo.
- **B y C** heredan todas las dependencias duras del estudio EFS: conector
  VPC propio nunca medido (Q46), convivencia con `INTERNET_EGRESS` (Q82),
  efs-utils sin systemd, sólo caps. B además: export al bucket tras **60 s**
  sin escrituras; en conflicto **gana el bucket** y la versión del sandbox va
  a `.s3files-lost+found-<fs>`, **invisible desde un access point con
  RootDirectory** ([s3-files-synchronization](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-synchronization.html)).
  Un `write_file` del SDK sobre un fichero que un sandbox edita pierde el
  trabajo del sandbox en silencio.

### 1.4 Recomendación

0. **Ya, independiente de todo**: sustituir el `sync(2)` sin plazo de
   `/suspend` (`crates/rayd/src/hooks/mod.rs`, `flush_page_cache`) por
   `syncfs` acotado por sistema de ficheros en un hijo desechable. Un montaje
   de red colgado deja `sync(2)` en estado D, `/suspend` agota su plazo y la
   VM termina. 0,5–1 día. Va en las fundaciones (§8.2).
1. **Opción A** como `mounts={path: S3Mount(...)}` nativo y como respuesta a
   la fila 111. `Volume` del shim en modo "prefijo S3" **divergente y
   declarado**, con `read_file/write_file/list/remove/exists/get_info` por
   boto3 con consistencia fuerte de S3.
2. **Discrepancia informe ↔ crítico sobre B.** El informe prefería S3 Files
   (B) sobre EFS (C) para el `Volume` POSIX, en contra del estudio EFS. El
   crítico lo rebaja a experimental porque **MicroVMs no está en la lista de
   compute soportado de S3 Files** (EC2, funciones Lambda, EKS, ECS; VERIFICADO
   en [s3-files-attach-compute](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-attach-compute.html)).
   **Creo al crítico**: construir un producto sobre una combinación que AWS no
   soporta, detrás de un conector VPC que no hemos medido, es riesgo doble; y
   el lost+found invisible es pérdida silenciosa de datos. B y C quedan como
   "experimental" detrás de las mediciones de red. La única pieza soportada
   hoy para ops POSIX fuera del VM es **D** (funciones Lambda sí montan EFS y
   S3 Files).
3. gcsfuse/rclone/s3fs genérico: fuera (ADR-003, AWS-only). El puerto
   `Mounter` admite un driver S3-compatible (R2/MinIO vía `--endpoint-url`)
   sin tocar el dominio.

### 1.5 Diseño hexagonal

`rayd-core` (dominio, sin tokio/tonic/nix), módulo `volume`:

- Tipos: `MountSpec = S3Prefix{bucket, prefix, region, endpoint?, mode:
  ReadOnly|WriteNew|WriteOverwrite{allow_delete}} | NfsAccessPoint{fs_kind:
  Efs|S3Files, fs_id, access_point_id, mount_target_ip?, read_only}`;
  `MountPath` validado (absoluta, canónica, bajo `/mnt` o `/home/user/<sub>`,
  sin solaparse, ≤ 4); `MountState` (`Requested → Mounting → Mounted →
  Degraded → Remounting → Unmounted | Failed{class}`); `ResumePolicy` (FUSE:
  sonda 2 s y relanzar daemon; NFS: **remontar siempre** en `/resume`);
  `CredentialLease` (expiración, margen de 10 min); `VolumeError` sin ids ni
  rutas.
- Puertos: `Mounter { support(), mount(spec, path), unmount(path, mode),
  probe(path, budget) }`, `FuseDaemon { spawn(fd, spec, uid, env), restart }`,
  `CredentialSink` (lo alimenta el *credential broker* único, §8.2),
  `BoundedSync { syncfs(path, budget) }`. `MountManager` traduce los hooks en
  acciones puras, como `PersistenceManager`.

`rayd` (adaptadores): `adapters/fuse_mount.rs`, `adapters/efs_mount.rs`
(sólo si B/C), `adapters/bounded_sync.rs`, `grpc/volume.rs`. Los montajes se
aplican por la RPC `ConfigureSandbox` (§8.2), no por `runHookPayload`, para
que funcionen con el pool de suspendidos (ADR-008: se montan tras `take()`).
Contrato: `HealthResponse.volumes_supported` + `repeated MountStatus mounts`.

SDK (Python y TS espejo): dominio `S3Mount`, `S3FilesVolume`/`EfsVolume`;
`VolumeStore` (`s3prefix|s3files|efs`, boto3 del llamante, manifiesto con
`If-None-Match`); puerto `VolumeContent` con adaptadores `S3ObjectContent`,
`GatewayContent` (D) o `Unimplemented`.

### 1.6 Superficie SDK y shim

```python
Sandbox.create("rayito-base-caps", execution_role_arn=..., mounts={
    "/mnt/data": S3Mount(bucket="<bucket>", prefix="team7/", read_only=True),
    "/mnt/out":  S3Mount(bucket="<bucket>", prefix="runs/42/", allow_overwrite=True),
})
sbx.mounts  # {path: MountStatus(state, kind, last_error_class)}
store = VolumeStore.s3prefix(bucket="<bucket>", prefix="rayito-volumes/")
vol = store.create("ws-7"); vol.write_file("a.txt", b"..."); vol.list("/", depth=2)
```

- `mounts=` sin imagen caps → `UnimplementedError` tras terminar la VM
  (patrón ADR-012); fallo de montaje → `VolumeMountException(code=network|
  iam_denied|not_found|helper_missing|timeout)`.
- Shim E2B 2.x: `Volume.create/connect/list/get_info/destroy`, operaciones de
  instancia, `Sandbox.create(volume_mounts=)`, `SandboxInfo.volume_mounts`,
  las tres excepciones. `uid/gid/mode` distintos de 1000 → `RayitoCompatWarning`;
  `update_metadata` → `UnimplementedError` en s3prefix.
- Filas: 26 → divergente (s3prefix no POSIX); 34 rellena `volume_mounts`; 90
  (volúmenes) implementado; 111 → divergente (monta `rayd`, no `sudo s3fs`).

### 1.7 Seguridad

- Monta siempre `rayd` (root con `CAP_SYS_ADMIN`, sólo en caps). uid 1000
  tiene `CapEff=0` y `NoNewPrivs` (Q47) e IMDS bloqueado (Q48): no monta ni
  lee credenciales. Es mejor que E2B, Vercel y Daytona, donde las
  credenciales del bucket viven en el sandbox.
- **Credenciales del daemon** (corrección del crítico): la documentación de
  Mountpoint ([CONFIGURATION.md](https://github.com/awslabs/mountpoint-s3/blob/main/doc/CONFIGURATION.md))
  lista instance profile, rol de tarea ECS y `role_arn`+`credential_source`;
  **no menciona `credential_process`**, que el informe daba por bueno sólo por
  el repo de un tercero. Queda ASUMIDO hasta VOL-3. Además el diseño del
  informe no daba mínimo privilegio por montaje. Propuesta adoptada: el
  *credential broker* de `rayd` hace `sts:AssumeRole` sobre un rol base con
  **session policy inline limitada a `bucket/prefix`** y entrega esas
  credenciales al daemon; renovación antes de 55 min (role chaining: 1 h
  máximo). Alcance por montaje, no por inquilino.
- **El daemon FUSE es una amenaza nueva** (T19), no una nota: procesa
  peticiones de código no confiable y, al correr con uid < 1000, queda fuera
  de la política de egress de ADR-012, así que un exploit del daemon es un
  canal de salida. Mitigaciones: seccomp/landlock sobre el daemon,
  `--read-only` por defecto, límites de recursos, nada sensible en `argv`
  (`hidepid` sin medir: uid 1000 podría ver `cmdline`), salida por el cliente
  saliente único con allowlist (§8.2).
- Lista blanca de buckets en `environmentVariables` de la imagen
  (`RAYITO_ALLOWED_MOUNT_BUCKETS`) para que el access token no baste para
  montar otro bucket accesible por el rol.
- S3 Files: `write_file` del SDK exige `force=True` o un lease por manifiesto
  mientras haya montajes activos.

### 1.8 Mediciones

| Id | Qué | Coste | Parada |
|---|---|---|---|
| VOL-1 | `/proc/filesystems` (fuse, nfs4), `/dev/fuse`, `mount -t fuse` → EPERM en `rayito-base` | < $0,01 | **Sí**: sin FUSE, A cae entera |
| VOL-2 | `dnf install mount-s3 fuse` en al2023-minimal ARM64; delta de `codeInstallSizeInBytes` y `memorySnapshotSizeInBytes`; latencia de montaje p50/p95 | ≈ $0,10 | Sí |
| VOL-3 | Montaje separado por privilegios (fd de `/dev/fuse` + daemon uid < 1000); ¿`credential_process` o credenciales por fichero funcionan?; uid 1000 no lee environ ni mata el daemon | ≈ $0,02 | |
| VOL-4 | Suspend/resume con mount-s3 activo: pausas de 60 s, 10 min y 70 min con lectura abierta y multipart en curso | ≈ $0,10 | Sí |
| VOL-5 | `/suspend` con S3 inalcanzable: ¿cuelga `sync(2)`? repetir con `syncfs` acotado | ≈ $0,03 | |
| VOL-6 | Con `allow_internet_access=False` el daemon sigue llegando a S3 | < $0,02 | |
| VOL-7 | Rendimiento: 1 GB secuencial, 10 000 ficheros de 4 KB, `ls -R` de 50 000 claves **con el coste de LIST/HEAD resultante** y el de egress del conector (añadido por el crítico) | ≈ $0,15 | |
| VOL-8 | S3 Files en MicroVM con conector VPC propio (cubre Q81/Q85/Q86 del estudio EFS) | ≈ $0,10 + VPC | Sí para B |
| VOL-9 | Latencias de sincronización S3 Files y visibilidad del lost+found | ≈ $0,05 | |
| VOL-10 | Metadatos POSIX que S3 Files escribe en los objetos | < $0,02 | |
| VOL-11 | NFS (S3 Files y EFS) tras resume: ¿recupera la sesión NFSv4.1 o hay que remontar? | ≈ $0,10 | Sí para B/C |
| VOL-12 | Sólo si D: función Lambda y MicroVM sobre el mismo access point (close-to-open en ambos sentidos) | ≈ $0,05 | |
| VOL-13 | Precios reales de S3 Files en Cost Explorer | ≈ $0,01 + 1 día | |

### 1.9 Riesgos

1. Kernel sin módulos cargables (Q48): FUSE y NFSv4.1 sólo si están
   compilados dentro. Riesgo binario (VOL-1).
2. Vender `Volume` sobre mount-s3 sin decir que no es POSIX sería aproximar en
   silencio.
3. NFSv4.1 con leases es peor que el NFSv3 sin estado de E2B tras una pausa:
   asumir remontaje en cada `/resume` (≈ 2 s según el prototipo de AWS).
4. Credenciales de 1 h: nunca claves estáticas en el entorno del daemon.
5. Tamaño de snapshot: efs-utils trae Python (~67 MB) + efs-proxy (~24 MB)
   según el prototipo de AWS; mount-s3 es un binario Rust (sin medir).
6. Nota operativa: la investigación de este grupo borró un directorio `E2B/`
   de un scratchpad compartido al reclonar. No afecta al repo.

### 1.10 Fuentes

[e2b-dev/infra](https://github.com/e2b-dev/infra) (`nfsproxy/proxy.go`,
`nfsproxy/chroot/nfs.go`, `chrooted/builder.go`, `envd/internal/api/init.go`,
`handlers/volume_token.go`, `volumes/*.go`);
[e2b-dev/E2B](https://github.com/e2b-dev/E2B) (`spec/openapi-volumecontent.yml`,
`e2b/volume/volume_sync.py`, `e2b/exceptions.py`);
[E2B volumes](https://docs.e2b.dev/volumes);
[limitaciones](https://docs.e2b.dev/faq/volumes-beta-limitations);
[connect-bucket](https://docs.e2b.dev/sandbox/connect-bucket);
[mountpoint-s3 SEMANTICS](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md) y
[CONFIGURATION](https://github.com/awslabs/mountpoint-s3/blob/main/doc/CONFIGURATION.md);
[S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html),
[sincronización](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-synchronization.html),
[compute soportado](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-attach-compute.html),
[montaje en Lambda](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-mounting-lambda.html);
[Lambda FileSystemConfig](https://docs.aws.amazon.com/lambda/latest/dg/configuration-filesystem.html);
[escrituras condicionales S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html);
[microvms-images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html);
Daytona, Modal, Vercel, Cloudflare y Fly.io citados en §1.2; terceros:
[dev.to/aws](https://dev.to/aws/baking-a-fast-lambda-microvm-lessons-learned-in-the-trenches-745),
[beeblastco/lambda-sanbdox](https://github.com/beeblastco/lambda-sanbdox),
[precios S3 Files (tercero)](https://awsfundamentals.com/blog/aws-s3-files);
estudio EFS de la rama `research/m11-efs`.

## 2. Secretos

Filas 80 (`Secret`/`AsyncSecret`) y 90 (`SecretException`,
`SecretNotFoundException`; en JS `SecretError`, `SecretNotFoundError`). Fuera
de este grupo, por imposibles: fila 24 (`iam=`/`Secret.iam_token`) y la
inyección transparente de cabeceras de `network.rules`.

### 2.1 Cómo lo hace E2B

VERIFICADO (docs en beta privada y código):

- **API de gestión**: REST `POST/GET /secrets`, `GET/POST/DELETE
  /secrets/{id}`. El valor es de sólo escritura, nunca vuelve en lecturas ni
  errores; todas las respuestas llevan `Cache-Control: no-store`
  (`api/internal/middleware/secrets.go`). Nombres 1–128 `[A-Za-z0-9_-]`, en
  minúsculas, prefijo `sec_` reservado; los errores no repiten el nombre
  ("names are confidential selectors", `shared/pkg/secretsstore/name.go`).
  Versiones enteras crecientes e inmutables. Límites: valor 64 KiB, 100
  secretos por proyecto, metadata 8 KiB ([limits](https://docs.e2b.dev/secrets/limits.md)).
- La API reenvía a un backend gRPC aparte (`SecretManagementService`) cuya
  implementación **no está en el repo público**.
- **SDK**: `Secret.fill(name)` es un formateador local que devuelve
  `${e2b.secrets.<name>}` sin comprobar que exista (`e2b/secret/base.py`).
- **Inyección**: sólo como transformaciones de cabecera en `network.rules`
  por host. El proxy de egress del host resuelve el valor en cada petición
  HTTPS y lo sustituye (MITM con CA por sandbox, que `envd` añade a
  `/etc/ssl/certs`, `envd/internal/host/cacerts.go`). Si la búsqueda falla,
  **falla abierto** para el tráfico: reenvía la petición sin la cabecera. La
  build abierta sólo trae `NoopEgressProxy`; el inyector es cerrado.

La garantía de E2B ("el valor nunca entra en la VM") depende del proxy del
host. En Lambda MicroVMs el egress sale por conectores gestionados sin
ningún gancho (§2 y Q44/Q60 de `AWS_API_NOTES.md`).

### 2.2 Cómo lo hacen otros

Dos familias:

- **Inyección en env/fichero** (el código ve el valor): Modal
  ([secrets](https://modal.com/docs/guide/secrets)), Northflank
  ([inject-secrets](https://northflank.com/docs/v1/application/secure/inject-secrets)),
  Runloop directo, Fly secrets.
- **Mediación fuera del proceso no confiable**: MITM con CA por sandbox
  (E2B; Vercel, "the credential never enters the microVM",
  [firewall](https://vercel.com/docs/sandbox/concepts/firewall); Cloudflare
  Outbound Workers, [sandbox-auth](https://blog.cloudflare.com/sandbox-auth/);
  Daytona, con placeholder `dtn_secret_<id>` y limpieza de respuestas,
  [secrets](https://www.daytona.io/docs/en/secrets/)) o **gateway explícito**
  (Runloop Agent Gateways, [docs](https://docs.runloop.ai/docs/devboxes/agent-gateways);
  Fly [tokenizer](https://github.com/superfly/tokenizer)). El gateway
  explícito no necesita CA ni interceptación transparente: es lo que Rayito
  puede hacer.

### 2.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|
| **1. CRUD en Secrets Manager desde el SDK** | Nombre `<prefijo>/<name>` (por defecto `rayito/`); `secret_id` = ARN (el sufijo aleatorio da id nuevo al recrear, como E2B); versión entera codificada en `ClientRequestToken` = `rayito-secret-version-{n:020d}` (42 caracteres, SM admite 32–64); metadata en `Description` (≤ 2048); `destroy` = `DeleteSecret(ForceDeleteWithoutRecovery)` | No | 3–4 d | $0,40/secreto-mes + $0,05/10k llamadas |
| **2. Push por RPC a env** | El SDK lee con las credenciales del llamante y empuja una vez por `SecretsService.Put` (x-access-token). `rayd` guarda los valores en un vault en memoria y sólo los resuelve por el parámetro explícito `secrets={"ENV": "name"}`; los placeholders en `envs` normales **no** se resuelven | No | 5–7 d | ≈ $1,50/mes por 100k sandboxes × 3 secretos |
| 3. Pull de `rayd` con el execution role | El payload lleva sólo nombres; `rayd` hace `GetSecretValue` con credenciales IMDS; refresco en `/resume`. Sólo caps; alcance por rol (no hay session tags ni identidad por VM en `RunMicrovm`) | No | 8–10 d | ≈ $3/mes por 100k |
| **4. Gateway de credenciales en loopback** | `rayd` escucha en `127.0.0.1:<efímero>` por ruta; el código apunta su SDK a la URL local; `rayd` quita las cabeceras de la plantilla, sustituye valores del vault, **falla cerrado**, abre TLS al upstream fijo con el cliente y `FilteringResolver` de ADR-010 | No | 10–12 d | $0 extra |

### 2.4 Recomendación

- **Fase 1 (8–10 d): opciones 1 + 2.** CRUD completo del shim (`Secret`,
  `AsyncSecret`, `SecretInfo`, paginadores, `fill`, excepciones) sobre Secrets
  Manager, sin plano de control: las filas 80 y 90 salen de "fuera por SPEC".
  Entrega nativa `secrets=` por push, etiquetada "el valor es visible para el
  código del sandbox". `network.rules` e `iam_token` siguen en
  `UnimplementedError`.
- **Fase 2 (10–12 d, tras SEC-3 y SEC-7): opción 4**, alimentada por push.
  Es la respuesta a "¿se puede entregar un secreto sin inyección de
  cabeceras?": sí, como credencial usable pero no legible por uid 1000, con el
  riesgo residual de que el valor vive dentro de la VM.
- Opción 3 sólo como modo opcional en caps.
- Nunca valores en `runHookPayload`, metadata ni `environmentVariables`.

Correcciones del crítico que adopto:

- **Concurrencia optimista**: dos escritores con **valores distintos** que
  calculan el mismo `n+1` chocan y SM rechaza al segundo (VERIFICADO,
  [PutSecretValue](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_PutSecretValue.html)).
  Con el **mismo valor**, la llamada es idempotente y uno "pisa" al otro sin
  error. Es inocuo (el valor es el mismo), pero se documenta.
- **Frecuencia de `update`**: SM conserva las 100 versiones más recientes más
  todas las de las últimas 24 h y recomienda no escribir más de una vez cada
  10 min de forma sostenida. Un bucle de rotación estilo E2B acaba en
  `LimitExceededException`. El SDK avisa (y opcionalmente limita) cuando
  `update` se llama con más frecuencia.
- **Vector nuevo en la opción 4**: cualquier proceso de uid 1000 puede **usar**
  la credencial contra el upstream fijado. Si ese upstream permite escribir
  contenido (p. ej. crear un gist en la API de GitHub), hay exfiltración con
  la identidad del cliente (confused deputy). Y ese tráfico sale como root,
  fuera de la política de ADR-012: es un bypass de
  `allow_internet_access=False` hacia ese host. Mitigaciones obligatorias:
  **allowlist de métodos y rutas por gateway** y límite de tasa.
- **Opción 2 y snapshots**: los valores quedan en el environ de los kernels y,
  por tanto, en el snapshot de suspensión; SEC-5 (cifrado de snapshots en
  reposo) sigue sin respuesta. El push sólo en `take()` del pool debe ser un
  test, no una frase.
- **Candidato para "lo imposible después"**: el SDK puede acuñar credenciales
  STS cortas (`AssumeRole` + session policy + session tags) y empujarlas por
  el mismo canal. Es un análogo parcial de `iam_token` (fila 24).

### 2.5 Diseño hexagonal

`rayd-core`, módulo `secrets`: `SecretName` (normalización E2B, errores sin
nombre), `SecretValue` (`Zeroizing<Vec<u8>>`, sin `Debug`/`Display`/
`Serialize`, `header_safe()` rechaza CR/LF), parser de `Placeholder`
(gramática de E2B, 32 nombres), `SecretVault` (`swap_all` para rotación
atómica), `EnvBinding` (todo o nada, conflicto con `envs` = error),
`GatewayRoute` (upstream validado por `url_policy` de ADR-010, **métodos y
rutas permitidos**, límite de tasa), `GatewayDecision` pura. Puertos:
`SecretSource` (`PushedSource`, `SecretsManagerSource`), `UpstreamHttp`,
`LoopbackListener`.

`rayd`: `grpc/secrets.rs` (`Put/Refresh/List/Clear`; nunca se trazan los
cuerpos), `adapters/secretsmanager.rs` (sólo si `Health.imds_blocked`),
`network/gateway.rs`. `Health` expone sólo recuentos. Contrato:
`rayito/v1/secrets.proto`; los enlaces `EnvBinding`/`GatewayRoute` viajan por
`ConfigureSandbox` (§8.2).

SDK: `SecretName`, `SecretInfo`, `VersionToken`, `MetadataCodec`; puerto
`SecretStore` con `SecretsManagerStore`, `ParameterStoreStore` (opcional; 4/8
KB, no llega a 64 KiB) e `InMemoryStore`.

### 2.6 Superficie SDK y shim

- Shim Python: `Secret`/`AsyncSecret` con `create/update/get_info/list/
  exists/destroy/fill`; `iam_token` → `UnimplementedError`. `SecretInfo`
  (`secret_id` = ARN). Opciones `region`, `session`, `secret_prefix`,
  `kms_key_id`; `api_key`/`domain` ignorados con aviso. TS espejo.
- Nativo: `Sandbox.create(secrets={"ENV": "name"}, gateways={"anthropic":
  Gateway(upstream="https://api.anthropic.com", headers={...},
  allow=[("POST", "/v1/messages")])})`, `sbx.secrets.push/refresh/list`,
  `commands.run/pty.create/run_code(..., secrets=)`, `SandboxPool.take(secrets=)`,
  CLI `rayito secret create|update|list|destroy` (valor sólo por stdin).
- Diferencias documentadas: metadata ≤ 2048, sin tope de 100, recreación tras
  borrado con reintento, `secret_id` es ARN (no `sec_…`).

### 2.7 Seguridad

Nueva amenaza T18 ("custodia de secretos del usuario"):

- Canal: TLS al endpoint de AWS y h2c por loopback a `rayd`, como ficheros y
  comandos (ADR-004). Nunca en `runHookPayload` (puede aparecer en data
  events de CloudTrail, T4), metadata ni CloudWatch.
- En reposo: KMS en Secrets Manager (`SecretString` no va a CloudTrail);
  memoria de la VM y snapshot de suspensión (SEC-5 sin respuesta).
- Quién lee qué: opción 2 → cualquier código de uid 1000 (tratar como
  revelado a la carga). Opción 4 → uid 1000 usa pero no lee
  (`PR_SET_DUMPABLE=0`, `RLIMIT_CORE=0`, ptrace denegado sin
  `CAP_SYS_PTRACE`; SEC-3 lo confirma). Un escape a root o al kernel lo lee
  todo, y ésa es la brecha frente a E2B/Vercel/Cloudflare.
- Opción 3: falla si no hay `imds_blocked` (en `rayito-base` uid 1000 lee
  IMDS, T1).
- Falla cerrado (a diferencia de E2B): un secreto que no resuelve nunca deja
  pasar una cabecera sustituta del sandbox.

### 2.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| SEC-1 | Identidad del execution role dentro del VM: nombre de sesión, session tags, `SourceIdentity` | ≈ $0,02 |
| SEC-2 | `GetSecretValue` desde `rayd` en caps: latencia en `/run` y tras `/resume`; funciona con la política deny-all de ADR-012 | ≈ $0,10 |
| SEC-3 | uid 1000 no lee los secretos de `rayd` (`/proc/<pid>/mem`, `environ`, `process_vm_readv`, ptrace, core dumps), en base y caps, también tras resume. **Gate de la opción 4** | ≈ $0,04 |
| SEC-4 | Vault a través de suspend/resume y rotación mientras está suspendido | ≈ $0,03 |
| SEC-5 | ¿Están cifrados en reposo los snapshots de memoria? (pregunta a AWS; afecta a varios grupos, §8.2) | $0 |
| SEC-6 | ¿Aparece `runHookPayload` en los data events de CloudTrail? (comparte con CP-3) | ≈ $0,05 |
| SEC-7 | Fidelidad del gateway: SSE de una API LLM, subida chunked, latencia añadida | ≈ $0,05 |
| SEC-8 | Credenciales IMDS tras suspensión > 55 min (Q1 abierta) | ≈ $0,03 |
| SEC-9 | Semántica de SM sin VM: filtros de `ListSecrets`, borrado forzado → recreación inmediata, choque de `ClientRequestToken` | ≈ $0,01 |
| SEC-10 | Higiene: grep del valor y del nombre en logs tras una e2e completa | ≈ $0,01 |

### 2.9 Riesgos

1. Si el shim mapease en silencio `fill()` dentro de `envs` al valor real,
   degradaría la seguridad sin avisar. Por eso `secrets=` es sólo nativo.
2. Los usuarios tratarán la inyección en env como segura: aviso
   `RayitoCompatWarning` la primera vez que se usa `secrets=` sin gateway.
3. Refresco en `/resume` cerca del presupuesto de 30 s: siempre asíncrono.
4. El gateway añade una superficie de red como root (SSRF, request
   smuggling): guardas de ADR-010/012 y fuzzing.
5. La API de secretos de E2B sigue en beta privada: el shim puede tener que
   moverse.

### 2.10 Fuentes

[E2B secrets](https://docs.e2b.dev/secrets.md), [inject](https://docs.e2b.dev/secrets/inject.md),
[rotate](https://docs.e2b.dev/secrets/rotate.md), [limits](https://docs.e2b.dev/secrets/limits.md);
e2b-dev/infra (`handlers/secrets.go`, `middleware/secrets.go`,
`secretsstore/name.go`, `networktransform/placeholders.go`,
`envd/internal/host/cacerts.go`, `sandbox/network/egressproxy.go`);
e2b-dev/E2B (`e2b/secret/base.py`, `secret_sync.py`, `exceptions.py`,
`js-sdk/src/errors.ts`); Daytona, Vercel
([changelog](https://vercel.com/changelog/safely-inject-credentials-in-http-headers-with-vercel-sandbox)),
Cloudflare ([changelog](https://developers.cloudflare.com/changelog/post/2026-04-13-sandbox-outbound-workers-tls-auth/)),
Runloop, Modal, Northflank, Fly tokenizer (§2.2);
[microvms-security](https://docs.aws.amazon.com/lambda/latest/dg/microvms-security.html),
[microvms-images-snapshots](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html);
[límites de SM](https://docs.aws.amazon.com/secretsmanager/latest/userguide/reference_limits.html),
[precios de SM](https://aws.amazon.com/secrets-manager/pricing/),
[CreateSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_CreateSecret.html),
[PutSecretValue](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_PutSecretValue.html),
[DeleteSecret](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DeleteSecret.html),
[ABAC](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access-abac.html);
tercero: [awsteele.com](https://awsteele.com/blog/2026/06/23/some-notes-on-lambda-microvms.html)
(no se pasan session tags).

## 3. Templates declarativos

Filas 56 (`E2B(...).Template`), 81 (API de build), 89 (`TemplateException`,
`BuildException`), 112 (CLI `template`). Rompe un no-objetivo explícito de
`SPEC.md` §4 ("Templates declarativos con CLI propia").

### 3.1 Cómo lo hace E2B

VERIFICADO (docs y código):

- **SDK**: `Template()` es un builder fluido (`from_base_image/from_image/
  from_template/from_dockerfile/...`, `copy/run_cmd/pip_install/apt_install/
  set_envs/...`, `set_start_cmd(cmd, ready_cmd)` con helpers `wait_for_port/
  url/process/file/timeout`; `e2b/template/main.py`, `readycmd.py`). En el
  cable sólo hay 5 instrucciones: COPY, ENV, RUN, WORKDIR, USER
  (`template/types.py`). `to_dockerfile()` ya existe.
- **Protocolo**: `POST /v3/templates` → `{templateID, buildID}`; por cada
  COPY, `filesHash` (sha256 compatible con `.dockerignore`) y subida por URL
  prefirmada si no está; `POST /v2/templates/{id}/builds/{buildID}` con los
  pasos; sondeo de `/status?logsOffset=` cada 0,2 s. `BuildException` con el
  traceback del paso fallido; `FileUploadException(BuildException)`.
  `TemplateException` **no** es de build: se lanza por envd viejo o por
  nombres/tags inválidos (`js-sdk/src/template/utils.ts`).
- **Infra** (`orchestrator/pkg/template/build/builder.go`): baja la imagen,
  inyecta capas, extrae ext4, arranca Firecracker, ejecuta los pasos (cada
  paso = una capa = snapshot pausado y subido, clave por hash de la capa
  padre, `phases/steps/hash.go`), ejecuta start/ready cmd y hace snapshot. El
  start cmd queda vivo en el snapshot. Límites: ≤ 1 h, 8 vCPU/8 GiB, 20
  builds concurrentes ([build-limits](https://docs.e2b.dev/faq/build-limits)).
  V1 dejó de funcionar el 2026-08-01.

### 3.2 Cómo lo hacen otros

- **Lambda MicroVMs** (la plataforma): zip con Dockerfile en S3 →
  `CreateMicrovmImage`; AWS construye, espera `/ready`, hace snapshot,
  `/validate` opcional. Sin caché de capas documentada; 50 versiones por
  imagen; 5–10 builds concurrentes; `FROM` privado sólo desde ECR de la misma
  cuenta ([microvms-images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)).
  **Contradicción**: la doc de monitoring dice que los logs de build incluyen
  "output from Dockerfile execution", pero Q57 midió un `CONTAINER_BUILD_FAILED`
  sin ningún log. Y el modelo (`service-2.json`) describe `CodeArtifact.uri`
  como "such as an Amazon S3 path or Amazon ECR image URI" mientras la doc de
  usuario sólo habla de zip en S3.
- **Modal** ([images](https://modal.com/docs/guide/images)): DSL con caché por
  capa en su runtime.
- **Daytona** ([declarative-builder](https://www.daytona.io/docs/en/declarative-builder)):
  la clase `Image` **genera un Dockerfile**, contexto por hash en object
  storage, logs por stream. Es el patrón más cercano a lo que Rayito puede
  hacer sin plano de control.
- **Runloop** Blueprints (Dockerfile + caché de capas); **Vercel** sólo
  snapshots de un sandbox vivo (imposible aquí, fila 46); **Cloudflare**
  Dockerfile + `wrangler deploy`; **Fly** no construye en la API de Machines.

### 3.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|
| **A. Compilador en el SDK** | DSL de E2B v2 como dominio puro → `DockerfileRenderer` → zip determinista → S3 con clave = sha256 → `Create/UpdateMicrovmImage` → gate de tres estados. start/ready cmd horneados en `/etc/rayito/template.json` y ejecutados por `rayd` antes del 200 de `/ready`. Logs de pasos por "fallo diferido" | No | **25–35 d** (informe: 18–24) | ≈ $0,04/semana por versión |
| B. A + CodeBuild ARM y ECR opcionales | BuildKit con caché en ECR y logs reales de `docker build`; la imagen MicroVM parte de `FROM <ecr@sha256>` | No (stack opcional) | +10–14 d | ≈ $0,02/build + ECR |
| C. Servicio REST compatible con E2B en la cuenta del cliente | API Gateway + Lambda + Step Functions + DynamoDB | Sí | 35–50 d | $1–5/mes + operación |
| D. Template = snapshot de un sandbox vivo | — | — | Descartada: no hay API que copie un VM vivo a imagen | — |

### 3.4 Recomendación

**Opción A**, con puertos para enchufar B como adaptador; C y D descartadas.
Pasos: (1) mediciones TPL-1, 2, 3, 5, 6, 12 (≈ $0,5) **antes** del ADR,
porque deciden el diseño de logs; (2) mover el núcleo de
`rayito/cli/_publish.py` a un módulo del SDK compartido con `rayito image
publish`; (3) API nativa `rayito.Template`; (4) `rayd`: start/ready cmd y
volcado de logs de pasos; (5) shim con las excepciones lanzándose de verdad
(`TemplateException` para nombre/tag inválido y para imagen demasiado vieja,
como E2B con envd viejo); (6) divergencias declaradas: sin caché por capa
(`skip_cache` sólo fuerza rebuild), sólo ARM64, registries privados sólo ECR
de la cuenta, `min_free_disk_mb` verificado en `/validate`, `cpu_count`
derivado de `memory_mb`.

Correcciones del crítico que adopto:

- **Esfuerzo 25–35 d**, no 18–24: la DSL tiene unos 40 métodos, helpers de
  ready cmd, hashing compatible con `.dockerignore`, parser de
  `from_dockerfile` y paridad Python + TS.
- **"Fallo diferido" mejorado**: envolver cada RUN para que no rompa `docker
  build` hace que los pasos siguientes corran sobre un estado roto (tiempo y
  efectos laterales). Cada paso comprueba un marcador de fallo previo y sale
  con `exit 0` sin ejecutar nada.
- `from_base_image()` asume que `GetMicrovmImageVersion` devuelve un
  `codeArtifact.uri` utilizable y que el llamante tiene `s3:GetObject` sobre
  ese zip: ASUMIDO.
- **Control de concurrencia obligatorio** en el SDK: 50 versiones por imagen,
  mínimo de 1 semana de storage por versión y 5/10 builds concurrentes hacen
  que un CI multirrama agote la cuota en horas.

Discrepancia con el crítico sobre el **orden**: el crítico coloca templates en
el nivel 5 por esfuerzo y riesgo de UX de logs. Estoy de acuerdo con el
orden de implementación, pero adelanto sus mediciones (≈ $0,5) a la primera
campaña, porque es la función con más valor para migrar desde E2B y la
decisión de A frente a B depende sólo de TPL-1/TPL-2.

### 3.5 Diseño hexagonal

`rayd-core`: `template::StartSpec { start_cmd, ready_cmd, user (def. 1000),
workdir, envs, ready_poll }` con JSON versionado (`rayito.template/1`);
`BuildStepReport`, `BuildOutcome`; `ready_hook_decision(sidecar,
start_progress, probe) -> Retry | Ok | Fail(reason)` (503 mientras el ready
cmd no devuelva 0); `validate_hook_decision` con `min_free_disk`. Puertos
`TemplateSpecSource`, `ReadyProbe`, `BuildReportSource`; reutiliza
`ProcessSpawner` (el start cmd es un proceso gestionado, visible en
`commands.list`).

`rayd`: `adapters/fs_template_spec.rs`, `adapters/shell_probe.rs`,
`adapters/build_report.rs` (emite logs de pasos sólo en fase Booting, nunca
tras `/run`). Sin cambio de proto obligatorio.

SDK (dominio puro): `TemplateSpec` y builders fluidos, `ContextHasher`
(compatible con `calculate_files_hash`), `DockerfileRenderer` (inserta pasos
antes de EXPOSE/CMD de la base, marcador de fallo, `USER root` + CMD de
`rayd` al final: `rayd` sigue siendo PID 1), `ArtifactAssembler`,
`SizePolicy`, `BuildStateMapper`, `LogEntry*`, `TagCodec`. Puertos
`ArtifactStore`, `ImageBuildGateway`, `BuildLogSource`; adaptador opcional
`CodeBuildPrebuilder` (B).

### 3.6 Superficie SDK y shim

- Nativo: `rayito.Template()` con los métodos de E2B; `Template.build(t,
  name, tags=, memory_mb=2048, skip_cache=, on_build_logs=, build_role_arn=,
  base_image_version=, timeout=1800) -> BuildInfo(template_id=imageArn,
  build_id='<ver>/<buildId>')`; `build_in_background`, `get_build_status`,
  `exists`, `assign_tags/remove_tags/get_tags`, `to_json/to_dockerfile` (el
  Dockerfile **real** que se sube). `Sandbox.create(template='name:tag')` →
  `imageVersion`.
- CLI: `rayito template build|status|logs|tags`; `rayito image publish` sobre
  el mismo núcleo.
- Shim: `Template`, `AsyncTemplate`, `BuildException`, `FileUploadException`,
  `TemplateException` pasan a lanzarse; `from_gcp_registry` y registries con
  usuario/contraseña → `UnimplementedError`; `apt_install` sobre al2023 →
  error claro. Filas 56, 81, 89, 112 → divergente.
- Dependencias nuevas en TS: `@aws-sdk/client-cloudwatch-logs` y un escritor
  de zip (a auditar con `check_license`). Python sin dependencias nuevas.

### 3.7 Seguridad

- Construye el builder gestionado de AWS con el build role. Rayito nunca
  acepta credenciales en el DSL (E2B sí: claves AWS o JSON de GCP en el
  cuerpo del build).
- Política IAM `template-builder` separada de la de sandboxes: un agente que
  crea sandboxes no debe poder publicar imágenes.
- Cadena de suministro (T10): aviso o fallo (`require_digest=True`) si
  `from_image` no va por `@sha256`; clave S3 = hash de contenido.
- `set_envs` se hornea en la imagen y en todos los snapshots: "nunca
  secretos".
- Bases no-AL2023: OpenSSL sólo es snap-safe con al2023-minimal u
  `openssl-snapsafe-libs` ([snapshots](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html)):
  Debian/Ubuntu marcadas como riesgo hasta TPL-8.

### 3.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| TPL-1 | ¿El grupo de logs recibe la salida de los RUN en un build correcto y en uno fallido? | ≈ $0,10 |
| TPL-2 | ¿`codeArtifact.uri` acepta una URI de ECR? | ≈ $0,05 |
| TPL-3 | ¿Hay caché de capas entre versiones de la misma imagen? | ≈ $0,10 |
| TPL-4 | Sintaxis del builder: `# syntax=`, `RUN --mount`, heredocs, multi-stage, `.dockerignore` | ≈ $0,10 |
| TPL-5 | `/ready` con 500/4xx: ¿falla al instante o reintenta? `stateReason` | ≈ $0,05 |
| TPL-6 | Fallo diferido extremo a extremo (logs de pasos en el stream de la VM de build) | ≈ $0,05 |
| TPL-7 | Pulls desde Docker Hub, GHCR, public.ecr.aws y ECR de otra cuenta | ≈ $0,10 |
| TPL-8 | Bases Debian/Ubuntu arm64 con capa rayito: unicidad de PRNG/TLS en dos VMs | ≈ $0,10 |
| TPL-9 | Límites de disco y tiempo del build | ≈ $0,20 |
| TPL-10 | Coste real del build en Cost Explorer | ≈ $0,15 + 24–48 h |
| TPL-11 | 6 y 11 builds concurrentes contra la cuota | ≈ $0,30 |
| TPL-12 | Estado parcial durante `IN_PROGRESS` y TPS de los `Get*` | ≈ $0,05 |
| TPL-13 | Dos builds simultáneos del mismo nombre (`ConflictException`) | ≈ $0,05 |
| TPL-14 | Máximo de tags y consistencia de `ListTags` | ≈ $0 |
| TPL-15 | El start cmd sobrevive al snapshot y sigue gestionado tras run/suspend/resume | ≈ $0,05 |
| TPL-16 | ¿Emite MicroVMs eventos de EventBridge por estado de build? | ≈ $0 |

### 3.9 Riesgos

- Logs pobres si TPL-1 confirma Q57; entonces sólo B da logs de calidad E2B.
- Sin caché por capa: 2–4 min por iteración frente a segundos en E2B.
- Contradicciones doc ↔ modelo (`codeArtifact` ECR, nombres de estado): gana
  el modelo hasta medir.
- Deriva rápida de la API de templates de E2B.
- Rompe un no-objetivo de SPEC §4: ADR y aceptación del mantenedor antes de
  código.

### 3.10 Fuentes

[E2B template quickstart](https://docs.e2b.dev/template/quickstart),
[how-it-works](https://docs.e2b.dev/template/how-it-works),
[caching](https://docs.e2b.dev/template/caching),
[start-ready-command](https://docs.e2b.dev/template/start-ready-command),
[migration v2](https://docs.e2b.dev/migration/template-v2),
[v1 deprecation](https://docs.e2b.dev/migration/v1-build-deprecation),
[start-build-v2](https://docs.e2b.dev/api-reference/templates/start-build-v2),
[build-limits](https://docs.e2b.dev/faq/build-limits);
e2b-dev/E2B (`template/main.py`, `utils.py`, `readycmd.py`, `types.py`,
`template_sync/*.py`, `js-sdk/src/template/*.ts`); e2b-dev/infra
(`template/build/builder.go`, `phases/steps/hash.go`,
`layer/layer_executor.go`, `buildlogger/log_entry_logger.go`);
[microvms-images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html),
[microvms-monitoring](https://docs.aws.amazon.com/lambda/latest/dg/microvms-monitoring.html);
[CodeBuild precios](https://aws.amazon.com/codebuild/pricing/);
[Modal](https://modal.com/docs/guide/images),
[Daytona](https://www.daytona.io/docs/en/declarative-builder),
[Runloop](https://docs.runloop.ai/docs/devboxes/blueprints/overview),
[Vercel](https://vercel.com/docs/vercel-sandbox/concepts/snapshots),
[Cloudflare](https://developers.cloudflare.com/sandbox/configuration/dockerfile/),
[Fly](https://docs.fly.io/machines/api/machines-resource/).

## 4. Tamaño CPU/RAM

Fila 82 (`cpu_count`/`memory_mb` por sandbox), `Template.build(cpu_count=,
memory_mb=)` y `SandboxInfo.cpu_count/memory_mb`.

### 4.1 Cómo lo hace E2B

**Hallazgo clave (VERIFICADO): en E2B el tamaño tampoco es por sandbox, sino
por build de template**, igual que en Lambda MicroVMs.

- `Sandbox.create` no tiene argumento de CPU ni memoria
  (`e2b/sandbox_sync/main.py:168`); `cpu_count/memory_mb` sólo existen en
  `Template.build` (`template_sync/main.py`) y en `e2b template create
  --cpu-count --memory-mb`.
- El OpenAPI `NewSandbox` no tiene cpu/memoria (`spec/openapi.yml:915`);
  validación en `api/internal/team/limits.go` (cpu 1 o par hasta 32, memoria
  par ≥ 128 MiB, límites de equipo).
- El orchestrator crea el VM con `Build.Vcpu/RamMb`
  (`orchestrator/create_instance.go`) y un pausado conserva el tamaño del
  build.
- Para varios tamaños, la doc recomienda construir variantes con nombres
  distintos, `myapp-small`/`myapp-large` ([names](https://docs.e2b.dev/template/names)):
  es literalmente un catálogo por tamaño.
- Contradicción menor: la doc dice 2 vCPU/512 MiB por defecto y el código
  2/1024.

### 4.2 Cómo lo hacen otros

Las plataformas con scheduler y arranque en frío dimensionan por instancia
(Modal `cpu=/memory=`, [resources](https://modal.com/docs/guide/resources);
Fly `guest`, [sizing](https://docs.fly.io/machines/guides-examples/machine-sizing/);
Runloop `resource_size_request`, [sizes](https://docs.runloop.ai/docs/devboxes/configuration/sizes);
Vercel vCPU con 2 GB por vCPU, [pricing](https://vercel.com/docs/vercel-sandbox/pricing)).
Las que arrancan desde un snapshot de memoria atan el tamaño al snapshot o a
la clase (E2B; Daytona `daytona-small/medium/large`,
[snapshots](https://www.daytona.io/docs/en/snapshots);
Cloudflare `instance_type` por clase,
[limits](https://developers.cloudflare.com/containers/platform-details/limits/)).
Es estructural: restaurar un snapshot exige el mismo layout de memoria
(ASUMIDO para Lambda por herencia de Firecracker; da igual, porque la API no
permite otro tamaño).

### 4.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|
| **C. Sólo documentación** | Fila 82 → "implementado por template, como E2B"; documentar `rayito image publish --memory-mib` con los 5 tamaños y validar el valor | No | 1–2 d | $0 |
| **A. Catálogo por sufijo + resolvedor** | Una imagen por (variante, tamaño) desde el **mismo zip**: `rayito-base-{512mb,1gb,2gb,4gb,8gb}`; `rayito image publish --sizes`; `Sandbox.create(resources=Resources(cpu, memory_mb, policy='baseline'\|'peak'))` elige el menor tamaño que cumple y verifica con `GetMicrovmImageVersion` que `resources[0].minimumMemoryInMiB` coincide | No | 10–12 d (informe: 7–10) | ≈ $0,16–0,20 por tamaño/región/mes |
| B. Tamaños como versiones de una imagen | Una versión por tamaño | No | 5–7 d | Como A |
| D. Broker de tamaños en la cuenta | Lambda + DynamoDB con catálogo atómico y límites por equipo | Sí | 12–18 d | $1–5/mes |

B se descarta: `run-microvm` sin `imageVersion` usa la última versión ACTIVE,
así que un cliente que no fija versión recibe en silencio el último tamaño
construido; además las actualizaciones por imagen se serializan
(`ConflictException`, medido) y IAM no puede restringir por tamaño.

### 4.4 Recomendación

Primero **C** (1–2 d, correcto hoy: E2B también dimensiona por build),
después **A**. D sólo si alguien pide límites por equipo; IAM Deny sobre
ARNs concretos (`*-8gb`) cubre casi todo.

Decisiones que necesitan visto bueno:

1. Política por defecto `baseline` (lo pedido es la línea base garantizada;
   2 vCPU/1024 de E2B → imagen de 4 GB, $0,25/h) con `peak` opt-in cuando
   RES-3/RES-4 muestren que el burst es fiable.
2. Un `create()` sin `resources` nunca cambia el tamaño.
3. Redondeo hacia arriba → `RayitoCompatWarning`; peticiones imposibles →
   `InvalidArgumentException` antes de llamar a AWS.
4. `SandboxInfo.cpu_count/memory_mb` del shim = vista del guest (Q68: 4 /
   8016 en una imagen de 2048); nativo añade `baseline_cpu/baseline_memory_mb`.
5. `hard_cap` con cgroup v2: **no se promete en la API hasta RES-12**. El
   crítico tiene razón: sin `CAP_SYS_ADMIN` en `rayito-base`, `rayd`
   probablemente no pueda escribir en cgroupfs.

Nota sobre SPEC §4: la línea "Tamaño por sandbox" ya dice "un template = un
tamaño". A no la contradice (sigue siendo un template por tamaño); sólo
añade un resolvedor en el cliente. Basta con actualizar la frase, no hace
falta cambiar el principio.

Coste que el informe no contaba (crítico): 5 tamaños × 3 variantes × N
regiones × rebuild por release, cada uno con mínimo de 1 semana, en olas
limitadas por la cuota 5/10 (≈ 3–4 olas de 4 min por release); y un pool de
suspendidos por tamaño multiplica el storage de snapshots suspendidos.
Recomiendo publicar por defecto sólo `1gb`, `2gb` y `4gb` de `rayito-base`, y
el resto bajo demanda.

### 4.5 Diseño hexagonal

`rayd-core`: `resources::ResourceView { cpu_count, memory_total_bytes,
baseline_memory_mib }`, `baseline_cpu(mib) = mib/2048`; `HardCap` (sólo si
RES-12). Puertos `SizeHintSource` (lee `RAYITO_BASELINE_MEMORY_MIB`, que
publish hornea como env de imagen) y `CgroupLimiter` (`NoopLimiter` por
defecto con `hard_cap_enforced=false` en `Health`).

SDK (`rayito._sizing`, `src/sizing.ts`, mismas tablas y vectores de prueba
compartidos como `limits.json`): catálogo `Size`, `ResourceRequest`,
`resolve(request, available) -> SizeChoice | error` puro; puerto
`SizeCatalog` con `ConventionCatalog` (sufijo + verificación), `StaticCatalog`
y, en el futuro, `BrokerCatalog`. `SandboxPool` se indexa por ARN resuelto.
Si AWS añadiera tamaño por `RunMicrovm`, el cambio queda dentro del puerto.

### 4.6 Superficie SDK y shim

- Nativo: `Sandbox.create(template=..., resources=Resources(...))` o
  `size='4gb'`; `sbx.reincarnate(resources=...)` = cambio de tamaño sólo de
  ficheros (id nuevo); `get_info()` añade `baseline_cpu`,
  `baseline_memory_mb`, `size`; CLI `rayito image publish --sizes`, `rayito
  image sizes`, comprobación en `doctor` (incluida paridad de
  `agent_version` entre tamaños).
- Shim: `Sandbox.create` no cambia (E2B no tiene tamaño ahí); fila 82 →
  "implementado por template (como E2B)"; el mapeo de
  `Template.build(cpu_count, memory_mb)` llega con §3.

### 4.7 Seguridad

- Palanca de coste y DoS (hasta 16× entre 0,5 y 8 GB): la mitigación es IAM
  por ARN de imagen (`RunMicrovm` se autoriza sobre la imagen, §10); RES-13
  lo confirma. Los límites del SDK son sólo orientativos.
- Cuota regional de memoria compartida: tamaños grandes la agotan antes
  (RES-7: ¿cuenta línea base o pico?).
- Integridad del catálogo: ARNs sólo de la propia cuenta y verificación de
  `resources` antes de usar una imagen.

### 4.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| RES-1 | Valores aceptados de `minimumMemoryInMiB` (512…8192 y 256/3072/16384) | ≈ $0,50 |
| RES-2 | Vista del guest en los 5 tamaños (`nproc`, `MemTotal`, `df`, cgroups) | ≈ $0,10 |
| RES-3 | Fiabilidad del burst hasta el pico | ≈ $0,30 |
| RES-4 | Facturación del burst en Cost Explorer; ¿se reclama la memoria liberada? | ≈ $0,50 + 24 h |
| RES-5 | Tamaño de snapshot y arranque por tamaño (20 lanzamientos cada uno) | ≈ $1,50 |
| RES-6 | Suspend/resume por tamaño con working sets de 0,5/4/12 GB | ≈ $0,50 |
| RES-7 | Contabilidad de la cuota regional de memoria | ≈ $0,20 |
| RES-8 | Storage real del catálogo en 8 días | ≈ $1–2 |
| RES-9 | Ancho de banda y conexiones del endpoint por tamaño | ≈ $0,20 |
| RES-10 | ¿Funciona `rayito-base` en 512 MiB? | ≈ $0,10 |
| RES-11 | Publicación paralela de N tamaños | ≈ $0,30 |
| RES-12 | Delegación de cgroup v2 en base y caps | ≈ $0,05 |
| RES-13 | Restricción IAM por tamaño | ≈ $0 |

### 4.9 Riesgos

- Semántica distinta: línea base + burst 4× con proporción fija 2 GB : 1 vCPU
  y sólo 5 valores. El código de E2B que lee `nproc` puede
  sobreparalelizar.
- Deriva del catálogo si los tamaños salen de zips distintos.
- 512 MiB puede no servir para la imagen completa (RES-10).

### 4.10 Fuentes

[microvms-images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html),
[precios de Lambda](https://aws.amazon.com/lambda/pricing/),
[métricas de MicroVMs](https://aws.amazon.com/blogs/compute/collecting-cpu-and-memory-metrics-for-aws-lambda-microvms/);
`docs/aws-api/service-2.json` (`Resources.minimumMemoryInMiB`, sin tamaño en
`RunMicrovm`, sin alias); `AWS_API_NOTES.md` §2, §4, §11, §12, Q68;
[E2B customize-cpu-ram](https://docs.e2b.dev/sandbox-template/customize-cpu-ram),
[billing](https://docs.e2b.dev/billing), [names](https://docs.e2b.dev/template/names);
e2b-dev/infra (`team/limits.go`, `constants/templates.go`, `spec/openapi.yml`,
`orchestrator/create_instance.go`, `handlers/sandbox_get.go`); e2b-dev/E2B
(`sandbox_sync/main.py`, `template_sync/main.py`,
`cli/src/commands/template/create.ts`); Daytona, Modal, Vercel, Fly, Runloop,
Cloudflare en §4.2.

## 5. Plano de control: pausados por metadatos, eventos y webhooks

Filas 40 (`SandboxQuery.metadata` sobre `PAUSED`) y 107 (API REST y webhooks
de ciclo de vida).

### 5.1 Cómo lo hace E2B

VERIFICADO en `e2b-dev/infra`:

- **Listado**: `GET /v2/sandboxes?metadata=…&state=running,paused`
  (`handlers/sandboxes_list.go`) une dos fuentes: los `RUNNING` salen de la
  vista en memoria del orchestrator y los `PAUSED` de Postgres, tabla
  `snapshots` con `metadata jsonb` consultada con `@>`
  (`db/queries/get_snapshots_with_cursor.sql`). **E2B no pregunta al agente de
  un pausado: copió los metadatos a su almacén al pausar.** Es exactamente lo
  que le falta a Rayito, donde los metadatos viven sólo en `rayd`.
- **Eventos**: el orchestrator publica `sandbox.lifecycle.{created,killed,
  paused,resumed,updated,checkpointed}` en cada transición
  (`orchestrator/pkg/server/sandboxes.go`, `shared/pkg/events/sandbox.go`),
  hacia ClickHouse (API de eventos, retención 7 días) y un Redis Stream (sólo
  si el equipo tiene webhook).
- **Webhooks**: fachada REST sobre un backend gRPC aparte que no está en el
  repo (`handlers/webhooks_backend.go`). Según la
  [doc](https://docs.e2b.dev/sandbox/lifecycle-events-webhooks): cabeceras
  `e2b-webhook-id`, `e2b-delivery-id`, `e2b-signature-version: v1`,
  `e2b-signature` = base64(sha256(secret + payload)) sin `=`, hasta 3
  intentos.

E2B resuelve las dos cosas con un plano de control con estado que ve todas
las transiciones porque es él quien las ejecuta.

### 5.2 Cómo lo hacen otros

Fly (`metadata.{key}` en el listado para cualquier estado,
[machines-resource](https://docs.fly.io/machines/api/machines-resource/)),
Modal (`set_tags`, `list(tags=)`,
[Sandbox](https://modal.com/docs/reference/modal.Sandbox)), Vercel (5 tags,
[tags](https://vercel.com/docs/sandbox/concepts/tags)), Daytona (labels y
webhooks `sandbox.created`, `sandbox.state.updated`,
[webhooks](https://www.daytona.io/docs/en/webhooks/)): todos guardan las
etiquetas en **su** plano de control. Cloudflare
([architecture](https://developers.cloudflare.com/sandbox/concepts/architecture/))
deja el índice al cliente (un Durable Object por sandbox con id elegido por
el cliente): es el patrón más parecido a Rayito. Lambda MicroVMs no admite
tags por MicroVM (`TaggableResource` no incluye microvm en `service-2.json`).

### 5.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste (10k sandboxes/mes) |
|---|---|---|---|---|
| **A. Índice DynamoDB opt-in** | El SDK hace `PutItem` con `attribute_not_exists` tras `run-microvm` (metadatos inmutables, TTL = `startedAt` + `maximumDurationInSeconds` + margen). `list(metadata=, states=)` recorre `list-microvms` (fuente de verdad del estado), `BatchGetItem`, descarta filas cuyo `image_arn`/`startedAt` no coinciden y filtra en el cliente | Almacén en la cuenta del cliente, sin servicio | 5–7 d | < $0,10/mes |
| B. CloudTrail → EventBridge | Data events `RunMicrovm/SuspendMicrovm/ResumeMicrovm/TerminateMicrovm` → reglas → API destination o SQS | No (plantilla) | 2–3 d | < $1/mes |
| **C. Eventos por stdout de `rayd`** | En cada hook, `rayd` escribe una línea JSON con prefijo fijo; CloudWatch Logs (ya existente) → subscription filter → Lambda forwarder (sandbox_id del nombre del stream) → tabla de eventos (TTL 7 d) y bus `rayito` → deliverer con firma E2B. Reconciliador programado que sintetiza `killed` por timeout comparando con `list-microvms` | Sí, opcional (Lambdas en la cuenta del cliente) | 10–14 d | ≈ $1–2/mes |
| D. API REST compatible con E2B autodesplegada | API Gateway + Lambda + DynamoDB sobre A y C | Sí | 20–30 d + operación | $1–5/mes + mantenimiento |
| **E. DynamoDB Streams del índice** (añadida por el crítico) | Put = `created`, Delete = `killed` (por petición), reutilizando A | Almacén + 1 Lambda | 2–3 d sobre A | céntimos |

Sobre E: el crítico la propone como eventos baratos sin CloudTrail. Es
válida, pero hay que decir su límite: sólo ve lo que hace el SDK (create y
kill), no la pausa por idle, el auto-resume ni los timeouts. Es un sustituto
de B (auditoría barata de acciones del SDK), no de C.

### 5.4 Recomendación

- **Fase 1: A** (5–7 d, < $0,10/mes). Además de la fila 40, quita la sonda
  `Health` por sandbox del listado actual, que consume
  `CreateMicrovmAuthToken` (50 TPS) y cuenta como tráfico de idle (Q45). Las
  dos reglas que lo hacen correcto: el estado sale **siempre** de
  `list-microvms` y el índice sólo guarda datos inmutables escritos una vez.
  `startedAt` existe en el modelo (`service-2.json`) para la verificación
  cruzada. Si `PutItem` falla, por defecto se termina la VM y se lanza
  `IndexWriteError` (`index_failure='warn'` lo evita).
- **Fase 2: E o B** como "auditoría de acciones" (documentada así, no como
  eventos de ciclo de vida), y **C** para webhooks útiles (pausa por idle,
  timeout, resume) con payload y firma de E2B. No empezar C sin CP-1, CP-4,
  CP-5 y CP-7.
- **Fase 3: D** sólo con demanda explícita de clientes que no usan el SDK.

Corrección del crítico sobre C que adopto (**falsificación**): el informe
analizaba si uid 1000 puede escribir en `/proc/1/fd/1` (probablemente no sin
ptrace). El riesgo real es la **herencia**: si los procesos que lanza `rayd`
(comandos, PTY, sidecar, kernels) heredan el stdout de PID 1, el código del
sandbox escribe líneas `{"rayito.event":1,…}` en el mismo stream y el
forwarder no puede distinguirlas. Dos medidas: (1) verificar (y testear) que
`ProcessSpawner` nunca hereda el fd 1 de `rayd`; (2) **MAC por evento** con
una clave HMAC entregada en `ConfigureSandbox`, sólo en memoria de `rayd`,
que el forwarder verifica. También adopto: `TerminateMicrovm` sobre una VM
ya terminada o fallida genera evento en B, así que el mapeo a `killed` debe
deduplicar.

### 5.5 Diseño hexagonal

`rayd-core`, módulo `lifecycle_events`: `LifecycleEvent { id (OsRng tras
/run), sandbox_id, kind, generation, occurred_at_ms, image_arn,
image_version, mac }` con `EventKind = Created | Resumed | Paused |
Killed{reason: Request|Timeout|Unknown} | Updated`; función pura
`events_for(transition, audit)` sobre el `Transition` de `lifecycle.rs`,
deduplicada por (kind, generation); **nunca** valores de metadata ni envs.
Puerto `LifecycleEventSink` no bloqueante con cola acotada: en `/suspend`
nunca convierte el hook en un 500 (va dentro del `SuspendBudget`, §8.2).
Adaptadores `StdoutEventSink`, `NullSink` (defecto) y, sólo en caps,
`EventBridgeSink` sobre `signed_http`.

SDK: `IndexRecord`, `join_index(items, records)` pura, `ListFilters` que
admite estados no `RUNNING` con índice; puertos `SandboxIndex` (`put`,
`delete`, `batch_get`) y `EventStore`; adaptadores `NullSandboxIndex`
(comportamiento actual) y `DynamoDbSandboxIndex` (junto a
`LambdaMicrovmsControlPlane` en `_aws.py`; en TS `@aws-sdk/client-dynamodb`
como dependencia opcional).

### 5.6 Superficie SDK y shim

- Nativo: `Sandbox.create/list/get_info(..., index=)` o `RAYITO_INDEX_TABLE`;
  `Sandbox.get_events(sandbox_id=None, types=, limit<=100, order=)` en fase
  2; CLI `rayito sandbox list --metadata k=v --state paused`; `doctor`
  comprueba tabla, TTL y permisos.
- Shim: `SandboxQuery(metadata=…, state=[PAUSED])` se mapea con índice; sin
  él sigue `UnimplementedError` con un mensaje que nombra `index=`. E2B no
  expone eventos ni webhooks en su SDK (sólo REST), así que no hay método
  nuevo; se documenta el payload (`sandbox_template_id` = ARN de imagen,
  `sandbox_execution_id` = `<id>#<resume_generation>`, sin `checkpointed`).
- Filas: 40 → mapeado con índice; 107 → "mapeado con nota" (plantillas).

### 5.7 Seguridad

- El índice es una copia nueva en reposo de `metadata` (ya declarada no
  secreta): nunca token, hash del token, envs ni payload. IAM mínimo
  (`PutItem/DeleteItem/BatchGetItem`); multiinquilino con `tenant#sandbox_id`
  y `dynamodb:LeadingKeys`. Integridad: `attribute_not_exists`, verificación
  contra `list-microvms` (una fila falsa nunca crea un sandbox fantasma),
  rol escritor separado del lector.
- C exige execution role con `logs:*`; en `rayito-base` uid 1000 lee esas
  credenciales (T1). Política §8.2: C sólo en caps o con aceptación explícita.
- Webhooks: secreto en Secrets Manager, sólo https, sin IPs privadas (SSRF),
  idempotencia por id de evento.
- D: una Lambda con permisos sobre MicroVMs es un confused deputy; SigV4 antes
  que API keys.

### 5.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| CP-1 | ¿Generan data events las transiciones de la plataforma (idle, auto-resume, timeouts)? Se espera que no | ≈ $0,05 |
| CP-2 | Latencia CloudTrail → EventBridge → SQS | ≈ $0,10 |
| CP-3 | Contenido real del data event de `RunMicrovm` (¿`runHookPayload`?, prefijo del id) | ≈ $0,01 |
| CP-4 | ¿Se invoca `/terminate` en RUNNING al agotar `maximumDurationInSeconds` y en SUSPENDED? | ≈ $0,03 |
| CP-5 | ¿Llegan a CloudWatch las líneas escritas dentro de `/suspend` antes del checkpoint? Latencia hasta la Lambda | ≈ $0,05 |
| CP-6 | Egress HTTPS dentro de los hooks (sólo variante PutEvents directo) | ≈ $0,03 |
| CP-7 | ¿Pueden los procesos de uid 1000 escribir en el stdout de `rayd` (herencia o `/proc/1/fd/1`)? | ≈ $0,01 |
| CP-8 | Retardo de aparición en `list-microvms` y cuota de `ListMicrovms` | ≈ $0,02 |
| CP-9 | E2E de A: 50 suspendidos, `list(metadata=, state=PAUSED)` sin despertar ninguno | ≈ $0,25 |
| CP-10 | ¿Filtran los advanced event selectors por `eventName` en `AWS::Lambda::MicrovmImage`? | ≈ $0,01 |
| CP-11 | Coste de ingesta en EventBridge de data events | ≈ $0 |

### 5.9 Riesgos

- Vender B o E como "eventos de ciclo de vida" sería falso.
- Sin reconciliador faltarían los `killed` por timeout de pausa, justo los
  interesantes (CP-4).
- `rayd` no distingue suspend por API de suspend por idle (el cuerpo del hook
  está vacío): `reason=unknown` salvo correlación.
- Deriva del índice (sandboxes creados fuera del SDK): la unión con
  `list-microvms` manda.
- TTL de DynamoDB borra "within a few days": filtrar por `expires_at` en
  lectura ([TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/howitworks-ttl.html)).

### 5.10 Fuentes

[microvms-monitoring](https://docs.aws.amazon.com/lambda/latest/dg/microvms-monitoring.html),
[microvms-how-it-works](https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html),
[EventBridge + CloudTrail](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail.html),
[API destinations](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-api-destinations.html),
[precios EventBridge](https://aws.amazon.com/eventbridge/pricing/),
[CloudTrail](https://aws.amazon.com/cloudtrail/pricing/),
[DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/),
[API Gateway](https://aws.amazon.com/api-gateway/pricing/);
[E2B webhooks](https://docs.e2b.dev/sandbox/lifecycle-events-webhooks),
[events API](https://docs.e2b.dev/sandbox/lifecycle-events-api),
[list](https://docs.e2b.dev/sandbox/list); e2b-dev/infra (`spec/openapi.yml`,
`handlers/sandboxes_list.go`, `db/queries/get_snapshots_with_cursor.sql`,
`shared/pkg/events/sandbox.go`, `orchestrator/pkg/server/sandboxes.go`,
`delivery_redis_streams.go`, `handlers/webhooks_backend.go`); Fly, Modal,
Vercel, Daytona, [Runloop](https://docs.runloop.ai/docs/devboxes/lifecycle),
Cloudflare en §5.2.

## 6. Exportación OpenTelemetry

Fila 108.

### 6.1 Cómo lo hace E2B

- Docs ([otel-telemetry-export](https://docs.e2b.dev/sandbox/otel-telemetry-export.md)):
  sólo Enterprise, configurado por E2B en el onboarding, sin API ni kwarg.
  Sólo métricas y logs (no trazas) por OTLP/HTTP: `e2b.sandbox.cpu.used`,
  `cpu.total`, `ram.used/total/cache`, `disk.used/total`,
  `e2b.team.sandbox.created`, `e2b.team.sandbox.running`, con `sandbox_id`,
  `team_id`, `build_id`, `sandbox_type`. Entrega best-effort.
- Código (`e2b-dev/infra`): las métricas **no salen del guest**. El
  orchestrator del host sondea `GET /metrics` de envd en cada sandbox
  (`orchestrator/pkg/metrics/sandboxes.go`, 100 ms de timeout) y exporta cada
  5 s a un collector del host (`shared/pkg/telemetry/meters.go`), que filtra
  `e2b.*` y escribe en ClickHouse (`embed/kubernetes/config/otel-collector.yaml`).
  El reenvío al endpoint del cliente no está en el código abierto (ASUMIDO:
  exporter por equipo en su collector gestionado). Los logs de envd salen al
  host por una dirección recibida por MMDS (`envd/internal/host/mmds.go`,
  `logs/exporter/exporter.go`). Antes de pausar, envd hace `FlushAndPurge`
  con presupuesto acotado (`envd/internal/api/init.go`): precedente directo
  para un flush en `/suspend`.
- El SDK de E2B no tiene instrumentación OTel propia.

### 6.2 Cómo lo hacen otros

- **Daytona** ([otel-collection](https://www.daytona.io/docs/en/observability/otel-collection.md)):
  el modelo más completo y el más cercano. Dos caminos: telemetría del
  sandbox con endpoint configurado por organización, y **SDK tracing**
  (`otel_enabled`) con variables `OTEL_*` estándar.
- **Modal** ([otel-integration](https://modal.com/docs/guide/otel-integration)):
  por workspace, 12 métricas de contenedor.
- **Cloudflare** ([exporting-opentelemetry-data](https://developers.cloudflare.com/workers/observability/exporting-opentelemetry-data/)):
  trazas y logs, no métricas.
- **Fly** ([metrics](https://docs.fly.io/monitoring/metrics/)): Prometheus
  gestionado; logs por fly-log-shipper.
- **Northflank**: sólo log sinks. **Vercel Sandbox**: nada propio.

Patrón: las métricas de recursos las recoge la plataforma, el destino se
configura por cuenta y el tracing del SDK es opcional.

### 6.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|
| **A. OTel en el SDK + `traceparent` hasta `rayd`** | Extra opcional `rayito[otel]` con sólo la API de OTel (no-op sin provider). Spans `rayito.sandbox.create/connect/kill/pause/resume`, `rayito.code.run`, `rayito.commands.run`, `rayito.files.*`, `rayito.pool.take`; interceptor gRPC que inyecta `traceparent`; `rayd` sólo lo lee y escribe `trace_id/span_id` en sus logs. Puente opcional de métricas desde `get_metrics_history` | No | 6–8 d | $0 |
| **B1. `rayd` → CloudWatch OTLP firmado** | Puerto `TelemetrySink`; POST OTLP/HTTP protobuf+gzip firmado a `monitoring.<r>/v1/metrics` con las 7 gauges cada 60 s; opcional logs de ciclo de vida a `logs.<r>/v1/logs` | No | 7–9 d | $0,50/GB (ASUMIDO) ≈ $0,00014/sandbox-hora |
| B1'. Igual, con **bearer token** (añadida por el crítico) | La API key acotada a un log group se empuja por `ConfigureSandbox` a `rayd` (root, sólo memoria): exporta desde `rayito-base` **sin execution role** | No | +1–2 d sobre B1 | Igual |
| B2. EMF por stdout | Sin red; coste por serie ≈ 2,3 % del cómputo | No | incluido en B | — |
| C. `rayd` → collector OTLP del cliente | Tercer adaptador del mismo puerto; endpoint validado anti-SSRF | No | +4–5 d | Transferencia saliente |
| D. Exportador de flota | Lambda programada que publica `sandbox.running` por imagen | Sí, opcional | 4–6 d | < $0,10/mes |

### 6.4 Recomendación

- **Fase 1: A** (6–8 d). Sin IAM ni egress; cubre lo más pedido (ver en el APM
  qué tarda `create`/`run_code`) y deja la correlación lista. **Puente de
  métricas apagado por defecto** (énfasis del crítico, que comparto): sondear
  mantiene vivos los sandboxes que deberían suspenderse (Q45). Receta
  documentada sin código: CloudWatch Logs → subscription filter → Firehose →
  receptor `awsfirehose` del Collector. Fila 108 → divergente.
- **Fase 2: B1** tras OT1, OT5 y OT9, activado sólo si se pide. **Preferir la
  variante B1' con bearer token** cuando el cliente no quiera dar execution
  role a `rayito-base`: evita T1 (credenciales de IMDS legibles por uid
  1000), que era el principal contra de B1. El riesgo pasa a ser una clave de
  larga vida: rotación y alcance a un log group. VERIFICADO por el crítico
  que logs y métricas aceptan bearer además de SigV4
  ([CloudWatch-OTLPEndpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)).
- **Fase 3: C** sólo para collectors en la VPC del cliente (tras Q46/OT6).
  D sólo con demanda.
- Límites VERIFICADOS: 500 TPS por cuenta, 1 000 puntos por petición. Con
  lotes de 60 s da para unos 30 000 sandboxes simultáneos, compartidos con
  cualquier otro productor OTLP de la cuenta: **backoff con jitter por
  sandbox** para no sincronizar picos tras un resume masivo.
- Nombres `rayito.sandbox.*` con el mismo shape que `e2b.sandbox.*`; alias
  `names='e2b'` en el shim. `e2b.team.*` sólo con D.

### 6.5 Diseño hexagonal

`rayd-core`: `telemetry/config.rs` (`TelemetryConfig { mode: Off |
CloudWatchOtlp | Otlp{endpoint validado, auth} | Emf, interval 15..=300 s,
service_name, extra_attrs ≤ 8 }`, llega por `ConfigureSandbox`, no por el
payload), `telemetry/model.rs` (tipos cerrados: `MetricPoint{kind: enum}`,
`LifecycleEvent`, `RpcSummary`; `AttrKey` es un enum, así que no se puede
exportar una ruta, comando o env por construcción), `telemetry/batcher.rs`
(cola acotada con `dropped_*`, backoff con jitter, reutiliza el sampler de 5 s
de M9, `plan_suspend_flush(budget)`), puerto `OtlpEncoder` para no acoplar el
dominio a `opentelemetry-proto`, puerto `TelemetrySink`.

`rayd`: `adapters/otlp_codec.rs` (opentelemetry-proto sobre prost 0.14, que
ya usa el repo), `adapters/cloudwatch_otlp_sink.rs` (stack de
`signed_http.rs`, SigV4 o bearer), `adapters/otlp_http_sink.rs`
(`FilteringResolver`, sin redirects), `adapters/emf_sink.rs`. Hooks: `/run`
arranca, `/suspend` flush acotado dentro del `SuspendBudget`, `/resume`
invalida conexiones. Interceptor gRPC que lee `traceparent`. `Health.telemetry`
con sólo contadores; sin RPC de ingesta.

SDK: interceptor que inyecta `traceparent/tracestate` (nunca baggage),
fachada `Instrumentation` no-op sin OTel, allowlist de atributos compartida
con el `Logger`, `MetricsBridge`.

### 6.6 Superficie SDK y shim

Python `pip install rayito[otel]`; `Sandbox.create(...,
telemetry=TelemetryExport(mode="cloudwatch", interval_s=60, service_name=...,
names="rayito"))`; `sbx.get_health().telemetry`; `MetricsBridge(sbx).start()`;
`otel=False` por instancia; variables `RAYITO_OTEL_*`. TS espejo con
`@opentelemetry/api` como peer opcional. `telemetry=` contra un `rayd` viejo →
`UnimplementedError` (patrón M9). El shim E2B no añade kwargs: lee
`RAYITO_OTEL_*` y ofrece `names='e2b'` con `sandbox_id`, `team_id` = cuenta,
`build_id` = `imageArn:imageVersion`.

### 6.7 Seguridad

- Sólo números, ids y códigos; nunca comandos, rutas, contenido, envs,
  metadata, tokens ni URLs prefirmadas.
- uid 1000 no tiene vía para escribir en el exportador.
- Con execution role en `rayito-base`, un sandbox malicioso puede falsificar
  o inflar métricas (integridad y coste): política mínima; en caps el riesgo
  desaparece; B1' con bearer evita el rol.
- Modo otlp: SSRF desde root; HTTPS obligatorio, sin loopback/link-local/IMDS.
- Nueva amenaza en `SECURITY.md` ("telemetría").

### 6.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| OT1 | POST OTLP firmado desde `rayd` a `monitoring.<r>/v1/metrics` (parada de B1) | ≈ $0,02 |
| OT2 | Bytes facturados reales por sandbox-hora | ≈ $0,20 |
| OT3 | ¿Extrae la plataforma EMF del stdout sin la cabecera de formato? | ≈ $0,01 |
| OT4 | ¿Llega `traceparent` en la metadata gRPC a `rayd` a través del proxy? | ≈ $0,01 |
| OT5 | Flush en `/suspend`, primer export tras `/resume`, credenciales tras > 55 min | ≈ $0,10 |
| OT6 | Exportador con `allow_internet_access=False` y por conector VPC con endpoints | ≈ $0,10 |
| OT7 | Tamaño del binario y CPU del exportador; que no retrase el idle | ≈ $0,09 |
| OT8 | Endpoint OTLP de logs en el log group/stream de la VM | ≈ $0,01 |
| OT9 | ¿Respeta el endpoint OTLP condiciones IAM como `cloudwatch:namespace`? ¿La acción es `PutMetricData`? | ≈ $0,01 |
| OT10 | Fiabilidad de los logs de runtime (Q56) | ≈ $0,05 |
| OT11 | ¿Salen las métricas OTLP por metric streams? | ≈ $0,05 |
| OT12 | Bearer token: alcance a un log group, rotación, comportamiento al caducar | ≈ $0,01 |

### 6.9 Riesgos

- El guest ve 4 vCPU/8016 MiB en una imagen de 2048 (Q68): añadir el tamaño
  de imagen como atributo.
- Pérdidas en suspend y huecos por pausa (como E2B).
- Coste inesperado con EMF clásico y `sandbox_id` como dimensión.
- Crates OTel de Rust 0.x inestables: sólo tipos proto detrás del puerto.

### 6.10 Fuentes

[E2B OTel](https://docs.e2b.dev/sandbox/otel-telemetry-export.md),
[metrics](https://docs.e2b.dev/sandbox/metrics); e2b-dev/infra
(`metrics/sandboxes.go`, `telemetry/meters.go`, `otel-collector.yaml`,
`envd/internal/logs/exporter/exporter.go`, `envd/internal/host/mmds.go`,
`envd/internal/api/init.go`, `logger/sandbox/logger.go`);
[CloudWatch OTLP endpoint](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html),
[getting started](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPGettingStarted.html),
[metrics-otel-send](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/metrics-otel-send.html),
[precios CloudWatch](https://aws.amazon.com/cloudwatch/pricing/),
[EMF](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Generation_PutLogEvents.html),
[metric streams](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-metric-streams-formats.html),
[subscription filters](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/SubscriptionFilters.html),
[awsfirehosereceiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/awsfirehosereceiver),
[instrumentación de librerías](https://opentelemetry.io/docs/concepts/instrumentation/libraries/);
Daytona, [SigNoz/Daytona (tercero)](https://signoz.io/docs/daytona-monitoring/),
Modal, Cloudflare, Fly, [Northflank](https://northflank.com/docs/v1/application/observe/configure-log-sinks),
[SigNoz/Vercel (tercero)](https://signoz.io/docs/vercel-sandbox-observability/).

## 7. Dominio propio

Fila 110; desbloquea 15 (`allow_public_traffic=True`), 16
(`traffic_access_token` con semántica E2B), 51/52 (`get_host(port)` sin
cabeceras). 17 (`mask_request_host`) y 18 (`https_ports`) siguen imposibles.

### 7.1 Cómo lo hace E2B

VERIFICADO en código:

- Host `${port}-${sandboxId}.${domain}` (`js-sdk/src/connectionConfig.ts`).
- El `client-proxy` (`client-proxy/internal/proxy/proxy.go`) sólo mira la
  etiqueta más a la izquierda (`shared/pkg/proxy/host.go`, `parseHost`): **el
  dominio se ignora**. Resuelve sandbox → nodo en Redis; si está pausado,
  pide auto-resume.
- El proxy del nodo (`orchestrator/pkg/proxy/proxy.go`) valida
  `e2b-traffic-access-token` con comparación en tiempo constante contra un
  token **estático por sandbox**, sólo si `allowPublicTraffic=false`.
- Dominio propio ([custom-domain](https://docs.e2b.dev/sandbox/custom-domain)):
  no es un producto; documentan un Caddy con DNS comodín que hace
  `reverse_proxy {sandboxId}.e2b.app:443`. Funciona **sin credenciales**
  porque por defecto el tráfico público de E2B no pide token.

Diferencia clave: en Lambda MicroVMs **no hay modo sin autenticar**
(`X-aws-proxy-auth` obligatorio), el JWE expira en ≤ 60 min, se acuña con IAM
(50 TPS por cuenta y región) y el proxy de AWS exige SNI y Host = hostname
del MicroVM. El Caddy de E2B no se puede copiar: el proxy necesita
credenciales IAM y un ciclo de refresco.

### 7.2 Cómo lo hacen otros

- **Daytona**: preview privado con `x-daytona-preview-token` y "Custom
  Preview Proxy" open source ([daytona-proxy-samples](https://github.com/daytonaio/daytona-proxy-samples/blob/main/typescript/index.ts)):
  parsea `{port}-{sandboxId}`, pide el token con la API key del cliente, lo
  cachea y lo inyecta. **Es exactamente el patrón que necesita Rayito.**
- **Cloudflare**: `{port}-{id}-{token}.tudominio.com`, exige dominio propio con
  comodín y ruta de Worker ([preview-urls](https://developers.cloudflare.com/sandbox/concepts/preview-urls/)).
- **Fly**: `fly certs add` y `fly-replay` para enrutar a una Machine
  ([dynamic-request-routing](https://docs.fly.io/networking/dynamic-request-routing/)).
- **Modal**: túneles públicos sin auth con URL aleatoria
  ([tunnels](https://modal.com/docs/guide/tunnels)).

### 7.3 Opciones

| Opción | Diseño | ¿Plano propio? | Esfuerzo | Coste |
|---|---|---|---|---|
| **D. Proxy local** `rayito sandbox proxy` | Listener local que inyecta el JWE que ya renueva `TokenRefresher` (como `fly proxy`) | No | 2–3 d | $0 |
| **A. CloudFront + Function + KVS** | `*.sbx.<dominio>` → CloudFront con cert comodín; la Function (viewer-request) parsea `{port}-{alias}`, lee `kvs.get("<alias>:<port>")`, valida el traffic token y llama `cf.updateRequestOrigin` al endpoint con `x-aws-proxy-auth`/`x-aws-proxy-port`. Un único escritor Lambda (DynamoDB Streams + Scheduler) acuña `CreateMicrovmAuthToken(allowedPorts=[{port}])` y refresca a los 40 min | Sí, opcional | 9–13 d | ≈ $20/mes a 10M req + 100 GB |
| B. CloudFront + Lambda@Edge que acuña | Sin KVS; cada contenedor de borde acuña su propio token | Sí | 7–10 d | ≈ $27–30/mes |
| **C. Gateway Rust en Fargate tras ALB** | `rayito-gateway` con TokenCache por (vm, puerto), single-flight, `x-aws-proxy-force-h2` para gRPC, revocación inmediata | Sí, opcional | 13–18 d | ≈ $45–80/mes fijo |
| Descartada: API Gateway HTTP API | URI de integración fija, 30 s, sin WS | — | — | — |

B se descarta: ráfagas contra la cuota de 50 TPS (un token por contenedor de
borde), cold starts cross-region y gRPC no admite Lambda@Edge.

### 7.4 Recomendación

- **Ya: D** (2–3 d, sin infraestructura). Cubre el uso de desarrollo; no
  resuelve la fila 110.
- **Después**, add-on opcional desplegado por el cliente
  (`infra/custom-domain.yaml`), eligiendo entre A y C **según DOM-1**.

**La corrección más importante del crítico en todo el documento** (la
adopto): el modelo (`AWS_API_NOTES.md`) permite que el `authToken` llegue a
**8000 caracteres**, y su longitud real no está medida. Contra eso chocan tres
límites VERIFICADOS de CloudFront
([cloudfront-limits](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html)):
valor de KVS de 1 KB, valor de cabecera custom hacia el origen de 1 783
caracteres (que aplique a los `customHeaders` de `updateRequestOrigin` es A
MEDIR) y store de 5 MB. Con un JWE de ~2 KB cada ruta ocupa 2–3 claves y el
store limita a unos 1 500–2 000 puertos expuestos a la vez por distribución,
un techo de escala que el informe no mencionaba. Por tanto: **DOM-1 va
primero; si el JWE supera ~1,7 KB, A cae y queda C.** C además es la vía para
gRPC/h2 extremo a extremo y puede alojar la API REST y la pasarela de
volúmenes (§8).

Otras correcciones adoptadas: `keepAliveTimeout` dentro de la función tiene
como máximo 120 s; los `customHeaders` no pueden duplicar cabeceras
entrantes, así que la Function **borra** `x-aws-proxy-*` de la petición del
viewer antes de inyectarlas (además es un control de seguridad: impide que el
viewer meta su propio JWE o puerto); el traffic token en `?token=` acaba en
logs de CloudFront y en Referer: intercambio de un solo uso por cookie
HttpOnly y query excluida de logs.

Esquema de host: `{port}-{alias}.{public_domain}` en una sola etiqueta (el
cert comodín cubre un nivel), `alias` = uuid del microvmId, control de acceso
por traffic token y no por oscuridad. **Dominio registrable dedicado**, nunca
un subdominio del corporativo (cookie tossing entre sandboxes).

### 7.5 Diseño hexagonal

`rayd-core`/`rayd`: sin cambios obligatorios (el guest no ve por qué dominio
llega el tráfico). Sólo se extrae a `limits.json` la lista de puertos
reservados (9000 hooks, 8080 `rayd`) como fuente única.

Componente nuevo (projector Lambda en Python para A; crate `rayito-gateway`
para C): dominio `ingress` con `HostnameScheme::parse`, `Route`,
`TokenLease` (`needs_refresh` a 40 min), `AccessDecision` (comparación en
tiempo constante), `RefreshPlan`; puertos `RouteStore`, `EdgeTokenStore`
(KVS en A con troceo si hace falta, memoria en C), `TokenMinter`,
`MicrovmStateReader`, `RateLimiter` (presupuesto de la cuota de 50 TPS,
§8.2). La CloudFront Function es un adaptador "tonto" probado con los mismos
vectores JSON que el dominio.

SDK: `PublicDomainConfig`, `Exposure`; puerto `RouteRegistry`
(`DynamoRouteRegistry` / `NoopRouteRegistry` por defecto).

### 7.6 Superficie SDK y shim

- `sbx.expose(port, public=False) -> Exposure` (traffic token de 32 bytes; se
  guarda sólo su SHA-256), `sbx.unexpose(port)`, `get_host(port)` devuelve
  `{port}-{alias}.{public_domain}` sin cabeceras obligatorias cuando hay
  dominio; `kill()` borra rutas; `RAYITO_PUBLIC_DOMAIN`, `RAYITO_ROUTE_TABLE`;
  CLI `rayito sandbox expose|unexpose|proxy`, `doctor --custom-domain`.
- Shim: con add-on, filas 15, 16, 51, 52, 110 cambian de estado
  (`traffic_access_token` pasa a ser el token del add-on); sin add-on, todo
  sigue como hoy.

### 7.7 Seguridad

1. Cambio de modelo: (vm, puerto) pasa a ser alcanzable desde Internet.
   Visibilidad por defecto `TokenRequired`; `public=True` explícito; WAF.
2. Puertos reservados denegados en tres capas; tokens siempre
   `allowedPorts=[{port}]`, nunca `allPorts`.
3. El JWE nunca sale del borde; leer el KVS equivale a poder acuñar.
4. Sin API de revocación del JWE: borrar ruta; DOM-7 decide si una conexión
   keep-alive sigue sirviendo tras expirar.
5. Amplificación de coste: tráfico público despierta VMs suspendidas y las
   mantiene RUNNING; opción "no enrutar suspendidas".
6. IAM del projector acotado por ARN de imagen (sólo imágenes "web").
7. Nueva amenaza T-ingress en `SECURITY.md`.

### 7.8 Mediciones

| Id | Qué | Coste |
|---|---|---|
| DOM-1 | **Longitud real del JWE** con 1 puerto, rango y 10 puertos. Parada de A | ≈ $0,01 |
| DOM-2 | HTTP/1.1 por CloudFront con `updateRequestOrigin` y cabeceras añadidas; límite de 1 783 caracteres en `customHeaders` | ≈ $0,10 |
| DOM-3 | WebSocket: ¿corre la Function en el upgrade? ¿acepta el proxy de AWS la cabecera en el upgrade? | ≈ $0,05 |
| DOM-4 | SSE y respuestas largas (>120 s, >10 min) | ≈ $0,10 |
| DOM-5 | Propagación del KVS (put → visible, delete → 404) | ≈ $0,05 |
| DOM-6 | ¿Llegan `X-Forwarded-*` a la app? | incluido en DOM-2 |
| DOM-7 | Expiración del JWE con keep-alive CloudFront → origen | ≈ $0,05 |
| DOM-8 | Auto-resume por el dominio propio (GET y POST) | ≈ $0,05 |
| DOM-9 | Rendimiento de subida/bajada por CloudFront | ≈ $0,10 |
| DOM-10 | Conexiones por VM con varios viewers | ≈ $0,10 |
| DOM-11 | ¿El endpoint on.aws ya está detrás de CloudFront? (límite de 2 encadenadas) | ≈ $0 |
| DOM-12 | Ráfaga de acuñado del projector junto al SDK | ≈ $0,50 |
| DOM-13 | Sólo si C: gRPC h2 y WS por ALB → gateway → endpoint | ≈ $1 |

### 7.9 Riesgos

- JWE grande (DOM-1) → A inviable.
- CloudFront sólo habla HTTP/1.1 con orígenes custom; readTimeout ≤ 120 s; WS
  idle 10 min.
- Cuota de 50 TPS compartida entre SDK y projector.
- Latencia de `expose` de varios segundos en A (Stream → Lambda → KVS →
  edges).

### 7.10 Fuentes

[E2B custom-domain](https://docs.e2b.dev/sandbox/custom-domain),
[public-url](https://docs.e2b.dev/network/public-url),
[restrict-public-access](https://docs.e2b.dev/network/restrict-public-access);
e2b-dev/infra (`shared/pkg/proxy/host.go`, `client-proxy/internal/proxy/proxy.go`,
`orchestrator/pkg/proxy/proxy.go`); e2b-dev/E2B (`js-sdk/src/connectionConfig.ts`);
[Daytona preview](https://www.daytona.io/docs/en/preview-and-authentication/),
[custom preview proxy](https://www.daytona.io/docs/en/custom-preview-proxy/);
[Cloudflare production](https://developers.cloudflare.com/sandbox/guides/production-deployment/);
[Fly custom-domain](https://docs.fly.io/networking/custom-domain/);
[CloudFront origin modification](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/helper-functions-origin-modification.html),
[límites](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html),
[restricciones de edge functions](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/edge-function-restrictions-all.html),
[KVS](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/kvs-with-functions.html),
[WebSockets](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-working-with.websockets.html),
[gRPC](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-using-grpc.html),
[orígenes custom](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/RequestAndResponseBehaviorCustomOrigin.html),
[precios CloudFront](https://aws.amazon.com/cloudfront/pricing/),
[Fargate](https://aws.amazon.com/fargate/pricing/),
[ELB](https://aws.amazon.com/elasticloadbalancing/pricing/),
[API Gateway quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html);
`AWS_API_NOTES.md` §3, §7, §10.

## 8. Arquitectura transversal

### 8.1 Un único plano opcional en la cuenta del cliente ("rayito-plane")

Cuatro grupos (control-plane D, custom-domain A/C, templates C, resources D)
y parte de volumes D y secrets proponían **por separado** Lambda + DynamoDB +
EventBridge + CloudFormation. Coincido con el crítico: si se hace, que sea
**un solo stack opcional**, `infra/rayito-plane.yaml`, desplegado por el
cliente en su cuenta y región:

| Pieza | Contenido | La usan |
|---|---|---|
| Tabla DynamoDB única (diseño de tabla única) | Entidades `SANDBOX` (índice, §5), `EVENT` (TTL 7 d), `ROUTE` (dominio propio, §7), `VOLUME` (manifiesto, §1), `BUILD` (estado de templates, §3), `WEBHOOK` (referencia a Secrets Manager) | 1, 3, 5, 7 |
| Bus EventBridge `rayito` | Eventos de ciclo de vida normalizados al esquema de E2B | 5, 6 |
| Lambda **reconciler/projector** (Scheduler, 1–5 min) | Reacuña JWEs de rutas dentro de un presupuesto de la cuota de 50 TPS; sintetiza `killed` por timeout con `ListMicrovms` (nunca tokens); `image prune`; limpieza del índice | 3, 5, 7 |
| Lambda **forwarder/deliverer** | Verifica el MAC de las líneas de `rayd`, escribe eventos, entrega webhooks firmados al estilo E2B | 5 |
| Opcional: **gateway Fargate** | Dominio propio con h2/WS, API REST compatible con E2B, volume content gateway | 1, 5, 7 |

Reglas duras:

1. **El SDK funciona sin el stack**: `create/connect/kill` siguen yendo
   directo a AWS. El stack sólo desbloquea funciones.
2. **Reconstruible**: nada en el stack es fuente de verdad del estado de un
   sandbox (manda `list-microvms`); perderlo degrada funciones, no rompe
   sandboxes.
3. **Por piezas**: cada pieza se activa con un parámetro de la plantilla; el
   índice solo (§5 fase 1) no arrastra Lambdas.
4. Sin datos fuera de la cuenta del cliente. Sin servidor de Rayito.

Matiz propio: no propongo construir el stack completo de golpe. La primera
entrega es sólo la tabla (índice), porque es lo único con valor inmediato y
coste casi cero; las Lambdas llegan con eventos (fase 4) y el gateway sólo si
DOM-1 lo exige.

#### Propuesta de ADR-014 (no se modifica `SPEC.md` en este PR)

> **ADR-014 — Componentes opcionales en la cuenta del cliente**
>
> *Contexto*: `SPEC.md` §4 excluye un "servicio de plano de control" y "un
> almacén del lado cliente". Varias funciones de E2B (pausados por metadatos,
> eventos y webhooks, dominio propio, templates, operaciones de volumen fuera
> del VM) sólo son alcanzables con estado o cómputo fuera del SDK. E2B las
> resuelve con su propio plano de control (Postgres, ClickHouse, Redis,
> proxies de host).
>
> *Decisión*: se distingue entre **servicio hospedado por Rayito** (sigue
> prohibido) y **componentes opcionales en la cuenta del cliente** (se
> permiten). Estos últimos se entregan como un único stack CloudFormation
> (`infra/rayito-plane.yaml`) activable por piezas, con tres invariantes: (1)
> el SDK crea, conecta y termina sandboxes sin ellos; (2) no son fuente de
> verdad del estado de un sandbox (manda la API de Lambda MicroVMs); (3) no
> sacan datos de la cuenta del cliente. Toda función que dependa de ellos se
> documenta como "requiere rayito-plane" y lanza un error explícito sin él.
>
> *Consecuencias*: se actualiza SPEC §4 (líneas de "almacén del lado cliente",
> "servicio de plano de control" y "templates declarativos"). Aumenta la
> superficie de seguridad (nuevas amenazas en `SECURITY.md`) y el soporte.
> Las filas 40, 107, 110 y parte de 26 pasan de "fuera por SPEC" a
> "mapeado con add-on".
>
> *Alternativas rechazadas*: un ADR por función (cuatro ADR-014 en conflicto);
> un servicio SaaS de Rayito (rompe la tesis del producto); no hacer nada
> (deja fuera funciones que los usuarios de E2B usan).

Enmienda de SPEC §4 propuesta (texto, para otro PR):

- Sustituir "Servicio de plano de control: en M1–M5 el SDK llama a AWS
  directamente" por "Servicio de plano de control **hospedado por Rayito**:
  nunca. Componentes opcionales en la cuenta del cliente: permitidos según
  ADR-014".
- En metadatos: "Sin almacén del lado cliente **obligatorio**; índice
  opcional en la cuenta del cliente (ADR-014)".
- En templates: "Templates declarativos compilados en el cliente a
  Dockerfile (ADR-015 si se aprueba §3); sin plano de control de templates".

### 8.2 Fundaciones compartidas (prerrequisito de todo)

1. **RPC `ConfigureSandbox` posterior a `/run`** en lugar de meter
   configuración en `runHookPayload`. Montajes, referencias de secretos,
   telemetría, `hard_cap`, eventos y el token competirían por 4096
   caracteres; el payload puede aparecer en CloudTrail (T4); y es
   incompatible con el pool (ADR-008), donde todo debe aplicarse en `take()`.
   Una única RPC autenticada (x-access-token), idempotente, que aplica
   montajes, secretos, telemetría, gateways y la clave HMAC de eventos. El
   payload queda sólo para lo que debe existir antes del primer byte.
2. **`SuspendBudget` único** en el dominio: reparte los 30 s de `/suspend`
   entre `syncfs` de volúmenes, flush de OTel, eventos y vault, y **siempre
   devuelve 200** (un 500 termina la VM). El arreglo del `sync(2)` sin plazo
   es la primera tarea.
3. **Credential broker único** en `rayd-core`, no cuatro lectores de IMDS:
   fuentes IMDS, empujadas por RPC y `AssumeRole` con session policy por
   función (volúmenes por prefijo, OTel, eventos).
4. **Política "rol de ejecución ⇒ caps"**: cualquier función que dé
   credenciales de rol a `rayd` exige `rayito-base-caps` con `imds_blocked`, o
   credenciales empujadas por RPC. Matiz propio frente al crítico: en
   `rayito-base` permito rol sólo con aceptación explícita
   (`allow_role_in_base=True`) y aviso, porque hay clientes que ya lo usan
   con persistencia y el riesgo (T1) ya está documentado; no lo convierto en
   prohibición.
5. **Cliente saliente único como root**: mount-s3, el gateway de secretos,
   los exportadores OTel y los PutEvents salen fuera de la política de ADR-012
   (uid ≥ 1000). Un solo cliente (SignedHttp + `FilteringResolver`) con
   allowlist de hosts declarada en `Health`, para que
   "`allow_internet_access=False`" siga siendo auditable.
6. **Presupuesto de la cuota de `CreateMicrovmAuthToken`** (50 TPS): la
   comparten SDK, projector del dominio propio y puente de métricas.
   Documentar el reparto; el reconciliador usa `ListMicrovms`.
7. **Una pregunta a AWS para todos**: ¿están cifrados en reposo los
   snapshots de memoria de suspensión, y con qué clave? (SEC-5). Afecta a
   secretos, credenciales de montaje y API keys de OTel.

Esfuerzo de las fundaciones: 5–7 días, sin riesgo de AWS.

## 9. Hoja de ruta priorizada

Numeración de milestones a partir de M11 (M11 ya está reservado para
volúmenes por el estudio EFS; aquí se reordena). Esfuerzos en días-persona
con Python + TS + e2e + docs.

| Orden | Milestone | Contenido | Días | Depende de | Decisión del mantenedor |
|---|---|---|---|---|---|
| 0 | **Campaña de mediciones 1** | VOL-1/2/4, DOM-1, TPL-1/2/5/6, RES-1/2/10/13, OT1/4, SEC-3, CP-7 | 2–3 | — | Aprobar gasto ≈ $3 |
| 1 | **M11 — Fundaciones** | `syncfs` acotado, `SuspendBudget`, `ConfigureSandbox`, credential broker, política de roles, cliente saliente único, ADR-014 + enmienda de SPEC §4 | 5–7 | — | **Sí**: ADR-014 y SPEC |
| 2 | **M12 — Victorias rápidas** | Resources C (docs, fila 82); `rayito sandbox proxy` (dominio D) | 3–5 | — | No |
| 3 | **M13 — Cliente sin plano** | Secretos 1+2 (CRUD SM + push a env); OTel A (spans + `traceparent`) | 14–18 | M11 | Sí: aceptar "secreto visible al sandbox" como nativo |
| 4 | **M14 — Índice** | Control-plane A (tabla opt-in; primera pieza de rayito-plane) | 5–7 | M11 (ADR-014) | Incluida en ADR-014 |
| 5 | **M15 — Tamaños y buckets** | Resources A (catálogo 1/2/4 GB); Volumes A (mount-s3 divergente) | 22–27 | M11, mediciones VOL/RES | Sí: política `baseline` por defecto; `Volume` divergente |
| 6 | **M16 — Eventos y telemetría del guest** | Eventos E o B (auditoría); eventos C (stdout + MAC + forwarder + reconciliador); OTel B1/B1' | 19–26 | M14, CP-1/4/5, OT5/9 | Sí: primeras Lambdas de rayito-plane |
| 7 | **M17 — Templates** | Templates A (DSL, compilador, `rayd` start/ready, shim) | 25–35 | M11, TPL-1/2/5/6 | **Sí**: ADR-015 y quitar el no-objetivo |
| 8 | **M18 — Gateway de secretos** | Secretos 4 con allowlist de métodos/rutas | 10–12 | M13, SEC-3/7 | No |
| 9 | **M19 — Dominio propio** | A si DOM-1 ≤ ~1,7 KB y DOM-2/3/7 pasan; si no, C | 9–18 | M14, DOM-* | Sí: coste fijo si es C |
| 10 | **M20 — Volumen POSIX (experimental)** | S3 Files o EFS + pasarela D | 20–30 | Q46, VOL-8/11 | **Sí**: aceptar combinación no soportada por AWS o quedarse en D |
| — | Opcionales con demanda | Templates B (CodeBuild), control-plane D (API REST), OTel C/D, resources D | — | — | Sí |

Total: **150–200 días-persona**. Coste de mediciones: ≈ $8–12 más VPC para
M20.

Por qué este orden: primero lo que no depende de AWS ni de gobernanza
(fundaciones y documentación), después lo que vive sólo en el cliente
(secretos CRUD, spans), luego la primera pieza del plano opcional con valor
inmediato (índice), y al final lo de alto esfuerzo o alto riesgo (templates,
dominio propio, NFS). La campaña de mediciones 1 va antes que todo porque
tres mediciones baratas son de parada binaria (VOL-1 para volúmenes, DOM-1
para CloudFront, TPL-1/2 para el diseño de logs de templates).

Quedan fuera, para "lo imposible lo vemos después": `iam=`/`Secret.iam_token`
(con el análogo parcial de STS por push, §2.4), inyección transparente de
`network.rules`, `mask_request_host`, `https_ports`, snapshots de un VM
vivo.

## Resultados de la primera campaña de mediciones (2026-10)

Medido el 2026-09-30 en la cuenta de pruebas (us-east-1, ARM64), con
imágenes sonda desechables construidas fuera del repo y borradas al terminar:
una imagen mínima (al2023-minimal + un servidor de hooks en Python con un
endpoint de ejecución, sin `rayd`), la imagen de producto con
`RAYITO_ALLOW_ROOT=1` (misma `rayd` de `main`, 2048 y 512 MiB) y esa misma
imagen con `mount-s3` y `additionalOsCapabilities: ["ALL"]`. Un rol de
ejecución desechable con acceso a un prefijo S3 de pruebas y a
`cloudwatch:PutMetricData`. Coste estimado de la campaña: **≈ $0,5–0,7**
(sobre todo el mínimo de una semana de storage de 8 versiones construidas; el
cómputo de VMs no llega a $0,10). Cada fila tiene su Q en `AWS_API_NOTES.md`
§16 (Q79–Q94) con el detalle.

| Id | Resultado | Implicación para el diseño |
|---|---|---|
| VOL-1 (Q79) | El kernel del guest (6.1, AL2023) trae `fuse`, `fuseblk`, `fusectl`, `nfs` y `nfs4` en `/proc/filesystems` en todas las imágenes. En la imagen por defecto **no existe `/dev/fuse`** y root (`CapEff` `a80425fb`, sin `CAP_SYS_ADMIN`) recibe `EPERM` al montar incluso un tmpfs. Con `ALL`, `/dev/fuse` existe (10,229, `0666`), root monta FUSE y tmpfs, y uid 1000 abre `/dev/fuse` pero su `mount(2)` da `EPERM` | **La opción A de §1 sigue viva, sólo en `rayito-base-caps`.** Confirma el diseño "monta `rayd` (root) y nunca el sandbox"; `mounts=` sin caps debe fallar con `UnimplementedError`, como ya propone §1.6 |
| VOL-2 (Q80) | `mount-s3` está en los repos de AL2023 (`mount-s3-1.22.3-1.amzn2023`, arrastra `fuse` 2.9.9, `fuse-common` y `fuse-libs`); no hace falta el RPM externo. Code install **+22,4 MB** frente a la misma imagen sin él; memoria del snapshot sin cambio (dentro del ruido de ±13 MB de Q50). Montaje (root, mismo prefijo, 20 veces) **p50 0,117 s, p95 0,153 s**; primer `ls` 15–22 ms; daemon ≈ 12 MB de RSS. Un `>>` sobre un fichero existente da `EPERM` (sin append, como dice SEMANTICS.md). **Hallazgo lateral**: cada `mount-s3` en modo daemon dejó un proceso `<defunct>` colgado de `rayd` (PID 1): `rayd` no recoge huérfanos reasignados | Coste de imagen y de montaje despreciables: el montaje cabe en `ConfigureSandbox` sin presupuesto especial. El adaptador `FuseDaemon` debe lanzar `mount-s3 --foreground` como hijo propio y esperarlo, y `rayd` necesita recoger zombis huérfanos como init (arreglo independiente, entra en las fundaciones de §8.2) |
| VOL-4 (Q81) | Con un lector abierto sobre un objeto de 32 MiB y una escritura secuencial en curso (1 MiB/s, multipart de `mount-s3`), pausas de **60 s y 10 min**: ni un error de E/S; la escritura cierra 0,2 s después del resume, el objeto aparece en S3 con los 16 MiB completos, el montaje y el daemon siguen vivos, las lecturas y escrituras nuevas funcionan. En una segunda corrida los mismos fd siguen leyendo y escribiendo 40 operaciones después del resume sin error. **70 min** (la pausa cruza la caducidad de las credenciales): igual, sin errores, y IMDS entrega **credenciales nuevas** tras el resume, que `mount-s3` recoge solo. Suspensión 1,0–1,2 s, resume 0,4–1,1 s | FUSE + suspend/resume funciona sin remontar incluso con pausas de más de una hora: la `ResumePolicy` de FUSE ("sonda de 2 s y relanzar daemon") queda como red de seguridad, no como camino normal. Con credenciales de IMDS no hace falta renovar a mano; el *credential broker* de §1.7 sólo es necesario para la session policy por montaje, y entonces sí debe renovar en `/resume` |
| DOM-1 (Q82) | El `X-aws-proxy-auth` es un JWE compacto de 5 partes (`alg: dir`, `enc: A256GCM`, `kid`, sin clave cifrada ni compresión) de **823 caracteres** con 1 puerto, con un rango, con 10 y con 50 puertos, a 1 y a 60 min (767 con `allPorts`): la longitud **no depende** del número de puertos ni de la expiración | **A (CloudFront + Function + KVS) no cae por DOM-1**: 823 B caben en una sola clave de KVS (1 KB) y en el límite de 1 783 caracteres de cabecera; una ruta = una clave, así que el store de 5 MB admite del orden de 5 000 rutas y no 1 500–2 000. Siguen abiertos DOM-2/3/7 antes de elegir A |
| TPL-1 (Q83) | **Contradice Q57**: el log group de la imagen recibe la salida completa de BuildKit (`#N [k/n] RUN …`, stdout y stderr de cada RUN) en un stream propio, tanto en un build correcto como en uno que falla; en el fallido aparece el comando, su salida y `exit code: 3`, con `stateReason` "The container image build failed." a los 73 s. Todas las líneas llegan **de golpe al terminar** `docker build` (mismo timestamp), no en vivo. Además hay un stream por VM de snapshot (dos: Graviton 3 y 4) y otro por VM de `/validate` con el stdout del `CMD`. Cuota observada: **10 builds concurrentes** por cuenta (`ServiceQuotaExceededException` en el undécimo) | **La opción A de §3 da logs de calidad E2B sin CodeBuild**: B deja de hacer falta por los logs. El SDK no puede hacer streaming en vivo de los pasos; debe leer el stream al terminar el build y mostrar el paso que falló. El control de concurrencia del SDK se dimensiona con 10 |
| TPL-2 (Q84) | `codeArtifact.uri` **sólo acepta `s3://<bucket>/<key>`**: una URI de ECR (`<registro>/<repo>:<tag>`, ARN de repositorio, URL `https` del registro) o de `public.ecr.aws` da `ValidationException` "CodeArtifact URI must be a valid S3 URI…" antes de crear nada. La documentación del modelo (`service-2.json`) es incorrecta | ECR sólo puede entrar como `FROM <ecr>@sha256` dentro del Dockerfile del zip (B sigue siendo posible como adaptador, nunca como artefacto directo). `from_base_image()` y el compilador de A trabajan siempre con zips en S3 |
| TPL-5 (Q85) | `/ready` con **500** o **404** falla el build **en la primera llamada**, sin reintento (el servidor contó una sola llamada por VM), con `stateReason` "Ready hook check failed: the application returned a server error (HTTP 5xx) response" o "…client error (HTTP 4xx) response"; el build acaba `FAILED` en 94–105 s frente a 114 s del correcto | `rayd` sólo debe devolver 5xx en `/ready` cuando el fallo es definitivo (503 sigue siendo "reintentar", Q de §8); el `stateReason` distingue 4xx de 5xx, así que el SDK puede mapear a `BuildException` con un motivo legible |
| TPL-6 (Q86) | Fallo diferido extremo a extremo: un RUN envuelto que falla guarda su salida y un marcador; el siguiente RUN comprueba el marcador y no ejecuta nada; el `CMD` imprime los logs de pasos en `/ready` y devuelve 500. Resultado: la salida del paso aparece en los streams de las dos VMs de snapshot y el build termina `FAILED` con el `stateReason` de 5xx | Funciona, pero con TPL-1 **ya no hace falta** para ver qué RUN falló: queda sólo para pasos que deben ejecutarse en la VM real (start/ready cmd), no para la DSL entera |
| RES-1 (Q87) | Aceptados **512, 1024, 2048, 4096 y 8192 MiB**; 256, 3072, 10240 y 16384 dan `ValidationException` al crear ("Supported memory sizes in MiB are: [512, 1024, 2048, 4096, 8192]", ligado a la imagen base `al2023-1`), sin build ni coste. El modelo limita `resources` a un elemento (`max: 1`) | Catálogo cerrado de 5 tamaños: el resolvedor de §4 valida en local y redondea hacia arriba; un tamaño por versión, así que B (tamaños como versiones) sigue descartada |
| RES-2 (Q88) | El guest ve **4 veces la memoria nominal** y **memoria/512 vCPU**: 512 → 1 vCPU / 1 989 MiB; 1024 → 2 / 3 998; 2048 → 4 / 8 016; 4096 → 8 / 16 052; 8192 → 16 / 32 123. Disco raíz ext4 de 8,3 GB hasta 2048, 16,7 GB a 4096 y 33,6 GB a 8192; `/dev/shm` 64 MiB siempre; cgroup v2 sin montar (`0::/system.slice/app`, sin `memory.max`/`cpu.max`); mezcla de Graviton 3 y 4. El snapshot de memoria de la misma imagen mínima crece con el tamaño (446 MB a 512, 503 a 1024, 597 a 2048, 782 a 4096, 1 146 a 8192) | El guest ve el **pico**, no la línea base: `SandboxInfo.cpu_count/memory_mb` del shim no sirve para elegir tamaño y el resolvedor debe leer el tamaño de la versión de imagen (o `RAYITO_BASELINE_MEMORY_MIB`). Cada tamaño cuesta más storage aun con la misma imagen: refuerza publicar por defecto sólo 1/2/4 GB |
| RES-10 (Q89) | **Sí**: la imagen de producto construye a 512 MiB (258 s; snapshot de memoria 759–761 MB frente a 907–912 MB a 2048) y funciona: `create` 12 s, celda de pandas 0,11 s, gráfico 0,26 s, pausa y resume correctos. El guest ve 1 vCPU y 1 989 MiB; reservar 2 GiB en numpy da `MemoryError` sin tumbar el kernel | El tamaño `512mb` entra en el catálogo de A; conviene documentar que la RAM visible (≈ 2 GB) es pico facturado como exceso |
| RES-13 (Q90) | Un rol con `lambda:RunMicrovm` permitido sólo sobre el ARN de la imagen de 1024 MiB lanza esa imagen y recibe `AccessDeniedException` ("…not authorized to perform: lambda:RunMicrovm on resource: …microvm-image:<nombre>") sobre las de 4096 y 8192 | La restricción por tamaño vía IAM por ARN de imagen funciona: es la mitigación de coste de §4.7 y hace innecesario el broker D para límites simples |
| OT1 (Q91) | Un POST OTLP/HTTP protobuf firmado con SigV4 (servicio `monitoring`) desde el guest, como root y con las credenciales IMDS del execution role, a `https://monitoring.us-east-1.amazonaws.com/v1/metrics` responde **200 en 29–63 ms**. La acción autorizada es `cloudwatch:PutMetricData` sobre el recurso `arn:aws:cloudwatch:<región>:<cuenta>:dataset/default`; una política con condición `cloudwatch:namespace` **deniega** (403) aunque se envíe la cabecera `x-amz-cloudwatch-namespace`. Las métricas no aparecen en `ListMetrics` (no van a un namespace clásico). En `rayito-base` sin caps, uid 1000 también obtiene las credenciales y el 403/200 es el mismo | **B1 es viable**. La política mínima no puede acotarse por namespace (responde a OT9: sólo por `dataset/default`), así que con execution role en `rayito-base` un sandbox puede escribir métricas arbitrarias: refuerza B1' (bearer) o caps. La consulta de lo exportado va por la vía OTLP/PromQL de CloudWatch, no por `GetMetricData` clásico: documentarlo |
| OT4 (Q92) | `traceparent`, `tracestate` y `baggage` llegan intactos a un servidor gRPC (grpcio) dentro del VM a través del proxy con `x-aws-proxy-force-h2`, y a un servidor HTTP/1.1 sin ella; el proxy sólo añade `x-amzn-requestid` y quita `x-aws-proxy-*`. `grpc-trace-bin` no llegó, pero el cliente grpcio filtra las claves `grpc-*`, así que no es concluyente | La propagación de la opción A de §6 (interceptor que inyecta `traceparent`, `rayd` que lo lee) funciona sin cambios en el proxy; usar W3C y nunca `grpc-trace-bin` |
| SEC-3 (Q93) | Como uid 1000, en la imagen por defecto y en `ALL`, antes y después de una pausa: `/proc/1/{environ,maps,mem,auxv,stack,syscall,io,fd}` de `rayd` → `EACCES`; `process_vm_readv` y `ptrace` (ATTACH y SEIZE) → `EPERM`; `kill(1, 0)` → `EPERM`. Legibles: `cmdline` y `status` (Uid 0, `NoNewPrivs` 1). Sin Yama; `core_pattern` a systemd-coredump y `suid_dumpable` 0; sin gdb/strace. En cambio, el `environ` de otros procesos de uid 1000 (sidecar y kernel) **sí** es legible | **Pasa el gate de la opción 4 de §2**: un secreto que viva sólo en la memoria de `rayd` no es legible por el sandbox. Nunca debe pasar al entorno del sidecar ni del kernel, y ningún proceso root debe llevar secretos en `argv` (`cmdline` es legible y no hay `hidepid`; p. ej. el `argv` de `mount-s3` con bucket y prefijo) |
| CP-7 (Q94) | uid 1000 **no puede escribir** en el stdout/stderr de `rayd` (`/proc/1/fd/1` y `/2`: `EACCES`, ni siquiera `readlink`), ni en `/dev/console`, `/dev/kmsg`, `tty1`, `ttyS0` o `hvc0`, ni en los pipes del sidecar. El stdout de un proceso del sandbox es un pipe hacia `rayd` que vuelve por RPC y no se reenvía a CloudWatch: ningún marcador escrito por uid 1000 apareció en el stream de runtime | Los eventos de ciclo de vida por stdout de `rayd` (opción C de §5) no son falsificables desde el sandbox por esta vía; la MAC por evento sigue siendo necesaria para el tramo CloudWatch → forwarder, no contra el guest |

Mediciones de la campaña que no se omitieron: ninguna necesitó VPC ni NAT. Lo
que queda por medir de estos grupos va a la campaña 2 (DOM-2/3/7, VOL-3/5, TPL-3/4/11/12, RES-3/4/5, OT5).

Cambios que esta campaña introduce en las recomendaciones anteriores:

1. §3: la decisión A frente a B ya no depende de los logs (TPL-1): **A basta**;
   B queda sólo para caché de capas (TPL-3) y registries privados.
2. §7: **DOM-1 no descarta A**; el orden M19 "A si DOM-1 ≤ ~1,7 KB" se cumple
   con margen (823 B).
3. §1 y §8.2: añadir a las fundaciones que `rayd` recoja zombis huérfanos como
   PID 1 (lo exige cualquier daemon hijo, empezando por `mount-s3`).
4. §4: el guest ve el pico (4× la memoria nominal); el shim no puede derivar
   el tamaño de lo que ve el guest.
5. §2 y §6: la rotación de credenciales de IMDS tras una suspensión larga
   (Q1, SEC-8) queda medida de paso en VOL-4: IMDS entrega credenciales
   nuevas tras el resume.
6. §6: OT9 queda respondida en negativo (sin acotación por namespace), lo que
   inclina B1 hacia la variante B1' con bearer token.
