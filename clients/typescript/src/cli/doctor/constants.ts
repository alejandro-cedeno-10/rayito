/**
 * Los nombres y umbrales de `rayito doctor`. `testdata/cli/doctor-contract.json`
 * lleva los mismos valores y la CLI de Python los reproduce: un test de cada
 * lado compara contra ese fichero.
 */

import { IMAGE_ARTIFACT_KEY_PREFIX } from "../../images/managed.js";

/** Las siete comprobaciones de sólo lectura; las tres siguientes necesitan un sandbox. */
export const PRE_LAUNCH_CHECK_COUNT = 7;
export const EFS_NETWORK_CHECK_NAME = "efs-network";
export const DEFAULT_TEMPLATE = "rayito-base";
/** Vida del sandbox efímero de `--launch` (`maximumDurationInSeconds`). */
export const LAUNCH_TIMEOUT_SECONDS = 300;
export const LAUNCH_METADATA: Readonly<Record<string, string>> = { rayito: "doctor" };
/** Con más MicroVMs `RUNNING` de la imagen que esto, `sandboxes` es `FAIL`. */
export const MAX_RUNNING_BEFORE_FAIL = 10;
export const MAX_LISTED_IDS = 20;
export const ACCESS_DENIED = "AccessDeniedException";
export const CAPS_IMAGE_SUFFIX = "-caps";
export const BUCKET_ENV_VAR = "RAYITO_BUCKET";
/** Los segundos que `Health` tiene para contestar a la sonda de la comprobación 9. */
export const HEALTH_PROBE_TIMEOUT_MS = 5000;

export const ADVISORY_SUMMARY =
  "simulación orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan";
export const BUCKET_PERMISSIONS_HINT =
  "publish necesita s3:ListBucket sobre el bucket y s3:GetObject/s3:PutObject " +
  `sobre ${IMAGE_ARTIFACT_KEY_PREFIX}/* (CallerPolicy de infra/iam.yaml)`;

export const IMAGE_ACTIONS = [
  "lambda:RunMicrovm",
  "lambda:GetMicrovm",
  "lambda:SuspendMicrovm",
  "lambda:ResumeMicrovm",
  "lambda:TerminateMicrovm",
  "lambda:CreateMicrovmAuthToken",
  "lambda:GetMicrovmImage",
  "lambda:UpdateMicrovmImage",
  "lambda:DeleteMicrovmImageVersion",
  "lambda:GetMicrovmImageVersion",
  "lambda:ListMicrovmImageVersions",
  "lambda:GetMicrovmImageBuild",
  "lambda:ListMicrovmImageBuilds",
] as const;
export const WILDCARD_ACTIONS = [
  "lambda:ListMicrovms",
  "lambda:ListMicrovmImages",
  "lambda:ListManagedMicrovmImages",
  "lambda:ListManagedMicrovmImageVersions",
  "lambda:CreateMicrovmImage",
  "logs:DescribeLogStreams",
  "logs:GetLogEvents",
  "servicequotas:ListServiceQuotas",
] as const;
export const CONNECTOR_ACTIONS = ["lambda:PassNetworkConnector"] as const;
export const BUCKET_OBJECT_ACTIONS = ["s3:PutObject", "s3:GetObject"] as const;
export const BUCKET_ACTIONS = ["s3:ListBucket"] as const;
