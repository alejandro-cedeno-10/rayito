/**
 * Los textos de `--target agentcore` que el SDK produce (ADR-031): los errores
 * cerrados con su código de salida, las líneas de seguridad, los avisos del
 * plan, el coste de lista y el progreso del despliegue y del borrado. Son los
 * mismos del SDK de Python: `testdata/agentcore-deploy/contract.json` los fija
 * en los dos SDK y los tests de cada uno los comparan. Puro: sin imports de AWS
 * ni de E/S. Ningún mensaje lleva un ARN, una URL, un token ni el texto crudo
 * de AWS.
 */

export const AGENTCORE_ERROR_CODES = [
  "unsupported_region",
  "option_not_supported",
  "option_requires_agentcore",
  "target_unsupported",
  "provider_unsupported",
  "network_policy_unsupported",
  "name_invalid",
  "name_conflict",
  "model_required",
  "image_missing",
  "lifecycle_invalid",
  "tags_invalid",
  "env_invalid",
  "raw_protocol",
  "no_bucket",
  "bucket_region",
  "needs_confirmation",
  "session_required",
  "stack_blocked",
  "stack_failed",
  "image_build_failed",
  "image_build_timeout",
  "build_quota",
  "image_too_large",
  "runtime_blocked",
  "runtime_failed",
  "runtime_timeout",
  "runtime_not_found",
  "delete_failed",
  "access_denied",
  "aws_error",
  "lifecycle_order",
  "delete_needs_confirmation",
] as const;

export type AgentCoreErrorCode = (typeof AGENTCORE_ERROR_CODES)[number];

/** El código de salida de la CLI de cada error: 2 es uso o entorno, 1 es un fallo del lado de AWS. */

export const ERROR_EXIT_CODES: Readonly<Record<AgentCoreErrorCode, number>> = {
  unsupported_region: 2,

  option_not_supported: 2,

  option_requires_agentcore: 2,

  target_unsupported: 2,

  provider_unsupported: 2,

  network_policy_unsupported: 2,

  name_invalid: 2,

  name_conflict: 2,

  model_required: 2,

  image_missing: 2,

  lifecycle_invalid: 2,

  tags_invalid: 2,

  env_invalid: 2,

  raw_protocol: 2,

  no_bucket: 2,

  bucket_region: 2,

  needs_confirmation: 2,

  session_required: 2,

  stack_blocked: 1,

  stack_failed: 1,

  image_build_failed: 1,

  image_build_timeout: 1,

  build_quota: 1,

  image_too_large: 1,

  runtime_blocked: 1,

  runtime_failed: 1,

  runtime_timeout: 1,

  runtime_not_found: 1,

  delete_failed: 1,

  access_denied: 1,

  aws_error: 1,

  lifecycle_order: 2,

  delete_needs_confirmation: 2,
};

/** La plantilla en español de cada error (`{campo}` se rellena con `formatTemplate`). */

