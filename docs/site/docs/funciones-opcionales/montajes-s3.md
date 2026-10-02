# Montajes S3

Monta uno o más buckets S3 (o un prefijo suyo) como una carpeta normal
dentro del sandbox, con [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3)
(`mount-s3`). Sólo sobre `rayito-base-caps` (o una variante derivada por
tamaño): el daemon necesita las credenciales del execution role por IMDS, y
`rayito-base` no las concede dentro del guest.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `mounts=` (TypeScript: `mounts`) `rayd`
      no abre `/dev/fuse` ni lanza nada.
    - **Activa**: `mounts={"/mnt/data": S3Mount(bucket="...", prefix="...")}`
      en `Sandbox.create()` (necesita `execution_role_arn=` y una imagen
      `rayito-base-caps`).
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

!!! note "Experimental"
    Funciona de punta a punta (`Sandbox.create(mounts=)`/`create({ mounts })`,
    `sbx.mounts`), pero es nuevo: trátalo como experimental hasta la
    campaña de medición S3M-1..S3M-4 de la aceptación en AWS real.

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

## Ejemplo

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
      mounts: new Map([
        ["/mnt/data", new S3Mount({ bucket: "mi-bucket", prefix: "team7/" })],
        [
          "/mnt/out",
          new S3Mount({ bucket: "mi-bucket", prefix: "runs/42/", readOnly: false, allowOverwrite: true }),
        ],
      ]),
    });
    await sbx.commands.run("python3 -c \"import pandas; pandas.read_csv('/mnt/data/datos.csv')\"");
    console.log(await sbx.mounts()); // Map { "/mnt/data" => { state: "mounted" }, ... }
    await sbx.kill();
    ```

## Desplegar la política IAM

```bash
rayito stack deploy s3-mounts --param BucketName=mi-bucket --param ReadOnly=false
```

`rayito stack status s3-mounts` muestra el ARN de la política ya creada;
añádelo al execution role del sandbox. `rayito stack destroy s3-mounts` la
quita (nunca borra el bucket ni sus objetos).

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
  prefijo en `/proc` del daemon, nunca una credencial.
- El bucket debe estar en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la imagen
  (config de imagen, no una opción por sandbox); vacío o ausente deniega
  todos los buckets.

Detalle completo: [Seguridad](../security.md), amenaza T20.
