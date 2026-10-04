# Volúmenes EFS para Rayito (M11): viabilidad y diseño

Investigación de sólo lectura del 2026-09-30, **sin código ni recursos AWS
creados**. Fuentes: `AWS_API_NOTES.md` (§2, §4, §7, §8, §9, §15, §16 Q44, Q46,
Q47, Q48, Q53, Q66), `docs/aws-api/service-2.json` y `model_summary.md`,
`ARCHITECTURE.md` (ADR-008, ADR-009, ADR-012 y sus adendas, checklists de
`/suspend` y `/resume`), `SECURITY.md` (T1, T8, T15, T17), `SPEC.md` §4,
`docs/site/docs/{persistence,network,e2b-parity}.md`, `infra/{iam,egress-connector}.yaml`,
el modelo de servicio de EFS de botocore 1.43.105 (`efs` 2015-02-01) y la
documentación pública de AWS y E2B citada en cada fila.

Convención de etiquetas, igual que `AWS_API_NOTES.md`:

- **VERIFICADO**: lo dice literalmente la documentación oficial o el modelo
  de servicio, o ya está medido en este repo (se cita la fila Q).
- **ASUMIDO**: inferencia razonable a partir de Linux/NFS o de otros
  entornos de AWS; no está documentado para MicroVMs.
- **A MEDIR**: sólo una medición en AWS real lo decide. Cada una es una
  pregunta numerada de §9 (EFS-1–EFS-20, continúan la numeración de §16).
- *(tercero)*: afirmación de un artículo no oficial; no cuenta como
  verificación.

Regla del repo que condiciona todo el documento (`openspec/project.md`, regla
1): **ningún parámetro de API de AWS se usa si no está en `AWS_API_NOTES.md`
o en `docs/aws-api/`**. Hoy EFS no está en ninguno de los dos; el primer
entregable de M11 es añadir el contrato de EFS a `AWS_API_NOTES.md` (§19
nueva) desde el modelo de servicio, antes de cualquier código.

## 0. Hallazgos que cambian el planteamiento

