/**
 * Componente `efs-volumes` (`m15-efs-volumes`, ADR-018, experimental):
 * espejo de `rayito._stacks.components.efs_volumes`. `infra/efs-volumes.yaml`
 * crea, dentro de una VPC que ya existe, un sistema de ficheros EFS cifrado,
 * un mount target por subred, los grupos de seguridad NFS (nuevos) y un
 * `AWS::Lambda::NetworkConnector` dedicado; nunca modifica la VPC, sus
 * subredes, rutas, NACLs ni grupos existentes. `EfsVolumes`
 * (`volumes/efs-volumes.ts`) es su fachada. Desplegar esta pila no activa el
 * montaje: `rayd` sólo monta en una imagen con `amazon-efs-utils` y
 * `Sandbox.create({ volumes })` sigue en `UnimplementedError` (experimental).
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "efs-volumes",
  description:
    "Sistema de ficheros EFS cifrado (Elastic Throughput) en una VPC existente: un " +
    "mount target por subred, grupos de seguridad NFS nuevos y un " +
    "AWS::Lambda::NetworkConnector dedicado, para volumes= (experimental: " +
    "Sandbox.create(volumes=...) aún no monta).",
  parameters: [
    {
      name: "VpcId",
      description:
        "VPC existente de las subredes; aquí se crean los dos grupos de seguridad. " +
        "Nunca se modifica.",
      required: true,
    },
    {
      name: "SubnetIds",
      description:
        "De 1 a 3 subredes existentes de la VPC, separadas por comas, cada una en " +
        "otra AZ: un mount target por subred y las ENIs del conector.",
      required: true,
    },
    {
      name: "ConnectorName",
      description: "Nombre único del conector en la cuenta y región.",
      default: "rayito-efs",
    },
    {
      name: "AllowWrite",
      description:
        "'true' (por defecto): RayitoEfsVolumeClient también concede ClientWrite; " +
        "'false': sólo lectura (ClientMount). Nunca ClientRootAccess.",
      default: "true",
    },
    {
      name: "AccessPointArns",
      description:
        "ARNs de access points separados por comas: acotan RayitoEfsVolumeClient a " +
        "ellos; vacío = cualquier access point de la cuenta y región sobre este " +
        "sistema de ficheros.",
      default: "",
    },
    {
      name: "ReadOnlyAccessPointArns",
      description:
        "ARNs de access points separados por comas que se quedan en sólo lectura " +
        "aunque AllowWrite sea 'true': RayitoEfsVolumeClient les deniega ClientWrite. " +
        "Es lo que hace de sólo lectura un volumen: la opción ro del montaje no basta.",
      default: "",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::EFS::FileSystem",
      "AWS::EFS::MountTarget (1-3)",
      "AWS::EC2::SecurityGroup (x2, nuevos: mount targets y cliente)",
      "AWS::EC2::SecurityGroupEgress",
      "AWS::Lambda::NetworkConnector",
      "AWS::IAM::Role",
      "AWS::IAM::ManagedPolicy",
    ],
    idleMonthly:
      "$0,30/GB-mes (Standard) hasta los 30 días, luego $0,016/GB-mes (IA); " +
      "$0 si el sistema de ficheros está vacío",
    perUse: [
      "Elastic throughput: $0,03/GB leído, $0,06/GB escrito",
      "sin cargo listado por access points, mount targets ni ENIs del conector",
    ],
    removal:
      "destroy() borra el conector, los grupos de seguridad, los mount targets, el " +
      "rol y la política; el sistema de ficheros y sus datos se conservan " +
      "(DeletionPolicy: Retain) salvo con EfsVolumes.destroy(delete_file_system=True) " +
      "(TS: destroy({ deleteFileSystem: true })), que borra además sus access points " +
      "y el sistema de ficheros",
    source:
      "docs/research/2026-10-efs-persistence.md §6; AWS_API_NOTES.md §22; precios de EFS " +
      "us-east-1 consultados 2026-09-11",
  },
};
