/**
 * `m15-custom-domain` contra AWS real (etapa de aceptación serializada, ver
 * `MILESTONES.md` M15 y `design.md` del cambio). Espejo de
 * `clients/python/tests/e2e/test_m15_custom_domain.py`: despliega
 * `infra/custom-domain.yaml`, registra rutas con `trafficToken` y comprueba
 * por la distribución HTTP/1.1 (DOM-2), la propagación del KVS (DOM-5, sólo
 * se imprime), `refresh()` sin cortar la ruta (DOM-7, parcial), el upgrade
 * de WebSocket (DOM-3), el auto-resume por el dominio (DOM-8), 403 sin
 * token, 404 tras `unregister()` o con el TTL vencido, y que `destroy()`
 * espera de verdad a CloudFront.
 *
 * **Sólo variables de entorno.** Además de `RAYITO_E2E=1`/`RAYITO_TEMPLATE`
 * (`helpers.ts`): `RAYITO_E2E_DOMAIN` (un dominio cuyo comodín cubre el
 * certificado: con `*.sbx.example.com`, `sbx.example.com`),
 * `RAYITO_E2E_CERT_ARN` (ese certificado ACM, en `us-east-1`) y, opcional,
 * `RAYITO_ACCEPTANCE_RUN_TAG` (por defecto, el identificador aleatorio de la
 * corrida) y, opcional, `RAYITO_E2E_TAGS` (`tagsFromEnvironment`: etiquetas
 * `K=V` separadas por comas para la pila, para organizaciones que exigen
 * etiquetas en los recursos creados; la etiqueta de la corrida gana si
 * coincide una clave). Sin las dos primeras la suite entera se salta sin tocar
 * AWS.
 * El dominio no debe tener un registro DNS comodín hacia otra distribución
 * CloudFront (CloudFront rechazaría el nombre más específico,
 * `AWS_API_NOTES.md` §29).
 *
 * **Aislada por corrida y sin DNS.** Pila `rayito-cd-e2e-<id>` y, en vez
 * del comodín, sólo los hostnames exactos de sus rutas
 * (`alternateDomainNames`), así que nunca choca con otro alias de
 * CloudFront. El test conecta al `*.cloudfront.net` de la distribución con
 * el hostname propio como SNI y `Host` (como `curl --connect-to`); un
 * usuario real apunta un `CNAME`/alias de su DNS al `DistributionDomainName`.
 *
 * **Determinista frente a la propagación.** El KVS llega a cada servidor del
 * edge por separado y cada petición abre una conexión nueva, que puede caer
 * en otro: la visibilidad exige `STABLE_OBSERVATIONS` respuestas seguidas,
 * cada respuesta esperada se sondea con su plazo, el servidor del sandbox se
 * espera hasta que escucha y el eco de WebSocket atiende varias conexiones,
 * así que un upgrade que cae en un servidor sin la ruta se reintenta. El 404
 * por caducidad exige también `STABLE_OBSERVATIONS` respuestas seguidas y,
 * además, que hayan pasado al menos `SHORT_ROUTE_TTL_SECONDS` (menos
 * `EDGE_CLOCK_SKEW_TOLERANCE_SECONDS`) desde que se registró la ruta: un 404 de
 * un servidor del edge que aún no tiene la ruta hace fallar el test en lugar
 * de pasarlo.
 *
 * **Servidores en el sandbox.** Se arrancan en segundo plano sin timeout
 * (`timeoutMs: 0`) y el eco de WebSocket se sube como fichero
 * (`testdata/custom-domain/websocket_echo_server.py`, compartido con Python),
 * nunca citado en la shell.
 *
 * **Requisito: `cloudfront:CreateDistribution`.** Ante cualquier error de
 * permisos al crear el recurso `Distribution` (`DescribeStackEvents`), cada
 * test se salta nombrando ese requisito en vez de fallar.
 *
 * **Limpieza.** `afterAll` desregistra cada ruta, borra la pila (aunque el
 * `deploy()` fallara a medias) y comprueba que `status()` ya no la ve.
 *
 * Coste: una distribución CloudFront, su Function y su KVS durante ~20-30
 * min, tres sandboxes breves y unos pocos `PutKey`/`DeleteKey`; muy por
 * debajo de $1.
 */

