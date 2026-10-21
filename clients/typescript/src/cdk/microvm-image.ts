/**
 * `RayitoMicrovmImage` (agentcore-cdk, ADR-032 D9): publica una imagen de
 * Lambda MicroVMs con `rayd` desde CloudFormation, con la misma
 * configuración que `rayito image publish` (`desiredConfiguration` de
 * `images/publish-domain.ts`). Los MicroVMs se siguen arrancando con el SDK
 * (`Sandbox.create({ template: "<nombre>" })`): CloudFormation no tiene un
 * recurso para un MicroVM en marcha.
 */

import { Aws, RemovalPolicy } from "aws-cdk-lib";
import { PolicyDocument, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { CfnMicrovmImage, type CfnMicrovmImageProps } from "aws-cdk-lib/aws-lambda";
import { LogGroup, type RetentionDays } from "aws-cdk-lib/aws-logs";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import { Construct } from "constructs";
import { validateTags } from "../agentcore/domain.js";
import { inspectArtifact } from "../images/artifact-node.js";
import { DEFAULT_BASE_IMAGE_NAME, DEFAULT_MEMORY_MIB } from "../images/gateway.js";
import {
  ALL_OS_CAPABILITIES,
  BASELINE_MEMORY_ENV_VAR,
  desiredConfiguration,
  IMAGE_VARIANTS,
  type ImageSettings,
  type ImageVariant,
  logGroupOf,
  requireEfsCombination,
  sizedSettings,
  validateSizes,
} from "../images/publish-domain.js";
import { LATEST_BASE_IMAGE_VERSION } from "../images/publish-service.js";
import { CDK_MICROVM_IMAGE_LOG_RETENTION_DAYS } from "../limits.js";
import type { SizeName } from "../sizing/sizing.js";
import type { MicrovmImageArtifact } from "./artifact.js";
import { applyTags, stackEnvironment } from "./environment.js";
import { RayitoCdkError } from "./errors.js";
import { cfnImageProperties } from "./image-properties.js";

/** `Name` de `AWS::Lambda::MicrovmImage` (`AWS_API_NOTES.md`, «Constructos CDK»). */
export const IMAGE_NAME_PATTERN = /^[a-zA-Z0-9-_]+$/;
export const IMAGE_NAME_MAX_LENGTH = 64;
/** Los nombres de `rayito setup` y `rayito image publish` (`rayito-base*`). */
export const RESERVED_IMAGE_NAME_PREFIX = DEFAULT_BASE_IMAGE_NAME;
/** La variable de imagen con los buckets que `rayd` deja montar (`s3-mounts`). */
export const ALLOWED_MOUNT_BUCKETS_ENV_VAR = "RAYITO_ALLOWED_MOUNT_BUCKETS";
const MOUNT_BUCKETS_SEPARATOR = ",";
const DEFAULT_VARIANT: ImageVariant = "full";
/** `EnvironmentVariables` del esquema: hasta 50, clave de 1 a 256 y valor de hasta 4096 caracteres. */
export const MAX_ENVIRONMENT_VARIABLES = 50;
const ENVIRONMENT_KEY_MAX_CHARS = 256;
const ENVIRONMENT_VALUE_MAX_CHARS = 4096;
const ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** Un nombre de bucket de S3. */
const BUCKET_NAME_PATTERN = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
const LAMBDA_SERVICE = "lambda.amazonaws.com";
const SOURCE_ACCOUNT_CONDITION = "aws:SourceAccount";
const BUILD_POLICY_NAME = "build";
const LOG_ACTIONS = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"];
const HAS_MARKER = "lleva";
const LACKS_MARKER = "no lleva";

/** Las props de `RayitoMicrovmImage` (en Python, los mismos nombres en snake_case). */
export interface RayitoMicrovmImageProps {
  /** El nombre de la imagen (`Sandbox.create({ template })`); cada tamaño añade `-<tamaño>`. */
  readonly name: string;
  /** El zip de la imagen: `MicrovmImageArtifact.fromFile` o `await MicrovmImageArtifact.downloadRelease()`. */
  readonly artifact: MicrovmImageArtifact;
  /** La versión mayor de la imagen base gestionada `al2023-1` (p. ej. `"1"`); `latest` no vale: la síntesis no lee AWS. */
  readonly baseImageVersion: string;
  /** La variante del zip (`full` por defecto, `slim` o `poly`); debe coincidir con su marcador. */
  readonly variant?: ImageVariant | undefined;
  /** El zip lleva `amazon-efs-utils` (`rayito image zip --with-efs`); exige `full` y `osCapabilities`. */
  readonly withEfs?: boolean | undefined;
  /** `AdditionalOsCapabilities: [ALL]` (montajes de S3 y EFS). */
  readonly osCapabilities?: boolean | undefined;
  /** La memoria del baseline en MiB (2048 por defecto); con `sizes`, sólo el baseline. */
  readonly memoryMib?: number | undefined;
  /** Tamaños adicionales del catálogo (`512mb`, `4gb`, …): una imagen `<nombre>-<tamaño>` cada uno. */
  readonly sizes?: readonly SizeName[] | undefined;
  /** Buckets que los sandboxes pueden montar (`RAYITO_ALLOWED_MOUNT_BUCKETS`). */
  readonly allowedMountBuckets?: readonly string[] | undefined;
  /** Variables horneadas en la imagen: configuración, nunca secretos. */
  readonly environment?: Readonly<Record<string, string>> | undefined;
  /** Etiquetas del usuario; las tres `rayito:*` las pone el constructo. */
  readonly tags?: Readonly<Record<string, string>> | undefined;
}

function optionInvalid(option: string, reason: string): RayitoCdkError {
  return new RayitoCdkError("option_invalid", { option, reason });
}

function checkNames(name: string, sizes: readonly string[]): void {
  if (name.startsWith(RESERVED_IMAGE_NAME_PREFIX)) {
    throw new RayitoCdkError("image_name_reserved", { name, prefix: RESERVED_IMAGE_NAME_PREFIX });
  }
  for (const candidate of [name, ...sizes.map((size) => `${name}-${size}`)]) {
    if (candidate.length > IMAGE_NAME_MAX_LENGTH || !IMAGE_NAME_PATTERN.test(candidate)) {
      throw new RayitoCdkError("image_name_invalid", {
        name: candidate,
        max: IMAGE_NAME_MAX_LENGTH,
      });
    }
  }
}

function checkOptions(options: {
  readonly baseImageVersion: string;
  readonly variant: string;
  readonly withEfs: boolean;
  readonly osCapabilities: boolean;
  readonly memoryMib: number;
  readonly sizes: readonly string[];
}): void {
  if (options.baseImageVersion === "" || options.baseImageVersion === LATEST_BASE_IMAGE_VERSION) {
    throw optionInvalid(
      "baseImageVersion",
      "la versión mayor de la imagen base gestionada (p. ej. 1); latest leería AWS al sintetizar",
    );
  }
  if (!(IMAGE_VARIANTS as readonly string[]).includes(options.variant)) {
    throw optionInvalid("variant", `admitidas: ${IMAGE_VARIANTS.join(", ")}`);
  }
  if (options.withEfs) {
    try {
      requireEfsCombination(
        options.variant,
        options.osCapabilities ? ALL_OS_CAPABILITIES : undefined,
      );
    } catch (error) {
      throw optionInvalid("withEfs", (error as Error).message);
    }
  }
  try {
    validateSizes(options.sizes.length === 0 ? undefined : options.sizes.join(","));
  } catch (error) {
    throw optionInvalid("sizes", (error as Error).message);
  }
  if (!Number.isInteger(options.memoryMib) || options.memoryMib <= 0) {
    throw optionInvalid("memoryMib", "un número entero de MiB");
  }
  if (options.sizes.length > 0 && options.memoryMib !== DEFAULT_MEMORY_MIB) {
    throw optionInvalid(
      "memoryMib",
      `con sizes la imagen sin sufijo es el baseline de ${DEFAULT_MEMORY_MIB} MiB`,
    );
  }
}

/**
 * `EnvironmentVariables` de la imagen base: las del usuario y, con
 * `allowedMountBuckets`, `RAYITO_ALLOWED_MOUNT_BUCKETS` (separados por
 * comas). Una clave que Rayito escribe no la puede traer el usuario.
 */
export function imageEnvironment(
  environment: Readonly<Record<string, string>>,
  allowedMountBuckets: readonly string[],
): Record<string, string> {
  for (const [key, value] of Object.entries(environment)) {
    if (key === ALLOWED_MOUNT_BUCKETS_ENV_VAR || key === BASELINE_MEMORY_ENV_VAR) {
      throw optionInvalid("environment", `${key} lo pone el constructo`);
    }
    if (
      !ENV_KEY_PATTERN.test(key) ||
      key.length > ENVIRONMENT_KEY_MAX_CHARS ||
      value.length > ENVIRONMENT_VALUE_MAX_CHARS
    ) {
      throw optionInvalid(
        "environment",
        `${key}: clave de letras, dígitos o _ (hasta ${ENVIRONMENT_KEY_MAX_CHARS}) y valor de hasta ${ENVIRONMENT_VALUE_MAX_CHARS} caracteres`,
      );
    }
  }
  for (const bucket of allowedMountBuckets) {
    if (!BUCKET_NAME_PATTERN.test(bucket)) {
      throw optionInvalid("allowedMountBuckets", `${bucket} no es un nombre de bucket`);
    }
  }
  const merged: Record<string, string> = { ...environment };
  if (allowedMountBuckets.length > 0) {
    merged[ALLOWED_MOUNT_BUCKETS_ENV_VAR] = [...new Set(allowedMountBuckets)].join(
      MOUNT_BUCKETS_SEPARATOR,
    );
  }
  if (Object.keys(merged).length + 1 > MAX_ENVIRONMENT_VARIABLES) {
    throw optionInvalid("environment", `como mucho ${MAX_ENVIRONMENT_VARIABLES - 1} variables`);
  }
  return merged;
}

function checkMarkers(artifact: MicrovmImageArtifact, variant: string, withEfs: boolean): void {
  const found = inspectArtifact(artifact.path);
  if (found.variant !== variant) {
    throw new RayitoCdkError("artifact_variant_mismatch", { found: found.variant, variant });
  }
  if (found.withEfs !== withEfs) {
    throw new RayitoCdkError("artifact_efs_mismatch", {
      state: found.withEfs ? HAS_MARKER : LACKS_MARKER,
      withEfs: String(withEfs),
    });
  }
}

function userTags(tags: Readonly<Record<string, string>> | undefined): Record<string, string> {
  const given = { ...(tags ?? {}) };
  try {
    validateTags(given);
  } catch (error) {
    throw optionInvalid("tags", (error as Error).message);
  }
  return given;
}

function sizeId(base: string, size: string): string {
  return `${base}${size.toUpperCase()}`;
}

/**
 * Una imagen de Lambda MicroVMs con `rayd` (la base y, con `sizes`, una más
 * por tamaño) publicada por CloudFormation con la configuración de
 * `rayito image publish`. Crea un asset S3 con el zip (uno para todos los
 * tamaños), un rol de build propio (lee sólo ese objeto y escribe sólo los
 * grupos de logs de sus imágenes) y, por imagen, un grupo de logs
 * `/rayito/<nombre>` (14 días) y un `AWS::Lambda::MicrovmImage`. Los
 * nombres que empiezan por `rayito-base` son de `rayito setup`.
 *
 * Coste y activación
 * -------------------
 * Activa: `cdk deploy` de un stack que lo contenga; sintetizar no crea nada
 *     ni llama a AWS.
 * Recursos y llamadas AWS: el asset del zip en el bucket del bootstrap de
 *     CDK, un rol de IAM, un grupo de logs y un `AWS::Lambda::MicrovmImage`
 *     por imagen (CloudFormation llama a `CreateMicrovmImage` /
 *     `UpdateMicrovmImage`). Los MicroVMs se crean con el SDK.
 * Coste aproximado: cada versión de imagen construida guarda su snapshot una
 *     semana (≈ $0,10 por GB-mes de snapshot por tamaño, consultado
 *     2026-10-19 en https://aws.amazon.com/lambda/pricing/); los logs se
 *     guardan 14 días; el asset sigue en el bucket del bootstrap hasta
 *     `cdk gc`.
 * IAM: quien despliega necesita `lambda:CreateMicrovmImage`,
 *     `lambda:PassNetworkConnector` e `iam:PassRole` sobre el rol de build
 *     (el rol de ejecución por defecto de CDK los tiene).
 * Cómo apagarla: quitar el constructo y `cdk deploy`, o `cdk destroy`;
 *     `rayito image prune --image-name <nombre>` borra versiones viejas.
 * Ejemplo:
 *   const artifact = await MicrovmImageArtifact.downloadRelease();
 *   new RayitoMicrovmImage(stack, "Image", {
 *     name: "team-sandbox",
 *     artifact,
 *     baseImageVersion: "1",
 *     sizes: ["4gb"],
 *   });
 */
export class RayitoMicrovmImage extends Construct {
  /** El nombre de la imagen base (sin sufijo de tamaño). */
  readonly imageName: string;
  /** El ARN de la imagen base (`Fn::GetAtt ImageArn`). */
  readonly imageArn: string;
  /** La última versión `ACTIVE` de la imagen base. */
  readonly latestActiveImageVersion: string;
  /** El ARN del rol de build que crea el constructo. */
  readonly buildRoleArn: string;
  /** `/rayito/<nombre>`: el grupo de logs del build de la imagen base. */
  readonly logGroupName: string;
  /** El ARN de cada imagen adicional, por nombre de tamaño. */
  readonly sizeImageArns: Readonly<Record<string, string>>;

  constructor(scope: Construct, id: string, props: RayitoMicrovmImageProps) {
    const environment = stackEnvironment(scope, "RayitoMicrovmImage");
    const sizes: readonly SizeName[] = [...(props.sizes ?? [])];
    const memoryMib = props.memoryMib ?? DEFAULT_MEMORY_MIB;
    const variant = props.variant ?? DEFAULT_VARIANT;
    const withEfs = props.withEfs ?? false;
    const osCapabilities = props.osCapabilities ?? false;
    checkNames(props.name, sizes);
    checkOptions({
      baseImageVersion: props.baseImageVersion,
      variant,
      withEfs,
      osCapabilities,
      memoryMib,
      sizes,
    });
    const variables = imageEnvironment(props.environment ?? {}, props.allowedMountBuckets ?? []);
    const tags = userTags(props.tags);
    checkMarkers(props.artifact, variant, withEfs);
    super(scope, id);
    const base: ImageSettings = {
      imageName: props.name,
      baseImageVersion: props.baseImageVersion,
      memoryMib,
      osCapabilities: osCapabilities ? ALL_OS_CAPABILITIES : undefined,
      environmentVariables: variables,
    };
    const settings: Array<[string, ImageSettings]> = [
      ["", base],
      ...sizes.map((size): [string, ImageSettings] => [size, sizedSettings(base, size)]),
    ];
    const asset = new Asset(this, "Artifact", { path: props.artifact.path });
    const logGroups = new Map(
      settings.map(([size, item]) => [
        size,
        new LogGroup(this, sizeId("LogGroup", size), {
          logGroupName: logGroupOf(item),
          retention: CDK_MICROVM_IMAGE_LOG_RETENTION_DAYS as RetentionDays,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      ]),
    );
    const role = new Role(this, "BuildRole", {
      assumedBy: new ServicePrincipal(LAMBDA_SERVICE, {
        conditions: { StringEquals: { [SOURCE_ACCOUNT_CONDITION]: environment.account } },
      }).withSessionTags(),
      inlinePolicies: {
        [BUILD_POLICY_NAME]: new PolicyDocument({
          statements: [
            new PolicyStatement({
              actions: ["s3:GetObject"],
              resources: [`arn:${Aws.PARTITION}:s3:::${asset.s3BucketName}/${asset.s3ObjectKey}`],
            }),
            new PolicyStatement({
              actions: LOG_ACTIONS,
              resources: [...logGroups.values()].map((group) => group.logGroupArn),
            }),
          ],
        }),
      },
    });
    const artifactUri = `s3://${asset.s3BucketName}/${asset.s3ObjectKey}`;
    const images = new Map(
      settings.map(([size, item]) => {
        const configuration = desiredConfiguration({
          region: environment.region,
          settings: item,
          artifactUri,
          buildRoleArn: role.roleArn,
        });
        const image = new CfnMicrovmImage(
          this,
          sizeId("Image", size),
          cfnImageProperties(item.imageName, configuration) as unknown as CfnMicrovmImageProps,
        );
        const group = logGroups.get(size);
        if (group !== undefined) {
          image.node.addDependency(group);
        }
        return [size, image] as const;
      }),
    );
    const baseImage = images.get("") as CfnMicrovmImage;
    images.delete("");
    this.imageName = props.name;
    this.imageArn = baseImage.attrImageArn;
    this.latestActiveImageVersion = baseImage.attrLatestActiveImageVersion;
    this.buildRoleArn = role.roleArn;
    this.logGroupName = logGroupOf(base);
    this.sizeImageArns = Object.fromEntries(
      [...images.entries()].map(([size, image]) => [size, image.attrImageArn]),
    );
    applyTags(this, "RayitoMicrovmImage", tags);
  }
}
