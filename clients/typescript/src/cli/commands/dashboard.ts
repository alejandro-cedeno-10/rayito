/**
 * `rayito dashboard`: el panel de gestión local (ADR-026, fase 1). Sirve en
 * `127.0.0.1` una SPA y la API `/api/v1` sobre tus credenciales de AWS, hasta
 * que pulses Ctrl+C. De sólo lectura salvo `--allow-actions`. Con
 * `--account` (fase 3, opt-in) añade otras cuentas, de sólo lectura, por
 * roles que asume. Espejo de `rayito.cli.dashboard`.
 *
 * Códigos de salida: 0 al cerrar con Ctrl+C; 2 por uso o entorno (puerto
 * ocupado o inválido, sin región o credenciales).
 */

import { open } from "node:fs/promises";
import { extname } from "node:path";
import { awsClientSettingsOf, LambdaMicrovmsControlPlane } from "../../aws/control-plane.js";
import { type AccountSpec, AccountsApi, parseAccountSpecs } from "../../dashboard/accounts.js";
import type { ApiBackend } from "../../dashboard/api/contract.js";
import { DashboardApi } from "../../dashboard/api/router.js";
import { AssumedRole, assumedCredentials } from "../../dashboard/assume.js";
import { UsageCost } from "../../dashboard/cost.js";
import { Fleet } from "../../dashboard/fleet.js";
import { parseLogo } from "../../dashboard/logo.js";
import { CloudWatchLogsSource } from "../../dashboard/logs.js";
import { Quotas } from "../../dashboard/quotas.js";
import { Appearance, type Branding, parseTheme } from "../../dashboard/theme.js";
import { InvalidArgumentError } from "../../errors.js";
import { LifecycleEvents } from "../../lifecycle-events/service.js";
import {
  COST_CACHE_TTL_SECONDS,
  COST_EXPLORER_REQUEST_PRICE_USD,
  DASHBOARD_LOGO_MAX_BYTES,
  DASHBOARD_THEME_MAX_BYTES,
  PORT_MAX,
  PORT_MIN,
  SNAPSHOT_READ_PRICE_USD_PER_GB,
  SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
  SNAPSHOT_WRITE_PRICE_USD_PER_GB,
} from "../../limits.js";
import { SECONDS_PER_HOUR } from "../../units.js";
import { type CommandContext, defineCommand, defineGroup, flag, option } from "../command.js";
import { dashboardStaticDirectory } from "../dashboard-assets.js";
import { LocalDashboard } from "../dashboard-server.js";
import { UsageError } from "../failures.js";
import type { CliServices } from "../services.js";

export const ACCOUNTS_ENV = "RAYITO_DASHBOARD_ACCOUNTS";
export const EXTERNAL_ID_ENV = "RAYITO_DASHBOARD_EXTERNAL_ID";
export const THEME_ENV = "RAYITO_DASHBOARD_THEME";
export const LOGO_ENV = "RAYITO_DASHBOARD_LOGO";
const THEME_FORMATS: Readonly<Record<string, string>> = { ".json": "json", ".css": "css" };
const RANDOM_PORT = 0;
const BYTES_PER_KIB = 1024;

/** «1 cuenta» / «3 cuentas»: la concordancia de número del aviso de arranque. */
export function countAccounts(count: number): string {
  return `${count} ${count === 1 ? "cuenta" : "cuentas"}`;
}

/** Un precio en dólares con coma decimal, como en la documentación. */
export function usdEs(value: number): string {
  return `$${value}`.replace(".", ",");
}

const COST_EXPLORER_HELP =
  "Permite consultar el coste facturado en Cost Explorer. DE PAGO: cada consulta se factura " +
  `(${COST_EXPLORER_REQUEST_PRICE_USD.toFixed(2)} USD, también «Ignorar la caché») y la caché ` +
  `local, en memoria, la conserva ${COST_CACHE_TTL_SECONDS / SECONDS_PER_HOUR} h.`;