import { createHash, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import * as https from "node:https";
import { join } from "node:path";
import type { TLSSocket } from "node:tls";
import { CloudFormationClient, DescribeStackEventsCommand } from "@aws-sdk/client-cloudformation";
import { afterAll, beforeAll, beforeEach, describe, expect, test } from "vitest";
import {
  CUSTOM_DOMAIN_WAIT_TIMEOUT_MS,
  CustomDomain,
  type CustomDomainRoute,
  DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY,
} from "../../src/custom-domain/service.js";
import { CustomDomainError, StackError } from "../../src/errors.js";
import type { Sandbox } from "../../src/sandbox/sandbox.js";
import { createTestSandbox, e2eEnabled, HOME, tagsFromEnvironment, useE2E } from "./helpers.js";

const DOMAIN_VAR = "RAYITO_E2E_DOMAIN";
const CERT_ARN_VAR = "RAYITO_E2E_CERT_ARN";
// Opcional: sin ella, la etiqueta es el identificador aleatorio de la corrida.
const ACCEPTANCE_RUN_TAG_VAR = "RAYITO_ACCEPTANCE_RUN_TAG";
const ACCEPTANCE_RUN_TAG_KEY = "rayito:acceptance-run";

// 4 bytes = 8 hex: la pila queda muy por debajo de MAX_STACK_NAME_LENGTH (36).
const RUN_ID_BYTES = 4;
const STACK_NAME_PREFIX = "rayito-cd-e2e-";
const ROUTE_ALIAS_PREFIX = "e2e";
// Una ruta (y un nombre alternativo) por test.
const ROUTE_PURPOSES = ["http", "ws", "resume", "ttl"] as const;
type RoutePurpose = (typeof ROUTE_PURPOSES)[number];

const ROUTE_PORT = 8000;
// Suficiente para que la suite entera corra sin que la ruta caduque sola
// (T25); muy por debajo de MAX_TEST_SANDBOX_TIMEOUT_MS.
const ROUTE_TTL_SECONDS = 1800;
// TTL corto del test de caducidad: deja ver la ruta (STABLE_OBSERVATIONS
// respuestas seguidas más la propagación) y servir antes de que venza.
const SHORT_ROUTE_TTL_SECONDS = 90;
// Cuánto antes de SHORT_ROUTE_TTL_SECONDS desde register() puede verse el 404
// por caducidad y aún ser una caducidad de verdad: register() guarda
// `expires_at = floor(now) + ttl` (trunca hasta 1 s) y la Function lo compara
// con el reloj del edge. Criterio fijado antes de medir (no es una medida de
// AWS): 5 s cubren el truncado más un desfase de reloj entre la máquina de la
// prueba y el edge; un 404 más temprano es de un servidor sin la ruta, no de
// su TTL. Espejo de EDGE_CLOCK_SKEW_TOLERANCE_SECONDS de Python.
const EDGE_CLOCK_SKEW_TOLERANCE_SECONDS = 5;
const HTTPS_PORT = 443;
const HTTP_REQUEST_TIMEOUT_MS = 20_000;
const WEBSOCKET_HANDSHAKE_TIMEOUT_MS = 20_000;
// Tope de espera a que una escritura/borrado del KVS llegue al edge (DOM-5;
// AWS documenta "segundos", AWS_API_NOTES.md §29), y entre sondeos.
const PROPAGATION_TIMEOUT_MS = 120_000;
const PROPAGATION_POLL_MS = 1_000;
// Tope para que un sandbox pausado con auto-resume vuelva a servir (DOM-8).
const AUTO_RESUME_TIMEOUT_MS = 120_000;
// 300 s: sólo el pause() explícito suspende durante el test.
const RESUME_IDLE = { maxIdleSeconds: 300, autoResume: true };
// deploy()/destroy() esperan hasta CUSTOM_DOMAIN_WAIT_TIMEOUT_MS cada uno;
// el hookTimeout genérico de vitest.config.ts (300 s) no alcanza.
const STACK_HOOK_TIMEOUT_MS = CUSTOM_DOMAIN_WAIT_TIMEOUT_MS + 60_000;
// GUID fijo de RFC 6455 §1.3, para calcular Sec-WebSocket-Accept.
const WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
// El eco de WebSocket compartido con Python y dónde se sube en el sandbox.
const WEBSOCKET_ECHO_SERVER_SOURCE = join(
  import.meta.dirname,
  "..",
  "..",
  "..",
  "..",
  "testdata",
  "custom-domain",
  "websocket_echo_server.py",
);
const WEBSOCKET_ECHO_SERVER_PATH = `${HOME}/websocket_echo_server.py`;
// Cuánto esperar, dentro del sandbox, a que algo escuche en ROUTE_PORT, y
// cada cuánto mirarlo (`/dev/tcp` de bash, el shell de commands.run).
const SERVER_LISTEN_TIMEOUT_SECONDS = 30;
const SERVER_LISTEN_POLL_SECONDS = 0.2;
// Respuestas seguidas que cuentan como "la ruta ya está en el edge".
const STABLE_OBSERVATIONS = 5;
const TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token";
const HTTP_OK = 200;
const HTTP_FORBIDDEN = 403;
const HTTP_NOT_FOUND = 404;

/** El recurso de `infra/custom-domain.yaml` que crea la distribución, y lo que
 * deja CloudFormation en `ResourceStatusReason` cuando la pila no puede llamar
 * a `cloudfront:CreateDistribution`. */
const DISTRIBUTION_LOGICAL_ID = "Distribution";
const CREATE_FAILED = "CREATE_FAILED";
const ACCESS_DENIED_MARKERS = ["AccessDenied", "not authorized"];
const DISTRIBUTION_DENIED_REASON =
  "distribution permission error (needs cloudfront:CreateDistribution)";

/** Si la pila falló por un error de permisos al crear la distribución
 * (`DescribeStackEvents`, `AWS_API_NOTES.md` §21). */
async function distributionDenied(region: string | undefined, stackName: string): Promise<boolean> {
  const client = new CloudFormationClient(region === undefined ? {} : { region });
  try {
    const { StackEvents } = await client.send(
      new DescribeStackEventsCommand({ StackName: stackName }),
    );
    return (StackEvents ?? []).some(
      (event) =>
        event.LogicalResourceId === DISTRIBUTION_LOGICAL_ID &&
        event.ResourceStatus === CREATE_FAILED &&
        ACCESS_DENIED_MARKERS.some((marker) => (event.ResourceStatusReason ?? "").includes(marker)),
    );
  } catch {
    return false;
  } finally {
    client.destroy();
  }
}

function customDomainEnvReady(): boolean {
  return Boolean(process.env[DOMAIN_VAR] && process.env[CERT_ARN_VAR]);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function reportDomain(label: string, value: string): void {
  console.log(`\n[custom-domain e2e] ${label}: ${value}`);
}

/** La distribución de esta corrida: la `CustomDomain`, el `*.cloudfront.net`
 * al que se conecta el test (en lugar de resolver DNS) y el alias de cada
 * test. */
interface DomainUnderTest {
  readonly domain: CustomDomain;
  readonly connectHost: string;
  readonly aliases: Readonly<Record<RoutePurpose, string>>;
}

/** `curl --connect-to <host>:443:<connectHost>:443`: TCP contra
 * `connectHost` (la distribución), `host` como SNI y como `Host`, y el
 * certificado validado contra `host`. Sin DNS para `host`. */
function connectToOptions(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
  timeout: number,
): https.RequestOptions {
  return {
    host: target.connectHost,
    servername: host,
    port: HTTPS_PORT,
    path: "/",
    method: "GET",
    headers: { ...headers, Host: host },
    timeout,
    minVersion: "TLSv1.2",
  };
}

function httpsStatus(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = https.request(
      connectToOptions(target, host, headers, HTTP_REQUEST_TIMEOUT_MS),
      (response) => {
        response.resume();
        resolve(response.statusCode ?? 0);
      },
    );
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error(`sin respuesta de ${host} a tiempo`)));
    request.end();
  });
}

