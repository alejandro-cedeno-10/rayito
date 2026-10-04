# Volúmenes EFS (experimental)

Un sistema de ficheros compartido (NFS) **en tu cuenta**, montado dentro del
guest por `rayd`: el análogo de `Volume` de E2B. A diferencia de
`persist=` (una copia S3 restaurada/guardada en `create`/`pause`), un
volumen EFS se comparte **en vivo** entre varios sandboxes mientras están
vivos.

!!! warning "Experimental: `Sandbox.create(volumes=...)` aún no monta"
    El CRUD de volúmenes (`VolumeStore`), la pila en tu VPC (`EfsVolumes`)
    y el adaptador de montaje de `rayd` (`EfsUtilsMounter`) son reales y
    están probados, y la campaña contra AWS real pasó todos sus criterios de
    parada (2026-10-04). Pero `rayd` sólo monta en una imagen
    `rayito-base-caps` que traiga `amazon-efs-utils`, y **ninguna imagen
    publicada lo trae todavía**; por eso `Sandbox.create(volumes=...)`
    valida la petición (rutas, variante, conector) y termina siempre en
    `UnimplementedError`, sin lanzar nada. Ver
    [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md)
    para el estudio completo.

!!! tip "¿Ya tienes una VPC?"
    [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md) explica cómo comprobarla
    sin crear nada (`EfsVolumes.check`, `rayito doctor --efs-vpc-id`),
    desplegar sólo recursos nuevos dentro de ella y borrarlos todos.

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
      escritura) condicionado al `AccessPointArn`, nunca `ClientRootAccess`;
      un volumen de sólo lectura va en `read_only_access_point_arns`, que le
      deniega `ClientWrite` (ver [Sólo lectura de verdad](#solo-lectura-de-verdad)).
    - **Cómo apagarla**: no instancies `VolumeStore` ni pases `volumes=`.
      Para dejar de pagar: `VolumeStore.destroy(nombre)` borra el access
      point (los datos del directorio no se borran: ver
      [Diferencias con E2B](#diferencias-con-e2b)); `rayito stack destroy
      efs-volumes` borra el conector, los mount targets, los grupos de
      seguridad y el IAM, pero **siempre conserva** el sistema de ficheros
      y sus datos (`DeletionPolicy: Retain`, sin parámetro que lo cambie:
      redesplegar la pila nunca puede borrar ni sustituir los datos). Para
      borrarlos de verdad: `EfsVolumes(...).destroy(delete_file_system=True)`,
      o `EfsVolumes(...).delete_file_system("fs-…")` después de un
      `rayito stack destroy` (ver
      [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md#3-borralo)).

## Cuándo usarlo

- Varios sandboxes necesitan leer y escribir el **mismo** directorio a la
  vez, o un sandbox necesita ver en vivo lo que otro escribió — algo que
  `persist=` (checkpoint/restore) no da: dos sandboxes con `persist=` no
  comparten nada en vivo, y lo escrito entre checkpoints se pierde si la VM
  muere.
- **Cuándo no**: un `HOME` por sandbox que no necesita compartirse en vivo
  (ahí `persist=` basta, es más barato y ya funciona); un repositorio que
  sólo necesitas una vez (clónalo con `commands.run("git clone ...")`).

## `VolumeStore`: CRUD de volúmenes

El CRUD es real hoy, sobre un sistema de ficheros EFS que ya exista
(desplegado con `EfsVolumes.deploy`/`rayito stack deploy efs-volumes`, ver
[Volúmenes EFS en tu VPC](volumenes-efs-vpc.md), o uno propio). Cada
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
       hash de `file_system_id`+nombre).
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

!!! note "`list` y `get` tardan unos segundos en ponerse al día"
    `DescribeAccessPoints` es eventualmente consistente (medido: hasta
    ~11 s en listar un volumen nuevo y ~8 s en dejar de listar uno
    borrado). `create` siempre devuelve el volumen recién creado y, si el
    nombre ya existía, espera hasta 30 s a que aparezca en el listado;
    `destroy` de un volumen que el listado aún muestra pero ya no existe
    devuelve `False`. Un `get`/`list` justo después de `create` o `destroy`
    puede ver el estado anterior.

## `Sandbox.create(volumes=)`: todavía `UnimplementedError`

La forma final de la API (research doc §4.5):

```python
from rayito import EfsVolume, Sandbox

vol = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")
with Sandbox.create(
    "rayito-base-caps",
    execution_role_arn="arn:aws:iam::123456789012:role/<rol>",
    egress=["<ConnectorArn de la pila efs-volumes>"],  # (1)!
    volumes={"/mnt/datos": vol},
) as sbx:
    ...  # UnimplementedError se lanza antes de llegar aquí
```

1. Exactamente **un** conector: el de la pila (o uno tuyo que llegue al
   mount target). Nunca `INTERNET_EGRESS`: ver
   [Internet y volúmenes](#internet-y-volumenes).

Hoy, esta llamada valida la petición (rutas bajo `/mnt/` o `/home/user/`,
como mucho 4 montajes entre `mounts=` y `volumes=`, tipos correctos,
variante caps y el conector de `egress=`) y **siempre** termina en
`UnimplementedError` — nunca una llamada a AWS a medias.

## Internet y volúmenes

Un MicroVM admite **un solo conector de egress** (medido:
`egressNetworkConnectors=[INTERNET_EGRESS, <conector VPC>]` es un
`ValidationException` de AWS, `AWS_API_NOTES.md` §16 Q131), y un volumen
necesita el conector de tu VPC para llegar al mount target. Por eso el SDK
rechaza, **antes de lanzar nada** y con `InvalidArgumentException`/
`InvalidArgumentError`, un `volumes=` que:

- no trae `egress=` (el sandbox heredaría `INTERNET_EGRESS` de la imagen y
  nunca llegaría al mount target);
- incluye `INTERNET_EGRESS` (por nombre o por ARN);
- trae más de un conector.

Si un sandbox con volumen necesita internet, tiene que salir **por tu
VPC**: una NAT o un transit gateway en las subredes del conector y un
conector cuyo grupo de seguridad permita esa salida (el de la pila
`efs-volumes` sólo deja salir NFS). `EfsVolumes.check()` te dice cuántas de
tus subredes tienen esa ruta; ver
[Salida a internet](volumenes-efs-vpc.md#salida-a-internet).

## Lo que hace `rayd` con cada volumen

Esto es lo que el adaptador de montaje hace en una imagen con
`amazon-efs-utils`, y lo que midió la aceptación del 2026-10-04:

- **Montaje**: `mount -t efs -o tls,iam,accesspoint=…` con las credenciales
  del execution role (sin `systemd`: p50 313 ms, p95 589 ms), sobre un
  directorio de `rayd` al que el usuario del sandbox no llega, y después
  enlazado a tu ruta sin seguir nunca un enlace simbólico.
- **Desmontaje**: cada montaje arranca su propio `efs-proxy` y `umount` no
  lo para; `rayd` termina el de cada volumen al desmontarlo y al terminar
  el sandbox, así que no se acumulan procesos.
- **Pausa y reanudación**: tras pausas de 60 s y 10 min el volumen sigue
  igual. Si la pausa **cruza la caducidad de las credenciales** del rol con
  las que se firmó el túnel (≈ 1 h), el volumen respondería
  `Permission denied`: `rayd` lo detecta al reanudar y lo **vuelve a
  montar** (o, si no sabe cuándo caducan, prueba el volumen con un `stat`
  acotado y lo remonta si falla). Si el remontaje no termina dentro del
  presupuesto del hook `/resume`, sigue en segundo plano y el estado del
  volumen pasa por `remounting` hasta `mounted` (o `degraded` con la causa).
  Los ficheros que el código tenía abiertos en el volumen antes de un
  remontaje dan error al usarse: vuelve a abrirlos.
- **Suspensión**: antes de suspender, `rayd` vacía las escrituras de cada
  volumen con un plazo (dentro del presupuesto de `/suspend`, como mucho
  5 s).

!!! danger "Escrituras sin vaciar + mount target inalcanzable = datos perdidos"
    Si al pausar hay escrituras que aún no llegaron a EFS y el mount target
    no responde (red caída, grupo de seguridad cambiado), el vaciado no
    termina a tiempo, `rayd` responde a `/suspend` en su plazo y **AWS
    termina el MicroVM**: lo no vaciado se pierde y `pause()` lanza
    `SandboxNotFoundException` (medido, `AWS_API_NOTES.md` §16 Q130). Nada
    dentro del guest puede evitarlo. Si un dato importa, ciérralo con
    `fsync` (o `sync`) antes de `pause()`; con el mount target accesible y
    sin escrituras pendientes, la pausa funciona.

## Sólo lectura de verdad

`EfsVolume(read_only=True)` monta con `ro`, pero **eso solo no impide
escribir**: `efs-proxy` escucha en un puerto local y el usuario del
sandbox (uid 1000) puede conectarse a él y hablar NFS por el túnel ya
autenticado, con los permisos IAM del rol (medido, `AWS_API_NOTES.md` §16
Q133; el kernel del guest no permite filtrar por usuario, Q48). Lo que
hace un volumen de sólo lectura es la **política del rol**: despliega la
pila con `read_only_access_point_arns`, que le niega `ClientWrite` a esos
access points aunque el resto admita escritura (o con `allow_write=False`
si ningún volumen debe admitirla).

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    efs.deploy(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
        read_only_access_point_arns=[
            "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0"
        ],
    )
    ```

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    await efs.deploy({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
      readOnlyAccessPointArns: [
        "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0",
      ],
    });
    ```

## Shim E2B: `E2B(volume_store=...)`

`client.Volume`/`client.AsyncVolume` (`rayito.e2b`) son `UnimplementedError`
hasta que el cliente les liga un `VolumeStore` (sólo en el constructor del
cliente, nunca por llamada; en Python, el `VolumeStore` síncrono: un
`AsyncVolumeStore` es `InvalidArgumentException`). Ligado, el CRUD es el
mismo que arriba, con los nombres de E2B (`Volume.create/connect/list/
get_info/destroy`). `volume_id`/`volumeId` es el **nombre** del volumen, el
mismo identificador que reciben `connect`/`get_info`/`destroy`; el
`AccessPointId` va aparte, en `access_point_id`/`accessPointId`.
`Sandbox.create(volume_mounts={ruta: Volume|nombre})` valida la petición
sin ninguna llamada a AWS (un nombre de texto no se busca) y siempre
termina en `UnimplementedError`: el shim lanza siempre con `INTERNET_EGRESS`
y un MicroVM sólo admite un conector de egress, así que para montar un
volumen usa `rayito.Sandbox.create(volumes=..., egress=[ConnectorArn])`.

=== "Python"

    ```python
    from rayito import VolumeStore
    from rayito.e2b import E2B

    client = E2B(volume_store=VolumeStore(file_system_id="fs-0123abcd"))
    vol = client.Volume.create("datos-agente-7")  # sin volume_store: UnimplementedError
    client.Volume.destroy(vol.volume_id)
    ```

=== "TypeScript"

    ```ts
    import { VolumeStore } from "rayito";
    import { E2B } from "rayito/e2b";

    const client = new E2B({ volumeStore: new VolumeStore({ fileSystemId: "fs-0123abcd" }) });
    const vol = await client.Volume.create("datos-agente-7");
    await client.Volume.destroy(vol.volumeId);
    ```

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` vacío, con un valor que no es `EfsVolume`, con rutas solapadas o fuera de `/mnt/`·`/home/user/` | corrige la forma de la petición |
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` sin `egress=`, con `INTERNET_EGRESS` o con más de un conector | pasa sólo el `ConnectorArn` de la pila; internet, por tu VPC ([Internet y volúmenes](#internet-y-volumenes)) |
| `UnimplementedError` | `UnimplementedError` | cualquier `volumes=` bien formado, hoy siempre (ninguna imagen publicada trae `amazon-efs-utils`); o una variante no-caps | usa `persist=` (S3) mientras tanto |
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
- [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md) — `EfsVolumes`: comprobar, desplegar y borrar
- [Pilas opcionales](pilas-opcionales.md) — `rayito stack deploy efs-volumes`
- Plantilla: [`infra/efs-volumes.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/efs-volumes.yaml)

??? info "Fuentes y mediciones"
    - Estudio de viabilidad completo: [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md).
    - Contrato de la API de EFS: `AWS_API_NOTES.md` §22, en
      [GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md).
    - Decisión de diseño: ADR-018 en
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md).
    - Campaña de medición: `scripts/measure/efs_volumes.py`
      (`plan`/`run`/`report`/`cleanup`), preguntas EFS-1..EFS-20; resultados
      en `AWS_API_NOTES.md` §16 Q122–Q134.
    - Amenaza y riesgos residuales: `SECURITY.md` T21.
