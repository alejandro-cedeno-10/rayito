"""Componente `efs-volumes` (`m15-efs-volumes`, ADR-018, experimental):
`infra/efs-volumes.yaml` crea un sistema de ficheros EFS cifrado, sus mount
targets, el grupo de seguridad que sólo deja pasar NFS (2049) desde el
conector dedicado, la política del sistema de ficheros (TLS + access point +
sólo vía mount target) y el `NetworkConnector` propio. Desplegar esta pila
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
        "Sistema de ficheros EFS cifrado (Elastic Throughput), sus mount targets, el "
        "grupo de seguridad NFS y un AWS::Lambda::NetworkConnector dedicado, para "
        "volumes= (experimental: el montaje en el guest está pendiente de EFS-1..EFS-20)."
    ),
    parameters=(
        StackParameter(
            "VpcId",
            "VPC donde viven las subredes y los dos grupos de seguridad.",
            required=True,
        ),
        StackParameter(
            "SubnetId1",
            "Primera subred (su propia AZ) para un mount target y las ENIs del conector.",
            required=True,
        ),
        StackParameter(
            "SubnetId2",
            "Segunda subred (otra AZ); vacío = ninguna.",
        ),
        StackParameter(
            "SubnetId3",
            "Tercera subred (otra AZ); vacío = ninguna.",
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
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::EFS::FileSystem",
            "AWS::EFS::MountTarget (1-3)",
            "AWS::EC2::SecurityGroup (x2)",
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
            "rol y la política; el sistema de ficheros y sus datos se conservan siempre "
            "(DeletionPolicy: Retain) y sólo se borran con un "
            "`aws efs delete-file-system` explícito aparte"
        ),
        source=(
            "docs/research/2026-10-efs-persistence.md §6; AWS_API_NOTES.md §22; precios "
            "de EFS us-east-1 consultados 2026-09-11"
        ),
    ),
)