| Hallazgo | Evidencia | Implicación |
|---|---|---|
| **Los MicroVMs no tienen un `FileSystemConfig` como las funciones Lambda** | `service-2.json` de `lambda-microvms` no tiene ningún campo de sistema de ficheros, EFS ni montaje (búsqueda de `efs`, `nfs`, `mount`, `FileSystem`: cero resultados); `RunMicrovmRequest` sólo trae conectores de red (§2) | El montaje lo tiene que hacer el guest (`rayd` como root), no la plataforma. No hay atajo gestionado |
| **La plataforma documenta que `ALL` permite montar sistemas de ficheros** | [microvms-images.html, "Operating system capabilities"](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html): "Elevated capabilities enable operations such as mounting filesystems, creating network namespaces, or running eBPF programs"; Q47: en `rayito-base-caps` root tiene `CapEff` completo, incluido `CAP_SYS_ADMIN` | El privilegio de montaje existe en `rayito-base-caps` (VERIFICADO) y **no** en `rayito-base` (Q20/Q47: sin `sys_admin`). EFS sólo puede ser una función de la variante caps |
| **Un conector VPC de egress llega a recursos privados de tu VPC** | [microvms-networking.html, "Outbound connectivity"](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html): "To connect MicroVMs with resources in your private VPCs – such as RDS, ElastiCache, internal APIs … create a Lambda Network Connector"; "outbound traffic is subject to security group rules and network ACLs" | Un mount target de EFS (TCP 2049 en tu VPC) es alcanzable en principio (VERIFICADO por analogía documental: EFS no aparece nombrado). **Q46 sigue sin medir**: Rayito nunca ha desplegado un conector propio |
| **El kernel del guest no tiene módulos cargables** | Q48: kernel 6.1 amzn2023 "sin módulos cargables", `xt_owner` ausente | Si el cliente NFSv4.1 no está compilado dentro del kernel, **EFS es imposible** en MicroVMs (y también FUSE para la opción B). Es la primera pregunta de parada (EFS-1) |
| **Un tercero afirma haber montado EFS en un MicroVM** | [dev.to, "AWS Lambda MicroVMs : bastions serverless… + EFS"](https://dev.to/aws-builders/aws-lambda-microvms-bastions-serverless-sandboxes-ia-et-cloud9-diy-avec-vs-code-oss-efs-3pa7) *(tercero)*: `mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 "${EFS_DNS}:/"` como root en el entrypoint, con `nfs-utils` y `additionalOsCapabilities ["ALL"]`, conector VPC, nombre DNS del sistema de ficheros; sin TLS, sin IAM, sin access point en el montaje, sin datos de suspend/resume | Indicio fuerte de que EFS-1–EFS-5 salen bien, pero **no** cubre TLS, IAM, access points ni suspend/resume, que son justo lo que el diseño seguro necesita |
| **Access points e IAM exigen el mount helper (`amazon-efs-utils`) y TLS** | [efs-access-points.html](https://docs.aws.amazon.com/efs/latest/ug/efs-access-points.html): "You use the EFS mount helper when mounting a file system using an access point … include … the `tls` mount option"; [iam-access-control-nfs-efs.html](https://docs.aws.amazon.com/efs/latest/ug/iam-access-control-nfs-efs.html): "You must use the EFS mount helper … in order to use IAM authorization" | Un `mount -t nfs4` pelado (el del tercero) **no** sirve para aislar inquilinos. Hace falta `efs-utils` (Python + `efs-proxy` en Rust) dentro de la imagen, sin `systemd` (EFS-7, EFS-8) |
| **El `/suspend` actual llama a `sync(2)` sin plazo** | `crates/rayd/src/hooks/mod.rs`, `flush_page_cache`: `spawn_blocking(libc::sync)` esperado sin `timeout` | Con un montaje NFS `hard` colgado (mount target inalcanzable), `sync(2)` bloquea, `/suspend` agota su `suspendTimeoutInSeconds` de 30 s y la VM probablemente termina (EFS-13). M11 tiene que cambiar ese paso aunque no se implemente nada más |
| **S3 Files (GA 2026-04) es EFS por debajo** | [s3-files.html](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html): "Built using Amazon EFS", NFS 4.1/4.2, mount targets, access points, TLS; [s3-files-attach-compute.html](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-attach-compute.html): "must run in the same Amazon VPC … through mount targets on NFS port 2049"; compute soportado: EC2, Lambda, EKS, ECS (MicroVMs no aparece) | La opción B tiene **exactamente** las mismas dependencias de red y kernel que EFS. No es una alternativa de viabilidad, sólo de modelo de datos |
| **Las condiciones IAM de cliente NFS son sólo cuatro** | [iam-access-control-nfs-efs.html](https://docs.aws.amazon.com/efs/latest/ug/iam-access-control-nfs-efs.html): `aws:SecureTransport`, `aws:SourceIp`, `elasticfilesystem:AccessPointArn`, `elasticfilesystem:AccessedViaMountTarget`; "Any other condition keys are not enforced" | No hay ABAC por etiqueta para el cliente NFS: el aislamiento entre inquilinos se expresa con ARNs de access point concretos o con un sistema de ficheros por inquilino, y el execution role es la identidad (como en T15) |

## 1. Veredicto de viabilidad por requisito

| # | Requisito | Veredicto | Estado | Fuente / medición |
|---|---|---|---|---|
| R1 | Tráfico TCP 2049 del guest a un mount target en la VPC del cliente | Sí, por conector VPC de egress | VERIFICADO en docs (recursos privados de la VPC, SG y NACL aplican); **A MEDIR** en Rayito (Q46 → EFS-3) | [microvms-networking.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html) |
| R2 | Internet **y** VPC a la vez (pip, git, APIs + EFS) | Desconocido. `egressNetworkConnectors` admite hasta 10 ARNs (§2), pero nada documenta cómo se enruta con `INTERNET_EGRESS` + un conector VPC | **A MEDIR** (EFS-4). Si sólo vale uno, internet sale por un NAT gateway de la VPC del cliente (coste §6) | §2, §11 de `AWS_API_NOTES.md` |
| R3 | Resolver el nombre DNS del mount target (`fs-….efs.<región>.amazonaws.com`, zona privada de la VPC) | Desconocido: los resolvedores del guest son locales (`127.0.0.2`, `169.254.53.53`, Q66); no se sabe si reenvían al Route 53 Resolver de la VPC cuando hay conector | **A MEDIR** (EFS-5). Mitigación sin DNS: `mounttargetip=` de `efs-utils` con la IP que el SDK obtiene de `DescribeMountTargets` | [efs-mount-helper.html](https://docs.aws.amazon.com/efs/latest/ug/efs-mount-helper.html) ("Mount target IP address"); *(tercero)* montó por DNS |
| R4 | Cliente NFSv4.1 en el kernel del guest | Desconocido; sin módulos cargables (Q48) sólo vale si está compilado dentro | **A MEDIR, criterio de parada** (EFS-1) | Q48 |
| R5 | Privilegio para `mount(2)` | Sí en `rayito-base-caps` (root con `CAP_SYS_ADMIN`); no en `rayito-base` | VERIFICADO (docs + Q47); **A MEDIR** el `mount` real dentro del contenedor de la app (EFS-2) | microvms-images.html; Q47 |
| R6 | uid 1000 (el código del usuario) nunca monta ni ve credenciales | Sí: uid 1000 tiene `CapEff=0` y `NoNewPrivs=1` (Q47); IMDS bloqueado para 1000–65535 en caps (Q48); `rayd` monta como root | VERIFICADO (Q47, Q48) | T1 |
| R7 | Autenticación IAM de cliente NFS con el execution role leído de IMDSv2 por root | Probable: `efs-utils` lee IMDSv2; el MicroVM sirve `…/security-credentials/execution_role` (§9) | ASUMIDO; **A MEDIR** que `efs-utils` acepte el IMDS del MicroVM (nombre del rol, sin `placement/*`) (EFS-6) | [efs-utils README](https://github.com/aws/efs-utils); §9 |
| R8 | TLS en tránsito | `efs-proxy` (efs-utils ≥ 2.0) o `stunnel`; `amazon-efs-utils` soporta Amazon Linux 2023 | VERIFICADO en docs; **A MEDIR** instalación en `al2023-minimal` ARM64 y funcionamiento **sin `systemd`** (EFS-7, EFS-8) | [efs-mount-helper.html](https://docs.aws.amazon.com/efs/latest/ug/efs-mount-helper.html), efs-utils README |
| R9 | Aislamiento por volumen: raíz forzada y uid/gid 1000 | Access point con `PosixUser{Uid,Gid}` y `RootDirectory{Path,CreationInfo}` | VERIFICADO (modelo `efs` `CreateAccessPoint`; docs de access points) | botocore 1.43.105 |
| R10 | Aislamiento entre inquilinos | Política del sistema de ficheros + identidad del execution role; la clave `elasticfilesystem:AccessPointArn` es la única que distingue volúmenes | VERIFICADO (lista cerrada de claves); consecuencia: un rol por inquilino (§4) | iam-access-control-nfs-efs.html |
| R11 | Suspender y reanudar con el volumen montado | Las conexiones salientes no locales mueren en resume (§15, VERIFICADO); el cliente NFS reconecta solo a `efs-proxy` por loopback (sobrevive, §15), y `efs-proxy` debe reconectar y reautenticar hacia fuera | ASUMIDO; **A MEDIR** con pausas de 60 s, 10 min y > 55 min (EFS-11, EFS-12) | §15; microvms-best-practices.html ("`/resume` for connection re-establishment") |
| R12 | `/suspend` siempre 200 y a tiempo con NFS | Hoy no está garantizado (`sync(2)` sin plazo) | Hallazgo de código; **A MEDIR** el comportamiento real con el mount target cortado (EFS-13) | `hooks/mod.rs` |
| R13 | Rendimiento útil (latencia y MB/s) | EFS Regional Elastic: ~1 ms lectura, ~2,7 ms escritura, 1 500 MiB/s por cliente con efs-utils ≥ 2.0 (500 MiB/s sin él); el tope de 4 MB/s del endpoint no aplica (egress no pasa por el proxy, Q53) | VERIFICADO en docs; **A MEDIR** desde el MicroVM (EFS-9) | [performance.html](https://docs.aws.amazon.com/efs/latest/ug/performance.html) |
| R14 | Cuotas | 10 000 access points por sistema de ficheros (ajustable), 25 000 conexiones por sistema (fijo), 1 mount target por AZ | VERIFICADO | [limits.html](https://docs.aws.amazon.com/efs/latest/ug/limits.html) |
| R15 | Escala del conector (IPs de subnet por MicroVM, ENIs) | No documentado | **A MEDIR** (EFS-20) | microvms-networking.html (ENIs gestionados por el operator role) |

**Veredicto global: viable condicionado.** Ningún documento oficial lo
prohíbe, dos lo sostienen (montaje con `ALL`, conector VPC a recursos
privados) y un tercero lo ha hecho sin TLS ni IAM. Tres preguntas deciden
entre "se hace" y "se abandona" antes de escribir código: EFS-1 (NFSv4.1 en el
kernel), EFS-3 (conector VPC propio operativo) y EFS-8 (`efs-utils` con TLS + IAM
+ access point sin `systemd`). EFS-11/EFS-13 deciden si la experiencia con
suspend/resume es aceptable o si el volumen tiene que declararse
"no sobrevive a `pause()`".

## 2. Qué es un `Volume` en E2B y cómo encaja

E2B ([docs.e2b.dev/volumes](https://docs.e2b.dev/volumes),
[manage](https://docs.e2b.dev/volumes/manage),
[mount](https://docs.e2b.dev/volumes/mount),
[read-write](https://docs.e2b.dev/volumes/read-write),
[limitaciones de la beta](https://docs.e2b.dev/faq/volumes-beta-limitations)),
en beta privada:

- `Volume.create(name)` (letras, números y guiones), `Volume.connect(volume_id)`,
  `Volume.list()`, `Volume.get_info(volume_id)`, `Volume.destroy(volume_id) -> bool`;
  campos `volume_id` / `name`.
- `Sandbox.create(volume_mounts={"/mnt/my-data": volume})`: el valor es un
  `Volume` o su nombre. "Mounts are set at sandbox creation. You cannot add or
  replace them through resume, connect, or fork."
- Operaciones de fichero sin sandbox: `volume.read_file`, `write_file`,
  `make_dir`, `list`, `remove`.
- Limitaciones declaradas: `flock`/`fcntl` pueden colgarse, metadatos sin
  caché (lento con muchos ficheros pequeños), permisos no aplicados entre
  usuarios Linux, `ESTALE` con mucho rename, borrar un volumen montado puede
  impedir reanudar un sandbox pausado, sin snapshots ni montajes de sólo
  lectura, sólo regiones de EE. UU. y la UE.

Correspondencia con EFS:

| E2B | Rayito sobre EFS | Notas |
|---|---|---|
| `Volume` | un **access point** del sistema de ficheros del operador, `RootDirectory.Path = /rayito-volumes/<name>`, `PosixUser` 1000:1000, `CreationInfo` 1000:1000 `0750`, etiqueta `rayito:volume=<name>` | `Path` admite como mucho 4 componentes y 100 caracteres (modelo `efs`) |
| `volume_id` | `AccessPointId` (`fsap-…`) | |
| `Volume.create(name)` | `CreateAccessPoint` (`ClientToken` = hash del nombre, idempotente) | la unicidad del nombre la comprueba el SDK con `DescribeAccessPoints` antes de crear (carrera documentada) |
| `Volume.list` / `get_info` | `DescribeAccessPoints(FileSystemId)` filtrado por la etiqueta | |
| `Volume.destroy` | `DeleteAccessPoint`; **los datos del directorio no se borran** | honesto y documentado; el borrado de datos exige un montaje (ver §3.4, "purga") |
| `volume.read_file` y compañía | `UnimplementedError` en el shim | no hay plano de datos fuera de un VM (sin servicio de plano de control, `SPEC.md` §4); se podría servir con un sandbox efímero, pero sería una aproximación que cuesta un VM por llamada |
| `volume_mounts` | `Sandbox.create(volumes={path: EfsVolume})` + el conector VPC | exige `rayito-base-caps`, execution role y conector; si falta algo, **falla cerrado** antes de devolver el sandbox |
| `SandboxInfo.volume_mounts` | `[{name, path}]` desde `Health.volumes` | hoy siempre vacío (fila 34 de `e2b-parity.md`) |

## 3. Opciones de arquitectura

### A. `rayd` monta EFS por un conector VPC, un access point por volumen (recomendada)

```
cliente (SDK)                                   VPC del cliente
  │ CreateAccessPoint / DescribeMountTargets      ┌─────────────────────────┐
  │ (boto3, credenciales del llamante)            │ EFS Regional, Elastic    │
  │ run-microvm(egress=[conector VPC, …],         │ mount targets (1 por AZ) │
  │             executionRoleArn, payload)        │ SG: TCP 2049 desde SG-C  │
  ▼                                               └───────────▲─────────────┘
MicroVM rayito-base-caps                                       │ TLS 1.2 (efs-proxy)
  rayd (PID 1, root) ── mount -t efs -o tls,iam,accesspoint=fsap-…,mounttargetip=…
     │  supervisa efs-proxy + watchdog; IMDSv2 como root (uid 1000 bloqueado, Q48)
     └─ /mnt/<vol> (uid 1000, forzado por el access point) ← código del usuario
```

- **Pro**: semántica de fichero compartido en vivo (lo que E2B llama
  volumen), sin copia en `create()`, sin límite de 8 h, varios sandboxes a
  la vez, TLS e IAM de extremo a extremo, uid 1000 sin credenciales.
- **Contra**: exige VPC del cliente, conector propio (Q46 nunca medido),
  `efs-utils` en la imagen (tamaño del snapshot), variante caps obligatoria,
  y suspend/resume con un cliente NFS dentro es territorio no documentado.

### B. S3 Files o Mountpoint for S3

- **S3 Files**: mismas piezas que A (mount targets, NFS 2049, access points,
  TLS, `efs-utils`), más un bucket como almacén autoritativo y exportación
  asíncrona (~60 s según [terceros](https://www.theregister.com/on-prem/2026/04/09/aws-put-a-file-system-on-s3-i-stress-tested-it/5226687)). Útil si el cliente quiere ver
  los ficheros como objetos. Misma viabilidad que A (EFS-1–EFS-8) más EFS-19; no
  aparece MicroVMs entre los compute soportados. **Variante de A, no
  alternativa**: el puerto `VolumeMount` de §5 la cubre con otro
  `fstype` cuando se mida.
- **Mountpoint for Amazon S3 (FUSE)**: sin VPC (sale por `INTERNET_EGRESS`),
  pero necesita `/dev/fuse` y el módulo `fuse` compilado en el kernel (sin
  medir, fila 111 de `e2b-parity.md`), y no es POSIX (sin renombrar
  directorios, sin reescrituras aleatorias ni append sobre ficheros
  existentes): un `HOME` o un repo git no funcionan encima. **Descartada**
  para `volume_mounts`.

### C. Mantener S3 (ADR-009) y construir `Volume` encima

`Volume` = un prefijo S3; `volume_mounts` = `restore` en `create()` y
`checkpoint` en `pause()`/`kill()`. Sin VPC ni kernel nuevos, todo medido
(Q53: 31–97 MB/s). **Pero no es un volumen**: dos sandboxes no comparten
nada en vivo, lo escrito entre checkpoints se pierde si la VM muere, y el
último checkpoint gana. Mapearlo a `volume_mounts` de E2B sería "aproximar
en silencio" (regla 7 de `openspec/project.md`). **Descartada como
implementación de `Volume`**; sigue siendo la respuesta para quien no tiene
VPC (`persist=`).

### Comparación

| Criterio | A (EFS + AP) | B1 (S3 Files) | B2 (Mountpoint S3) | C (S3 checkpoint) |
|---|---|---|---|---|
| Semántica E2B `Volume` | sí | sí | parcial (no POSIX) | no |
| Necesita VPC + conector | sí | sí | no | no |
| Necesita imagen caps | sí | sí | sí (FUSE) | sí (persist actual) |
| Dependencia de kernel | NFSv4.1 (EFS-1) | NFSv4.1/4.2 | FUSE (sin medir) | ninguna |
| Aislamiento por volumen | AP + IAM + TLS | AP + IAM + TLS | IAM por prefijo | IAM por prefijo (T15) |
| Almacenamiento $/GB-mes (us-east-1) | 0,30 (Std) / 0,016 (IA) | S3 + caché | 0,023 | 0,023 |
| Estado | recomendada, tras medir | variante futura | descartada | ya existe (`persist=`) |

## 4. Diseño recomendado (opción A)

### 4.1 Reglas de dominio

1. Un volumen sólo se monta en `rayito-base-caps` y sólo si `rayd` detecta
   `CAP_SYS_ADMIN`, `nfs4` en `/proc/filesystems` y el mount helper
   instalado (`Health.volumes_supported`). En otro caso el SDK termina el VM y
   lanza `UnimplementedError`, como ADR-012.
2. Ruta de montaje absoluta, canónica, bajo `/mnt/` o `/home/user/` (nunca
   `/`, `/proc`, `/sys`, `/dev`, `/run`, `/etc`, `/usr`, `/tmp`, `/root`,
   `/home/user` en sí, ni una ruta de la `DenyList` de `filesystem`); sin
   solapamientos ni anidamiento entre montajes; como mucho **4** montajes
   (cabe en el `runHookPayload`, §4.4).
3. Identificadores validados con los patrones del modelo `efs`
   (`fs-[0-9a-f]{8,40}`, `fsap-[0-9a-f]{8,40}`, IPv4 del mount target).
4. El sistema de ficheros debe estar en la lista blanca de la **imagen**
   (`RAYITO_EFS_ALLOWED_FILE_SYSTEMS` en `environmentVariables`, fijada por el
   operador al publicar): quien tenga el access token de un sandbox no puede
   apuntarlo a un sistema de ficheros arbitrario (lo que T15 señala para S3).
5. `rayd` monta como root; el access point fuerza uid/gid 1000 en el
   servidor; `ClientRootAccess` nunca se concede (root squash).
6. Ningún montaje durante el build (`/ready`, `/validate`): el snapshot
   quedaría con un cliente NFS compartido por todos los VMs (§15). Sólo en
   `/run` o por RPC tras `/run`.
7. `rayd` nunca bloquea un hook por NFS: todo acceso al montaje desde un hook
   corre en un proceso hijo o hilo desechable con presupuesto, y el resultado
   se publica en `Health`.
8. Los logs llevan estado, recuentos y códigos de error, nunca el id del
   sistema de ficheros, del access point ni la IP (igual que T15 con el
   bucket).

### 4.2 `rayd-core` (dominio + puertos; sin tokio/tonic/nix)

Módulo nuevo `volume`:

| Tipo | Qué modela |
|---|---|
| `VolumeSpec { file_system_id, access_point_id, mount_path, read_only, mount_target_ip: Option }` | el volumen pedido, validado (reglas 2–4) |
| `VolumePlan` | lista de specs sin solapamientos, tope 4, orden de montaje determinista (por ruta) |
| `MountState` | `Requested → Mounting{attempt} → Mounted → Degraded{since} → Remounting → Unmounted` / `Failed{reason}`; transiciones legales como funciones puras |
| `RetryPolicy` | 0,5 → 1 → 2 → 4 → 8 s con jitter, presupuesto total 45 s en `/run`; en `/resume` un sondeo de 5 s y un solo remontaje |
| `ResumeDecision` | a partir de `ProbeOutcome` (`Healthy`, `Stale`, `Hung`, `Gone`): nada / remontar / publicar `Degraded` |
| `VolumeError` | `InvalidPath`, `Overlap`, `TooMany`, `NotAllowed`, `Unsupported`, `MountFailed{class}`; mensajes sin ids ni rutas |
| `VolumeSummary` | lo que `Health` publica: ruta, nombre lógico, estado, `last_error_class` |

Puerto nuevo (se introduce en M11 junto con su adaptador, regla del repo):

```rust
pub trait VolumeMounter: Send + Sync {
    fn support(&self) -> MountSupport;                       // caps, nfs4, helper
    fn mount(&self, spec: &VolumeSpec) -> BoxFuture<Result<(), MountFailure>>;
    fn unmount(&self, path: &MountPath, mode: UnmountMode) -> BoxFuture<Result<(), MountFailure>>;
    fn probe(&self, path: &MountPath, budget: Duration) -> BoxFuture<ProbeOutcome>;
}
```

`VolumeManager` (orquestación en el dominio, como `PersistenceManager`) aplica
el plan, lleva el registro de estados y responde a `on_run`, `on_suspend`,
`on_resume`, `on_terminate` con funciones puras que devuelven acciones.

### 4.3 Adaptador en `rayd`

- `EfsUtilsMounter` (`adapters/efs_mount.rs`): ejecuta
  `mount -t efs -o tls,iam,accesspoint=<fsap>,mounttargetip=<ip>,noresvport[,ro] <fs-id>: <ruta>`
  con `tokio::process`, entorno construido desde cero (`AWS_REGION`, `PATH`),
  como root; clasifica stderr en clases fijas (`network`, `iam_denied`,
  `not_found`, `tls`, `helper_missing`, `timeout`) sin copiarlo al log.
  Las opciones `tls`, `iam`, `accesspoint`, `mounttargetip`, `noresvport` y
  `ro` son del mount helper, no de la API de AWS; se fijan en §19 de
  `AWS_API_NOTES.md` tras EFS-8.
- **Supervisión de `efs-proxy`/watchdog sin `systemd`**: `rayd` es PID 1, no
  hay `systemd`; el adaptador lanza `amazon-efs-mount-watchdog` como hijo
  supervisado (reinicio con backoff), que es quien renueva el material de
  TLS/IAM y reinicia el túnel. Si EFS-8 muestra que `mount.efs` ya lo arranca
  solo sin `systemd`, el adaptador sólo lo vigila.
- `probe`: `stat` del punto de montaje **en un proceso hijo** con plazo (un
  hilo bloqueado en un NFS `hard` queda en estado D y no se puede matar; un
  hijo se abandona y se reapa después).
- Hooks:
  - `/run`: 200 inmediato (como hoy) y montaje en background con la
    `RetryPolicy`; `Health.volumes` pasa a `MOUNTED` o `FAILED`.
  - `/suspend`: **no** desmonta (los procesos del usuario tienen ficheros y
    `cwd` abiertos en el volumen); sustituye `sync(2)` por `syncfs` sobre
    cada sistema de ficheros local y, para cada volumen, `syncfs` en un hilo
    desechable con 5 s de presupuesto; responde 200 siempre (EFS-13).
  - `/resume`: sondeo con 5 s; si `Stale`/`Hung`, reinicia el watchdog/túnel
    y, si sigue, `umount -l` + remontaje (los descriptores abiertos pasan a
    `ESTALE`: se publica `volume_state_lost` como `kernel_state_lost`); el
    200 de `/resume` no espera al remontaje (tope 24 s del checklist).
  - `/terminate`: `umount -l` best effort con 2 s.
- Egress: en caps, la política de ADR-012 añade reglas fijas para uid ≥ 1000:
  `blackhole` de las IPs de los mount targets (el código del usuario no habla
  NFS directo) y `prohibit` del puerto local de `efs-proxy` con el mismo
  mecanismo de la guardia de DNS de M10 (`ip rule … uidrange 1000-65535
  ipproto tcp dport <puertos> prohibit`), porque ese puerto es un túnel ya
  autenticado (§4.6).

### 4.4 Contrato (`.proto`) y canal de configuración

- Nuevo `proto/rayito/v1/volume.proto`: `VolumeService.Mount(MountRequest)`
  (unario, `x-access-token`), `VolumeService.List`, y en `HealthResponse`
  los campos `volumes_supported` (16) y `repeated VolumeStatus volumes` (17).
  `Mount` por RPC, y no sólo por `runHookPayload`, porque el pool de
  suspendidos (ADR-008) lanza VMs antes de saber qué volumen se pedirá: un
  `take()` con volúmenes monta después. `create()` sin pool usa el mismo RPC
  tras la readiness, así hay un solo camino.
- El conector VPC sí tiene que ir en `run-microvm` (`egressNetworkConnectors`,
  §2: inmutable tras lanzar). Un pool con volúmenes es un pool lanzado con
  ese conector.
- Presupuesto del `runHookPayload`: no se usa para volúmenes (se deja el
  RPC); si una medición obligara a montarlo en `/run`, 4 × ~150 B caben en
  los 4096.

### 4.5 Superficie de los SDKs

Python (espejo exacto en TypeScript con `camelCase`, `InvalidArgumentError`, etc.):

```python
from rayito import EfsVolume, Sandbox, VolumeStore

store = VolumeStore(file_system_id="fs-…", region="us-east-1")   # boto3 del llamante
vol = store.create("datos-agente-7")          # CreateAccessPoint → EfsVolume
sbx = Sandbox.create(
    "rayito-base-caps",
    execution_role_arn="arn:aws:iam::<cuenta>:role/<rol-del-inquilino>",
    egress=["<arn del conector EFS>"],
    volumes={"/mnt/datos": vol},              # también EfsVolume(fs, fsap, read_only=True)
)
sbx.volumes        # {"/mnt/datos": VolumeStatus(state="mounted", ...)}
```

- `EfsVolume(file_system_id, access_point_id, name=None, region=None, read_only=False, mount_target_ip=None)`
  valor inmutable validado en construcción.
- `create(volumes=…)` exige `execution_role_arn` y al menos un conector en
  `egress` (`InvalidArgumentException` antes de cualquier llamada); resuelve
  `mount_target_ip` con `DescribeMountTargets` si no se da (una por AZ; elige
  la de la AZ de la subnet del conector si EFS-18 la revela, si no la primera
  `available`); tras la readiness llama a `VolumeService.Mount` y espera
  `MOUNTED` con `volume_timeout` (60 s por defecto); si falla o
  `volumes_supported` es falso: termina el VM (salvo `keep_on_failure`) y
  lanza `VolumeMountException(code=…)` o `UnimplementedError`.
- `VolumeStore.create/get/list/destroy/purge`: `destroy` borra el access
  point; `purge` (no E2B) lanza un sandbox efímero que monta el access point
  y borra su contenido, luego borra el access point.
- Shim `rayito.e2b`: `Volume.create/connect/list/get_info/destroy` sobre
  `VolumeStore` configurado por `E2B(..., efs_file_system_id=…)` o
  `RAYITO_EFS_FILE_SYSTEM_ID`/`RAYITO_EFS_CONNECTOR_ARN`/`RAYITO_EFS_ROLE_ARN`;
  `Sandbox.create(volume_mounts={path: Volume | name})` → `volumes=`;
  `volume.read_file/write_file/make_dir/list/remove` siguen
  `UnimplementedError` (sin plano de datos fuera de un VM); `SandboxInfo.volume_mounts`
  se rellena. `Volume*Exception` pasan de "nunca se lanzan" a lanzarse.
- Fila 26 y 111 de `e2b-parity.md`: 26 pasa a "implementado (M11)" con las
  salvedades; 111 sigue fuera.

### 4.6 Modelo de seguridad

| Capa | Control | Qué cubre |
|---|---|---|
| Red | SG del conector: egress sólo TCP 2049 al SG de los mount targets (más lo que el operador quiera); SG de mount targets: ingress 2049 sólo desde el SG del conector | nadie más de la VPC llega al NFS; el VM no llega a otra cosa de la VPC |
| Política del sistema de ficheros | `Deny` si `aws:SecureTransport=false`; `Deny` si `elasticfilesystem:AccessPointArn` es nulo (obliga a usar un access point); `Deny` si `AccessedViaMountTarget=false`; sin acceso anónimo (la política por defecto concede todo a cualquier cliente que llegue al mount target: [iam-access-control-nfs-efs.html](https://docs.aws.amazon.com/efs/latest/ug/iam-access-control-nfs-efs.html)) | sin TLS, sin IAM o sin access point no se monta |
| Identidad | execution role **por inquilino** con `ClientMount` (+ `ClientWrite` sólo si el volumen es de escritura) condicionado a `elasticfilesystem:AccessPointArn` ∈ los access points de ese inquilino; nunca `ClientRootAccess` | un inquilino no monta volúmenes de otro; sólo lectura aplicado en el servidor, no por la opción `ro` |
| Access point | `PosixUser` 1000:1000 y `RootDirectory` propio | el volumen no ve el resto del sistema de ficheros; todo fichero es de 1000:1000 |
| Guest | `rayd` root monta; uid 1000 sin capabilities (Q47), IMDS bloqueado (Q48), mount targets en `blackhole` y puerto local de `efs-proxy` en `prohibit` para uid ≥ 1000 | el código del usuario no obtiene credenciales ni abre un túnel propio |
| Imagen | `RAYITO_EFS_ALLOWED_FILE_SYSTEMS` | el access token no basta para apuntar a otro sistema de ficheros |

Riesgos residuales (entran en `SECURITY.md` como **T18**):

- Dentro de un mismo inquilino (mismo rol) cualquier sandbox con su access
  token puede pedir montar cualquier volumen de ese inquilino: el aislamiento
  es por rol, no por sandbox (igual que T15).
- El puerto local de `efs-proxy` es un túnel NFS autenticado; sin la regla
  `prohibit`, uid 1000 podría hablar NFS por él y saltarse `ro` (por eso el
  sólo lectura se aplica en IAM). La regla es de mejor esfuerzo como el resto
  de ADR-012 (root en el guest, exploit de kernel).
- Los permisos POSIX dentro del volumen no separan usuarios: todo es de
  1000:1000 (E2B declara lo mismo).
- Datos en reposo: cifrado KMS del sistema de ficheros (propiedad del
  operador, plantillada en §7).
- Un volumen borrado o con la política cambiada mientras un sandbox está
  suspendido: el `/resume` publica `Degraded`; nunca bloquea el hook.

### 4.7 Suspend / resume

| Momento | Qué pasa (hecho o hipótesis) | Qué hace `rayd` |
|---|---|---|
| Build | nada montado (regla 6) | — |
| `/run` | conexiones del snapshot muertas (§15, VERIFICADO) | monta en background, `Health.volumes` |
| `/suspend` | páginas sucias del NFS en memoria; el snapshot congela cliente NFS, `efs-proxy` y sus sockets | `syncfs` acotado por volumen, 200 siempre |
| Durante la pausa | el servidor no recibe `SEQUENCE`/renovaciones; tras el lease (valor de EFS **no documentado**; NFSv4 suele usar 30–90 s) el servidor puede expirar el estado del cliente (opens, locks) | — |
| Restore | AWS mata la conexión TLS saliente de `efs-proxy` (§15); el socket loopback kernel↔`efs-proxy` sobrevive | — |
| `/resume` | ASUMIDO: `efs-proxy` reconecta y reautentica (credenciales de IMDS válidas si < 55 min desde `run-microvm`, Q1); el cliente NFS recupera la sesión, reabre los ficheros sin reclamo de gracia; **los locks se pierden** (Linux notifica `EIO` salvo `recover_lost_locks`) | sondeo 5 s; si falla, reinicio del túnel y remontaje; `volume_state_lost` |
| Pausa > 55 min | credenciales del rol caducadas; si IMDS rota (sin medir, Q1) el watchdog las renueva, si no la reautenticación falla | igual; si falla, `Degraded` y el SDK lo reporta |

Reglas de producto (a documentar en `volumes.md`): no guardar SQLite ni
ficheros con `flock` en un volumen (E2B dice lo mismo); los ficheros abiertos
durante una pausa larga pueden devolver `EIO`/`ESTALE`; tras `resume()` el SDK
avisa si `volume_state_lost`.

## 5. Límites

| Límite | Valor | Fuente |
|---|---|---|
| Volúmenes por sandbox | 4 (decisión de diseño) | §4.1 |
| Access points por sistema de ficheros | 10 000 (ajustable) | limits.html |
| Conexiones por sistema de ficheros | 25 000 (fijo) | limits.html |
| `RootDirectory.Path` | 1–100 caracteres, ≤ 4 componentes | modelo `efs` |
| Throughput por cliente | 1 500 MiB/s (Elastic + efs-utils ≥ 2.0), si no 500 MiB/s | performance.html |
| Latencia | ~1 ms lectura / ~2,7 ms escritura (Regional); IA/Archive decenas de ms | performance.html |
| Locks por fichero | 512 entre todos los clientes; locks sólo advisory | limits.html |
| Sin xattrs NFSv4, sin `nconnect`, sin ACLs, sin delegaciones | — | limits.html; **choca con** `FileMetadata` de M9 (`user.rayito.*` xattrs): `files.write(metadata=)` sobre un volumen responde `unimplemented` |
| Disco local del VM | 8 GB (2 GB de memoria) | §4 de `AWS_API_NOTES.md`; el volumen no cuenta |
| Conectores por `run-microvm` | 10 | §2 |
| Vida del sandbox | 8 h (no cambia) | §11 |

## 6. Coste (us-east-1, precios públicos del 2026-09-11)

Fuente: lista de precios pública de EFS
(`pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEFS/current/us-east-1/index.json`,
`publicationDate` 2026-09-11) y §12 de `AWS_API_NOTES.md`.

| Concepto | Precio |
|---|---|
| Standard (Regional) | $0,30 / GB-mes |
| One Zone | $0,16 / GB-mes |
| Infrequent Access (con Elastic) | $0,016 / GB-mes (+ $0,01/GB de acceso) |
| Archive | $0,008 / GB-mes (+ $0,03/GB) |
| Elastic throughput | $0,03 / GB leído, $0,06 / GB escrito |
| Provisioned throughput | $6,00 / MiBps-mes |
| Access points, mount targets, ENIs del conector | sin cargo listado |
| NAT gateway (sólo si EFS-4 obliga a sacar internet por la VPC) | ~$0,045/h + $0,045/GB (tarifa estándar de VPC, no verificada aquí) |
| Transferencia entre AZ (VM y mount target en AZs distintas) | tarifa estándar de EC2 entre AZ (A MEDIR si aplica, EFS-18) |

Por sandbox (ejemplos):

| Escenario | EFS | Compute del VM (2 GB, $0,1261/h) |
|---|---|---|
| Volumen de 1 GB guardado un mes, sin uso | $0,30 (Standard) / ~$0,02 (IA tras 30 días) | — |
| Sesión de 1 h que escribe 500 MB y lee 2 GB | 0,5 × 0,06 + 2 × 0,03 = **$0,09** | $0,126 |
| Repo de 200 MB clonado una vez y leído 10 veces al mes | $0,06 almacenamiento + 0,2 × 0,06 + 2 × 0,03 = $0,13/mes | — |
| El mismo GB en `persist=` (S3 Standard) | ~$0,023/GB-mes + peticiones | — |

EFS Standard cuesta ≈ 13× S3 por GB almacenado y cobra cada byte leído y
escrito; compensa cuando el valor es compartir en vivo y evitar el
restore, no para archivar `HOME`s (para eso sigue `persist=`). Recomendación
en la plantilla: política de ciclo de vida a IA tras 30 días.

## 7. Infraestructura como código (descrita, no creada)

**`infra/efs-volumes.yaml` (nueva)**. Parámetros: `VpcId`, `SubnetIds`
(una por AZ), `ConnectorName`, `KmsKeyArn` (opcional), `AllowInternetEgress`
(si EFS-4 lo permite, el operador añade `INTERNET_EGRESS` en `create()`).
Recursos:

1. `ConnectorSecurityGroup`: sin ingress; egress TCP 2049 al SG de los mount
   targets (la regla placeholder `127.0.0.1/32` de `egress-connector.yaml`
   para quitar el allow-all implícito).
2. `MountTargetSecurityGroup`: ingress TCP 2049 desde `ConnectorSecurityGroup`.
3. `FileSystem` (`AWS::EFS::FileSystem`): cifrado, General Purpose, Elastic,
   `LifecyclePolicies` a IA a los 30 días, `FileSystemPolicy` con los tres
   `Deny` de §4.6, backup opcional.
4. `MountTarget` por subnet (`AWS::EFS::MountTarget`).
5. `OperatorRole` y `Connector` (`AWS::Lambda::NetworkConnector`,
   `VpcEgressConfiguration{AssociatedComputeResourceTypes: [MicroVm],
   NetworkProtocol: IPv4, SecurityGroupIds, SubnetIds}`): mismas propiedades
   ya validadas en `infra/egress-connector.yaml`. Nota: la documentación de
   MicroVMs pide al operator role `ec2:CreateNetworkInterface` sobre
   `network-interface/*`, `subnet/*`, `security-group/*` y `ec2:CreateTags`
   con `ec2:ManagedResourceOperator = network-connectors.lambda.amazonaws.com`
   ([microvms-networking.html](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html)),
   que la plantilla actual no incluye (la actual da `Describe*`/`Delete*`/
   `AssignPrivateIpAddresses`): revisar ambas plantillas en M11.
6. Outputs: `FileSystemId`, `FileSystemArn`, `ConnectorArn`,
   `MountTargetIps` (lista), la sentencia IAM que necesita el llamante.

Los nombres de propiedad de `AWS::EFS::*` se toman de la referencia de
CloudFormation y pasan `make infra-lint` (`validate-template` + `cfn-lint`)
antes de desplegar; ningún nombre de este documento vale como contrato.

**`infra/iam.yaml` (añadidos)**. Parámetros `VolumeFileSystemArn` y
`VolumeAccessPointArns` (lista, vacía = sin volúmenes; por inquilino) y
`VolumeWritable` (bool):

- `ExecutionRole`: `elasticfilesystem:ClientMount` (+ `ClientWrite` si
  `VolumeWritable`) sobre `VolumeFileSystemArn` con
  `Condition: StringEquals elasticfilesystem:AccessPointArn: <lista>` y
  `Bool aws:SecureTransport: true`. Sin `ClientRootAccess`.
- `CallerPolicy`: `elasticfilesystem:CreateAccessPoint`,
  `DescribeAccessPoints`, `DeleteAccessPoint`, `DescribeMountTargets`,
  `TagResource` sobre el sistema de ficheros y sus access points, y
  `lambda:PassNetworkConnector` sobre el conector de EFS.
- Para aislar inquilinos: un stack de `iam.yaml` (un execution role) por
  inquilino, como ya recomienda T15 para S3.

## 8. Modos de fallo

| Fallo | Síntoma | Comportamiento |
|---|---|---|
| Kernel sin NFSv4.1 | `mount` → `ENODEV` | `volumes_supported=false`; el SDK falla cerrado |
| Imagen por defecto (sin `CAP_SYS_ADMIN`) | `EPERM` | ídem |
| `efs-utils` ausente | helper no encontrado | ídem |
| SG/NACL cerrado, conector no `ACTIVE` | timeout de conexión | reintentos (45 s) → `VolumeMountException(code="network")`, VM terminado |
| DNS privado no resoluble | fallo de resolución | se evita siempre pasando `mounttargetip` |
| IAM deniega (rol sin permiso, AP ajeno, sin TLS) | `access denied` del helper | `code="iam_denied"`, sin reintento |
| AP borrado o no `available` | `not found` | `code="not_found"` |
| Pausa corta | túnel reconecta | transparente (A MEDIR, EFS-11) |
| Pausa larga / credenciales caducadas | `Stale`/`Hung` en el sondeo | remontaje; `volume_state_lost`; si falla, `Degraded` |
| Mount target caído durante `/suspend` | `sync(2)` colgado (hoy) | `syncfs` acotado; 200 siempre (EFS-13) |
| `hard` mount colgado con procesos del usuario | procesos en D | el sondeo en hijo no bloquea `rayd`; `kill()` del sandbox sigue funcionando (termina la VM) |
| Throttling del API de EFS en `create`/`list` | `ThrottlingException` | reintentos estándar de boto3 en el SDK |
| Subnets sin IPs libres | `run-microvm` falla o VM sin red (EFS-20) | error del plano de control tal cual |

## 9. Mediciones en AWS antes de implementar (EFS-1–EFS-20)

Continúan la numeración de §16 de `AWS_API_NOTES.md`; cada una entra allí
como fila con su resultado. Coste estimado por fila con los precios de §12 y
§6 (VM 2 GB $0,1261/h, lanzamiento ≈ $0,0014, suspend+resume ≈ $0,005, build
de imagen ≈ $0,04). **Orden**: primero las de parada (★); si una falla, se
para y se reescribe este documento.

| # | Pregunta | Cómo | Coste |
|---|---|---|---|
| EFS-1 ✅ | ¿`nfs4` (y `nfs`) en `/proc/filesystems` del guest, en `rayito-base` y en `rayito-base-caps`? ¿`fuse`? | **Respondida** (campaña M15 conjunta con VOL-1 de `docs/research/2026-10-e2b-out-of-scope.md`, "Resultados de la primera campaña de mediciones"): `fuse`, `fuseblk`, `fusectl`, `nfs` y `nfs4` están los cinco en `/proc/filesystems` en **ambas** imágenes. `rayito-base` no tiene `/dev/fuse` y root pierde `CAP_SYS_ADMIN` (`EPERM` incluso en un `tmpfs`); `rayito-base-caps` sí monta. El criterio de parada pasa: la opción A sigue viva, sólo en `rayito-base-caps` | $0 (ya medida, sin coste adicional) |
| EFS-2 ★ ✅ | ¿`mount -t nfs4` como root funciona dentro del contenedor de la app en caps? ¿`EPERM` en la imagen por defecto? | **Respondida 2026-10-02** (`AWS_API_NOTES.md` §16 Q123): en caps `tmpfs` monta y `nfs4` a TEST-NET agota el timeout (ni `EPERM` ni `ENODEV`); el `EPERM` por defecto ya estaba en Q79. Pasa | < $0,01 |
| EFS-3 ★ ✅ | **Respondida 2026-10-04** (`AWS_API_NOTES.md` §16 Q126–Q127, VPC existente prestada con permiso): pasa; conector `ACTIVE`, TCP 2049 en 133–466 ms. Cierra Q46. ¿Un conector VPC propio llega a `ACTIVE`, cuánto tarda, y un VM lanzado con él alcanza un puerto TCP de la VPC? ¿Con qué IP de origen (ENI del conector)? | desplegar `efs-volumes.yaml` en una VPC propia; `nc -z <ip-mount-target> 2049` desde el VM | VPC y SG gratis; EFS vacío ≈ $0; ≈ $0,02 |
| EFS-4 ✅ | **Respondida 2026-10-04** (Q131): **no**, sólo un conector de egress por VM. ¿Se acepta `egressNetworkConnectors=[INTERNET_EGRESS, <conector VPC>]`? ¿Cómo se enruta (CIDR de la VPC por la VPC y el resto por internet, o sólo uno)? ¿`ValidationException`? | tres lanzamientos: sólo VPC, sólo internet, ambos; `curl https://example.com` y `nc` al mount target | < $0,02 |
| EFS-5 ✅ | **Respondida 2026-10-04** (Q131): sí, como root y uid 1000. ¿Resuelve el guest `<fs-id>.efs.<región>.amazonaws.com` con el conector? ¿Qué resolvedor contesta? | `getent hosts` como root y como uid 1000 | < $0,01 |
| EFS-6 ✅ | **Respondida 2026-10-04** (Q128): sí, sin `placement/*`. ¿`efs-utils` obtiene las credenciales del rol del IMDSv2 del MicroVM (rol `execution_role`) como root, y necesita región/AZ del IMDS (`placement/*`)? | `mount -t efs -o tls,iam` con log de efs-utils en `debug` | incluido en EFS-8 |
| EFS-7 ★ ✅ | ¿Se instala `amazon-efs-utils` en `al2023-minimal` ARM64 (paquete de AL2023 o build), con `efs-proxy`? Delta de `codeInstallSizeInBytes` y `memorySnapshotSizeInBytes`, tiempo de build | **Respondida 2026-10-02** (Q122): paquete de AL2023 `amazon-efs-utils-3.1.3` (37 paquetes, `efs-proxy` en `/usr/sbin`); code install +197,6 MB, memoria sin cambio, build +10 s; el RPM `python3` 3.9 repunta `/usr/bin/python3` (la capa real debe rehacer el enlace). Pasa | 1 build ≈ $0,04 + storage de snapshot mínimo 1 semana (~2 GB × $0,08 × 0,25) ≈ $0,04 |
| EFS-8 ★ ✅ | **Respondida 2026-10-04** (Q128): pasa, p50 313 ms, p95 589 ms; `umount` no para `efs-proxy`. ¿`mount -t efs -o tls,iam,accesspoint=…,mounttargetip=…` funciona sin `systemd`? ¿Quién arranca `efs-proxy` y el watchdog? Latencia de montaje p50/p95 (20 muestras); mensajes con SG cerrado, IAM denegado, AP inexistente | script como root en un VM caps con rol | 20 lanzamientos ≈ $0,03 + 10 min de VM ≈ $0,02 |
| EFS-9 ✅ | **Respondida 2026-10-04** (Q132). Rendimiento desde el VM: `dd` 1 GB escritura y lectura secuencial, 10 000 ficheros de 4 KB, `git clone` de un repo mediano, latencia 4K aleatoria (`fio` si instala) frente a disco local | VM caps 2 GB | EFS: 2 GB escritos ($0,12) + 3 GB leídos ($0,09) + VM 15 min ($0,03) ≈ $0,25 |
| EFS-10 ✅ | **Respondida 2026-10-04** (Q133): uid 1000 alcanza el puerto local de `efs-proxy`. uid 1000 en el volumen: propietario 1000:1000 forzado por el AP, `chmod`, `rename`, `ESTALE` con muchos renames; ¿uid 1000 puede conectar al puerto local de `efs-proxy`? ¿Lo corta la regla `prohibit`? | pruebas como uid 1000 | < $0,02 |
| EFS-11 ★ ✅ | **Respondida 2026-10-04** (Q129): pasa con 60 s y 10 min (la de 60 min es EFS-12). Suspend/resume con montaje activo: pausas de 60 s, 10 min y 60 min; tiempo al primer `stat` correcto tras resume; escribir por un fd abierto antes de la pausa; `flock` mantenido; estado de `efs-proxy` | 3 ciclos por duración con el SDK | 3 × 3 × $0,005 + snapshots guardados ≈ 1 h ($0,0001/GB-h) + VM ≈ $0,10 |
| EFS-12 ❌ | **Medida 2026-10-04** (Q129): **falla**, tras 70 min suspendido el volumen da `Permission denied`; hay que remontar en `/resume`. Pausa > 55 min (caducidad de credenciales): ¿reautentica el túnel? ¿rota IMDS las credenciales? (cierra también Q1) | una pausa de 70 min (dentro del tope de 8 h) | ≈ $0,02 |
| EFS-13 ★ ✅ | **Respondida 2026-10-04** (Q130): no cuelga; con escrituras pendientes la plataforma termina el VM. `/suspend` con el mount target inalcanzable (revocar la regla del SG a mitad de sesión): ¿cuelga `sync(2)`? ¿qué hace la plataforma si `/suspend` agota el plazo (terminar como con un 500)? Repetir con `syncfs` acotado | dos VMs, SG modificado en caliente | < $0,03 |
| EFS-14 | ¿Cuánto crece el snapshot de suspend tras leer 1 GB del volumen (caché de páginas)? | `snapshotBuild` no aplica; Cost Explorer (`Snapshot-Write-GB`) del día | ≈ $0,01 |
| EFS-15 ✅ | **Respondida 2026-10-04** (Q133): ≈ 0,12 s. Dos sandboxes sobre el mismo AP: visibilidad de una escritura en el otro (close-to-open), latencia | dos VMs | < $0,03 |
| EFS-16 ✅ | **Respondida 2026-10-04** (Q134). Política del sistema de ficheros: montaje sin AP, sin TLS, con rol sin `ClientWrite` (¿`EROFS`/`EACCES` al escribir?), con el AP de otro "inquilino" | 4 montajes con 2 roles | < $0,02 |
| EFS-17 | Cold start con el conector VPC frente a `INTERNET_EGRESS` (`agent_ready` p50 de 10); `take()` del pool de suspendidos lanzado con conector | SDK | 20 lanzamientos ≈ $0,03 |
| EFS-18 | En parte (Q127, Q133): el VM alcanza mount targets de las dos AZs; no se midió en qué AZ sale. ¿En qué AZ sale el VM (ENI del conector)? ¿Latencia a un mount target de otra AZ? | `ip`, `nc` con tiempos | incluido en EFS-3 |
| EFS-19 | (opción B) El mismo flujo con S3 Files | sólo si A funciona y hay demanda | ≈ $0,10 |
| EFS-20 | Escala del conector: IPs de subnet consumidas por VM, 20 VMs simultáneos con conector en una subnet /28 | 20 lanzamientos | ≈ $0,05 |

**Resultados de la aceptación del 2026-10-02** (`AWS_API_NOTES.md` §16
Q122–Q125): EFS-7 y EFS-2 pasan; EFS-3 y todos los ★ que necesitan red
(EFS-8, EFS-11, EFS-13) quedan **bloqueados** porque una SCP de la
organización deniega `ec2:CreateVpc` en la cuenta de pruebas (no se usó la
VPC compartida de otro equipo ni una VPC por defecto para saltarse la
SCP). Ningún criterio de parada falló: la opción A sigue viva, pendiente de
una VPC prestada con permiso. De paso, el e2e del CRUD encontró que
`DescribeAccessPoints` es eventualmente consistente (Q125), corregido en
`VolumeStore` de ambos SDK. Coste de la aceptación ≈ $0,20 (dos builds de
imagen, cuatro VMs cortos, un sistema de ficheros vacío).

**Resultados de la aceptación del 2026-10-04** (`AWS_API_NOTES.md` §16
Q126–Q134), sobre una VPC existente de la cuenta de pruebas, usada con
permiso del mantenedor y pasada sólo por variables de entorno: **todos los
criterios de parada pasan** (EFS-3, EFS-8, EFS-11 y EFS-13; EFS-2 y EFS-7 ya
pasaban). La opción A es viable. Lo que cambia el diseño del adaptador real
(§4.3, §4.7, §12):

- **Un solo conector de egress por VM** (EFS-4): un sandbox con volumen no
  tiene internet salvo que se lo dé la VPC (NAT y un conector cuyo grupo lo
  permita). R2 queda resuelto en contra: `INTERNET_EGRESS` y el conector de
  la VPC no se combinan.
- **`efs-proxy` sobrevive a `umount`** (EFS-8): sin el watchdog de systemd
  queda un proceso por montaje; `rayd` tiene que pararlo al desmontar.
- **Escrituras pendientes + mount target inalcanzable = VM perdido**
  (EFS-13): `/suspend` de `rayd` respeta su plazo, pero la plataforma
  termina el MicroVM. El adaptador tiene que vaciar antes de suspender y la
  doc avisar de la pérdida.
- **uid 1000 alcanza el puerto local de `efs-proxy`** (EFS-10): un volumen
  de sólo lectura necesita además el rol sin `ClientWrite` o el puerto
  cerrado a uid ≥ 1000 (T21).
- **Una pausa que cruza la caducidad de las credenciales rompe el volumen**
  (EFS-12, no es de parada): tras 70 min, `Permission denied` hasta
  remontar. El adaptador tiene que remontar o renovar el túnel en `/resume`.
- Rendimiento (EFS-9): 128 MB/s escribiendo y 585 MB/s leyendo en
  secuencial, ≈ 17 ms por fichero pequeño: bueno para datos grandes, lento
  para árboles de muchos ficheros.

Coste de esta aceptación ≈ $0,50 (una imagen desechable, ≈ 25 VMs cortos
y uno suspendido 70 min, 1 GiB escrito y leído, tres pilas de vida corta).

**Total estimado de la campaña: ≈ $1–2 de uso AWS** (más el NAT gateway sólo
si EFS-4 obliga a probar internet por la VPC: ≈ $0,10 por 2 h). La VPC tiene
que ser **propia o prestada con permiso** (la puerta de despliegue de Q46
sigue en pie) y todo se borra al terminar (stack, access points, datos).

## 10. Plan de aceptación e2e (AWS real)

`clients/python/tests/e2e/test_m11_volumes.py`, saltado sin `RAYITO_E2E=1`,
`RAYITO_TEMPLATE_CAPS`, `RAYITO_EXECUTION_ROLE_ARN`,
`RAYITO_EFS_FILE_SYSTEM_ID`, `RAYITO_EFS_CONNECTOR_ARN`:

1. `VolumeStore.create(f"e2e-{uuid}")`; sandbox A con `volumes={"/mnt/v": vol}`:
   `Health.volumes` `MOUNTED`, escribir 50 MB aleatorios + un árbol pequeño,
   propietario `1000:1000`, `imds_blocked` sigue `true`, uid 1000 no alcanza
   ni el mount target ni el puerto local de `efs-proxy`.
2. Sandbox B con el mismo volumen **a la vez**: mismo sha256; escritura en B
   visible en A tras `close`.
3. `pause()` de A 90 s → `resume()`: el fichero se lee, un fd abierto antes
   sigue escribiendo o el SDK avisa `volume_state_lost` (se registra cuál).
4. `kill()` de A y B; sandbox C con el volumen: datos intactos.
5. Volumen `read_only=True` con un rol sin `ClientWrite`: la escritura falla
   con `EROFS`/`EACCES`; el sandbox sigue sano.
6. Imagen por defecto con `volumes=` → `UnimplementedError` y VM terminado;
   sin conector → `InvalidArgumentException` sin llamar a AWS.
7. Shim: `e2b.Volume.create`, `Sandbox.create(volume_mounts={"/mnt/v": name})`,
   `get_info().volume_mounts == [{"name": …, "path": "/mnt/v"}]`,
   `volume.read_file` → `UnimplementedError`.
8. Teardown: `purge` del volumen, cero VMs vivos, cero access points con la
   etiqueta de la ejecución.

Espejo en TypeScript (`m11.e2e.test.ts`) con 1, 3 y 7. Los números medidos
van a `AWS_API_NOTES.md` §16, ADR-014 y `MILESTONES.md` M11.

## 11. Estimación de esfuerzo

| Hito | Contenido | Esfuerzo |
|---|---|---|
| M11.0 Spike de medición | VPC propia, `efs-volumes.yaml` mínimo, imagen caps con `efs-utils`, EFS-1–EFS-20, filas en §16 y §19 nueva de `AWS_API_NOTES.md` (contrato EFS desde el modelo) | 3–4 días |
| M11.1 Contrato y dominio | `volume.proto`, `Health` 16/17, `rayd-core::volume`, puerto `VolumeMounter`, tests puros | 3 días |
| M11.2 Adaptador `rayd` | `EfsUtilsMounter`, supervisión del watchdog, hooks (`/run`, `/suspend` con `syncfs` acotado, `/resume`, `/terminate`), reglas de egress, `VolumeService`, tests de adaptador en Lima con un servidor NFS local | 5 días |
| M11.3 Imagen e infra | `rayito-base-caps` con `efs-utils`, `efs-volumes.yaml`, `iam.yaml`, `make infra-lint` | 2 días |
| M11.4 SDKs nativos | `EfsVolume`, `VolumeStore`, `create(volumes=)`, pool, errores, paridad sync/async, TypeScript | 4 días |
| M11.5 Shim E2B | `Volume`/`AsyncVolume`, `volume_mounts`, `SandboxInfo.volume_mounts`, excepciones | 2 días |
| M11.6 Docs, seguridad, e2e | ADR-014, `SPEC.md` §4, T18, `volumes.md`, `e2b-parity.md`, e2e Python/TS en AWS | 3 días |
| **Total** | | **≈ 22–23 días-persona (4–5 semanas), talla XL** |

Si EFS-1, EFS-3 o EFS-8 fallan, sólo se gasta M11.0 (≈ 4 días y ≈ $2) y la fila 26
de `e2b-parity.md` pasa de "fuera por SPEC" a "imposible en la plataforma"
con la medición como motivo.

## 12. Riesgos abiertos

1. ~~**Kernel sin NFS** (EFS-1)~~ **Descartado**: EFS-1 respondió que `nfs4`/`fuse` están en el kernel de `rayito-base-caps` (ver la fila EFS-1 de §9); habría matado la opción A y la B enteras si hubiera fallado.
2. **Conector VPC nunca medido** (Q46/EFS-3) y su convivencia con internet
   (EFS-4): si sólo hay un egress, cada sandbox con volumen necesita un NAT
   gateway del cliente para salir a internet.
3. **Suspend/resume con NFS** (EFS-11–EFS-13): el producto se basa en pausar; si el
   túnel no se recupera, los volúmenes sólo valen para sandboxes que no se
   pausan (y el pool de suspendidos no sirve con volúmenes).
4. **`sync(2)` en `/suspend`** puede colgar hoy mismo con cualquier montaje
   de red (también FUSE): arreglarlo es independiente de M11.
5. **`efs-utils` sin `systemd`** y su peso en el snapshot (EFS-7/EFS-8); si
   no funciona, alternativa: montar NFS con TLS propio en `rayd` (fuera de
   alcance: sería reimplementar `efs-proxy`).
6. **Aislamiento por rol, no por sandbox** (T18), y el puerto local de
   `efs-proxy` como túnel autenticado para uid 1000 (mitigación de mejor
   esfuerzo en caps).
7. **Cambio de `SPEC.md` §4** ("EFS sigue fuera"): M11 necesita ADR-014 y
   la enmienda del no-objetivo por escrito (regla 8 de la constitución).
8. **Superficie del shim**: las operaciones de fichero de `Volume` sin
   sandbox quedan `UnimplementedError`; es honesto pero es la parte más
   usada de la beta de E2B.