/** Sondea hasta que `host` responde `expected` `consecutive` veces seguidas;
 * devuelve los segundos que tardó. Un 5xx, otro estado o un error de red
 * mientras tanto (el origen despertando, DOM-8, o un servidor del edge al que
 * aún no llegó el KVS) cuenta como "todavía no" y reinicia la cuenta. */
async function waitForStatus(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
  expected: number,
  timeoutMs: number,
  consecutive = 1,
): Promise<number> {
  const started = performance.now();
  let last: unknown;
  let seen = 0;
  while (performance.now() - started < timeoutMs) {
    try {
      last = await httpsStatus(target, host, headers);
    } catch (error) {
      last = String(error);
    }
    seen = last === expected ? seen + 1 : 0;
    if (seen >= consecutive) {
      return (performance.now() - started) / 1000;
    }
    await sleep(PROPAGATION_POLL_MS);
  }
  throw new Error(
    `${host} no respondió ${expected} en ${timeoutMs / 1000} s (último: ${String(last)})`,
  );
}

/** DOM-5 (escritura): una ruta con `trafficToken` ya en el edge responde
 * 403 sin el token, sin contactar el origen (antes, 404). Hacen falta
 * `STABLE_OBSERVATIONS` 403 seguidos: cada conexión puede caer en otro
 * servidor del edge. */
