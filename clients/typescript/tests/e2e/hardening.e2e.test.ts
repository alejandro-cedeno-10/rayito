/**
 * Espejo de `test_m6_hardening.py::test_cpu_time_limit` y
 * `::test_egress_allowlist`, las dos opciones de `create()` que el SDK
 * TypeScript traduce a la petición (`payload.ts`, `control-plane.ts`):
 * `cpuTimeLimit` llega a `rayd` (`ulimit -t`/`-Ht` y un bucle ocupado que
 * muere, sin tocar el kernel de código) y `egress` aparece en
 * `getInfo().egress` y restringe la salida. El segundo test se omite sin
 * `RAYITO_EGRESS_CONNECTOR_ARN` (un conector de `infra/egress-connector.yaml`).
 */

import { describe, expect, test } from "vitest";
import { CommandExitError } from "../../src/index.js";
import { createTestSandbox, e2eEnabled, report, seconds, useE2E } from "./helpers.js";

const EGRESS_CONNECTOR_VAR = "RAYITO_EGRESS_CONNECTOR_ARN";
const CPU_LIMIT_SECONDS = 2;
const CPU_HARD_LIMIT_MARGIN_SECONDS = 5;
const CPU_KILL_BUDGET_S = 8;
const SIGNAL_EXIT_BASE = 128;
const SIGXCPU = 24;
const SIGKILL = 9;
const CPU_KILLED_EXIT_CODES = [SIGNAL_EXIT_BASE + SIGXCPU, SIGNAL_EXIT_BASE + SIGKILL];
const POST_KILL_COMMAND = "sleep 3";
const COMMAND_TIMEOUT_MS = 30_000;
/** Plazo de cada intento de conexión de `urlopen` en la sonda de egress. */
const EGRESS_PROBE_TIMEOUT_S = 5;
/** Direcciones a las que resuelve `example.com`; `urlopen` espera el plazo en cada una (AWS_API_NOTES Q46). */
const EGRESS_PROBE_ADDRESSES = 2;
/** Margen para arrancar el intérprete y resolver el DNS; el máximo medido fue 11,7 s. */
const EGRESS_PROBE_STARTUP_SLACK_S = 5;
const EGRESS_REFUSAL_BUDGET_S =
  EGRESS_PROBE_TIMEOUT_S * EGRESS_PROBE_ADDRESSES + EGRESS_PROBE_STARTUP_SLACK_S;
const BUSY_LOOP = "python3 -c 'while True: pass'";
const EGRESS_PROBE =
  `python3 -c 'import urllib.request as u; u.urlopen("https://example.com", timeout=${EGRESS_PROBE_TIMEOUT_S}); print("egress ok")'`;
const BUSY_KERNEL_CELL = "sum(range(10**7))";
const BUSY_KERNEL_RESULT = "49999995000000";
const egressConnector = process.env[EGRESS_CONNECTOR_VAR];

describe.skipIf(!e2eEnabled())("hardening options of create() (mirror of M6)", () => {
  const context = useE2E();

  test("cpuTimeLimit reaches rayd: ulimit -t and a busy loop that dies", async () => {
    const sbx = await createTestSandbox(context, { cpuTimeLimit: CPU_LIMIT_SECONDS });
    const limits = await sbx.commands.run("ulimit -t; ulimit -Ht", {
      timeoutMs: COMMAND_TIMEOUT_MS,
    });
    expect(limits.stdout.split(/\s+/).filter(Boolean)).toEqual([
      String(CPU_LIMIT_SECONDS),
      String(CPU_LIMIT_SECONDS + CPU_HARD_LIMIT_MARGIN_SECONDS),
    ]);
    const started = performance.now();
    const killed = await sbx.commands
      .run(BUSY_LOOP, { timeoutMs: COMMAND_TIMEOUT_MS })
      .catch((error: unknown) => error);
    const elapsed = seconds(started);
    expect(killed).toBeInstanceOf(CommandExitError);
    report("cpuTimeLimit: busy loop killed", elapsed);
    expect(CPU_KILLED_EXIT_CODES).toContain((killed as CommandExitError).exitCode);
    expect(elapsed).toBeLessThanOrEqual(CPU_KILL_BUDGET_S);
    expect(
      (await sbx.commands.run(POST_KILL_COMMAND, { timeoutMs: COMMAND_TIMEOUT_MS })).exitCode,
    ).toBe(0);
    expect((await sbx.runCode(BUSY_KERNEL_CELL)).text).toBe(BUSY_KERNEL_RESULT);
  });

  test.skipIf(!egressConnector)(
    `egress connector shows in getInfo().egress and restricts the way out (requires ${EGRESS_CONNECTOR_VAR})`,
    async () => {
      const connector = egressConnector as string;
      const restricted = await createTestSandbox(context, { egress: [connector] });
      expect((await restricted.getInfo()).egress).toEqual([connector]);
      const started = performance.now();
      const refused = await restricted.commands
        .run(EGRESS_PROBE, { timeoutMs: COMMAND_TIMEOUT_MS })
        .catch((error: unknown) => error);
      expect(refused).toBeInstanceOf(CommandExitError);
      expect(seconds(started)).toBeLessThanOrEqual(EGRESS_REFUSAL_BUDGET_S);
      const open = await createTestSandbox(context);
      expect(
        (await open.commands.run(EGRESS_PROBE, { timeoutMs: COMMAND_TIMEOUT_MS })).stdout,
      ).toBe("egress ok\n");
    },
  );
});
