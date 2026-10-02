# docs-delta: m15-s3-mounts

Applied by `m15-docs-integration` only; `s3-mounts` never edits a shared
doc table itself (see the M15 shared-file protocol, §5).

## `SECURITY.md` — new threat-table row (after T19)

```markdown
| T20 | integridad del guest / credenciales | `mounts=` (m15-s3-mounts) monta un bucket S3 en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`. Riesgos: credenciales del execution role filtradas al código del sandbox (uid 1000), el FUSE daemon corriendo como un uid que uid 1000 pueda matar o inspeccionar, un bucket o un prefijo fuera de lo autorizado, un cliente (no el SDK) que pida un `mount_path` fuera de `/mnt/`/`/home/user/`, y uid 1000 cambiando su propio directorio de montaje por un enlace simbólico para que root monte el bucket sobre un directorio del sistema | `rayd` abre `/dev/fuse` y hace el `mount(2)` como root, pero lanza `mount-s3` como el usuario dedicado `rayito-mount` (uid 990, `image/Dockerfile`), nunca como uid 1000 ni como root tras el `exec`; el entorno del daemon se reconstruye desde cero (sólo `AWS_REGION`/`PATH`, **nunca** una credencial en argv ni en entorno — SEC-3); `mount-s3` resuelve las credenciales del execution role por **su propio** acceso a IMDS: uid 990 queda **fuera** del rango `uidrange 1000-65535` que M6 blackholea para el sandbox, así que sigue viendo IMDS directamente (ADR-012), mientras uid 1000 sigue bloqueado como siempre. `features.s3_mounts` sólo es `true` con `CAP_SYS_ADMIN` en el conjunto efectivo de `rayd` (es decir, en `rayito-base-caps`): el binario y el usuario existen en las cuatro variantes, pero sin la capacidad el SDK termina el VM y lanza `UnimplementedError`. El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` (config de imagen, nunca un interruptor de activación — ADR-014 regla 4): vacío o ausente deniega todo, nunca permite todo. `read_only=True` por defecto; `allow_overwrite`/`allow_delete` exigen `read_only=False` explícito. `rayd_core::mount_path` revalida la forma de cada `mount_path` en el propio agente (absoluta, canónica, bajo `/mnt/`/`/home/user/`, sin solapar, como mucho 4) antes de montar nada: un cliente que no sea el SDK (o uno con un fallo) no puede llevar `mount(2)` a `/etc/cron.d`, `/usr/local/bin` ni a `/` — se rechaza con `SECTION_CODE_INVALID`/`invalid_path`. **Enlaces simbólicos**: la ruta nunca llega a una llamada que los siga; `rayd` la recorre componente a componente desde `/` con `O_PATH|O_DIRECTORY|O_NOFOLLOW` (creando con `mkdirat` lo que falte), rechaza cualquier enlace (`invalid_path`) y hace `mount(2)` sobre `/proc/self/fd/<dirfd>` y `umount2(MNT_DETACH|UMOUNT_NOFOLLOW)` sobre `/proc/self/fd/<padre>/<último>`, también en cada relanzamiento del vigilante y de la sonda de `/resume`. **IAM**: la política `RayitoS3MountAccess` (`infra/s3-mounts.yaml`) cubre sólo el bucket declarado y, dentro de él, sólo los prefijos declarados (hasta 4), también para `GetObject`/`PutObject`/`DeleteObject`/`AbortMultipartUpload`: aunque el guest ignorase `mount-s3 --prefix`, el rol no puede tocar objetos de otro prefijo. **Residual aceptado (SEC-3, documentado, no un fallo)**: uid 1000 puede leer `cmdline`/`environ` del daemon `mount-s3` (mismo filesystem `/proc` que cualquier otro proceso del guest) — el bucket y el prefijo se declaran **no secretos**, igual que la `metadata` de T4, así que esto no es una fuga: sólo el nombre del bucket y el prefijo son visibles, nunca una credencial, que nunca llega a argv/entorno. El montaje usa `user_id=1000,group_id=1000,allow_other` en las opciones FUSE (`allow_other` es necesario para que la propia sonda de disponibilidad de `rayd` pueda comprobar el montaje; `mount-s3` recibe `--uid 1000 --gid 1000` para que la propiedad de los ficheros siga siendo la del usuario del guest), así que el contenido del bucket lo lee quien ya está dentro del guest, el mismo nivel de confianza que cualquier fichero en `/home/user` | M15 (`m15-s3-mounts`) |
```

## `docs/site/docs/security.md` — new row in "Lo que protege el SDK por defecto"

```markdown
| Montajes S3 (T20) | apagado por defecto; con `mounts=` el daemon `mount-s3` corre como el uid dedicado `rayito-mount` (990), nunca uid 1000 ni root; credenciales sólo por IMDS propio del daemon, nunca en argv/entorno; bucket y prefijo son **no secretos** (como la metadata); el bucket debe estar en el allowlist de la imagen (`RAYITO_ALLOWED_MOUNT_BUCKETS`); el agente revalida la forma de cada `mount_path` por su cuenta y nunca sigue un enlace simbólico al montar; la política IAM se acota a los prefijos declarados ([Montajes S3](funciones-opcionales/montajes-s3.md)) |
```

## `docs/site/docs/e2b-parity.md` — replace row 111 and the summary counts

Summary table: "divergente" 22 → 23, "fuera por SPEC" 8 → 7.

Old row:

```markdown
| 111 | montajes de buckets s3fs/gcsfuse (docs) | fuera por SPEC | receta de template más montajes compartidos en vivo, fuera por `SPEC.md` §4; FUSE en `rayito-base-caps` no está medido; análogos: persistencia en S3 y transferencias por S3 | [Ficheros](files.md) |
```