function waitUntilRouteVisible(target: DomainUnderTest, host: string): Promise<number> {
  return waitForStatus(
    target,
    host,
    {},
    HTTP_FORBIDDEN,
    PROPAGATION_TIMEOUT_MS,
    STABLE_OBSERVATIONS,
  );
}

function waitForOk(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
): Promise<number> {
  return waitForStatus(target, host, headers, HTTP_OK, PROPAGATION_TIMEOUT_MS);
}

/** Un upgrade que no devolvió 101: se puede reintentar (el servidor del edge
 * quizá aún no tenía la ruta). */
class WebSocketRejected extends Error {}

/**
 * Apertura de WebSocket mínima (RFC 6455) con un único frame de texto de
 * ida y vuelta, sin más dependencia que `node:https`: suficiente para DOM-3
 * (¿corre la Function en el upgrade? ¿el proxy de AWS acepta la cabecera
 * de token en la petición de upgrade?), no un cliente de propósito general.
 */
function websocketHandshakeAndEcho(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
  message: Buffer,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const websocketKey = randomBytes(16).toString("base64");
    const request = https.request(
      connectToOptions(
        target,
        host,
        {
          ...headers,
          Connection: "Upgrade",
          Upgrade: "websocket",
          "Sec-WebSocket-Key": websocketKey,
          "Sec-WebSocket-Version": "13",
        },
        WEBSOCKET_HANDSHAKE_TIMEOUT_MS,
      ),
    );
    request.on("error", reject);
    request.on("timeout", () =>
      request.destroy(new Error(`el upgrade a ${host} no llegó a tiempo`)),
    );
    request.on("upgrade", (response, socket: TLSSocket, head: Buffer) => {
      const expectedAccept = createHash("sha1")
        .update(websocketKey + WEBSOCKET_ACCEPT_GUID)
        .digest("base64");
      if (response.headers["sec-websocket-accept"] !== expectedAccept) {
        socket.destroy();
        reject(new Error("Sec-WebSocket-Accept no coincide con la clave enviada"));
        return;
      }
      // Un único frame de texto cliente->servidor (enmascarado, RFC 6455
      // §5.1) de menos de 126 bytes: la longitud cabe en el segundo byte.
      // `readUInt8`, no `[]`, por `noUncheckedIndexedAccess`.
      const mask = randomBytes(4);
      const maskedPayload = Buffer.from(
        message.map((byte, index) => byte ^ mask.readUInt8(index % 4)),
      );
      socket.write(
        Buffer.concat([Buffer.from([0x81, 0x80 | message.length]), mask, maskedPayload]),
      );
      let buffered = head;
      const onData = (chunk: Buffer): void => {
        buffered = Buffer.concat([buffered, chunk]);
        if (buffered.length < 2) {
          return;
        }
        const payloadLength = buffered.readUInt8(1) & 0x7f;
        if (buffered.length < 2 + payloadLength) {
          return;
        }
        socket.off("data", onData);
        socket.end();
        resolve(buffered.subarray(2, 2 + payloadLength));
      };
      socket.on("data", onData);
    });
    request.on("response", (response) => {
      response.resume();
      reject(
        new WebSocketRejected(`el upgrade de WebSocket no se aceptó: HTTP ${response.statusCode}`),
      );
    });
    request.end();
  });
}

