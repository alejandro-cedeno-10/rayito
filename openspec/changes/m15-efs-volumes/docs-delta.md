# docs-delta: m15-efs-volumes

For `m15-docs-integration` to apply. Exact replacement/addition rows; this
feature does not edit the shared files directly.

## docs/site/docs/e2b-parity.md

Replace row 26 (currently "fuera por SPEC"):

```
| 26 | `Sandbox.create(volume_mounts=)` + API `Volume`/`AsyncVolume` | divergente (0.6, experimental) | `VolumeStore` hace CRUD real de access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `volume_id`/`volumeId` es el nombre del volumen (el mismo identificador de `connect`/`get_info`/`destroy`); `volume_mounts=` pasa por la misma puerta sin I/O que `volumes=` con el `volume_store`/`volumeStore` ligado al cliente (nunca busca un nombre) y hoy siempre lanza `UnimplementedError`; sin store, `UnimplementedError("Volume")` igual que `client.Volume`. `Sandbox.create(volumes=)` valida la petición y siempre lanza `UnimplementedError`, pendiente de la campaña de medición EFS-1..EFS-20 (`docs/research/2026-10-efs-persistence.md`); operaciones de contenido del `Volume` del shim (incluida `update_metadata`/`updateMetadata`) siguen sin plano de datos propio | [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) |
```

Row 90 is unchanged by this feature (it belongs to secrets-gateway/other); if another feature's delta also touches it, reconcile the `Volume*Exception` clause with: "los `Volume*Exception` ya existen y `VolumeStore`/el shim `Volume` los lanzan (fila 26, m15-efs-volumes); su API de montaje real sigue pendiente".

## docs/site/docs/optional-features.md

Add a row to the "Estado" table (same columns as the `metadata-index` row):

```
| [Volúmenes EFS (experimental)](funciones-opcionales/volumenes-efs.md) | CRUD real; montaje pendiente de EFS-1..EFS-20 | `volumes=EfsVolume(...)` / `VolumeStore(...)` | `volumes: new EfsVolume({...})` / `new VolumeStore({...})` | `None` / `undefined` | `VolumeStore` crea/lista/borra access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `Sandbox.create(volumes=)` valida y siempre lanza `UnimplementedError` hasta que la campaña de medición decida un adaptador de montaje real | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` sobre el sistema de ficheros (llamante) + `elasticfilesystem:ClientMount`/`ClientWrite` del execution role vía la política gestionada `RayitoEfsVolumeClient` que crea `infra/efs-volumes.yaml` | Access points: sin cargo propio listado; el sistema de ficheros se factura aparte ($0,30/GB-mes Standard, $0,016/GB-mes IA) | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` (credenciales del llamante) | No instancies `VolumeStore` ni pases `volumes=`; `rayito stack destroy efs-volumes` (conserva siempre el sistema de ficheros y sus datos; `aws efs delete-file-system` aparte para borrarlos) | `clients/python/src/rayito/_volumes/` / `clients/typescript/src/volumes/` |
```

## docs/site/docs/cost.md

Add under the per-feature cost table, same row shape as other `OptionalStack` components:

```
| `efs-volumes` (experimental) | $0,30/GB-mes (Standard) hasta 30 días, $0,016/GB-mes después (IA); $0,03/GB leído y $0,06/GB escrito (Elastic); $0 vacío | `rayito stack deploy efs-volumes` |
```

## SECURITY.md (threat table) and docs/site/docs/security.md

Add **T21**, the id the M15 architecture pre-allocates to efs-volumes (NFS
volumes; `ARCHITECTURE.md` ADR-018 already cites it). T20 belongs to
`m15-s3-mounts` (FUSE daemon), which writes its own T20 row; the two never
collide:

