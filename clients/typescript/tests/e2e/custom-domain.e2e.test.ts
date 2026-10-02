/**
 * `m15-custom-domain` contra AWS real (D3 + etapa de aceptación serializada,
 * ver `MILESTONES.md` M15 y `design.md` del cambio): despliega
 * `infra/custom-domain.yaml`, registra una ruta con `trafficToken`,
 * comprueba HTTP/1.1 de verdad a través de la distribución (DOM-2), el
 * upgrade de WebSocket (DOM-3), que una petición sin el token da 403, que
 * una ruta ya `unregister()`-ada da 404, y que `destroy()` espera de
 * verdad a que CloudFront deshabilite y borre la distribución (en vez de
 * agotar el timeout genérico de `OptionalStacks` y lanzar
 * `StackError(code: "in_progress")` — el hallazgo de revisión de PR #74
 * que motivó `CUSTOM_DOMAIN_WAIT_TIMEOUT_MS`). Espejo de
 * `clients/python/tests/e2e/test_m15_custom_domain.py`.
 *
 * Se salta por completo salvo que el entorno de aceptación traiga, además
 * de `RAYITO_E2E=1`/`RAYITO_TEMPLATE` (`helpers.ts`), un dominio propio, un
 * certificado ACM en `us-east-1` que lo cubra y una etiqueta de ejecución
 * — D3: sólo el mantenedor puede aportar los dos primeros; la etiqueta la
 * decide quien lance la corrida. D3 bloquea *ejecutar* este fichero, no
 * escribirlo (hallazgo de revisión de PR #74).
 *
 * Coste: una distribución CloudFront, su CloudFront Function y su
 * KeyValueStore durante la vida de la suite (deploy + destroy, con
 * destroy esperando a que CloudFront la deshabilite y borre de verdad)
 * más un par de sandboxes breves y un puñado de `PutKey`/`DeleteKey` —
 * cifra exacta pendiente de confirmar en el propio informe de aceptación
 * (ver `dominio-propio.md`, "Coste y activación").
 */

import { createHash, randomBytes } from "node:crypto";
import * as https from "node:https";
import type { TLSSocket } from "node:tls";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { CustomDomain, type CustomDomainRoute } from "../../src/custom-domain/service.js";
import { createTestSandbox, e2eEnabled, useE2E } from "./helpers.js";

const PUBLIC_DOMAIN_VAR = "RAYITO_CUSTOM_DOMAIN";
const CERTIFICATE_ARN_VAR = "RAYITO_CUSTOM_DOMAIN_CERTIFICATE_ARN";
// Regla de limpieza de AWS: tras aceptar, sólo se borran las VMs y
// versiones de imagen de esta corrida. Sin valor por defecto: una corrida
// de aceptación real siempre la trae.
const ACCEPTANCE_RUN_TAG_VAR = "RAYITO_ACCEPTANCE_RUN_TAG";
const ACCEPTANCE_RUN_TAG_KEY = "rayito:acceptance-run";

function customDomainEnvReady(): boolean {
  return Boolean(
    process.env[PUBLIC_DOMAIN_VAR] &&
      process.env[CERTIFICATE_ARN_VAR] &&
      process.env[ACCEPTANCE_RUN_TAG_VAR],
  );
}

const ROUTE_PORT = 8000;
// Suficiente para que la suite entera corra sin que la ruta caduque sola
// (T25); muy por debajo de MAX_TEST_SANDBOX_TIMEOUT_MS.
const ROUTE_TTL_SECONDS = 1800;
const HTTPS_PORT = 443;
const HTTP_REQUEST_TIMEOUT_MS = 20_000;
const WEBSOCKET_HANDSHAKE_TIMEOUT_MS = 20_000;
// GUID fijo de RFC 6455 §1.3, para calcular Sec-WebSocket-Accept.
const WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const HTTP_SERVER_BOOT_GRACE_MS = 2_000;
const TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function httpsStatus(host: string, path: string, headers: Record<string, string>): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = https.request(
      { host, port: HTTPS_PORT, path, method: "GET", headers, timeout: HTTP_REQUEST_TIMEOUT_MS },
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

/**
 * Apertura de WebSocket mínima (RFC 6455) con un único frame de texto de
 * ida y vuelta, sin más dependencia que `node:http`/`node:tls`: suficiente
 * para DOM-3 (¿corre la Function en el upgrade? ¿el proxy de AWS acepta la
 * cabecera de token en la petición de upgrade?), no un cliente de
 * WebSocket de propósito general.
 */
function websocketHandshakeAndEcho(
  host: string,
  path: string,
  headers: Record<string, string>,
  message: Buffer,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const websocketKey = randomBytes(16).toString("base64");
    const request = https.request({
      host,
      port: HTTPS_PORT,
      path,
      method: "GET",
      headers: {
        ...headers,
        Connection: "Upgrade",
        Upgrade: "websocket",
        "Sec-WebSocket-Key": websocketKey,
        "Sec-WebSocket-Version": "13",
      },
      timeout: WEBSOCKET_HANDSHAKE_TIMEOUT_MS,
    });
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
      // Un único frame de texto cliente->servidor (enmascarado, como exige
      // RFC 6455 §5.1 para toda trama que sale de un cliente) de menos de
      // 126 bytes, así que la longitud cabe en el segundo byte sin
      // extensión. `readUInt8`/`writeUInt8`, no el índice `[]`, para que
      // `noUncheckedIndexedAccess` no marque cada acceso como posiblemente
      // `undefined`.
      const mask = randomBytes(4);
      const maskedPayload = Buffer.from(
        message.map((byte, index) => byte ^ mask.readUInt8(index % 4)),
      );
      const frame = Buffer.concat([
        Buffer.from([0x81, 0x80 | message.length]),
        mask,
        maskedPayload,
      ]);
      socket.write(frame);
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
      reject(new Error(`el upgrade de WebSocket no se aceptó: HTTP ${response.statusCode}`));
    });
    request.end();
  });
}

