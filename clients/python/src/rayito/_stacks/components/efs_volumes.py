"""Componente `efs-volumes` (`m15-efs-volumes`, ADR-018, experimental):
`infra/efs-volumes.yaml` crea, dentro de una VPC que ya existe, un sistema
de ficheros EFS cifrado, un mount target por subred, el grupo de seguridad
que sólo deja pasar NFS (2049) desde el grupo del conector dedicado (también
nuevo), la política del sistema de ficheros (TLS + IAM + access point + sólo
vía mount target) y el `NetworkConnector` propio; nunca modifica la VPC, sus
subredes, rutas, NACLs ni grupos existentes. `EfsVolumes` (`_volumes`) es su
fachada: comprueba la VPC antes (`check`) y borra el sistema de ficheros
conservado si se le pide (`destroy(delete_file_system=True)`). Desplegar esta pila
no activa el montaje dentro del sandbox: `rayd` sólo tiene
`UnavailableEfsMounter` hasta que la campaña de medición EFS-1..EFS-20
(`docs/research/2026-10-efs-persistence.md`) decida un adaptador real;
`VolumeStore` (CRUD de access points) sí es real y no depende de esta pila
para `create`/`get`/`list`/`destroy`, sólo para el montaje posterior.
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="efs-volumes",
    description=(
        "Sistema de ficheros EFS cifrado (Elastic Throughput) en una VPC existente: un "
        "mount target por subred, grupos de seguridad NFS nuevos y un "
        "AWS::Lambda::NetworkConnector dedicado, para volumes= (experimental: el montaje "
        "en el guest está pendiente de EFS-1..EFS-20)."
    ),
    parameters=(
        StackParameter(
            "VpcId",
            "VPC existente de las subredes; aquí se crean los dos grupos de seguridad. "
            "Nunca se modifica.",
            required=True,
        ),
        StackParameter(
            "SubnetIds",
            "De 1 a 3 subredes existentes de la VPC, separadas por comas, cada una en "
            "otra AZ: un mount target por subred y las ENIs del conector.",
            required=True,
        ),
        StackParameter(
            "ConnectorName",
            "Nombre único del conector en la cuenta y región.",
            default="rayito-efs",
        ),
        StackParameter(
            "AllowWrite",
            "'true' (por defecto): RayitoEfsVolumeClient también concede ClientWrite; "
            "'false': sólo lectura (ClientMount). Nunca ClientRootAccess.",
            default="true",
        ),
        StackParameter(
            "AccessPointArns",
            "ARNs de access points separados por comas: acotan RayitoEfsVolumeClient a "
            "ellos; vacío = cualquier access point de la cuenta y región sobre este "
            "sistema de ficheros.",
            default="",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::EFS::FileSystem",
            "AWS::EFS::MountTarget (1-3)",
            "AWS::EC2::SecurityGroup (x2, nuevos: mount targets y cliente)",
            "AWS::EC2::SecurityGroupEgress",
            "AWS::Lambda::NetworkConnector",
            "AWS::IAM::Role",
            "AWS::IAM::ManagedPolicy",
        ),
        idle_monthly=(
            "$0,30/GB-mes (Standard) hasta los 30 días, luego $0,016/GB-mes (IA); "
            "$0 si el sistema de ficheros está vacío"
        ),
        per_use=(
            "Elastic throughput: $0,03/GB leído, $0,06/GB escrito",
            "sin cargo listado por access points, mount targets ni ENIs del conector",
        ),
        removal=(
            "destroy() borra el conector, los grupos de seguridad, los mount targets, el "
            "rol y la política; el sistema de ficheros y sus datos se conservan "
            "(DeletionPolicy: Retain) salvo con EfsVolumes.destroy(delete_file_system=True) "
            "(TS: destroy({ deleteFileSystem: true })), que borra además sus access points "
            "y el sistema de ficheros"
        ),
        source=(
            "docs/research/2026-10-efs-persistence.md §6; AWS_API_NOTES.md §22; precios "
            "de EFS us-east-1 consultados 2026-09-11"
        ),
    ),
)
