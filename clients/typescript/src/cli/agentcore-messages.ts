/**
 * Los textos de la salida humana de `rayito agent ... --target agentcore`
 * (ADR-031): encabezados, pasos, avisos, coste, líneas finales y las de
 * `list`, `delete` y `stop`. Son los mismos de la CLI de Python:
 * `testdata/agentcore-deploy/contract.json` los fija en las dos CLI y los tests
 * de cada una los comparan. Sólo texto: no lee AWS ni escribe en la terminal.
 */

/** El encabezado del plan. */
export const HEADER_TEMPLATE =
  "rayito agent deploy: {name} en Amazon Bedrock AgentCore Runtime, región {region}";

/** Un paso del plan o del despliegue. */
export const STEP_TEMPLATE = "  {label} {name}: {action}";

/** La etiqueta de cada recurso de un paso. */
export const STEP_LABELS = {
  stack: "pila",
  image: "imagen",
  runtime: "runtime",
} as const;

/** Lo que crea la pila, tras el paso de la pila. */
export const STACK_CONTENTS_LINE =
  "  (la pila crea el repositorio ECR, el proyecto de CodeBuild, su grupo de logs, el rol de build y el rol del runtime)";

/** El texto de cada acción; algunas llevan `{status}` o `{version}`. */
export const ACTION_TEMPLATES = {
  stackPlanned: "crear o actualizar (se decide al leer la cuenta)",
  stackCreate: "crear",
  stackUpdate:
    "actualizar si cambió (CloudFormation no toca nada si la plantilla y los parámetros son los mismos)",
  stackReuse: "ya existe ({status})",
  stackWait: "en curso ({status}): esperar",
  imagePlanned: "construir con CodeBuild si no existe (sin Docker local)",
  imageBuild: "construir con CodeBuild (sin Docker local)",
  imageReuse: "ya existe",
  runtimePlanned: "crear o actualizar",
  runtimeCreate: "crear y exigir MMDSv2",
  runtimeUpdate: "actualizar (versión nueva)",
  runtimeReuse: "ya está al día (versión {version})",
  runtimeWait: "en curso ({status}): esperar",
} as const;

/** El encabezado de la sección de seguridad. */
export const SECURITY_HEADER = "Seguridad:";

/** Una línea de seguridad. */
export const SECURITY_LINE_TEMPLATE = "  - {text}";

/** El encabezado de los avisos. */
export const WARNINGS_HEADER = "Avisos:";

/** Un aviso. */
export const WARNING_LINE_TEMPLATE = "  aviso: {text}";

/** El encabezado del coste. */
export const COST_HEADER = "Coste aproximado (us-east-1, precios de lista):";

/** Una línea de coste. */
export const COST_LINE_TEMPLATE = "  {text}";

/** La última línea de `--dry-run`. */
export const DRY_RUN_FOOTER = "no se creó ni cambió nada (--dry-run)";

/** La pregunta de confirmación del despliegue. */
export const CONFIRM_QUESTION = "¿Desplegar en AgentCore?";

/** Las líneas `clave=valor` con las que termina un despliegue. */
export const DEPLOYED_LINES = [
  "target=agentcore",
  "name={name}",
  "runtime_id={runtimeId}",
  "runtime_arn={runtimeArn}",
  "version={version}",
  "region={region}",
  "logs={logGroup}",
  'invoca con: rayito agent invoke {name} "<prompt>" --target agentcore',
] as const;

/** Las líneas de `rayito agent status --target agentcore`. */
export const STATUS_LINES = [
  "target: agentcore",
  "name: {name}",
  "runtime_id: {runtimeId}",
  "version: {version}",
  "status: {status}",
  "image: {imageUri}",
  "region: {region}",
  "logs: {logGroup}",
] as const;

/** Un agente de `rayito agent list`. */
export const LIST_LINE_TEMPLATE =
  "{name} runtime={runtimeName} id={runtimeId} version={version} status={status}";

/** Sin agentes en la región. */
export const LIST_EMPTY_TEMPLATE = "no hay agentes de Rayito en AgentCore en {region}";

/** El encabezado de `rayito agent delete`. */
export const DELETE_HEADER_TEMPLATE = "rayito agent delete: {name} en AgentCore, región {region}";

/** El runtime que se borra. */
export const DELETE_RUNTIME_LINE = "  runtime {runtimeName} ({runtimeId})";

/** La pila que se borra. */
export const DELETE_STACK_LINE =
  "  pila {stackName} (repositorio ECR con sus imágenes, proyecto y logs de CodeBuild, rol de build y rol del runtime)";

/** El grupo de logs que se borra. */
export const DELETE_LOG_GROUPS_LINE = "  grupo de logs del runtime {logGroup}";

/** La pregunta de confirmación del borrado. */
export const DELETE_CONFIRM_QUESTION = "¿Borrar el agente y sus recursos?";

/** No había nada que borrar. */
export const DELETE_NOTHING_TEMPLATE =
  "no hay ningún despliegue de Rayito llamado {name} en {region}: nada que borrar";

/** El resultado de un borrado. */
export const DELETED_TEMPLATE =
  "borrado {name}: runtime {runtimeDeleted}, pila {stackDeleted}, grupo de logs {logGroupDeleted}";

/** Una sesión parada. */
export const STOPPED_TEMPLATE = "sesión {sessionId} parada en {name}";

/** El prefijo de la línea de error en stderr. */
export const ERROR_PREFIX = "rayito: ";
