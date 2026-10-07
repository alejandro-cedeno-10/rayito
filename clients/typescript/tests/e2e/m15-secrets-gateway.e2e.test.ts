/**
 * M15 `gateways` contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` = una
 * imagen con `rayd` 0.6.0 o posterior). Corre sólo en la etapa de aceptación
 * serializada. Espejo de `tests/e2e/test_m15_secrets_gateway.py`: inyección y
 * anti-suplantación (T24), 403 fuera de `allow` y ante `..` bajo `/*`, 429,
 * rotación con el mismo puerto, SEC-7 (subida troceada) y SEC-10 (el secreto
 * nunca en los logs del SDK ni en un `environ` del guest).
 *
 * `RAYITO_E2E_GATEWAY_UPSTREAM` elige el eco (por defecto
 * `https://postman-echo.com`). Coste: un secreto durante < 5 min
 * (≈ $0,0005), ~6 llamadas a Secrets Manager y un sandbox (~$0,03).
 */

import { randomBytes } from "node:crypto";
import { describe, expect, test } from "vitest";
import { Sandbox, SecretCache, SecretGateway, SecretStore } from "../../src/index.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

const UPSTREAM_VAR = "RAYITO_E2E_GATEWAY_UPSTREAM";
const DEFAULT_UPSTREAM = "https://postman-echo.com";
const ROUTE = "echo";
const INJECTED_HEADER = "x-rayito-e2e";
/** Capacidad del cubo (`refresh()` lo rellena); la ráfaga final la supera
 * con margen para la recarga (8/min) que caiga mientras corre. */
const RATE_PER_MINUTE = 8;
const BURST_MARGIN = 4;
const CURL_TIMEOUT_MS = 60_000;
/** SEC-7: la imagen no trae /etc/hostname (aceptación 2026-10-02), así que el
 * test escribe su propia carga. 96 KiB supera el búfer de subida de curl
 * (64 KiB por defecto), luego viaja en varios trozos, y cabe bajo el límite
 * de cuerpo del eco por defecto: postman-echo responde 500 a 128 KiB también
 * en directo, sin la pasarela. */
const UPLOAD_PATH = "/tmp/rayito-e2e-upload";
const UPLOAD_BYTES = 96 * 1024;
/** Secrets Manager es de consistencia eventual: justo después de
 * `UpdateSecret`, `GetSecretValue` puede devolver un momento la versión
 * anterior, y `refresh()` empuja lo que lee. El test repite `refresh()` +
 * petición hasta ver el valor nuevo (cada `refresh()` rellena el cubo, así
 * que los reintentos no gastan la ráfaga del final). */
const ROTATION_ATTEMPTS = 6;
const ROTATION_RETRY_MS = 2_000;

async function curl(sandbox: Sandbox, args: string): Promise<[number, string]> {
  const result = await sandbox.commands.run(
    `curl -sS -o /tmp/body -w '%{http_code}' ${args}; echo; cat /tmp/body`,
    { timeoutMs: CURL_TIMEOUT_MS },
  );
  const newline = result.stdout.indexOf("\n");
  return [Number(result.stdout.slice(0, newline).trim()), result.stdout.slice(newline + 1)];
}

function echoedHeader(body: string): string | undefined {
  const headers = (JSON.parse(body) as { headers?: Record<string, string> }).headers ?? {};
  return headers[INJECTED_HEADER];
}

/** `refresh()` y una petición, hasta `ROTATION_ATTEMPTS` veces, mientras el
 * eco no devuelva el valor rotado. */
async function refreshUntilRotated(
  sandbox: Sandbox,
  url: string,
  rotated: string,
): Promise<[number, string]> {
  let result: [number, string] = [0, ""];
  for (let attempt = 0; attempt < ROTATION_ATTEMPTS; attempt += 1) {
    if (attempt > 0) {
      await new Promise((resolve) => setTimeout(resolve, ROTATION_RETRY_MS));
    }
    await sandbox.gateways.refresh();
    result = await curl(sandbox, `${url}/headers`);
    if (result[0] === 200 && echoedHeader(result[1]) === rotated) {
      break;
    }
  }
  return result;
}

describe.runIf(e2eEnabled())("M15 secrets gateway (AWS real)", () => {
  const e2e = useE2E();

  test("inyecta, aplica allow/límite, rota con el mismo puerto y nunca filtra", async () => {
    const upstream = process.env[UPSTREAM_VAR] || DEFAULT_UPSTREAM;
    const store = new SecretStore({ region: e2e.controlPlane.region });
    const name = `e2e-gw-${randomBytes(6).toString("hex")}`;
    const value = `sentinel-${randomBytes(16).toString("hex")}`;
    const rotated = `${value}-rotated`;
    const logged: string[] = [];
    const record = (message: string, fields?: unknown) =>
      logged.push(message, JSON.stringify(fields));
    const logger = { debug: record, info: record, warn: record, error: record };
    await store.create(name, value);
    try {
      const sandbox = await Sandbox.create({
        template: e2e.templateArn,
        timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
        idle: null,
        executionRoleArn: e2e.settings.executionRoleArn,
        logging: e2e.settings.logging,
        controlPlane: e2e.controlPlane,
        gateways: {
          [ROUTE]: new SecretGateway({
            upstream,
            headers: { [INJECTED_HEADER]: name },
            allow: [
              ["GET", "/headers"],
              ["GET", "/get/*"],
              ["POST", "/post"],
            ],
            ratePerMinute: RATE_PER_MINUTE,
          }),
        },
        secretCache: new SecretCache({ store }),
        logger,
      });
      e2e.created.push(sandbox);
      const status = sandbox.gateways.get(ROUTE);
      expect(status).toBeDefined();
      const url = status?.url ?? "";
      const port = status?.port;

      let [code, body] = await curl(sandbox, `-H '${INJECTED_HEADER}: spoofed' ${url}/headers`);
      expect(code).toBe(200);
      expect(echoedHeader(body)).toBe(value);

      expect((await curl(sandbox, `${url}/status/200`))[0]).toBe(403);
      expect((await curl(sandbox, `--path-as-is ${url}/get/../headers`))[0]).toBe(403);
      expect((await curl(sandbox, `--path-as-is ${url}/get/%2e%2e/headers`))[0]).toBe(403);

      await sandbox.commands.run(`head -c ${UPLOAD_BYTES} /dev/zero > ${UPLOAD_PATH}`);
      [code] = await curl(
        sandbox,
        "-X POST -H 'Transfer-Encoding: chunked' " +
          `-H 'Content-Type: application/octet-stream' --data-binary @${UPLOAD_PATH} ${url}/post`,
      );
      expect(code).toBe(200);

      await store.update(name, rotated);
      [code, body] = await refreshUntilRotated(sandbox, url, rotated);
      expect(sandbox.gateways.get(ROUTE)?.port).toBe(port);
      expect(code).toBe(200);
      expect(echoedHeader(body)).toBe(rotated);

      const codes: number[] = [];
      for (let index = 0; index < RATE_PER_MINUTE + BURST_MARGIN; index += 1) {
        codes.push((await curl(sandbox, `${url}/headers`))[0]);
      }
      expect(codes).toContain(429);

      const leaked = await sandbox.commands.run(
        `cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c -e ${value} || true`,
      );
      expect(["", "0"]).toContain(leaked.stdout.trim());
      await sandbox.kill();
    } finally {
      await store.destroy(name);
    }
    const everything = logged.join("\n");
    for (const secretText of [value, rotated, name]) {
      expect(everything).not.toContain(secretText);
    }
  });
});