/** `websocketHandshakeAndEcho` hasta que un servidor del edge con la ruta
 * acepta el upgrade, dentro de `PROPAGATION_TIMEOUT_MS`. */
async function echoWithRetries(
  target: DomainUnderTest,
  host: string,
  headers: Record<string, string>,
  message: Buffer,
): Promise<Buffer> {
  const deadline = performance.now() + PROPAGATION_TIMEOUT_MS;
  for (;;) {
    try {
      return await websocketHandshakeAndEcho(target, host, headers, message);
    } catch (error) {
      if (performance.now() >= deadline || !retriableUpgrade(error)) {
        throw error;
      }
    }
    await sleep(PROPAGATION_POLL_MS);
  }
}

function retriableUpgrade(error: unknown): boolean {
  return error instanceof WebSocketRejected || (error as { code?: unknown })?.code !== undefined;
}

async function jweOf(sandbox: Sandbox): Promise<string> {
  return (await sandbox.getHost(ROUTE_PORT)).headers["x-aws-proxy-auth"] ?? "";
}

/** Arranca `command` en segundo plano y espera, dentro del sandbox, a que
 * algo escuche en `ROUTE_PORT`: un servidor lento no se confunde con una ruta
 * rota. */
async function startInBackground(sandbox: Sandbox, command: string): Promise<void> {
  await sandbox.commands.run(command, { background: true, timeoutMs: 0 });
  const attempts = Math.ceil(SERVER_LISTEN_TIMEOUT_SECONDS / SERVER_LISTEN_POLL_SECONDS);
  await sandbox.commands.run(
    `for _ in $(seq ${attempts}); do ` +
      `(exec 3<>/dev/tcp/127.0.0.1/${ROUTE_PORT}) 2>/dev/null && exit 0; ` +
      `sleep ${SERVER_LISTEN_POLL_SECONDS}; done; exit 1`,
    { timeoutMs: SERVER_LISTEN_TIMEOUT_SECONDS * 2000 },
  );
}

