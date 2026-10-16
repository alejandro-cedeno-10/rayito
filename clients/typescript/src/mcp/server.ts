/**
 * `createMcpApp`: el servidor `rayito` con sus seis herramientas (espejo de
 * `rayito.mcp._server`). El `SandboxLease` es del proceso y las instancias de
 * `McpServer` son baratas: el SDK pide una por conexión (stdio) o por
 * petición (HTTP), y todas comparten el mismo lease, así que hay un solo
 * sandbox por proceso.
 *
 * Nombres, descripciones, anotaciones y esquemas son el contrato de
 * `testdata/mcp/tools.json`, que también comprueba el servidor de Python.
 */

import { MCP_MAX_OUTPUT_CHARS } from "../limits.js";
import { VERSION } from "../version.js";
import type { SandboxEnvironment } from "./lease.js";
import { SandboxLease } from "./lease.js";
import type { McpPeers, McpSdk, Zod } from "./peers.js";
import type { McpSettings } from "./settings.js";
import {
  DEFAULT_CODE_TIMEOUT_SECONDS,
  DEFAULT_COMMAND_TIMEOUT_SECONDS,
  DEFAULT_LIST_DEPTH,
  DEFAULT_LIST_PATH,
  MAX_LIST_DEPTH,
  MAX_TOOL_TIMEOUT_SECONDS,
  McpTools,
} from "./tools.js";

export const SERVER_NAME = "rayito";

export const SERVER_INSTRUCTIONS =
  "Todas las herramientas comparten un único sandbox Linux (usuario uid 1000, " +
  "directorio /home/user) con un kernel Python que conserva el estado entre " +
  "llamadas a run_code; hay salida a internet salvo que la imagen lo impida. " +
  "El sandbox aparece en la primera llamada que lo necesita y se destruye cuando " +
  "el servidor termina; si pasa un rato sin uso AWS lo suspende y la siguiente " +
  "llamada lo reanuda sola. Un exit_code distinto de cero en run_command o un " +
  "error del kernel en run_code son datos para leer, no fallos que reintentar " +
  "a ciegas.";

const THOUSANDS_GROUP = /\B(?=(\d{3})+(?!\d))/g;

/** `100000` como `100 000`: el grupo de miles con espacio que usan las descripciones en español. */
function spacedThousands(value: number): string {
  return String(value).replace(THOUSANDS_GROUP, " ");
}

const RUN_CODE_DESCRIPTION =
  "Ejecuta código Python en el kernel con estado del sandbox (las variables " +
  "persisten entre llamadas). Devuelve un bloque JSON con `text`, `stdout`, " +
  "`stderr`, `error`, `execution_count` y los mime types de cada resultado, " +
  "seguido de las imágenes PNG/JPEG y los SVG que produjo la celda. Un error " +
  "del kernel llega en `error`.";
const RUN_COMMAND_DESCRIPTION =
  "Ejecuta un comando de shell en el sandbox y espera a que termine. Devuelve " +
  "`stdout`, `stderr` y `exit_code`; un exit code distinto de cero es " +
  "información, no un error.";
const READ_FILE_DESCRIPTION =
  "Lee un fichero de texto UTF-8 del sandbox. Devuelve `content`, `size` y " +
  `\`truncated\` (la salida se corta a ${spacedThousands(MCP_MAX_OUTPUT_CHARS)} caracteres).`;
const WRITE_FILE_DESCRIPTION =
  "Escribe un fichero de texto UTF-8 en el sandbox, creando los directorios " +
  "intermedios y sobrescribiendo si ya existe. Devuelve `path` y `size`.";
const LIST_FILES_DESCRIPTION =
  "Lista un directorio del sandbox hasta `depth` niveles. Cada entrada trae " +
  "`name`, `path`, `type` (`file`, `dir`, `symlink` o null), `size` y " +
  "`modified_time` (ISO 8601).";
const LIST_SANDBOXES_DESCRIPTION =
  "Lista los sandboxes vivos de la imagen configurada en `RAYITO_TEMPLATE` " +
  "(cualquier estado salvo terminado); `current` marca el de este servidor. " +
  "Nunca crea un sandbox.";

const CODE_DESCRIPTION = "Código Python que ejecutar en el kernel del sandbox.";
const CODE_TIMEOUT_DESCRIPTION =
  "Segundos de reloj que el agente concede a la celda antes de interrumpirla.";
const COMMAND_DESCRIPTION = "Comando que ejecuta /bin/sh -c como el usuario del sandbox.";
const COMMAND_TIMEOUT_DESCRIPTION = "Segundos de reloj antes de que el agente mate el comando.";
const CWD_DESCRIPTION = "Directorio de trabajo; por defecto /home/user.";
const PATH_DESCRIPTION = "Ruta absoluta o relativa a /home/user.";
const CONTENT_DESCRIPTION = "Contenido del fichero en UTF-8.";
const LIST_PATH_DESCRIPTION = "Directorio que listar.";
const DEPTH_DESCRIPTION = "Niveles de subdirectorios que incluir.";

