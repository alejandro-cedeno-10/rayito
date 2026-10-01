# docs-delta: m15-s3-mounts

Applied by `m15-docs-integration` only; `s3-mounts` never edits a shared
doc table itself (see the M15 shared-file protocol, §5).

## `SECURITY.md` — new threat-table row (after T19)

```markdown
| T20 | integridad del guest / credenciales | `mounts=` (m15-s3-mounts) monta un bucket S3 en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`. Riesgos: credenciales del execution role filtradas al código del sandbox (uid 1000), el FUSE daemon corriendo como un uid que uid 1000 pueda matar o inspeccionar, un bucket fuera de lo autorizado por la imagen | `rayd` abre `/dev/fuse` y hace el `mount(2)` como root, pero lanza `mount-s3` como el usuario dedicado `rayito-mount` (uid 990, `image/Dockerfile`), nunca como uid 1000 ni como root tras el `exec`; el entorno del daemon se reconstruye desde cero (sólo `AWS_REGION`/`PATH`, **nunca** una credencial en argv ni en entorno — SEC-3); `mount-s3` resuelve las credenciales del execution role por **su propio** acceso a IMDS: uid 990 queda **fuera** del rango `uidrange 1000-65535` que M6 blackholea para el sandbox, así que sigue viendo IMDS directamente (ADR-012), mientras uid 1000 sigue bloqueado como siempre. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` (config de imagen, nunca un interruptor de activación — ADR-014 regla 4): vacío o ausente deniega todo, nunca permite todo. `read_only=True` por defecto; `allow_overwrite`/`allow_delete` exigen `read_only=False` explícito. **Residual aceptado (SEC-3, documentado, no un fallo)**: uid 1000 puede leer `cmdline`/`environ` del daemon `mount-s3` (mismo filesystem `/proc` que cualquier otro proceso del guest) — el bucket y el prefijo se declaran **no secretos**, igual que la `metadata` de T4, así que esto no es una fuga: sólo el nombre del bucket y el prefijo son visibles, nunca una credencial, que nunca llega a argv/entorno. El montaje usa `user_id=1000,group_id=1000` en las opciones FUSE (sin `allow_other` más allá de eso), así que el contenido del bucket sólo lo lee quien ya es uid 1000 dentro del guest, el mismo nivel de confianza que cualquier fichero en `/home/user`. IAM: la política `RayitoS3MountAccess` (`infra/s3-mounts.yaml`) sólo cubre el bucket declarado, nunca `*` | M15 (`m15-s3-mounts`) |
```

## `docs/site/docs/security.md` — new row in "Lo que protege el SDK por defecto"

```markdown
| Montajes S3 (T20) | apagado por defecto; con `mounts=` el daemon `mount-s3` corre como el uid dedicado `rayito-mount` (990), nunca uid 1000 ni root; credenciales sólo por IMDS propio del daemon, nunca en argv/entorno; bucket y prefijo son **no secretos** (como la metadata); el bucket debe estar en el allowlist de la imagen (`RAYITO_ALLOWED_MOUNT_BUCKETS`) ([Montajes S3](funciones-opcionales/montajes-s3.md)) |
```

## `docs/site/docs/e2b-parity.md` — replace row 111

Old:

```markdown
| 111 | montajes de buckets s3fs/gcsfuse (docs) | fuera por SPEC | receta de template más montajes compartidos en vivo, fuera por `SPEC.md` §4; FUSE en `rayito-base-caps` no está medido; análogos: persistencia en S3 y transferencias por S3 | [Ficheros](files.md) |
```

New:

```markdown
| 111 | montajes de buckets s3fs/gcsfuse (docs) | divergente (0.6.0) | `mounts=`/`mounts` monta un bucket S3 (no gcsfuse) con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`; lo monta `rayd`, no hay API equivalente en el shim de E2B | [Montajes S3](funciones-opcionales/montajes-s3.md) |
```

## `docs/site/docs/optional-features.md` — new row in "Funciones con coste AWS"

Insert after the OpenTelemetry row, before the closing "Una fila pasa a..." note:

```markdown
| [Montajes S3](#s3-mounts) | implementado en 0.6.0 (agente + IAM); `Sandbox.create(mounts=)` en integración final | `mounts=` | `mounts` | `None` / `undefined` | Monta uno o más buckets S3 (o un prefijo suyo) en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`; `read_only=True` por defecto, `allow_overwrite`/`allow_delete` exigen `read_only=False` | Ningún recurso nuevo por sí solo; `mount-s3` lee/escribe el bucket que montes bajo las credenciales del execution role, vía IMDS propio (nunca en argv/entorno); el bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la imagen | $0 propio de Rayito; pagas las peticiones y el almacenamiento normales de S3 del bucket que montes ([precios de S3](https://aws.amazon.com/s3/pricing/), consultado 2026-10-01, us-east-1) | política `RayitoS3MountAccess` (`infra/s3-mounts.yaml`, desplegada con `rayito stack deploy s3-mounts --param BucketName=...`) en el execution role | No pasar `mounts=` / `mounts` (o pasar `None`/`undefined`); `rayito stack destroy s3-mounts` quita la política | `clients/python/src/rayito/_s3_mounts/` / `clients/typescript/src/s3-mounts/` |
```

Also add `s3-mounts` to the "De un vistazo" table near the top, same row content condensed to that table's columns (Función / Por defecto / Qué activa / Coste / IAM / Cómo apagarla), linking to `funciones-opcionales/montajes-s3.md`.

## `docs/site/docs/cost.md` — new entry under "Funciones con coste AWS" (or its own subsection)

```markdown
### Montajes S3 (`mounts=`)

$0 propio de Rayito: pagas las peticiones (`GET`/`PUT`/`LIST`) y el
almacenamiento normales de S3 del bucket que montes, igual que cualquier
otro acceso a ese bucket
([precios de S3](https://aws.amazon.com/s3/pricing/), consultado
2026-10-01, us-east-1). La política `RayitoS3MountAccess`
(`infra/s3-mounts.yaml`) es sólo IAM: $0 en reposo.
```

## `docs/site/docs/referencia/errores.md` — new row

```markdown
| `MountException` | `MountError` | `SECTION_CODE_FAILED`/`SECTION_CODE_INVALID` de `Configure` | un montaje de `mounts=` falló o el bucket no está en el allowlist de la imagen | `code` es uno de `network`, `iam_denied`, `not_found`, `not_allowed`, `helper_missing`, `timeout` |
```

## `docs/site/docs/referencia/variables-de-entorno.md` — new row under "## Imagen"

```markdown
| `RAYITO_ALLOWED_MOUNT_BUCKETS` | en el `Dockerfile`/`rayito image publish --env`, nunca por sandbox | lista de buckets (coma-separados) que `mounts=` puede montar; vacía o ausente deniega todos (nunca permite todos) |
```

## Not changed

- `limits.json`/`limits.md`: s3-mounts adds no new numeric limit (mount
  count is `_mount_path.MAX_MOUNTS`, already documented there by
  foundations).
- `NOTICE`: no vendored third-party source added by this change (the
  `mount-s3` binary is a separately-licensed Apache-2.0 tool fetched at
  image-build time, not vendored source in this repository).