const ALLOW_ACTIONS_HELP =
  "Habilita matar, suspender y reanudar desde el panel (con confirmación). Sin esta opción " +
  "el panel es de sólo lectura. Las acciones no facturan por la llamada, pero cambian la " +
  "factura (AWS_API_NOTES.md §12): kill deja de facturar cómputo; suspend escribe un " +
  `snapshot (${usdEs(SNAPSHOT_WRITE_PRICE_USD_PER_GB)}/GB) y paga almacenamiento ` +
  `(${usdEs(SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH)}/GB-mes); resume lee el snapshot ` +
  `(${usdEs(SNAPSHOT_READ_PRICE_USD_PER_GB)}/GB) y vuelve a facturar cómputo al precio de ` +
  "su tamaño.";
const EVENTS_STACK_HELP =
  "Nombre de la pila events-webhooks desplegada: activa la vista de eventos. DE PAGO, " +
  "aunque mínimo: lecturas bajo demanda de DynamoDB sobre su tabla, sin sondeo.";
const LOG_GROUP_HELP =
  "Grupo de CloudWatch de los logs (por defecto, por imagen). Leer los logs usa " +
  "DescribeLogStreams y GetLogEvents, sin tarifa propia publicada por CloudWatch " +
  "(https://aws.amazon.com/cloudwatch/pricing/): sólo cuentan la ingesta, el almacenamiento " +
  "y Logs Insights.";
const COST_EXPLORER_BANNER = `, coste facturado activado (${COST_EXPLORER_REQUEST_PRICE_USD.toFixed(2)} USD por consulta)`;
const GROUP_HELP =
  "Panel de gestión de MicroVMs: sin subcomando, el panel local (cuotas, coste, " +
  "flota y acciones); `cloudwatch` genera un dashboard de CloudWatch. Apagado y de sólo " +
  "lectura por defecto: las lecturas son gratuitas; lo que cuesta es opt-in " +
  "(--cost-explorer y --events-stack, de pago; --allow-actions, donde resume vuelve a " +
  "facturar cómputo).";
const COMMAND_HELP =
  "Panel de gestión local de MicroVMs (cuotas, coste, flota y acciones).\n\n" +
  "Apagado y de sólo lectura por defecto: las lecturas son gratuitas. Lo que " +
  "cuesta es opt-in: --cost-explorer (Cost Explorer, de pago por consulta), " +
  "--events-stack (lecturas de DynamoDB) y --allow-actions (resume vuelve a " +
  "facturar cómputo).";

/** Lee un fichero de aspecto sin cargar más de `limit` bytes: uno enorme se rechaza antes de leerlo entero. El mensaje nombra la opción, no la ruta. */
async function readBounded(path: string, limit: number, flagName: string): Promise<Uint8Array> {
  try {
    const handle = await open(path, "r");
    try {
      const buffer = Buffer.alloc(limit + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      return buffer.subarray(0, bytesRead);
    } finally {
      await handle.close();
    }
  } catch {
    throw new UsageError(`${flagName}: no se pudo leer el fichero`);
  }
}

/** El tema y el logo de usuario, validados antes de abrir el puerto. Cualquier fallo sale con código 2 y un mensaje que no repite valores. */
export async function loadAppearance(
  theme: string | undefined,
  logo: string | undefined,
): Promise<Appearance> {
  try {
    return new Appearance({
      theme: theme === undefined ? undefined : await loadTheme(theme),
      logo:
        logo === undefined
          ? undefined
          : parseLogo(await readBounded(logo, DASHBOARD_LOGO_MAX_BYTES, "--logo")),
    });
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw new UsageError(error.message);
    }
    throw error;
  }
}

async function loadTheme(path: string): Promise<ReturnType<typeof parseTheme>> {
  const format = THEME_FORMATS[extname(path).toLowerCase()];
  if (format === undefined) {
    throw new UsageError("--theme: la extensión debe ser .json o .css");
  }
  const data = await readBounded(path, DASHBOARD_THEME_MAX_BYTES, "--theme");
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(data);
  } catch {
    throw new UsageError("--theme: el fichero no es texto UTF-8");
  }
  return parseTheme(text, { format });
}