New row:

```markdown
| 111 | montajes de buckets s3fs/gcsfuse (docs) | divergente (0.6.0) | `mounts=`/`mounts` monta un bucket S3 (no gcsfuse) con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`; lo monta `rayd`, no hay API equivalente en el shim de E2B | [Montajes S3](funciones-opcionales/montajes-s3.md) |
```

## `docs/site/docs/optional-features.md`

"De un vistazo" table, after the OpenTelemetry row:

```markdown
| [Montajes S3](funciones-opcionales/montajes-s3.md) | apagados | `mounts=` / `mounts`: monta un bucket S3 (o un prefijo suyo) como una carpeta normal, sólo sobre `rayito-base-caps` | $0 desde Rayito; lo normal de S3 del bucket que montes | la política `RayitoS3MountAccess` sobre tu bucket (y el bucket en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la imagen) | no pasar `mounts=` |
```

New anchored section after "Trazas OpenTelemetry":

````markdown
<a id="s3-mounts"></a>

### Montajes S3

Guía completa, allowlist de la imagen, rutas de montaje y seguridad:
[Montajes S3](funciones-opcionales/montajes-s3.md).

=== "Python"

    ```python
    from rayito import S3Mount, Sandbox

    sbx = Sandbox.create(
        "rayito-base-caps",
        execution_role_arn="arn:aws:iam::<cuenta>:role/mi-execution-role",
        mounts={"/mnt/data": S3Mount(bucket="mi-bucket", prefix="team7/")},
    )
    sbx.commands.run("ls /mnt/data")
    sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, S3Mount } from "rayito";

    const sbx = await Sandbox.create({
      template: "rayito-base-caps",
      executionRoleArn: "arn:aws:iam::<cuenta>:role/mi-execution-role",
      mounts: { "/mnt/data": new S3Mount({ bucket: "mi-bucket", prefix: "team7/" }) },
    });
    await sbx.commands.run("ls /mnt/data");
    await sbx.kill();
    ```

Sin `mounts=`/`mounts` (por defecto): `rayd` no abre `/dev/fuse` ni lanza
`mount-s3`.
````

"Funciones con coste AWS" table, after the OpenTelemetry row:

```markdown
| [Montajes S3](#s3-mounts) | implementado en 0.6.0, pendiente de aceptación en AWS real | `mounts=` | `mounts` | `None` / `undefined` | Monta uno o más buckets S3 (o un prefijo suyo) en el guest con `mount-s3`/FUSE, sólo sobre `rayito-base-caps`; `read_only=True` por defecto, `allow_overwrite`/`allow_delete` exigen `read_only=False`; `create()` espera a que cada montaje esté montado (o lanza `MountException`); `sbx.mounts` da el estado en vivo | Ningún recurso nuevo por sí solo; `mount-s3` lee/escribe el bucket que montes bajo las credenciales del execution role, vía IMDS propio (nunca en argv/entorno); el bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la imagen | $0 propio de Rayito; pagas las peticiones y el almacenamiento normales de S3 del bucket que montes ([precios de S3](https://aws.amazon.com/s3/pricing/), consultado 2026-10-01, us-east-1) | política `RayitoS3MountAccess` (`infra/s3-mounts.yaml`, desplegada con `rayito stack deploy s3-mounts --param BucketName=... --param Prefixes=...`) en el execution role | No pasar `mounts=` / `mounts` (o pasar `None`/`undefined`); `rayito stack destroy s3-mounts` quita la política | `clients/python/src/rayito/_s3_mounts/` / `clients/typescript/src/s3-mounts/` |
```

## `scripts/tests/test_optional_features_docs.py`

Register the `s3-mounts` anchor (in `FUNCTION_ANCHORS`, or through the
`optional_features.d` drop-in once it exists) and raise the expected
row count of "Funciones con coste AWS" by one, together with the row above.

## `docs/site/docs/cost.md` — "Reglas prácticas", first bullet

```markdown
- Todo lo de esta página es lo que **crea o llama el propio `create()` /
  `connect()`**. Las funciones opcionales (secretos, índice de metadatos,
  montajes S3) tienen su propio coste, apagado por defecto y activado sólo
  con una opción explícita del SDK: [Funciones opcionales](optional-features.md).
```

## `docs/site/docs/referencia/errores.md` — new row

```markdown
| `MountException` | `MountError` | `SECTION_CODE_FAILED`/`SECTION_CODE_INVALID` de `Configure`, o un montaje `failed` (o todavía `pending` al agotar la espera) en `ConfigureStatus` antes de que `create()` vuelva | un montaje de `mounts=` falló, el bucket no está en el allowlist de la imagen o la ruta no pasó la validación del agente | `code` es uno de `network`, `iam_denied`, `not_found`, `not_allowed`, `invalid_path`, `helper_missing`, `timeout` |
```

## `docs/site/docs/referencia/variables-de-entorno.md` — new row under "## Imagen"

```markdown
| `RAYITO_ALLOWED_MOUNT_BUCKETS` | `rayito image publish --env RAYITO_ALLOWED_MOUNT_BUCKETS=...` (o el `Dockerfile`), nunca por sandbox | lista de buckets (coma-separados) que `mounts=` puede montar; vacía o ausente deniega todos (nunca permite todos) |
```

## Not changed

- `limits.json`/`limits.md`: s3-mounts adds no new numeric limit (mount
  count is `_mount_path.MAX_MOUNTS`, already documented there by
  foundations).
- `NOTICE`: no vendored third-party source added by this change (the
  `mount-s3` binary is a separately-licensed Apache-2.0 tool fetched at
  image-build time, not vendored source in this repository).