export const ERROR_MESSAGES: Readonly<Record<AgentCoreErrorCode, string>> = {
  unsupported_region:
    "la región {region} no tiene AgentCore Runtime con builds arm64 de CodeBuild: usa una de {regions}",

  option_not_supported: "{option} no aplica a --target agentcore: {reason}",

  option_requires_agentcore: "{option} sólo aplica a --target agentcore",

  target_unsupported:
    "rayito agent {command} sólo aplica a --target agentcore: para una flota de MicroVMs usa rayito agent stop o status",

  provider_unsupported:
    "el proveedor de modelo {provider} no está soportado en AgentCore: sin pasarela de secretos sólo se cablea bedrock, con el rol del runtime",

  network_policy_unsupported:
    "AgentCore no puede aplicar la política de salida del proyecto ({what}): en modo PUBLIC el agente sale a Internet sin filtro; quítala para desplegar en AgentCore",

  name_invalid:
    "el nombre {name} no vale para AgentCore: hasta {max} caracteres, minúsculas y dígitos, con - o _ sueltos entre ellos; cámbialo con [image].template",

  name_conflict:
    "ya existe un runtime {runtimeName} que no es del agente {name} de Rayito: cambia el nombre con [image].template",

  model_required:
    "no se sabe qué modelo de Bedrock usa el agente: pásalo con --bedrock-model (bedrock_models en el SDK)",

  image_missing:
    "--no-build: no hay ninguna imagen {imageTag} en {repositoryName}; despliega sin --no-build",

  lifecycle_invalid: "{option} debe ser un número entero de segundos entre {min} y {max}",

  tags_invalid:
    "--tag necesita CLAVE=VALOR, hasta {max} etiquetas, con letras, dígitos, espacios y _ . : / = + - @",

  env_invalid:
    "AgentCore admite como mucho {maxVariables} variables de entorno de hasta {maxChars} caracteres",

  raw_protocol:
    "una app con protocol='raw' no cumple el contrato de AgentCore (/ping + /invocations)",

  no_bucket:
    "no hay bucket para el contexto de build: pasa --bucket, [deploy].bucket o RAYITO_BUCKET, o crea uno con rayito setup",

  bucket_region:
    "el bucket {bucket} está en {bucketRegion}, no en {region}: CodeBuild sólo lee el código de un bucket de su región",

  needs_confirmation:
    "desplegar en AgentCore crea recursos con coste: confírmalo con --yes (o desde una terminal)",

  session_required:
    "con --target agentcore, rayito agent stop necesita --session-id: AgentCore no cobra sin sesiones; para borrar el agente usa rayito agent delete",

  stack_blocked:
    "la pila {stackName} está en {status}: bórrala con rayito agent delete {name} --target agentcore y vuelve a desplegar",

  stack_failed:
    "la pila {stackName} no se pudo desplegar ({status}): mira sus eventos en la consola de CloudFormation",

  image_build_failed:
    "el build {buildId} terminó en {status}: los logs están en el grupo {logGroup}",

  image_build_timeout:
    "el build {buildId} sigue en curso tras {seconds} s: síguelo en el grupo {logGroup}",

  build_quota:
    "CodeBuild rechazó el build por la cuota de builds simultáneos de la cuenta: espera a que acaben los que hay o pide más cuota",

  image_too_large: "la imagen ocupa {megabytes} MB y AgentCore admite como mucho {maxMegabytes} MB",

  runtime_blocked:
    "el runtime {runtimeName} está en {status}: bórralo con rayito agent delete {name} --target agentcore y vuelve a desplegar",

  runtime_failed: "el runtime {runtimeName} quedó en {status}: mira sus logs en {logGroup}",

  runtime_timeout: "el runtime {runtimeName} sigue en {status} tras {seconds} s",

  runtime_not_found: "no hay ningún agente de Rayito llamado {name} en AgentCore en {region}",

  delete_failed: "no se pudo borrar {resource} {resourceName} ({status})",

  access_denied:
    "faltan permisos para {operation} ({awsCode}): mira los permisos de --target agentcore en la guía",

  aws_error: "AWS rechazó {operation} ({awsCode})",

  lifecycle_order:
    "idle_session_timeout ({idle} s) no puede superar max_session_lifetime ({max} s)",

  delete_needs_confirmation:
    "borrar un agente no se puede deshacer: confírmalo con --yes (o desde una terminal)",
};

/** Las líneas de la sección «Seguridad:» del plan; el plan guarda sus claves. */
export const SECURITY_MESSAGES = {
  noGateway:
    "AgentCore no tiene la pasarela de secretos de Rayito: el agente obtiene las credenciales del rol del runtime, y cualquier código que corra en la sesión puede leerlas",
  bedrockModels: "el rol del runtime puede invocar en Bedrock: {models}",
  noBedrockModels: "el rol del runtime no puede invocar ningún modelo de Bedrock",
  readableSecrets: "el rol del runtime puede leer los secretos: {secrets}",
  publicNetwork:
    "red PUBLIC: el agente tiene salida a Internet y la política de salida de Rayito no se aplica",
  sessionIsolation:
    "cada sesión corre en su propia microVM de AgentCore, aislada de las demás; AgentCore no asocia sesiones a usuarios: eso es cosa de tu backend",
  envCredentials:
    "este framework lee las credenciales de variables de entorno: el host se las pone, leídas del rol del runtime, en cada invocación",
} as const;