function credentialsOf(
  services: CliServices,
): ReturnType<typeof awsClientSettingsOf>["credentials"] {
  return awsClientSettingsOf(services.controlPlane).credentials;
}

interface PrimaryOptions {
  readonly allowActions: boolean;
  readonly costExplorer: boolean;
  readonly eventsStack: string | undefined;
  readonly logGroup: string | undefined;
  readonly branding: Branding;
}

/** Los servicios del SDK sobre la sesión de la CLI; ninguno llama a AWS hasta que la SPA pide su vista. */
export function buildApi(services: CliServices, options: PrimaryOptions): DashboardApi {
  const plane = services.controlPlane;
  const credentials = credentialsOf(services);
  const sessionOptions = {
    region: services.region,
    ...(credentials === undefined ? {} : { credentials }),
    controlPlane: plane,
  };
  const events =
    options.eventsStack === undefined
      ? undefined
      : new LifecycleEvents({
          stackName: options.eventsStack,
          region: services.region,
          ...(credentials === undefined ? {} : { credentials }),
        });
  return new DashboardApi({
    fleet: new Fleet({ ...sessionOptions, events, allowActions: options.allowActions }),
    quotas: new Quotas(sessionOptions),
    cost: new UsageCost(sessionOptions),
    region: services.region,
    logs: new CloudWatchLogsSource(services.logs, plane, { logGroup: options.logGroup }),
    eventsConfigured: events !== undefined,
    costExplorer: options.costExplorer,
    branding: options.branding,
  });
}

/**
 * La API de una cuenta adicional: sólo lectura, sin coste facturado, logs ni
 * eventos. Las credenciales asumen el rol al firmar la primera petición, no
 * ahora. Comparte la marca del panel principal para que cambiar de cuenta no
 * cambie el nombre ni el logo.
 */
export function buildAccountApi(
  services: CliServices,
  spec: AccountSpec,
  externalId: string | undefined,
  branding: Branding,
): DashboardApi {
  if (services.sts === undefined) {
    throw new UsageError("--account: esta sesión no puede asumir roles");
  }
  const role = new AssumedRole(services.sts, { roleArn: spec.roleArn, externalId });
  const credentials = assumedCredentials(role);
  const plane = LambdaMicrovmsControlPlane.fromRegion(services.region, { credentials });
  const sessionOptions = { region: services.region, credentials, controlPlane: plane };
  return new DashboardApi({
    fleet: new Fleet(sessionOptions),
    quotas: new Quotas(sessionOptions),
    cost: new UsageCost(sessionOptions),
    region: services.region,
    branding,
  });
}

function withAccounts(
  services: CliServices,
  primary: DashboardApi,
  specs: readonly AccountSpec[],
  externalId: string | undefined,
  branding: Branding,
): ApiBackend {
  if (specs.length === 0) {
    return primary;
  }
  const others = new Map<string, ApiBackend>(
    specs.map((spec) => [spec.label, buildAccountApi(services, spec, externalId, branding)]),
  );
  return new AccountsApi(primary, others);
}

function waitUntilAborted(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    signal.addEventListener("abort", () => resolve(), { once: true });
  });
}