```
| T21 | datos del cliente / NFS / IAM del execution role | Un volumen EFS compartido en vivo expone el `HOME` o directorio de trabajo de un sandbox a cualquier otro con el mismo execution role; hoy el montaje no existe (`UnavailableEfsMounter`), así que el riesgo real hoy es el CRUD de `VolumeStore` (credenciales del llamante, no del sandbox) y la política IAM que un operador adjunta al execution role | `VolumeStore.create` fuerza `PosixUser` 1000:1000 y `RootDirectory` bajo `/rayito-volumes/<nombre>` (root squash, `ClientRootAccess` nunca concedido); `destroy` sólo borra el access point, nunca el directorio; `ClientToken` es un hash de `file_system_id`+nombre (nunca sólo el nombre), así que el mismo nombre en dos sistemas de ficheros no comparte token y un `ClientToken` repetido (`AccessPointAlreadyExists`) se resuelve con `get(name)`, nunca creando un duplicado. El `FileSystemPolicy` deniega `ClientMount`/`ClientWrite` sin TLS, sin access point o fuera de un mount target; la política gestionada `RayitoEfsVolumeClient` (`infra/efs-volumes.yaml`) es el único Allow identity-side, también condicionado a un access point y sin `ClientRootAccess`. Aislamiento por execution role (no por sandbox), igual que T15 con S3 — documentado, no resuelto: un sandbox con el access token de un inquilino puede pedir montar cualquier access point que ese rol pueda alcanzar una vez el montaje real exista; `AccessPointArns` (`EfsVolumes.deploy(access_point_arns=...)`) acota la política a access points exactos. **Despliegue en una VPC existente** (`EfsVolumes`): la pila sólo crea recursos nuevos dentro de la VPC (nunca VPC, subredes, rutas, NACLs, gateways ni reglas sobre grupos existentes; tests de plantilla y e2e con foto antes/después), el grupo de mount targets sólo admite TCP 2049 desde el grupo cliente dedicado (nunca un CIDR) y el conector sólo deja salir NFS a los mount targets (sin internet: depende del NAT de la VPC y de otro conector, documentado); `check()`/`rayito doctor --efs-vpc-id` son de sólo lectura (`ec2:Describe*`) y `deploy()` se niega con un `FAIL`; borrar el sistema de ficheros conservado exige `destroy(delete_file_system=True)`/`delete_file_system(id)`, que sólo actúan sobre uno con la etiqueta literal `rayito=efs-volumes` de la plantilla. **Medido en AWS real (2026-10-04, `AWS_API_NOTES.md` §16 Q130/Q133), pendiente para el adaptador real**: `efs-proxy` escucha en `127.0.0.1` y uid 1000 puede conectar a su puerto, así que un proceso del usuario habla NFS por el túnel ya autenticado con los permisos IAM del rol (un montaje `ro` no basta: hace falta cerrar ese puerto a uid ≥ 1000 o un rol sin `ClientWrite`); y con escrituras pendientes y el mount target inalcanzable la plataforma termina el MicroVM en `/suspend` (pérdida de lo no vaciado, a documentar) | M15 (`m15-efs-volumes`, CRUD + políticas; montaje pendiente de EFS-1..EFS-20) |
```

## docs/site/docs/optional-features.md and cost.md

Add "Volúmenes EFS en tu VPC" (`EfsVolumes`) next to the efs-volumes row:
off by default; `check()` is free (`ec2:Describe*`); `deploy()` costs what
the `efs-volumes` component costs ($0 empty; storage and Elastic
Throughput per GB); `destroy(delete_file_system=True)` removes it all.

## docs/site/docs/limits.md

No new limit rows beyond the existing shared mount-path cap (`MAX_MOUNTS = 4`, already documented for `mounts=`/`volumes=` together). Add one line under that cap's description: "EFS: `fs-[0-9a-f]{8,40}` y `fsap-[0-9a-f]{8,40}` (AWS_API_NOTES.md §22)."

## docs/site/docs/referencia/errores.md

Add rows (Python / TypeScript / cuándo), after the existing Mount row:

```
| `VolumeException` | `VolumeError` | `VolumeStore.create/get/list/destroy` falló (IAM, límite de access points, sistema de ficheros no disponible) |
| `VolumeNotFoundException` | `VolumeNotFoundError` | `VolumeStore.get`/`destroy` sobre un nombre que no existe |
```

## docs/site/docs/referencia/variables-de-entorno.md

No new rows: no environment variable activates `volumes=`/`VolumeStore` (ADR-014 rule 4). `RAYITO_EFS_*` activation variables proposed in the original research doc were dropped per the M15 architecture's decision 7.
