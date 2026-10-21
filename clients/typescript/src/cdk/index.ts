/**
 * `rayito/cdk`: los constructos CDK de Rayito (agentcore-cdk, ADR-032).
 * `RayitoAgent` despliega un agente en Amazon Bedrock AgentCore Runtime y
 * `RayitoMicrovmImage` publica una imagen de Lambda MicroVMs con `rayd`, los
 * dos con `cdk deploy`. Necesita los peers opcionales `aws-cdk-lib`
 * (^2.261.0) y `constructs` (^10.5.0); ni `rayito` ni sus otras entradas los
 * cargan nunca.
 *
 * Coste y activación: sintetizar no llama a AWS ni crea nada; sólo
 * `cdk deploy` de un stack con alguno de los constructos crea recursos con
 * coste (cada constructo lo detalla en su TSDoc).
 */

export {
  AgentImageBuild,
  type CodeBuildOptions,
  RayitoAgent,
  type RayitoAgentProps,
} from "./agent.js";
export {
  type DownloadReleaseOptions,
  type FromFileOptions,
  MicrovmImageArtifact,
} from "./artifact.js";
export { RayitoCdkError } from "./errors.js";
export { RayitoMicrovmImage, type RayitoMicrovmImageProps } from "./microvm-image.js";
