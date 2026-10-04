/**
 * La tabla D14: cada nombre de E2B 2.x que Lambda MicroVMs no puede dar, con
 * su motivo. Los textos son idénticos a los de `rayito/e2b/_unimplemented.py`
 * (una única cadena por SDK); la clave es también el `feature` del error.
 */

import { UnimplementedError } from "../errors.js";

export const COMPAT_DOC_PATH = "docs/site/docs/e2b-compat.md";

const NO_MEMORY_COPY_REASON =
  "ninguna operación de Lambda MicroVMs copia la memoria de un MicroVM en marcha " +
  "(AWS_API_NOTES.md §1 y §15); el análogo de sólo ficheros es checkpoint_files() + create(persist=)";
const SUSPEND_KEEPS_MEMORY_REASON =
  "suspend-microvm siempre guarda memoria y disco (AWS_API_NOTES.md §5); " +
  "el análogo es checkpoint_files() + kill()";
const MCP_REASON =
  "cada petición al endpoint necesita además un JWE en cabecera con TTL de 60 min como máximo " +
  "(AWS_API_NOTES.md §3 y §7), así que una URL con token fijo no sirve; usa el servidor rayito-mcp";
const VOLUME_REASON =
  "sin un volume_store/volumeStore configurado en el cliente E2B no hay volumen; incluso " +
  "configurado, volumes=/volume_mounts sigue en UnimplementedError (experimental: ninguna " +
  "imagen publicada trae amazon-efs-utils, AWS_API_NOTES.md §22, m15-efs-volumes); usa " +
  "persist= (S3) o upload_url/download_url mientras tanto";
const VOLUME_CONTENT_REASON =
  "no hay plano de datos de ficheros fuera de un MicroVM (SPEC.md §4); conecta un sandbox y " +
  "monta el volumen, o usa upload_url/download_url sobre persist=";
// m15-templates: create/update-microvm-image no expone un Tags por versión (sólo por imagen,
// con lambda:TagResource aparte), así que el etiquetado de E2B no tiene análogo todavía.
const TEMPLATE_TAGS_REASON =
  "create/update-microvm-image no admite etiquetas por versión (sólo por imagen, con " +
  "lambda:TagResource aparte, AWS_API_NOTES.md §27): usa el ARN de la imagen con la CLI de " +
  "AWS mientras tanto";

export const UNIMPLEMENTED_REASONS: Readonly<Record<string, string>> = Object.freeze({
  fork: NO_MEMORY_COPY_REASON,
  createSnapshot: NO_MEMORY_COPY_REASON,
  listSnapshots: NO_MEMORY_COPY_REASON,
  deleteSnapshot: NO_MEMORY_COPY_REASON,
  "connect({ onResume: 'reboot' })":
    "resume-microvm siempre restaura memoria y disco (AWS_API_NOTES.md §5); " +
    "el análogo es reincarnate(), con un id nuevo",
  "pause({ keepMemory: false })": SUSPEND_KEEPS_MEMORY_REASON,
  "lifecycle.onTimeout.keepMemory=false": SUSPEND_KEEPS_MEMORY_REASON,
  "network.rules":
    "no hay un proxy de egress fuera del VM donde inyectar cabeceras: el proxy de Lambda MicroVMs " +
    "sólo gestiona el ingress (AWS_API_NOTES.md §7)",
  "network.maskRequestHost":
    "el proxy de Lambda MicroVMs siempre reenvía Host: <endpoint> y no lo reescribe (AWS_API_NOTES.md §7)",
  "network.allowPublicTraffic=true":
    "no existe acceso sin autenticar: toda petición al endpoint exige X-aws-proxy-auth " +
    "(AWS_API_NOTES.md §3 y §7); allow_public_traffic=False es el comportamiento permanente",
  iam:
    "los MicroVMs no emiten tokens con audiencia: la única identidad es el execution role por " +
    "IMDSv2 (AWS_API_NOTES.md §9)",
  mcp: MCP_REASON,
  getMcpUrl: MCP_REASON,
  getMcpToken: MCP_REASON,
  Volume: VOLUME_REASON,
  "volume.content": VOLUME_CONTENT_REASON,
  getSignature:
    "una firma de envd no autentica en el proxy: el JWE sólo viaja en cabecera o en el subprotocolo " +
    "WebSocket (AWS_API_NOTES.md §7); usa upload_url/download_url, que firman en S3",
  // m15-templates: Template ya construye de verdad (./template.js, sobre rayito.Template);
  // sólo el etiquetado de E2B sigue sin equivalente.
  "Template.aliasExists": TEMPLATE_TAGS_REASON,
  "Template.assignTags": TEMPLATE_TAGS_REASON,
  "Template.removeTags": TEMPLATE_TAGS_REASON,
  "Template.getTags": TEMPLATE_TAGS_REASON,
});

/** El `UnimplementedError` de la tabla, con la página de compatibilidad como `doc`. */
export function unimplemented(feature: string): UnimplementedError {
  const reason = UNIMPLEMENTED_REASONS[feature];
  if (reason === undefined) {
    throw new RangeError(`feature sin motivo en la tabla D14: ${feature}`);
  }
  return new UnimplementedError(feature, reason, COMPAT_DOC_PATH);
}

/** Para los miembros asíncronos: E2B devuelve una promesa, así que el error llega rechazado. */
export function rejectUnimplemented(feature: string): Promise<never> {
  return Promise.reject(unimplemented(feature));
}
