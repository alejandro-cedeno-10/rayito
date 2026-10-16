/**
 * `rayito sandbox connect | proxy` (ADR-027, fase 4): una terminal
 * interactiva en un sandbox existente y un proxy TCP local hacia un puerto
 * del guest. Espejo de `connect_command` y `proxy_command` de
 * `rayito.cli.sandbox`; la lógica vive en `interactive-terminal.ts` y
 * `proxy-run.ts`, aquí sólo se leen las opciones.
 *
 * `connect` usa el access token del sandbox (sólo por `--token-file` o
 * `RAYITO_ACCESS_TOKEN`, nunca por argv) y no lo termina al salir; el
 * código de salida es el del shell. `proxy` no necesita ese token: sólo el
 * JWE del proxy, que acuña el SDK.
 */

import { isWildcard } from "../../authority.js";
import { ALLOW_ORIGIN_OPTION, ALLOWED_HOST_OPTION, isLoopbackBind } from "../access.js";
import { argument, type CommandContext, defineCommand, flag, option } from "../command.js";
import { UsageError } from "../failures.js";
import { runTerminal } from "../interactive-terminal.js";
import { runProxy } from "../proxy-run.js";
import {
  ALLOW_REMOTE_OPTION,
  BIND_NEEDS_ALLOW_REMOTE_MESSAGE,
  DEFAULT_BIND,
  DEFAULT_MAX_CONNECTIONS,
  MAX_CONNECTIONS_OPTION,
  validateLocalPort,
  validateMaxConnections,
  validateProxyPort,
  WILDCARD_NEEDS_ALLOWED_HOST_MESSAGE,
} from "../proxy-settings.js";
import {
  cwdOption,
  envOption,
  interactiveOrUsage,
  pairsOrUsage,
  tokenFileOption,
  tokenOrUsage,
  userOption,
} from "./sandbox.js";

const PROXY_REPORT_PREFIX = "rayito: proxy:";

export const connectCommand = defineCommand({
  path: ["sandbox", "connect"],
  help:
    "Terminal interactiva en un sandbox existente (lo reanuda si está suspendido y no lo " +
    "termina al salir).",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    userOption,
    cwdOption,
    envOption,
    tokenFileOption,
  ],
  run: async (context) => {
    const { values } = context;
    const envs = pairsOrUsage(values.strings("env"), "--env");
    const token = tokenOrUsage(context);
    const interactive = interactiveOrUsage(context);
    const services = await context.services();
    const sandbox = await services.sandboxes.connect(values.string("sandbox_id") ?? "", token);
    try {
      const code = await runTerminal(sandbox, interactive, {
        user: values.string("user"),
        cwd: values.string("cwd"),
        envs: Object.keys(envs).length === 0 ? undefined : envs,
        signal: context.signal,
      });
      return code === 0 ? undefined : code;
    } finally {
      sandbox.close();
    }
  },
});

/** `--bind` fuera de loopback: necesita `--allow-remote` (y `--allowed-host` con un comodín) y avisa. */
function checkRemoteBind(context: CommandContext): void {
  const { values } = context;
  const bind = values.string("bind") ?? DEFAULT_BIND;
  if (isLoopbackBind(bind)) {
    return;
  }
  if (!values.flag("allow_remote")) {
    throw new UsageError(BIND_NEEDS_ALLOW_REMOTE_MESSAGE);
  }
  if (isWildcard(bind) && values.strings("allowed_host").length === 0) {
    throw new UsageError(WILDCARD_NEEDS_ALLOWED_HOST_MESSAGE);
  }
  context.console.echo(
    `rayito: aviso: --bind ${bind} no es loopback; ` +
      "cualquiera que llegue a este puerto usa el sandbox mientras el proxy esté vivo",
    { err: true },
  );
}

export const proxyCommand = defineCommand({
  path: ["sandbox", "proxy"],
  help:
    "Expone un puerto del guest en `http://<bind>:<local-port>`. Sin coste de AWS más allá de " +
    "`GetMicrovm` y `CreateMicrovmAuthToken` (gratuitos; IAM: `lambda:GetMicrovm` y " +
    "`lambda:CreateMicrovmAuthToken`). Un sandbox `SUSPENDED` con auto-resume se despierta con " +
    "la primera petición (eso sí factura cómputo). Ctrl-C para el refresher del JWE y cierra el " +
    "listener. Sólo reenvía peticiones cuyo `Host` sea de loopback (o `--bind`, o " +
    "`--allowed-host`) y cuyo `Origin`, si lo trae, sea uno de esos mismos orígenes (o " +
    "`--allow-origin`): una web abierta en el navegador del operador no llega al guest por DNS " +
    "rebinding ni con un POST o un WebSocket entre orígenes.",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    option(["--port"], { type: "int", required: true, help: "Puerto del guest a exponer." }),
    option(["--local-port"], {
      type: "int",
      help: "Puerto local donde escuchar; por defecto --port.",
    }),
    option(["--bind"], { default: DEFAULT_BIND, help: "Dirección local donde escuchar." }),
    flag([ALLOW_REMOTE_OPTION], {
      help: "Permite --bind fuera de 127.0.0.1/::1/localhost.",
    }),
    option([ALLOWED_HOST_OPTION], {
      multiple: true,
      help:
        "Host (o host:puerto) que se acepta además de los de loopback; repetible. " +
        "Obligatorio con --bind 0.0.0.0/::.",
    }),
    option([ALLOW_ORIGIN_OPTION], {
      multiple: true,
      help: "Origen http(s)://host[:puerto] que se acepta en Origin; repetible.",
    }),
    option([MAX_CONNECTIONS_OPTION], {
      type: "int",
      default: DEFAULT_MAX_CONNECTIONS,
      help: "Conexiones reenviadas a la vez; la siguiente recibe 503.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const port = values.number("port");
    const localPort = values.optionalNumber("local_port") ?? port;
    const maxConnections = values.number("max_connections");
    checkRemoteBind(context);
    validateProxyPort(port);
    validateLocalPort(localPort);
    validateMaxConnections(maxConnections);
    const services = await context.services();
    await runProxy(services.controlPlane, {
      sandboxId: values.string("sandbox_id") ?? "",
      port,
      localPort,
      bind: values.string("bind") ?? DEFAULT_BIND,
      allowedHosts: values.strings("allowed_host"),
      allowedOrigins: values.strings("allow_origin"),
      maxConnections,
      onReady: (line) => context.console.echo(line),
      report: (reason) => context.console.echo(`${PROXY_REPORT_PREFIX} ${reason}`, { err: true }),
      signal: context.signal,
    });
    return undefined;
  },
});