/** Servidor de eco de WebSocket mínimo (sólo `net`, sin instalar nada),
 * para que el sandbox de prueba tenga algo real detrás de la ruta. */
const WEBSOCKET_ECHO_SERVER_PYTHON = (port: number): string => `
import hashlib, base64, socket
GUID = "${WEBSOCKET_ACCEPT_GUID}"
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", ${port}))
srv.listen(1)
conn, _ = srv.accept()
data = b""
while b"\\r\\n\\r\\n" not in data:
    data += conn.recv(4096)
key = None
for line in data.decode("latin1").split("\\r\\n"):
    if line.lower().startswith("sec-websocket-key:"):
        key = line.split(":", 1)[1].strip()
accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
conn.sendall((
    "HTTP/1.1 101 Switching Protocols\\r\\n"
    "Upgrade: websocket\\r\\n"
    "Connection: Upgrade\\r\\n"
    f"Sec-WebSocket-Accept: {accept}\\r\\n\\r\\n"
).encode())
frame = conn.recv(4096)
length = frame[1] & 0x7F
mask = frame[2:6]
payload = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:6 + length]))
conn.sendall(bytes([0x81, len(payload)]) + payload)
conn.close()
`;

describe.runIf(e2eEnabled() && customDomainEnvReady())("dominio propio (AWS real)", () => {
  const e2e = useE2E();
  let domain: CustomDomain;

  beforeAll(async () => {
    domain = new CustomDomain({
      publicDomain: process.env[PUBLIC_DOMAIN_VAR] as string,
      region: e2e.settings.region,
    });
    await domain.deploy({
      certificateArn: process.env[CERTIFICATE_ARN_VAR] as string,
      tags: { [ACCEPTANCE_RUN_TAG_KEY]: process.env[ACCEPTANCE_RUN_TAG_VAR] as string },
    });
  });

  afterAll(async () => {
    // La comprobación de que `destroy()` espera lo que de verdad tarda
    // CloudFront (CUSTOM_DOMAIN_WAIT_TIMEOUT_MS) es este propio teardown
    // no lanzando StackError(code: "in_progress").
    await domain.destroy();
  });

  test("enruta un sandbox registrado, rechaza sin token y 404 tras unregister", async () => {
    const sandbox = await createTestSandbox(e2e);
    await sandbox.commands.run(`python3 -m http.server ${ROUTE_PORT}`, {
      background: true,
      timeoutMs: 0,
    });
    await sleep(HTTP_SERVER_BOOT_GRACE_MS);
    const jwe = (await sandbox.getHost(ROUTE_PORT)).headers["x-aws-proxy-auth"] ?? "";
    const trafficToken = randomBytes(32).toString("base64url");
    const route: CustomDomainRoute = await domain.register(sandbox.sandboxId, ROUTE_PORT, {
      endpoint: sandbox.endpoint,
      jwe,
      trafficToken,
      ttlSeconds: ROUTE_TTL_SECONDS,
    });

    // DOM-2: HTTP/1.1 real a través de cf.updateRequestOrigin, con el
    // token correcto.
    expect(await httpsStatus(route.host, "/", { [TRAFFIC_TOKEN_HEADER]: trafficToken })).toBe(200);

    // SEC-T25: sin el token, 403 — nunca una ruta pública por omisión.
    expect(await httpsStatus(route.host, "/", {})).toBe(403);

    await domain.unregister(sandbox.sandboxId, ROUTE_PORT);

    // Tras unregister(), 404 — indistinguible de una ruta que nunca
    // existió.
    expect(await httpsStatus(route.host, "/", { [TRAFFIC_TOKEN_HEADER]: trafficToken })).toBe(404);
  });

  test("deja pasar un upgrade de WebSocket", async () => {
    const sandbox = await createTestSandbox(e2e);
    await sandbox.commands.run(
      `python3 -c ${JSON.stringify(WEBSOCKET_ECHO_SERVER_PYTHON(ROUTE_PORT))}`,
      {
        background: true,
        timeoutMs: 0,
      },
    );
    await sleep(HTTP_SERVER_BOOT_GRACE_MS);
    const jwe = (await sandbox.getHost(ROUTE_PORT)).headers["x-aws-proxy-auth"] ?? "";
    const trafficToken = randomBytes(32).toString("base64url");
    const route = await domain.register(sandbox.sandboxId, ROUTE_PORT, {
      endpoint: sandbox.endpoint,
      jwe,
      trafficToken,
      ttlSeconds: ROUTE_TTL_SECONDS,
    });
    try {
      // DOM-3: ¿corre la Function en la petición de upgrade, y acepta el
      // proxy de AWS la cabecera de token ahí también?
      const echoed = await websocketHandshakeAndEcho(
        route.host,
        "/",
        { [TRAFFIC_TOKEN_HEADER]: trafficToken },
        Buffer.from("ping"),
      );
      expect(echoed.toString("utf8")).toBe("ping");
    } finally {
      // Idempotente: borrar una ruta que el propio test ya hubiera dejado
      // a medias no es un error.
      await domain.unregister(sandbox.sandboxId, ROUTE_PORT);
    }
  });

  test("status() refleja la distribución ya desplegada", async () => {
    const status = await domain.status();
    expect(status).toBeDefined();
    expect(["CREATE_COMPLETE", "UPDATE_COMPLETE"]).toContain(status?.state);
    expect(status?.outputs.KvsArn).toBeDefined();
  });
});
