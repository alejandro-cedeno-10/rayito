# Volúmenes EFS en tu VPC

La forma rápida y segura de preparar los
[volúmenes EFS](volumenes-efs.md) en una **VPC que ya existe**: el caso
común, porque muchas cuentas no pueden crear VPCs (una política de la
organización suele denegar `ec2:CreateVpc`). `EfsVolumes` comprueba la VPC
sin tocar nada, despliega sólo recursos **nuevos** dentro de ella y los
borra todos cuando se lo pides.

!!! warning "Experimental"
    El sistema de ficheros, los grupos de seguridad, el conector y el CRUD
    de volúmenes (`VolumeStore`) son reales. El montaje dentro del sandbox
    (`Sandbox.create(volumes=...)`) sigue en `UnimplementedError` hasta la
    campaña de medición EFS-1..EFS-20 (ver [Volúmenes EFS](volumenes-efs.md)).

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin una llamada explícita a
      `EfsVolumes.deploy(...)` (o `rayito stack deploy efs-volumes`) no se
      crea nada; construir `EfsVolumes` no llama a AWS.
    - **Activa**: `EfsVolumes(...).deploy(vpc_id=..., subnet_ids=[...])`
      en Python o `new EfsVolumes(...).deploy({ vpcId, subnetIds })` en
      TypeScript (peer `@aws-sdk/client-ec2` para la comprobación y
      `@aws-sdk/client-efs` para borrar el sistema de ficheros).
      `check()` y `rayito doctor --efs-vpc-id ... --efs-subnet-ids ...` son
      de sólo lectura.
    - **Recursos y llamadas AWS**: `check()` sólo lee EC2
      (`DescribeVpcs`, `DescribeVpcAttribute`, `DescribeSubnets`,
      `DescribeRouteTables`). `deploy()` crea, etiquetado y dentro de la
      VPC dada: un sistema de ficheros EFS cifrado en reposo (clave KMS
      gestionada por AWS), un mount target por subred, dos grupos de
      seguridad nuevos, un `AWS::Lambda::NetworkConnector` de salida a la
      VPC para MicroVMs, su rol de operador y la política
      `RayitoEfsVolumeClient`. **Nunca** modifica la VPC, sus subredes,
      tablas de rutas, NACLs ni ningún grupo de seguridad existente.
    - **Coste aproximado** (us-east-1, consultado 2026-09-11, [precios de
      EFS](https://aws.amazon.com/efs/pricing/)): $0 con el sistema de
      ficheros vacío; $0,30/GB-mes (Standard) y $0,016/GB-mes tras 30 días
      (IA); $0,03/GB leído y $0,06/GB escrito (Elastic Throughput). Mount
      targets, access points y ENIs del conector no tienen cargo listado.
      Las llamadas `Describe*` de `check()` son gratis.
    - **IAM**: quien despliega necesita CloudFormation sobre la pila,
      crear los recursos de arriba (`CAPABILITY_IAM`) y los `ec2:Describe*`
      de `check()`; para `delete_file_system`,
      `elasticfilesystem:DescribeFileSystems`/`DescribeMountTargets`/
      `DescribeAccessPoints`/`DeleteAccessPoint`/`DeleteFileSystem`. El
      execution role del sandbox sólo recibe la política de la salida
      `CallerPolicyArn`: `ClientMount` (y `ClientWrite` salvo
      `allow_write=False`) sobre **este** sistema de ficheros y sus access
      points, nunca `ClientRootAccess`.
    - **Cómo apagarla**: `destroy()` borra la pila (conector, grupos,
      mount targets, rol y política) y **conserva** el sistema de ficheros
      con sus datos; `destroy(delete_file_system=True)` borra además sus
      access points y el propio sistema de ficheros: todo lo que
      `deploy()` creó. Si borraste la pila con `rayito stack destroy
      efs-volumes` (que siempre lo conserva), `delete_file_system("fs-…")`
      borra el que quedó; sólo acepta uno con la etiqueta
      `rayito=efs-volumes` que le pone la plantilla.

## 1. Comprueba la VPC (sin crear nada)

`check()` valida que la VPC y las subredes existen, que las subredes son
de esa VPC y están en AZs distintas (EFS admite un mount target por AZ),
que les quedan IPs libres (una para el mount target y al menos una para
una ENI del conector) y si la VPC resuelve DNS. Avisa de una sola AZ (sin
redundancia y con tráfico entre AZs facturado) y te dice cómo saldría a
internet un sandbox. El informe incluye lo que `deploy()` crearía y su
coste.

=== "CLI"

    ```bash
    rayito doctor --efs-vpc-id vpc-0123456789abcdef0 \
      --efs-subnet-ids subnet-0123456789abcdef0
    ```

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")  # (1)!
    report = efs.check(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
    )
    for finding in report.findings:
        print(finding.level, finding.code, finding.message)
    print("deploy crearía:", report.cost.creates)
    ```

    1. Construirlo no llama a AWS.

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito"; // + npm i @aws-sdk/client-ec2 @aws-sdk/client-efs

    const efs = new EfsVolumes({ region: "us-east-1" });
    const report = await efs.check({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
    });
    for (const finding of report.findings) {
      console.log(finding.level, finding.code, finding.message);
    }
    ```

| Hallazgo | Nivel | Qué significa |
|---|---|---|
| `vpc-missing`, `subnet-missing` | FAIL | la VPC o una subred no existe en esta cuenta y región |
| `subnet-other-vpc` | FAIL | la subred es de otra VPC |
| `subnet-same-az` | FAIL | dos subredes en la misma AZ: EFS admite un mount target por AZ |
| `subnet-free-ips` | FAIL | menos de 2 IPs libres en una subred |
| `vpc-unavailable`, `subnet-unavailable` | FAIL | no están en estado `available` |
| `vpc-dns-support`, `vpc-dns-hostnames` | WARN | sin DNS de VPC el nombre del sistema de ficheros no resuelve (montar por IP sí funciona) |
| `single-az` | WARN | una sola AZ |
| `internet-egress` | OK | informativo: ver [Salida a internet](#salida-a-internet) |

## 2. Despliega

`deploy()` vuelve a correr `check()` y se niega (sin crear nada) si algún
hallazgo es `FAIL`.

=== "CLI"

    ```bash
    rayito stack deploy efs-volumes \
      --param VpcId=vpc-0123456789abcdef0 \
      --param SubnetIds=subnet-0123456789abcdef0
    ```

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    status = efs.deploy(
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=["subnet-0123456789abcdef0"],
    )
    print(status.outputs["ConnectorArn"], status.outputs["CallerPolicyArn"])
    store = efs.volume_store()  # (1)!
    vol = store.create("datos-agente-7")
    ```

    1. Un `VolumeStore` sobre el `FileSystemId` de la pila.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncEfsVolumes


    async def main() -> None:
        efs = AsyncEfsVolumes(region="us-east-1")
        await efs.deploy(
            vpc_id="vpc-0123456789abcdef0",
            subnet_ids=["subnet-0123456789abcdef0"],
        )
        store = await efs.volume_store()
        await store.create("datos-agente-7")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    const status = await efs.deploy({
      vpcId: "vpc-0123456789abcdef0",
      subnetIds: ["subnet-0123456789abcdef0"],
    });
    console.log(status.outputs.ConnectorArn, status.outputs.CallerPolicyArn);
    const store = await efs.volumeStore();
    await store.create("datos-agente-7");
    ```

Opciones de `deploy()`:

- `subnet_ids` / `subnetIds`: de 1 a 3 subredes, una por AZ.
- `allow_write=False` / `allowWrite: false`: la política del execution
  role sólo concede `ClientMount` (lectura).
- `access_point_arns` / `accessPointArns`: acota la política a esos access
  points exactos; vacío (por defecto), a cualquier access point de la
  cuenta y región sobre **este** sistema de ficheros. Redespliega con la
  lista nueva cuando añadas un volumen.
- `connector_name` / `connectorName`: nombre del conector, único en la
  cuenta y región (por defecto `rayito-efs`).

Adjunta la salida `CallerPolicyArn` al execution role que montará los
volúmenes, y pasa `ConnectorArn` en `egress=[...]` cuando el montaje
exista.

### Shim E2B

El shim usa el mismo `VolumeStore`:

```python
from rayito import EfsVolumes
from rayito.e2b import E2B

client = E2B(volume_store=EfsVolumes(region="us-east-1").volume_store())
vol = client.Volume.create("datos-agente-7")
```

## Qué crea y por qué es seguro

| Recurso (nuevo) | Regla |
|---|---|
| Sistema de ficheros EFS | cifrado en reposo (KMS gestionada por AWS); su política deniega todo sin TLS (`aws:SecureTransport`), todo montaje sin access point y todo lo que no llegue por un mount target; no tiene ningún `Allow`, así que un cliente NFS sin IAM no obtiene nada |
| Mount targets (uno por subred) | en el grupo de mount targets |
| Grupo de seguridad de mount targets | sólo TCP 2049 de entrada, y sólo desde el grupo cliente (nunca un CIDR) |
| Grupo de seguridad cliente | usado por el conector; su única salida es TCP 2049 hacia el grupo de mount targets |
| `AWS::Lambda::NetworkConnector` | salida a la VPC para MicroVMs, en tus subredes, con el grupo cliente |
| Rol de operador del conector | sólo las acciones `ec2:*NetworkInterface*` que el conector necesita |
| Política `RayitoEfsVolumeClient` | `ClientMount`/`ClientWrite` sobre este sistema de ficheros y sus access points, nunca `ClientRootAccess` |

CloudFormation propaga las etiquetas de la pila
(`rayito:component=efs-volumes`, `rayito:managed-by`,
`rayito:sdk-version` y las tuyas de `tags=`) a los recursos que admiten
etiquetas; el sistema de ficheros lleva además, fija en la plantilla,
`rayito=efs-volumes`. La VPC, sus subredes, tablas
de rutas, NACLs y grupos existentes no se tocan nunca; el e2e
(`tests/e2e/test_m15_efs_volumes_vpc.py`) lo comprueba comparando una
foto de todos ellos antes y después.

## Salida a internet

El conector de esta pila **sólo** deja salir NFS (2049) hacia los mount
targets: no da salida a internet. Si un sandbox necesita internet a la vez:

- la plataforma la da con su conector `INTERNET_EGRESS`; si combinarlo con
  el conector de la VPC funciona es la pregunta EFS-4, aún sin medir;
- si quieres que salga **por tu VPC**, depende del NAT de tu VPC (el
  `check()` cuenta cuántas de tus subredes tienen ruta por defecto a un
  NAT) y de un conector propio que lo permita
  ([`infra/egress-connector.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/egress-connector.yaml));
  esta pila no crea ninguno.

## 3. Bórralo

=== "Python"

    ```python
    from rayito import EfsVolumes

    efs = EfsVolumes(region="us-east-1")
    efs.destroy()  # (1)!
    efs.destroy(delete_file_system=True)  # (2)!
    efs.delete_file_system("fs-0123456789abcdef0")  # (3)!
    ```

    1. Borra la pila y conserva el sistema de ficheros con sus datos.
    2. Borra también sus access points y el sistema de ficheros: todo lo
       que `deploy()` creó.
    3. Tras un `rayito stack destroy efs-volumes`: borra el sistema de
       ficheros que quedó (sólo uno con la etiqueta `rayito=efs-volumes`).

=== "TypeScript"

    ```ts
    import { EfsVolumes } from "rayito";

    const efs = new EfsVolumes({ region: "us-east-1" });
    await efs.destroy({ deleteFileSystem: true });
    ```

## Errores

| Python | TypeScript | Cuándo |
|---|---|---|
| `InvalidArgumentException` | `InvalidArgumentError` | ids mal formados, más de 3 subredes, un ARN de access point inválido, o `deploy()` sobre una VPC con algún `FAIL` (no se creó nada) |
| `VolumeException` | `VolumeError` | `volume_store()` sin pila, o `delete_file_system` sobre uno sin la etiqueta de la pila o que no suelta sus mount targets |
| `StackException` | `StackError` | CloudFormation falló al desplegar o borrar |

## Ver también

- [Volúmenes EFS](volumenes-efs.md): `VolumeStore`, `volumes=` y el shim.
- [Pilas opcionales](pilas-opcionales.md): `rayito stack deploy|status|destroy`.
- Plantilla: [`infra/efs-volumes.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/efs-volumes.yaml)
- Contrato de las llamadas a EC2 y EFS: `AWS_API_NOTES.md` §22.