export interface McpApp {
  /** El único sandbox del proceso; el llamador lo cierra al terminar. */
  readonly lease: SandboxLease;
  /** Una instancia nueva del servidor, con las seis herramientas, sobre el mismo lease. */
  createServer(): InstanceType<McpSdk["McpServer"]>;
}

export function createMcpApp(
  peers: McpPeers,
  settings: McpSettings,
  environment: SandboxEnvironment = {},
): McpApp {
  const lease = new SandboxLease(settings, environment);
  const tools = new McpTools(lease, environment);
  return {
    lease,
    createServer: () => buildServer(peers, tools),
  };
}

function buildServer(peers: McpPeers, tools: McpTools): InstanceType<McpSdk["McpServer"]> {
  const server = new peers.sdk.McpServer(
    { name: SERVER_NAME, version: VERSION },
    { instructions: SERVER_INSTRUCTIONS },
  );
  registerTools(server, peers.zod.z, tools);
  return server;
}

function boundedSeconds(z: Zod["z"], fallback: number, description: string) {
  return z
    .number()
    .int()
    .min(1)
    .max(MAX_TOOL_TIMEOUT_SECONDS)
    .default(fallback)
    .describe(description);
}

function registerTools(
  server: InstanceType<McpSdk["McpServer"]>,
  z: Zod["z"],
  tools: McpTools,
): void {
  server.registerTool(
    "run_code",
    {
      description: RUN_CODE_DESCRIPTION,
      inputSchema: z.object({
        code: z.string().describe(CODE_DESCRIPTION),
        timeout: boundedSeconds(z, DEFAULT_CODE_TIMEOUT_SECONDS, CODE_TIMEOUT_DESCRIPTION),
      }),
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    },
    (args) => tools.runCode(args),
  );
  server.registerTool(
    "run_command",
    {
      description: RUN_COMMAND_DESCRIPTION,
      inputSchema: z.object({
        cmd: z.string().describe(COMMAND_DESCRIPTION),
        timeout: boundedSeconds(z, DEFAULT_COMMAND_TIMEOUT_SECONDS, COMMAND_TIMEOUT_DESCRIPTION),
        cwd: z.string().optional().describe(CWD_DESCRIPTION),
      }),
      outputSchema: z.object({
        stdout: z.string(),
        stderr: z.string(),
        exit_code: z.number().int(),
        truncated: z.boolean(),
      }),
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    },
    (args) => tools.runCommand(args),
  );
  server.registerTool(
    "read_file",
    {
      description: READ_FILE_DESCRIPTION,
      inputSchema: z.object({ path: z.string().describe(PATH_DESCRIPTION) }),
      outputSchema: z.object({
        path: z.string(),
        content: z.string(),
        size: z.number().int(),
        truncated: z.boolean(),
      }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    (args) => tools.readFile(args),
  );
  server.registerTool(
    "write_file",
    {
      description: WRITE_FILE_DESCRIPTION,
      inputSchema: z.object({
        path: z.string().describe(PATH_DESCRIPTION),
        content: z.string().describe(CONTENT_DESCRIPTION),
      }),
      outputSchema: z.object({ path: z.string(), size: z.number().int() }),
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    },
    (args) => tools.writeFile(args),
  );
  server.registerTool(
    "list_files",
    {
      description: LIST_FILES_DESCRIPTION,
      inputSchema: z.object({
        path: z.string().default(DEFAULT_LIST_PATH).describe(LIST_PATH_DESCRIPTION),
        depth: z
          .number()
          .int()
          .min(1)
          .max(MAX_LIST_DEPTH)
          .default(DEFAULT_LIST_DEPTH)
          .describe(DEPTH_DESCRIPTION),
      }),
      outputSchema: z.object({
        path: z.string(),
        entries: z.array(
          z.object({
            name: z.string(),
            path: z.string(),
            type: z.string().nullable(),
            size: z.number().int(),
            modified_time: z.string(),
          }),
        ),
      }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    (args) => tools.listFiles(args),
  );
  server.registerTool(
    "list_sandboxes",
    {
      description: LIST_SANDBOXES_DESCRIPTION,
      outputSchema: z.object({
        sandboxes: z.array(
          z.object({
            sandbox_id: z.string(),
            state: z.string(),
            template: z.string(),
            template_version: z.string(),
            started_at: z.string(),
            current: z.boolean(),
          }),
        ),
      }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    () => tools.listSandboxes(),
  );
}