export type SecurityKey = keyof typeof SECURITY_MESSAGES;

/** Los avisos del plan por valores del fichero de proyecto que AgentCore no usa; el plan guarda sus claves. */
export const WARNING_MESSAGES = {
  fileScaling:
    "[scaling] del proyecto no aplica en AgentCore: AgentCore crea una microVM por sesión y escala solo",
  fileMemory:
    "memory_mib del proyecto no aplica en AgentCore: cada sesión usa hasta 2 vCPU y 8 GB y se cobra por consumo real",
  filePort: "[serve].port no aplica en AgentCore: el contenedor sirve en el 8080",
  fileTimeout:
    "[serve].timeout no aplica en AgentCore: la vida de una sesión es max_session_lifetime (8 h por defecto)",
  fileIdle: "[serve].idle no aplica en AgentCore: usa idle_session_timeout (15 min por defecto)",
  fileBaseImage:
    "[deploy].base_image_version no aplica en AgentCore: no usa la imagen base de MicroVMs",
} as const;

export type WarningKey = keyof typeof WARNING_MESSAGES;

/** Las líneas de coste de lista del plan (us-east-1, `AWS_API_NOTES.md`, consultado 2026-10-19). */
export const COST_MESSAGES = {
  runtime:
    "runtime (plataforma V1, la que crea Rayito): $0,0895 por vCPU-hora y $0,00945 por GB-hora de uso real, por segundo; la CPU no se cobra en espera de E/S y la memoria sí, hasta que la sesión termina (por defecto, 15 min sin uso); sin sesiones no hay cómputo",
  build:
    "build: $0,0034 por minuto en CodeBuild arm1.small, redondeado al minuto (100 minutos al mes en el nivel gratuito)",
  image: "imagen en ECR: $0,10 por GB-mes; se conservan las últimas {retained}",
  logs: "logs: $0,50 por GB ingerido en CloudWatch Logs",
  model: "el modelo lo cobra su proveedor",
} as const;

/** Las líneas de progreso del despliegue; el `kind` del paso es la clave. */
export const PROGRESS_MESSAGES = {
  stackDeploy: "desplegando la pila {stackName}",
  stackWait: "esperando a la pila {stackName} ({status})",
  stackReady: "pila {stackName} lista",
  sourceUpload: "subiendo el contexto de build ({bytes} bytes)",
  buildStart: "construyendo la imagen {imageTag} con CodeBuild (build {buildId})",
  imageReady: "imagen {imageTag} lista ({megabytes} MB)",
  imageReused: "imagen {imageTag} reutilizada: el contenido no cambió",
  runtimeCreate: "creando el runtime {runtimeName}",
  runtimeRequireMmdsV2: "exigiendo MMDSv2 en el runtime {runtimeName}",
  runtimeUpdate: "actualizando el runtime {runtimeName}",
  runtimeWait: "esperando al runtime {runtimeName} ({status})",
  runtimeReady: "runtime {runtimeName} listo (versión {version})",
  runtimeReuse: "runtime {runtimeName} al día (versión {version}): nada que cambiar",
  stackNoChanges: "pila {stackName} sin cambios",
  sourceDeleted: "contexto de build borrado del bucket",
} as const;

export type ProgressKey = keyof typeof PROGRESS_MESSAGES;

/** Las líneas de progreso de `rayito agent delete`; el `kind` del paso es la clave. */
export const DELETE_PROGRESS_MESSAGES = {
  runtime: "borrando el runtime {runtimeName}",
  stack: "borrando la pila {stackName}",
  logGroup: "borrando el grupo de logs {logGroup}",
} as const;

export type DeleteProgressKey = keyof typeof DELETE_PROGRESS_MESSAGES;
