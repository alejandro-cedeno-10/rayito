/**
 * `cfnImageProperties` (ADR-032 D9.3): la configuración que `rayito image
 * publish` manda a `CreateMicrovmImage` (`desiredConfiguration`, nombres de
 * la API en camelCase) con los nombres de las propiedades de
 * `AWS::Lambda::MicrovmImage` (`AWS_API_NOTES.md`, «Constructos CDK»). Puro:
 * ningún import de `aws-cdk-lib`; el constructo pasa el resultado a
 * `CfnMicrovmImage`, cuyas props son esos nombres en camelCase.
 *
 * El recurso exige todas las propiedades: las que la API admite omitir van
 * vacías (`AdditionalOsCapabilities`, `EgressNetworkConnectors`,
 * `EnvironmentVariables`). Las variables de entorno van como lista de
 * `Key`/`Value` ordenada por clave.
 */

import { CDK_OWNERSHIP_SUFFIX } from "../limits.js";
import { formatTemplate } from "../template-text.js";

/** `contract.constants.microvmImageDescriptionTemplate`. */
export const MICROVM_IMAGE_DESCRIPTION_TEMPLATE = `Rayito image {name} (rayito.microvm-image/1${CDK_OWNERSHIP_SUFFIX})`;

/** Una variable de entorno de la imagen en la forma del recurso. */
export interface CfnEnvironmentVariable {
  readonly key: string;
  readonly value: string;
}

/** Las props de `CfnMicrovmImage` (sin `tags`, que pone `Tags.of`). */
export interface CfnImageProperties {
  readonly name: string;
  readonly description: string;
  readonly baseImageArn: string;
  readonly baseImageVersion: string;
  readonly buildRoleArn: string;
  readonly codeArtifact: { readonly uri: string };
  readonly resources: ReadonlyArray<{ readonly minimumMemoryInMiB: number }>;
  readonly cpuConfigurations: ReadonlyArray<{ readonly architecture: string }>;
  readonly hooks: Readonly<Record<string, unknown>>;
  readonly logging: { readonly cloudWatch: { readonly logGroup: string } };
  readonly additionalOsCapabilities: readonly string[];
  readonly egressNetworkConnectors: readonly string[];
  readonly environmentVariables: readonly CfnEnvironmentVariable[];
}

/** La descripción con la marca `+cdk`; sin fecha ni versión: cambiarla crearía otra versión. */
export function microvmImageDescription(name: string): string {
  return formatTemplate(MICROVM_IMAGE_DESCRIPTION_TEMPLATE, { name });
}

function environmentList(value: unknown): CfnEnvironmentVariable[] {
  const variables = (value ?? {}) as Readonly<Record<string, string>>;
  return Object.keys(variables)
    .sort()
    .map((key) => ({ key, value: variables[key] ?? "" }));
}

/** `configuration` (de `desiredConfiguration`) con los nombres del recurso, `name` y su descripción. */
export function cfnImageProperties(
  name: string,
  configuration: Readonly<Record<string, unknown>>,
): CfnImageProperties {
  return {
    name,
    description: microvmImageDescription(name),
    baseImageArn: configuration.baseImageArn as string,
    baseImageVersion: configuration.baseImageVersion as string,
    buildRoleArn: configuration.buildRoleArn as string,
    codeArtifact: configuration.codeArtifact as CfnImageProperties["codeArtifact"],
    resources: configuration.resources as CfnImageProperties["resources"],
    cpuConfigurations: configuration.cpuConfigurations as CfnImageProperties["cpuConfigurations"],
    hooks: configuration.hooks as CfnImageProperties["hooks"],
    logging: configuration.logging as CfnImageProperties["logging"],
    additionalOsCapabilities: [...((configuration.additionalOsCapabilities ?? []) as string[])],
    egressNetworkConnectors: [],
    environmentVariables: environmentList(configuration.environmentVariables),
  };
}