describe.runIf(e2eEnabled() && customDomainEnvReady())("dominio propio (AWS real)", () => {
  const e2e = useE2E();
  const runId = randomBytes(RUN_ID_BYTES).toString("hex");
  const aliases = Object.fromEntries(
    ROUTE_PURPOSES.map((purpose) => [purpose, `${ROUTE_ALIAS_PREFIX}-${runId}-${purpose}`]),
  ) as Record<RoutePurpose, string>;
  let domain: CustomDomain;
  let target: DomainUnderTest;
  let denied = false;

  beforeEach((context) => {
    context.skip(denied, DISTRIBUTION_DENIED_REASON);
  });

  /** Registra la ruta de `purpose` con un token nuevo; devuelve la ruta y
   * las cabeceras que la autorizan. */
  async function register(
    purpose: RoutePurpose,
    sandbox: Sandbox,
    ttlSeconds = ROUTE_TTL_SECONDS,
  ): Promise<[CustomDomainRoute, Record<string, string>]> {
    const trafficToken = randomBytes(32).toString("base64url");
    const route = await domain.register(aliases[purpose], ROUTE_PORT, {
      endpoint: sandbox.endpoint,
      jwe: await jweOf(sandbox),
      trafficToken,
      ttlSeconds,
    });
    return [route, { [TRAFFIC_TOKEN_HEADER]: trafficToken }];
  }

  beforeAll(async () => {
    domain = new CustomDomain({
      publicDomain: process.env[DOMAIN_VAR] as string,
      stackName: `${STACK_NAME_PREFIX}${runId}`,
      region: e2e.settings.region,
    });
    const started = performance.now();
    let status: Awaited<ReturnType<CustomDomain["deploy"]>>;
    try {
      status = await domain.deploy({
        certificateArn: process.env[CERT_ARN_VAR] as string,
        alternateDomainNames: Object.values(aliases).map((alias) =>
          domain.hostFor(alias, ROUTE_PORT),
        ),
        tags: {
          ...tagsFromEnvironment(),
          [ACCEPTANCE_RUN_TAG_KEY]: process.env[ACCEPTANCE_RUN_TAG_VAR] || runId,
        },
      });
    } catch (error) {
      if (
        error instanceof StackError &&
        (await distributionDenied(e2e.settings.region, `${STACK_NAME_PREFIX}${runId}`))
      ) {
        denied = true;
        return;
      }
      throw error;
    }
    reportDomain("deploy() (s)", ((performance.now() - started) / 1000).toFixed(0));
    target = {
      domain,
      connectHost: status.outputs[DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY] as string,
      aliases,
    };
  }, STACK_HOOK_TIMEOUT_MS);

  afterAll(async () => {
    for (const alias of Object.values(aliases)) {
      try {
        await domain.unregister(alias, ROUTE_PORT);
      } catch (error) {
        // Sin KvsArn (el deploy falló antes) no hay nada que borrar.
        if (!(error instanceof CustomDomainError)) {
          throw error;
        }
      }
    }
    // destroy() no lanzando StackError(code: "in_progress") es también la
    // comprobación de CUSTOM_DOMAIN_WAIT_TIMEOUT_MS.
    const started = performance.now();
    await domain.destroy();
    reportDomain("destroy() (s)", ((performance.now() - started) / 1000).toFixed(0));
    expect(await domain.status()).toBeUndefined();
  }, STACK_HOOK_TIMEOUT_MS);

  test("enruta un sandbox registrado, rechaza sin token y 404 tras unregister", async () => {
    const sandbox = await createTestSandbox(e2e);
    await startInBackground(sandbox, `python3 -m http.server ${ROUTE_PORT}`);
    const [route, authorized] = await register("http", sandbox);
    const visible = await waitUntilRouteVisible(target, route.host);
    reportDomain("DOM-5 register() -> visible en el edge (s)", visible.toFixed(1));

    // DOM-2: HTTP/1.1 real a través de cf.updateRequestOrigin, con el token.
    await waitForOk(target, route.host, authorized);
    // SEC-T25: sin el token, 403 — nunca una ruta pública por omisión.
    await waitForStatus(target, route.host, {}, HTTP_FORBIDDEN, PROPAGATION_TIMEOUT_MS);

    // DOM-7 (parcial): refresh() reescribe j:/m: sin cortar la ruta.
    const refreshed = await domain.refresh(route, {
      jwe: await jweOf(sandbox),
      ttlSeconds: ROUTE_TTL_SECONDS,
    });
    expect(refreshed.host).toBe(route.host);
    await waitForOk(target, route.host, authorized);

    await domain.unregister(route.alias, ROUTE_PORT);
    // Tras unregister(), 404 — indistinguible de una ruta que nunca existió.
    const gone = await waitForStatus(
      target,
      route.host,
      authorized,
      HTTP_NOT_FOUND,
      PROPAGATION_TIMEOUT_MS,
    );
    reportDomain("DOM-5 unregister() -> 404 en el edge (s)", gone.toFixed(1));
  });

  test("una ruta con el TTL vencido responde 404", async () => {
    const sandbox = await createTestSandbox(e2e);
    await startInBackground(sandbox, `python3 -m http.server ${ROUTE_PORT}`);
    const registeredAt = performance.now();
    const [route, authorized] = await register("ttl", sandbox, SHORT_ROUTE_TTL_SECONDS);
    await waitUntilRouteVisible(target, route.host);
    await waitForStatus(target, route.host, authorized, HTTP_OK, SHORT_ROUTE_TTL_SECONDS * 1000);
    const expired = await waitForStatus(
      target,
      route.host,
      authorized,
      HTTP_NOT_FOUND,
      SHORT_ROUTE_TTL_SECONDS * 1000 + PROPAGATION_TIMEOUT_MS,
      STABLE_OBSERVATIONS,
    );
    const sinceRegistered = (performance.now() - registeredAt) / 1000;
    reportDomain("TTL vencido -> 404 (s desde visible)", expired.toFixed(1));
    reportDomain("TTL vencido -> 404 (s desde register)", sinceRegistered.toFixed(1));
    // Un 404 más temprano que el TTL es de un servidor del edge sin la ruta.
    expect(sinceRegistered).toBeGreaterThanOrEqual(
      SHORT_ROUTE_TTL_SECONDS - EDGE_CLOCK_SKEW_TOLERANCE_SECONDS,
    );
  });

  test("deja pasar un upgrade de WebSocket", async () => {
    const sandbox = await createTestSandbox(e2e);
    await sandbox.files.write(
      WEBSOCKET_ECHO_SERVER_PATH,
      readFileSync(WEBSOCKET_ECHO_SERVER_SOURCE, "utf8"),
    );
    await startInBackground(sandbox, `python3 ${WEBSOCKET_ECHO_SERVER_PATH} ${ROUTE_PORT}`);
    const [route, authorized] = await register("ws", sandbox);
    await waitUntilRouteVisible(target, route.host);
    // DOM-3: ¿corre la Function en la petición de upgrade, y acepta el
    // proxy de AWS la cabecera de token ahí también?
    const echoed = await echoWithRetries(target, route.host, authorized, Buffer.from("ping"));
    expect(echoed.toString("utf8")).toBe("ping");
  });

  test("el tráfico por el dominio despierta un sandbox pausado", async () => {
    // DOM-8: con autoResume, el proxy de AWS hace el resume; la Function no
    // sabe nada del estado del VM.
    const sandbox = await createTestSandbox(e2e, { idle: RESUME_IDLE });
    await startInBackground(sandbox, `python3 -m http.server ${ROUTE_PORT}`);
    const [route, authorized] = await register("resume", sandbox);
    await waitUntilRouteVisible(target, route.host);
    await waitForOk(target, route.host, authorized);
    expect(await sandbox.pause()).toBe(true);
    const resumed = await waitForStatus(
      target,
      route.host,
      authorized,
      HTTP_OK,
      AUTO_RESUME_TIMEOUT_MS,
    );
    reportDomain("DOM-8 pausado -> 200 por el dominio (s)", resumed.toFixed(1));
  });

  test("status() refleja la distribución ya desplegada", async () => {
    const status = await domain.status();
    expect(status).toBeDefined();
    expect(["CREATE_COMPLETE", "UPDATE_COMPLETE"]).toContain(status?.state);
    expect(status?.outputs.KvsArn).toBeDefined();
    expect(status?.outputs[DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY]).toBe(target.connectHost);
  });
});
