# Volúmenes EFS (experimental)

<small>Desde 0.7.0 ([Novedades](../novedades/0.7.0.md#volumenes-efs)).</small>

Un sistema de ficheros compartido (NFS) **en tu cuenta**, montado dentro del
guest por `rayd`: el análogo de `Volume` de E2B. A diferencia de
`persist=` (una copia S3 restaurada/guardada en `create`/`pause`), un
volumen EFS se comparte **en vivo** entre varios sandboxes mientras están
vivos.

!!! warning "Experimental, y sólo con la imagen opcional que trae `amazon-efs-utils`"
    `Sandbox.create(volumes=...)` monta de verdad, pero sólo sobre una imagen
    que traiga `amazon-efs-utils` y corra con `additionalOsCapabilities`
    `ALL`: la imagen opcional `rayito-base-caps-efs`
    ([Imagen con amazon-efs-utils](#imagen-con-amazon-efs-utils)). Las
    imágenes por defecto no cambian y en ellas `Health.features.efs_volumes`
    es `false`: `create(volumes=...)` termina el sandbox y lanza
    `UnimplementedError`. El estudio completo está en
    [`docs/research/2026-10-efs-persistence.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-efs-persistence.md).

!!! tip "¿Ya tienes una VPC?"
    [Volúmenes EFS en tu VPC](volumenes-efs-vpc.md) explica cómo comprobarla
    sin crear nada (`EfsVolumes.check`, `rayito doctor --efs-vpc-id`),
    desplegar sólo recursos nuevos dentro de ella y borrarlos todos.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `VolumeStore(...)` ni `volumes=`, Rayito
      no construye ningún cliente `efs` ni hace ninguna llamada a AWS.
    - **Activa**: `VolumeStore(file_system_id=...)` para el CRUD de
      volúmenes (access points); `Sandbox.create(volumes={...})` para
      montarlos, con `execution_role_arn=`, un solo conector en `egress=` y
      la imagen opcional `rayito-base-caps-efs`.
    - **Recursos y llamadas AWS**: `VolumeStore.create` = `CreateAccessPoint`;
      `get`/`list` = `DescribeAccessPoints`; `destroy` = `DeleteAccessPoint`
      (AWS_API_NOTES.md §22). `create(volumes=...)` hace además una
      `DescribeMountTargets` por sistema de ficheros, antes de lanzar, si un
      `EfsVolume` no trae `mount_target_ip`. El sistema de ficheros, sus mount targets, el
      grupo de seguridad NFS y el conector de egress dedicado los crea
      `rayito stack deploy efs-volumes` (`infra/efs-volumes.yaml`), por
      separado — `VolumeStore` nunca los crea.
    - **Coste aproximado** (us-east-1, consultado 2026-09-11, [precios de
      EFS](https://aws.amazon.com/efs/pricing/)): $0,30/GB-mes el primer
      mes (Standard), $0,016/GB-mes tras 30 días (IA, con la política de
      ciclo de vida de la plantilla); $0,03/GB leído y $0,06/GB escrito con
      Elastic Throughput. Los access points no tienen cargo propio listado.
      Un sistema de ficheros vacío cuesta $0. La imagen con
      `amazon-efs-utils` ocupa ≈ 198 MB más de código instalado
      (`AWS_API_NOTES.md` §16 Q122); el tráfico hacia un mount target de otra
      AZ lo factura EC2 como transferencia entre AZs.
    - **IAM**: `elasticfilesystem:CreateAccessPoint/DescribeAccessPoints/
      DeleteAccessPoint` (y `DescribeMountTargets` para montar sin
      `mount_target_ip`) sobre el sistema de ficheros (credenciales de quien
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

## `Sandbox.create(volumes=)`: montar volúmenes

=== "Python"

    ```python
    from rayito import Sandbox, VolumeStore

    store = VolumeStore(file_system_id="fs-0123abcd", region="us-east-1")
    datos = store.create("datos-agente-7")
    with Sandbox.create(
        "rayito-base-caps-efs",
        execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
        egress=["arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"],
        volumes={"/mnt/datos": datos},
    ) as sbx:
        sbx.commands.run("echo hola > /mnt/datos/saludo.txt")
        print(sbx.volumes["/mnt/datos"].state)  # "mounted"
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, VolumeStore } from "rayito";

    const store = new VolumeStore({ fileSystemId: "fs-0123abcd", region: "us-east-1" });
    const datos = await store.create("datos-agente-7");
    const sbx = await Sandbox.create({
      template: "rayito-base-caps-efs",
      executionRoleArn: "arn:aws:iam::123456789012:role/rayito-execution",
      egress: ["arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"],
      volumes: { "/mnt/datos": datos },
    });
    await sbx.commands.run("echo hola > /mnt/datos/saludo.txt");
    console.log((await sbx.volumes()).get("/mnt/datos")?.state); // "mounted"
    await sbx.kill();
    ```

Lo que hace `create()`, en orden:

1. **Antes de lanzar, sin red**: valida la petición (de 1 a 4 volúmenes,
   rutas bajo `/mnt/` o `/home/user/` sin solaparse, valores `EfsVolume`,
   variante caps si el nombre de la imagen la revela, exactamente **un**
   conector propio en `egress=` y `execution_role_arn=`). Cualquier fallo es
   `InvalidArgumentException`/`InvalidArgumentError` y no se lanza nada.
2. **Antes de lanzar, con EFS**: si un volumen no trae `mount_target_ip`,
   una `DescribeMountTargets` por sistema de ficheros (credenciales de quien
   llama; dos volúmenes del mismo sistema de ficheros comparten la
   respuesta) y elige el primer mount target `available` por
   `AvailabilityZoneId`. Sin ninguno disponible, `VolumeException`/
   `VolumeError` antes de lanzar.
3. **Tras la readiness**: manda la sección `efs_volumes` en el **mismo**
   `ConfigureSandbox` que `mounts=`, `gateways=`, `telemetry=` y `events=`.
   `rayd` monta los volúmenes de uno en uno dentro de esa llamada, cuyo
   plazo es como mucho 65 s (4 volúmenes × 15 s del helper, más margen);
   `create()` sólo vuelve con todos `mounted`.
4. **Si algo falla**: termina el sandbox (salvo `keep_on_failure=True`) y
   lanza `VolumeMountException`/`VolumeMountError` con `code` (`network`,
   `iam_denied`, `not_found`, `tls`, `helper_missing`, `timeout`,
   `invalid_path` o `unknown`), o `UnimplementedError` si la imagen no
   trae `amazon-efs-utils`.

`sbx.volumes` (Python síncrono), `await sbx.volumes()` (asíncrono y
TypeScript) lee el estado en vivo de cada ruta (`mounted`, `degraded`,
`remounting`…). `reincarnate()` vuelve a mandar la sección (y a resolver
las IPs) en el sucesor.

## Imagen con amazon-efs-utils

`volumes=` sólo monta en una imagen que traiga el *mount helper* de EFS
(`mount.efs` y `efs-proxy`, del paquete `amazon-efs-utils` de AL2023) y que
corra con `additionalOsCapabilities: ["ALL"]` (`CAP_SYS_ADMIN`). Ninguna de las
imágenes por defecto lo trae: `rayito-base`, `-slim`, `-caps` y `-poly` no
cambian, y en ellas `Health.features.efs_volumes` sigue en `false` (un
`create(volumes=...)` sobre ellas termina el sandbox y lanza
`UnimplementedError`). La imagen con efs-utils es una variante aparte,
`rayito-base-caps-efs`, que publicas tú:

```bash
make image-publish-caps-efs BUCKET=amzn-s3-demo-bucket
# o, paso a paso:
rayito image zip image image/rayito-image-efs.zip --sidecar kernel-sidecar --with-efs
rayito image publish --artifact image/rayito-image-efs.zip --with-efs \
    --os-capabilities ALL --bucket amzn-s3-demo-bucket --base-image-version 1
```

`--with-efs` añade al zip el marcador `kernel-sidecar/efs_variant`; la capa
condicional del `Dockerfile` instala entonces `amazon-efs-utils-3.1.3`
(clavado por versión), vuelve a enlazar `/usr/bin/python3` a Python 3.12 (el
paquete trae Python 3.9 y lo reapunta; `rayd` usa esa ruta) y comprueba que
`mount.efs` y `efs-proxy` existen. La región no se hornea: `rayd` se la pasa al
helper en cada montaje (`AWS_REGION` del MicroVM), así que la misma imagen
sirve en cualquier región donde la publiques. `rayito-base-caps-efs` cuenta
como variante caps para `mounts=` y `telemetry=` igual que `rayito-base-caps`.

!!! info "Coste de la imagen"
    Medido en AWS real (`AWS_API_NOTES.md` §16 Q122, AL2023 ARM64): 37 paquetes
    más (entre ellos `nfs-utils`, `stunnel`, Python 3.9 y `systemd`, que nadie
    arranca), **+~198 MB** de `codeInstallSizeInBytes` (almacenamiento de la
    imagen), el snapshot de memoria **no cambia** y la build tarda **≈ 10 s**
    más. Sin `--with-efs` no se instala nada ni cambia ningún coste.

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
`amazon-efs-utils`, y lo que midieron las aceptaciones del 2026-10-04
(`AWS_API_NOTES.md` §16 Q128–Q139):

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
  montar** (medido tras 70 min: `mounted` otra vez en 0,7–1,3 s,
  `AWS_API_NOTES.md` §16 Q139) (o, si no sabe cuándo caducan, prueba el volumen con un `stat`
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
`Sandbox.create(volume_mounts={ruta: Volume|nombre})` monta esos volúmenes
por el mismo camino que `volumes=` si el cliente trae además
`volume_connector_arn=`/`volumeConnectorArn` (el `ConnectorArn` de la pila):
como un MicroVM sólo admite un conector de egress, ese sandbox sale **sólo**
por ese conector, nunca por `INTERNET_EGRESS`, y un
`allow_internet_access=True` explícito es `InvalidArgumentException`. Un
`Volume` se monta con su `access_point_id` sin llamar a AWS; un nombre de
texto se busca con `VolumeStore.get` (`DescribeAccessPoints`). También
necesita `execution_role_arn=` y la imagen con `amazon-efs-utils`. Sin
`volume_connector_arn` falla antes de cualquier llamada a AWS.

=== "Python"

    ```python
    from rayito import VolumeStore
    from rayito.e2b import E2B

    client = E2B(
        volume_store=VolumeStore(file_system_id="fs-0123abcd"),
        volume_connector_arn="arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs",
    )
    vol = client.Volume.create("datos-agente-7")  # sin volume_store: UnimplementedError
    sbx = client.Sandbox.create(
        "rayito-base-caps-efs",
        execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
        volume_mounts={"/mnt/datos": vol},
    )
    sbx.kill()
    client.Volume.destroy(vol.volume_id)
    ```

=== "TypeScript"

    ```ts
    import { VolumeStore } from "rayito";
    import { E2B } from "rayito/e2b";

    const client = new E2B({
      volumeStore: new VolumeStore({ fileSystemId: "fs-0123abcd" }),
      volumeConnectorArn: "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs",
    });
    const vol = await client.Volume.create("datos-agente-7");
    const sbx = await client.Sandbox.create("rayito-base-caps-efs", {
      executionRoleArn: "arn:aws:iam::123456789012:role/rayito-execution",
      volumeMounts: { "/mnt/datos": vol },
    });
    await sbx.kill();
    await client.Volume.destroy(vol.volumeId);
    ```

## Errores y solución de problemas

| Python | TypeScript | Cuándo | Qué hacer |
|---|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` vacío, con más de 4 volúmenes, con un valor que no es `EfsVolume`, con rutas solapadas o fuera de `/mnt/`·`/home/user/`, o sin `execution_role_arn=` | corrige la forma de la petición |
| `InvalidArgumentException` | `InvalidArgumentError` | `volumes=` sin `egress=`, con `INTERNET_EGRESS` o con más de un conector | pasa sólo el `ConnectorArn` de la pila; internet, por tu VPC ([Internet y volúmenes](#internet-y-volumenes)) |
| `UnimplementedError` | `UnimplementedError` | la imagen no trae `amazon-efs-utils` (`Health.features.efs_volumes` es `false`: el sandbox ya se terminó) o es una variante no-caps | publica y usa `rayito-base-caps-efs` ([Imagen con amazon-efs-utils](#imagen-con-amazon-efs-utils)) |
| `VolumeMountException` (`code`) | `VolumeMountError` (`code`) | un volumen no montó: `iam_denied` (política del rol o access point fuera de `access_point_arns`), `network` (conector o grupo de seguridad), `not_found`, `tls`, `timeout`, `invalid_path` (la ruta es un enlace simbólico)… El sandbox ya se terminó (salvo `keep_on_failure`) | revisa el rol, el conector o la ruta según `code` |
| `VolumeException` | `VolumeError` | el sistema de ficheros no tiene ningún mount target `available` (antes de lanzar) | despliega la pila o espera a que termine |
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
