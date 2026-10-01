# Volúmenes EFS (experimental)

Un sistema de ficheros compartido (NFS) **en tu cuenta**, montado dentro del
guest por `rayd`: el análogo de `Volume` de E2B. A diferencia de
`persist=` (una copia S3 restaurada/guardada en `create`/`pause`), un
volumen EFS se comparte **en vivo** entre varios sandboxes mientras están
vivos.

!!! warning "Experimental: el montaje todavía no funciona"
    El dominio, el puerto `VolumeMounter` y el CRUD de volúmenes
    (`VolumeStore`) son reales y están probados. Pero `rayd` sólo trae
    `UnavailableEfsMounter` hasta que la campaña de medición EFS-1..EFS-20
    (tres criterios de parada: NFSv4.1 en el kernel del guest, un conector
    VPC propio que llegue al mount target, y `efs-utils` con TLS + IAM +
    access point sin `systemd`) se ejecute contra AWS real. Hasta entonces,
    `Sandbox.create(volumes=...)` siempre lanza `UnimplementedError`, incluso
    con una petición perfectamente válida. Ver
    [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md)
    para el estudio completo.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `VolumeStore(...)` ni `volumes=`, Rayito
      no construye ningún cliente `efs` ni hace ninguna llamada a AWS.
    - **Activa**: `VolumeStore(file_system_id=...)` para el CRUD de
      volúmenes (access points); `Sandbox.create(volumes={...})` para
      montarlos (hoy siempre `UnimplementedError`, ver arriba).
    - **Recursos y llamadas AWS**: `VolumeStore.create` = `CreateAccessPoint`;
      `get`/`list` = `DescribeAccessPoints`; `destroy` = `DeleteAccessPoint`
      (AWS_API_NOTES.md §22). El sistema de ficheros, sus mount targets, el
      grupo de seguridad NFS y el conector de egress dedicado los crea
      `rayito stack deploy efs-volumes` (`infra/efs-volumes.yaml`), por
      separado — `VolumeStore` nunca los crea.
    - **Coste aproximado** (us-east-1, consultado 2026-09-11, [precios de
      EFS](https://aws.amazon.com/efs/pricing/)): $0,30/GB-mes el primer
      mes (Standard), $0,016/GB-mes tras 30 días (IA, con la política de
      ciclo de vida de la plantilla); $0,03/GB leído y $0,06/GB escrito con
      Elastic Throughput. Los access points no tienen cargo propio listado.
      Un sistema de ficheros vacío cuesta $0.
    - **IAM**: `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/
      DeleteAccessPoint` sobre el sistema de ficheros (credenciales de quien
      llama al SDK). Dentro del guest, el execution role necesita
      `elasticfilesystem:ClientMount` (+ `ClientWrite` si el volumen admite
      escritura) condicionado al `AccessPointArn`, nunca `ClientRootAccess`.
    - **Cómo apagarla**: no instancies `VolumeStore` ni pases `volumes=`.
      Para dejar de pagar: `VolumeStore.destroy(nombre)` borra el access
      point (los datos del directorio no se borran: ver
      [Diferencias con E2B](#diferencias-con-e2b)); `rayito stack destroy
      efs-volumes` borra el sistema de ficheros (con `RetainData=false`;
      por defecto conserva los datos).

## Cuándo usarlo (cuando el montaje llegue)

- Varios sandboxes necesitan leer y escribir el **mismo** directorio a la
  vez, o un sandbix necesita ver en vivo lo que otro escribió — algo que
  `persist=` (checkpoint/restore) no da: dos sandboxes con `persist=` no
  comparten nada en vivo, y lo escrito entre checkpoints se pierde si la VM
  muere.
- **Cuándo no**: un `HOME` por sandbox que no necesita compartirse en vivo
  (ahí `persist=` basta, es más barato y ya funciona); un repositorio que
  sólo necesitas una vez (clónalo con `commands.run("git clone ...")`).

## `VolumeStore`: CRUD de volúmenes

El CRUD es real hoy, sobre un sistema de ficheros EFS que ya exista
(desplegado con `rayito stack deploy efs-volumes` o uno propio). Cada
volumen es un *access point* con `RootDirectory.Path =
/rayito-volumes/<nombre>` y usuario POSIX 1000:1000 forzado.

=== "Python"

    ```python
    from rayito import VolumeStore

    store = VolumeStore(file_system_id="fs-0123abcd", region="us-east-1")  # (1)!
    vol = store.create("datos-agente-7")  # (2)!
    store.get("datos-agente-7")
    store.list()
    store.destroy("datos-agente-7")  # (3)!
    ```

    1. Construirlo no llama a AWS: el cliente boto3 `efs` se crea en el
       primer método.
    2. `CreateAccessPoint`, idempotente por nombre (`ClientToken` es un
       hash del nombre).
    3. `DeleteAccessPoint`; el directorio en sí no se borra.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncVolumeStore


    async def main() -> None:
        store = AsyncVolumeStore(file_system_id="fs-0123abcd", region="us-east-1")
        vol = await store.create("datos-agente-7")
        print(vol.access_point_id)
        await store.destroy("datos-agente-7")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { VolumeStore } from "rayito"; // + npm i @aws-sdk/client-efs

    const store = new VolumeStore({ fileSystemId: "fs-0123abcd", region: "us-east-1" });
    const vol = await store.create("datos-agente-7");
    await store.get("datos-agente-7");
    await store.list();
    await store.destroy("datos-agente-7");
    ```

## `Sandbox.create(volumes=)`: todavía `UnimplementedError`

La forma final de la API (research doc §4.5), para cuando el montaje
llegue:

```python
from rayito import EfsVolume, Sandbox

vol = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")
with Sandbox.create(
    "rayito-base-caps",
    execution_role_arn="arn:aws:iam::<cuenta>:role/<rol>",
    egress=["<arn del conector de infra/efs-volumes.yaml>"],
    volumes={"/mnt/datos": vol},
) as sbx:
    ...  # UnimplementedError se lanza antes de llegar aquí
```

Hoy, esta llamada valida la petición (rutas bajo `/mnt/` o `/home/user/`,
como mucho 4 montajes entre `mounts=` y `volumes=`, tipos correctos) y
**siempre** termina en `UnimplementedError` nombrando la campaña de
medición pendiente — nunca una llamada a AWS a medias.

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` vacío, con un valor que no es `EfsVolume`, con rutas solapadas o fuera de `/mnt/`·`/home/user/` | corrige la forma de la petición |
| `UnimplementedError` | `UnimplementedError` | cualquier `volumes=` bien formado, hoy siempre (pendiente de EFS-1..EFS-20); o `execution_role_arn=`/variante no-caps | usa `persist=` (S3) mientras tanto |
| `VolumeException` | `VolumeError` | `VolumeStore.create/get/list/destroy` falló (IAM, límite de access points, sistema de ficheros no disponible) | revisa el permiso o el estado del sistema de ficheros |
| `VolumeNotFoundException` | `VolumeNotFoundError` | `get`/`destroy` sobre un nombre que no existe | lista con `VolumeStore.list()` |

## Diferencias con E2B

- E2B (`Volume.create/connect/list/get_info/destroy`, beta privada) no
  tiene un plano de datos propio para `read_file`/`write_file`/`make_dir`/
  `list`/`remove` sobre un volumen sin sandbox — Rayito tampoco: esas
  operaciones son `UnimplementedError` en el shim (`rayito.e2b.Volume`),
  porque no hay servicio de plano de control fuera de un MicroVM
  (`SPEC.md` §4).
- `Volume.destroy` en E2B no borra los datos del directorio; `VolumeStore.destroy`
  tampoco (sólo el access point). Borrar los datos exige montar el sistema
  de ficheros desde un sandbox.
- E2B no documenta un límite de montajes; Rayito limita a 4 entre `mounts=`
  y `volumes=` juntos (research doc §5).

## Ver también

- [Funciones opcionales](../optional-features.md)
- [Persistencia (S3)](../persistence.md) — la alternativa que funciona hoy
- [Pilas opcionales](pilas-opcionales.md) — `rayito stack deploy efs-volumes`
- Plantilla: [`infra/efs-volumes.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/efs-volumes.yaml)

??? info "Fuentes y mediciones"
    - Estudio de viabilidad completo: [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md).
    - Contrato de la API de EFS: `AWS_API_NOTES.md` §22, en
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Decisión de diseño: ADR-018 en
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
    - Campaña de medición pendiente: `scripts/measure/efs_volumes.py`
      (`plan`/`run`/`report`/`cleanup`), preguntas EFS-1..EFS-20.
