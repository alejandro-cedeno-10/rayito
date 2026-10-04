# Montajes S3

Monta uno o más buckets S3 (o un prefijo suyo) como una carpeta normal
dentro del sandbox, con [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3)
(`mount-s3`). Sólo sobre `rayito-base-caps` (o una variante derivada por
tamaño): `mount(2)` necesita `CAP_SYS_ADMIN`, que sólo esa variante da al
agente, y el daemon necesita las credenciales del execution role por IMDS.
En cualquier otra imagen, `create()` termina el VM y lanza
`UnimplementedError`.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `mounts=` (TypeScript: `mounts`) `rayd`
      no abre `/dev/fuse` ni lanza nada.
    - **Activa**: `mounts={"/mnt/data": S3Mount(bucket="...", prefix="...")}`
      en `Sandbox.create()`. Necesita tres cosas: una imagen
      `rayito-base-caps` publicada con el bucket en su allowlist
      (`RAYITO_ALLOWED_MOUNT_BUCKETS`, ver abajo), `execution_role_arn=` y
      la política IAM de la pila `s3-mounts` en ese rol.
    - **Recursos y llamadas AWS**: ninguno nuevo por sí solo. `mount-s3`
      hace las llamadas normales de S3 (`GetObject`/`ListObjectsV2`, y
      `PutObject`/`DeleteObject` con escritura) contra el bucket que
      montes, bajo las credenciales del execution role — las mismas que ya
      pagarías llamando a S3 por tu cuenta.
    - **Coste aproximado** (us-east-1, consultado 2026-10-01,
      [precios de S3](https://aws.amazon.com/s3/pricing/)): $0 propio de
      Rayito; pagas las peticiones y el almacenamiento normales de S3 del
      bucket que montes.
    - **IAM**: la política gestionada `RayitoS3MountAccess`
      (`infra/s3-mounts.yaml`, componente `OptionalStack` `s3-mounts`) en
      el execution role del sandbox, acotada a un bucket por pila.
    - **Cómo apagarla**: no pases `mounts=`; `rayito stack destroy
      s3-mounts` quita la política (no borra ningún objeto ni el bucket).

!!! note "Medido en AWS real"
    `Sandbox.create(mounts=)`/`create({ mounts })` y `sbx.mounts` pasaron
    la aceptación en AWS real (S3M-1..S3M-4, `AWS_API_NOTES.md`
    Q100–Q104): lectura y escritura, `pause()`/`resume()` con el montaje
    vivo, `allow_internet_access=False`, y el VM terminado ante un fallo
    de montaje. `mount-s3` 1.24.0 añade 72,7 MB a la imagen.

## Cuándo usarlo

- Tu código ya sabe trabajar con rutas normales (`pandas.read_csv`, `ls`,
  `grep`, una librería que sólo acepta un path) y el dato vive en S3: sin
  montaje, tendrías que descargarlo entero primero.
- Quieres que la salida de un agente (ficheros que escribe) aparezca
  directamente en S3, sin un paso extra de subida.
- **Cuándo no**: necesitas semántica POSIX completa (locks, `mmap`
  escribible, ficheros muy pequeños y muy numerosos con mucho `fsync`) — S3
  no es un filesystem, y `mount-s3` tiene el mismo límite que cualquier
  FUSE sobre un object store. Para eso, copia el fichero al `HOME` del
  sandbox con `files.write`/`sandbox.git` en vez de montarlo.

## Rutas de montaje

La clave de `mounts=`/`mounts` es la ruta absoluta donde aparece el bucket
dentro del guest: bajo `/mnt/` o `/home/user/`, sin solaparse con otro
montaje (la misma regla que compartirá `volumes=` de `m15-efs-volumes`), y
como mucho 4 montajes en total por sandbox.

## Antes de empezar: el allowlist de la imagen (obligatorio)

`rayd` sólo monta los buckets que la propia imagen declara en
`RAYITO_ALLOWED_MOUNT_BUCKETS` (coma-separados). Es configuración de la
imagen, no una opción por sandbox: así el código que llama a `create()` no
puede montar un bucket que quien publica la imagen no autorizó. **Vacío o
ausente deniega todos los buckets**: cada montaje acaba en
`MountException(code="not_allowed")`.

```bash
rayito image publish --artifact rayito-image.zip --base-image-version 1 \
  --image-name rayito-base-caps --os-capabilities ALL \
  --env RAYITO_ALLOWED_MOUNT_BUCKETS=mi-bucket,otro-bucket
```

!!! warning "Depende de `rayito image publish --env`"
    La opción `--env` llega con el catálogo de tamaños (`m15-sizes-catalog`).
    Hasta entonces, añade `ENV RAYITO_ALLOWED_MOUNT_BUCKETS=...` a un
    `Dockerfile` propio sobre `rayito-base-caps`.

## Desplegar la política IAM

```bash
rayito stack deploy s3-mounts --param BucketName=mi-bucket \
  --param Prefixes='team7/*,runs/*' --param ReadOnly=false
```

La política `RayitoS3MountAccess` cubre un solo bucket (una pila por
bucket) y, dentro de él, sólo los prefijos de `Prefixes` (hasta 4,
acabados en `*`; `*` por defecto es todo el bucket), también para leer,
escribir y borrar objetos: un sandbox que sólo debe escribir `runs/42/` no
puede tocar objetos de otro prefijo. `rayito stack status s3-mounts`
muestra el ARN de la política ya creada; añádelo al execution role del
sandbox. `rayito stack destroy s3-mounts` la quita (nunca borra el bucket
ni sus objetos).

## Ejemplo

`create()` no vuelve hasta que cada montaje está montado: si alguno falla
(o sigue sin responder tras 15 s), termina el VM y lanza `MountException`
con el motivo en `code` (`iam_denied`, `not_found`, `not_allowed`,
`invalid_path`, `helper_missing`, `network` o `timeout`). En cuanto
`create()` vuelve, la carpeta ya se puede leer.

=== "Python"

    ```python
    from rayito import S3Mount, Sandbox

    sbx = Sandbox.create(
        "rayito-base-caps",
        execution_role_arn="arn:aws:iam::<cuenta>:role/mi-execution-role",
        mounts={
            "/mnt/data": S3Mount(bucket="mi-bucket", prefix="team7/"),  # (1)!
            "/mnt/out": S3Mount(
                bucket="mi-bucket", prefix="runs/42/", read_only=False, allow_overwrite=True
            ),
        },
    )
    sbx.commands.run("python -c \"import pandas; pandas.read_csv('/mnt/data/datos.csv')\"")
    print(sbx.mounts)  # {"/mnt/data": MountStatus(state="mounted"), ...}
    sbx.kill()
    ```

    1. `read_only=True` por defecto: sólo lectura salvo que pidas lo
       contrario explícitamente.

=== "TypeScript"

    ```typescript
    import { Sandbox, S3Mount } from "rayito";

    const sbx = await Sandbox.create({
      template: "rayito-base-caps",
      executionRoleArn: "arn:aws:iam::<cuenta>:role/mi-execution-role",
      mounts: {
        "/mnt/data": new S3Mount({ bucket: "mi-bucket", prefix: "team7/" }),
        "/mnt/out": new S3Mount({
          bucket: "mi-bucket",
          prefix: "runs/42/",
          readOnly: false,
          allowOverwrite: true,
        }),
      },
    });
    await sbx.commands.run("python3 -c \"import pandas; pandas.read_csv('/mnt/data/datos.csv')\"");
    console.log(await sbx.mounts()); // Map { "/mnt/data" => { state: "mounted" }, ... }
    await sbx.kill();
    ```

`mounts` también acepta un `Map` en TypeScript.

`sbx.mounts` (TypeScript: `await sbx.mounts()`) lee el estado en vivo en
cada llamada: si el daemon de un montaje muere más tarde, `rayd` lo
relanza solo y, mientras tanto, ese montaje aparece como `"pending"` o
`"failed"`.

## Divergencias con E2B

E2B no tiene un equivalente a `mounts=` (fila 111 de
[Paridad con E2B](../e2b-parity.md)): el shim no añade ningún método nuevo
para esto.

## Seguridad

- El daemon `mount-s3` corre como el usuario dedicado `rayito-mount` (uid
  990), nunca como uid 1000 ni como root tras arrancar; sus credenciales
  llegan por su propio acceso a IMDS, nunca por argv ni por el entorno del
  proceso que `rayd` lanza (`AWS_REGION`/`PATH` son las únicas variables).
- El bucket y el prefijo **no son secretos** (como la `metadata` del
  sandbox): un proceso uid 1000 puede ver el nombre del bucket y el
  prefijo en la línea de órdenes del daemon (`/proc/<pid>/cmdline`), nunca
  una credencial; su entorno (`/proc/<pid>/environ`) ni siquiera es
  legible, y tampoco puede enviarle señales.
- El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la imagen
  (config de imagen, no una opción por sandbox); vacío o ausente deniega
  todos los buckets.
- La sonda de disponibilidad que `rayd` lanza como uid 1000 tras cada
  montaje (`/usr/bin/stat <ruta>`) y el propio `mount-s3` arrancan con un
  entorno construido desde cero (sólo `PATH`, más `AWS_REGION` en
  `mount-s3`), sin heredar ningún descriptor de `rayd` ni sus señales
  ignoradas: el entorno de `rayd` (el ARN de la imagen, variables
  `RAYITO_*` o `--env` de la imagen) nunca llega a un proceso que otro
  proceso uid 1000 pueda leer en `/proc/<pid>/environ`.
- `rayd` monta como root, pero nunca sigue un enlace simbólico en la ruta
  de montaje (ni al crearla ni al relanzar un montaje): si el código del
  sandbox cambia `/home/user/<carpeta>` por un enlace a un directorio del
  sistema, el montaje falla con `invalid_path` en vez de montar el bucket
  encima de ese directorio.
- La política IAM se acota a los prefijos que declares, también para
  escribir y borrar: la contención no depende sólo de `mount-s3 --prefix`
  dentro de un guest que ejecuta código no confiable.

Detalle completo: [Seguridad](../security.md), amenaza T20.
