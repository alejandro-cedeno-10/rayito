/**
 * Componente `efs-volumes` (`m15-efs-volumes`, ADR-018, experimental):
 * espejo de `rayito._stacks.components.efs_volumes`. `infra/efs-volumes.yaml`
 * crea un sistema de ficheros EFS cifrado, sus mount targets, el grupo de
 * seguridad NFS y un `AWS::Lambda::NetworkConnector` dedicado. Desplegar
 * esta pila no activa el montaje: `rayd` sólo tiene `UnavailableEfsMounter`
 * hasta que la campaña de medición EFS-1..EFS-20 decida un adaptador real.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "efs-volumes",
  description:
    "Sistema de ficheros EFS cifrado (Elastic Throughput), sus mount targets, el " +
    "grupo de seguridad NFS y un AWS::Lambda::NetworkConnector dedicado, para " +
    "volumes (experimental: el montaje en el guest está pendiente de EFS-1..EFS-20).",
  parameters: [
    {
      name: "VpcId",
      description: "VPC donde viven las subredes y los dos grupos de seguridad.",
      required: true,
    },
    {
      name: "SubnetId1",
      description: "Primera subred (su propia AZ) para un mount target y las ENIs del conector.",
      required: true,
    },
    { name: "SubnetId2", description: "Segunda subred (otra AZ); vacío = ninguna." },
    { name: "SubnetId3", description: "Tercera subred (otra AZ); vacío = ninguna." },
    {
      name: "ConnectorName",
      description: "Nombre único del conector en la cuenta y región.",
      default: "rayito-efs",
    },
    {
      name: "RetainData",
      description: "'true' (por defecto): destroy() conserva el sistema de ficheros y sus datos.",
      default: "true",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::EFS::FileSystem",
      "AWS::EFS::MountTarget (1-3)",
      "AWS::EC2::SecurityGroup (x2)",
      "AWS::Lambda::NetworkConnector",
      "AWS::IAM::Role",
    ],
    idleMonthly:
      "$0,30/GB-mes (Standard) hasta los 30 días, luego $0,016/GB-mes (IA); " +
      "$0 si el sistema de ficheros está vacío",
    perUse: [
      "Elastic throughput: $0,03/GB leído, $0,06/GB escrito",
      "sin cargo listado por access points, mount targets ni ENIs del conector",
    ],
    removal:
      "destroy() borra siempre el conector, el grupo de seguridad y el rol; el sistema " +
      "de ficheros y sus datos sólo se borran con RetainData=false",
    source:
      "docs/research/2026-10-efs-persistence.md §6; AWS_API_NOTES.md §22; precios de EFS " +
      "us-east-1 consultados 2026-09-11",
  },
};