export const dashboardCommand = defineCommand({
  path: ["dashboard"],
  help: COMMAND_HELP,
  parameters: [
    option(["--port"], {
      type: "int",
      default: RANDOM_PORT,
      help: "Puerto local; por defecto uno libre al azar. Sólo escucha en 127.0.0.1.",
    }),
    flag(["--allow-actions"], { help: ALLOW_ACTIONS_HELP }),
    flag(["--cost-explorer"], { help: COST_EXPLORER_HELP }),
    option(["--events-stack"], { help: EVENTS_STACK_HELP }),
    option(["--log-group"], { help: LOG_GROUP_HELP }),
    option(["--theme"], {
      type: "path",
      envvar: THEME_ENV,
      help:
        "Tema propio: un .json o .css con los tokens de diseño (colores, radios, " +
        "fuentes). Se valida antes de arrancar; ver «Personalizar el tema» en la guía.",
    }),
    option(["--logo"], {
      type: "path",
      envvar: LOGO_ENV,
      help: `Logo propio: un PNG o SVG de hasta ${DASHBOARD_LOGO_MAX_BYTES / BYTES_PER_KIB} KiB, servido desde el propio panel.`,
    }),
    option(["--account"], {
      multiple: true,
      envvar: ACCOUNTS_ENV,
      help:
        "Vista multicuenta (opt-in), repetible: NOMBRE=ARN_DEL_ROL de un rol de sólo " +
        "lectura que se asume (plantilla: rayito stack deploy dashboard-read-role en la " +
        "cuenta destino). También se lee de la variable " +
        `${ACCOUNTS_ENV}, con las entradas separadas por espacios, para armar el ARN ` +
        "en tiempo de ejecución sin escribirlo en la orden. Las cuentas adicionales son " +
        "de sólo lectura y sin coste facturado.",
    }),
    option(["--external-id"], {
      envvar: EXTERNAL_ID_ENV,
      help: "ExternalId que exigen los roles de --account (el mismo para todos).",
    }),
    flag(["--open"], { name: "open_browser", help: "Abre la URL de arranque en el navegador." }),
  ],
  run: runDashboard,
});

export const dashboardGroup = defineGroup({
  path: ["dashboard"],
  help: GROUP_HELP,
  root: dashboardCommand,
});

async function runDashboard(context: CommandContext): Promise<number | undefined> {
  const { values, console } = context;
  const port = values.number("port");
  if (port !== RANDOM_PORT && !(port >= PORT_MIN && port <= PORT_MAX)) {
    throw new UsageError(`--port: debe estar entre ${PORT_MIN} y ${PORT_MAX}`);
  }
  const appearance = await loadAppearance(values.string("theme"), values.string("logo"));
  const specs = specsOrUsage(values.strings("account"));
  const services = await context.services();
  const allowActions = values.flag("allow_actions");
  const costExplorer = values.flag("cost_explorer");
  const primary = buildApi(services, {
    allowActions,
    costExplorer,
    eventsStack: values.string("events_stack"),
    logGroup: values.string("log_group"),
    branding: appearance.branding,
  });
  const api = withAccounts(
    services,
    primary,
    specs,
    values.string("external_id"),
    appearance.branding,
  );
  const server = await startServer(api, port, appearance);
  const mode = allowActions ? "con acciones (kill, suspend, resume)" : "sólo lectura";
  const extra = specs.length > 0 ? `, más ${countAccounts(specs.length)} de sólo lectura` : "";
  const paid = costExplorer ? COST_EXPLORER_BANNER : "";
  console.echo(
    `Panel de Rayito (${services.region}, ${mode}${paid}${extra}). Ábrelo en tu navegador:`,
  );
  console.echo(server.url);
  console.echo("Ctrl+C para cerrar. El enlace sirve una sola vez.");
  try {
    if (values.flag("open_browser")) {
      await context.openUrl(server.url);
    }
    await waitUntilAborted(context.signal);
    console.echo("Panel cerrado.");
  } finally {
    await server.shutdown();
  }
  return undefined;
}

function specsOrUsage(values: readonly string[]): AccountSpec[] {
  try {
    return parseAccountSpecs(values);
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw new UsageError(error.message);
    }
    throw error;
  }
}

async function startServer(
  api: ApiBackend,
  port: number,
  appearance: Appearance,
): Promise<LocalDashboard> {
  try {
    return await LocalDashboard.start(api, {
      port,
      appearance,
      staticDirectory: dashboardStaticDirectory(),
    });
  } catch (error) {
    await api.close();
    if (
      (error as { code?: unknown }).code === "EADDRINUSE" ||
      (error as { code?: unknown }).code === "EACCES"
    ) {
      throw new UsageError(`no se pudo abrir el puerto ${port} en 127.0.0.1 (¿ocupado?)`);
    }
    throw error;
  }
}
