/**
 * La parte interactiva de la CLI `rayito` de TypeScript (ADR-027, fase 4)
 * contra AWS real: una terminal de verdad (`sandbox create` sin `--detach` y
 * `sandbox connect`, con la entrada por tubería) y `sandbox proxy` hacia un
 * servidor HTTP del guest, con sus rechazos de `Host` y `Origin`. Corre
 * `runCli` y `runProxy` en proceso con la sesión real de AWS, como `node
 * dist/cli.mjs`. Espejo de `test_sandbox_proxy_e2e.py`.
 *
 * Necesita `RAYITO_E2E=1` y `RAYITO_TEMPLATE` como el resto de los e2e; el
 * token del sandbox va a un fichero temporal y nunca a argv ni a la salida.
 * Sin `RAYITO_E2E=1` se omite sin llamar a AWS.
 *
 * Coste: dos sandboxes de la imagen `RAYITO_TEMPLATE` durante un par de
 * minutos (uno por la terminal de `create`, otro para `connect` y el proxy);
 * el proxy sólo usa `GetMicrovm` y `CreateMicrovmAuthToken`, gratuitos.
 */

import { request } from "node:http";
import { describe, expect } from "vitest";
import { runProxy } from "../../src/cli/proxy-run.js";
import { LambdaMicrovmsControlPlane } from "../../src/index.js";
import { defineCliInteractiveScenarios } from "../shared/cli-scenarios.js";
import { freePort } from "../unit/cli/proxy-harness.js";
import { realAwsCliWorld } from "./cli-world.js";
import { e2eEnabled, sleep, waitUntil } from "./helpers.js";

const HTTP_SERVER_PORT = 8000;
const HTTP_SERVER_BOOT_GRACE_MS = 2000;
const PROXY_READY_BUDGET_MS = 30_000;
const POLL_MS = 100;
const RUN_LABEL = `rayito-tscli-interactive-${Date.now()}`;
const MISDIRECTED_REQUEST = 421;
const FORBIDDEN = 403;

function get(port: number, headers: Record<string, string>, method = "GET") {
  return new Promise<number>((resolve, reject) => {
    const req = request({ host: "127.0.0.1", port, path: "/", method, headers }, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode ?? 0));
    });
    req.on("error", reject);
    req.end();
  });
}

/** `sandbox proxy` hacia el servidor HTTP que la sesión `connect` dejó en el guest. */
async function exerciseProxy(sandboxId: string): Promise<void> {
  await sleep(HTTP_SERVER_BOOT_GRACE_MS);
  const port = await freePort();
  const stop = new AbortController();
  const announced: string[] = [];
  const reports: string[] = [];
  const served = runProxy(LambdaMicrovmsControlPlane.fromRegion(undefined), {
    sandboxId,
    port: HTTP_SERVER_PORT,
    localPort: port,
    bind: "127.0.0.1",
    allowedHosts: [],
    allowedOrigins: [],
    maxConnections: 4,
    onReady: (line) => announced.push(line),
    report: (reason) => reports.push(reason),
    signal: stop.signal,
  });
  try {
    await waitUntil(() => announced.length > 0, PROXY_READY_BUDGET_MS, "el proxy", POLL_MS);
    expect(await get(port, {})).toBe(200);
    expect(await get(port, { Host: "attacker.example" })).toBe(MISDIRECTED_REQUEST);
    expect(await get(port, { Origin: "http://attacker.example" }, "POST")).toBe(FORBIDDEN);
  } finally {
    stop.abort();
    await served;
  }
  expect(reports.join("\n")).not.toMatch(/eyJ/);
}

describe.runIf(e2eEnabled())("CLI rayito interactiva (AWS real)", () => {
  defineCliInteractiveScenarios(realAwsCliWorld(), RUN_LABEL, {
    setupLines: [`python3 -m http.server ${HTTP_SERVER_PORT} >/dev/null 2>&1 &`],
    afterConnect: exerciseProxy,
  });
});
