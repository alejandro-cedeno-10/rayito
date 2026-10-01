# docs-delta: m15-efs-volumes

For `m15-docs-integration` to apply. Exact replacement/addition rows; this
feature does not edit the shared files directly.

## docs/site/docs/e2b-parity.md

Replace row 26 (currently "fuera por SPEC"):

```
| 26 | `Sandbox.create(volume_mounts=)` + API `Volume`/`AsyncVolume` | divergente (0.6, experimental) | `VolumeStore` hace CRUD real de access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `Sandbox.create(volumes=)` valida la petición y siempre lanza `UnimplementedError`, pendiente de la campaña de medición EFS-1..EFS-20 (`docs/research/2026-10-efs-persistence.md`); operaciones de contenido del `Volume` del shim siguen sin plano de datos propio | [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) |
```

Row 90 is unchanged by this feature (it belongs to secrets-gateway/other); if another feature's delta also touches it, reconcile the `Volume*Exception` clause with: "los `Volume*Exception` ya existen y `VolumeStore`/el shim `Volume` los lanzan (fila 26, m15-efs-volumes); su API de montaje real sigue pendiente".

## docs/site/docs/optional-features.md

Add a row to the "Estado" table (same columns as the `metadata-index` row):

```
| [Volúmenes EFS (experimental)](funciones-opcionales/volumenes-efs.md) | CRUD real; montaje pendiente de EFS-1..EFS-20 | `volumes=EfsVolume(...)` / `VolumeStore(...)` | `volumes: new EfsVolume({...})` / `new VolumeStore({...})` | `None` / `undefined` | `VolumeStore` crea/lista/borra access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `Sandbox.create(volumes=)` valida y siempre lanza `UnimplementedError` hasta que la campaña de medición decida un adaptador de montaje real | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` sobre el sistema de ficheros desplegado por `rayito stack deploy efs-volumes` | Access points: sin cargo propio listado; el sistema de ficheros se factura aparte ($0,30/GB-mes Standard, $0,016/GB-mes IA) | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` (credenciales del llamante) | No instancies `VolumeStore` ni pases `volumes=`; `rayito stack destroy efs-volumes` (`RetainData=false` para borrar también los datos) | `clients/python/src/rayito/_volumes/` / `clients/typescript/src/volumes/` |
```

## docs/site/docs/cost.md

Add under the per-feature cost table, same row shape as other `OptionalStack` components:

```
| `efs-volumes` (experimental) | $0,30/GB-mes (Standard) hasta 30 días, $0,016/GB-mes después (IA); $0,03/GB leído y $0,06/GB escrito (Elastic); $0 vacío | `rayito stack deploy efs-volumes` |
```

## SECURITY.md (threat table) and docs/site/docs/security.md

Add **T21** (pre-allocated id):

```
| T21 | datos del cliente / NFS | Un volumen EFS compartido en vivo expone el `HOME` o directorio de trabajo de un sandbox a cualquier otro con el mismo execution role; hoy el montaje no existe (`UnavailableEfsMounter`), así que el riesgo real es sólo el CRUD de `VolumeStore` (credenciales del llamante, no del sandbox) | `VolumeStore.create` fuerza `PosixUser` 1000:1000 y `RootDirectory` bajo `/rayito-volumes/<nombre>` (root squash, `ClientRootAccess` nunca concedido); `destroy` sólo borra el access point, nunca el directorio; aislamiento por execution role (no por sandbox), igual que T15 con S3 — documentado, no resuelto: un sandbox con el access token de un inquilino puede pedir montar cualquier access point de ese rol una vez el montaje real exista. Mitigación completa (SG de mount target, `FileSystemPolicy` con tres `Deny`, condición de IAM sobre `AccessPointArn`) descrita en ADR-018 y `infra/efs-volumes.yaml`, pendiente de validar con un montaje real tras EFS-1..EFS-20 | M15 (`m15-efs-volumes`, CRUD; montaje pendiente) |
```

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
