# docs-delta: m15-efs-volumes

For `m15-docs-integration` to apply. Exact replacement/addition rows; this
feature does not edit the shared files directly.

## docs/site/docs/e2b-parity.md

Replace row 26 (currently "fuera por SPEC"):

```
| 26 | `Sandbox.create(volume_mounts=)` + API `Volume`/`AsyncVolume` | divergente (0.6, experimental) | `VolumeStore` hace CRUD real de access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `volume_id`/`volumeId` es el nombre del volumen (el mismo identificador de `connect`/`get_info`/`destroy`); `volume_mounts=` pasa por la misma puerta sin I/O que `volumes=` con el `volume_store`/`volumeStore` ligado al cliente (nunca busca un nombre) y siempre lanza `UnimplementedError` (el shim lanza con `INTERNET_EGRESS` y un MicroVM sólo admite un conector de egress); sin store, `UnimplementedError("Volume")` igual que `client.Volume`. `Sandbox.create(volumes=)` valida la petición (incluido un único conector propio en `egress=`) y siempre lanza `UnimplementedError` mientras ninguna imagen publicada traiga `amazon-efs-utils` (`docs/research/2026-10-efs-persistence.md`); operaciones de contenido del `Volume` del shim (incluida `update_metadata`/`updateMetadata`) siguen sin plano de datos propio | [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) |
```

Row 90 is unchanged by this feature (it belongs to secrets-gateway/other); if another feature's delta also touches it, reconcile the `Volume*Exception` clause with: "los `Volume*Exception` ya existen y `VolumeStore`/el shim `Volume` los lanzan (fila 26, m15-efs-volumes); su API de montaje real sigue pendiente".

## docs/site/docs/optional-features.md

Add a row to the "Estado" table (same columns as the `metadata-index` row):

```
| [Volúmenes EFS (experimental)](funciones-opcionales/volumenes-efs.md) | CRUD real; montaje sólo en una imagen con `amazon-efs-utils` (ninguna publicada aún) | `volumes=EfsVolume(...)` / `VolumeStore(...)` | `volumes: new EfsVolume({...})` / `new VolumeStore({...})` | `None` / `undefined` | `VolumeStore` crea/lista/borra access points EFS (`CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`); `Sandbox.create(volumes=)` valida (un único conector propio, nunca `INTERNET_EGRESS`) y siempre lanza `UnimplementedError` mientras ninguna imagen publicada traiga `amazon-efs-utils` | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` sobre el sistema de ficheros (llamante) + `elasticfilesystem:ClientMount`/`ClientWrite` del execution role vía la política gestionada `RayitoEfsVolumeClient` que crea `infra/efs-volumes.yaml` | Access points: sin cargo propio listado; el sistema de ficheros se factura aparte ($0,30/GB-mes Standard, $0,016/GB-mes IA) | `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/DeleteAccessPoint` (credenciales del llamante) | No instancies `VolumeStore` ni pases `volumes=`; `rayito stack destroy efs-volumes` (conserva siempre el sistema de ficheros y sus datos; `aws efs delete-file-system` aparte para borrarlos) | `clients/python/src/rayito/_volumes/` / `clients/typescript/src/volumes/` |
```

## docs/site/docs/cost.md

Add under the per-feature cost table, same row shape as other `OptionalStack` components:

```
| `efs-volumes` (experimental) | $0,30/GB-mes (Standard) hasta 30 días, $0,016/GB-mes después (IA); $0,03/GB leído y $0,06/GB escrito (Elastic); $0 vacío | `rayito stack deploy efs-volumes` |
```

## SECURITY.md (threat table) and docs/site/docs/security.md

**T21** (the id the M15 architecture pre-allocates to efs-volumes; T20
belongs to `m15-s3-mounts`) is now written directly in `SECURITY.md` by
this change, with the risks the 2026-10-04 acceptance measured (Q128–Q133)
and their mitigations. `docs/site/docs/security.md` gets this summary row
next to T19:

```
| Volúmenes EFS (T21, experimental) | apagado por defecto; el sólo lectura lo impone la política del rol (`read_only_access_point_arns` deniega `ClientWrite`), porque uid 1000 alcanza el puerto local de `efs-proxy` y una opción `ro` no basta; montaje sin seguir enlaces simbólicos; `rayd` termina el `efs-proxy` de cada volumen al desmontar, remonta tras una pausa que cruza la caducidad de las credenciales y vacía con plazo antes de suspender; **riesgo residual**: por el túnel, uid 1000 hace lo que el rol permita, y con escrituras pendientes y el mount target inalcanzable AWS termina el MicroVM y se pierden ([Volúmenes EFS](funciones-opcionales/volumenes-efs.md)) |
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
