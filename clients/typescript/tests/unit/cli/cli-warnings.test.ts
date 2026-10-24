/**
 * Los avisos del SDK llegan a stderr sin `--verbose`, como en la CLI de
 * Python (su manejador de último recurso de `logging`): el de la receta que
 * pedía renovar claves sale con la línea de
 * `testdata/fleet-recipe/renewal.json` (`cliWarningLine`), y la flota que la
 * CLI reconecta recibe ese logger.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test, vi } from "vitest";
import { AgentFleet } from "../../../src/agent/fleet.js";
import { RECIPE_RENEWAL_IGNORED } from "../../../src/agent/fleet-recipe.js";
import { warnRecipe } from "../../../src/agent/fleet-recipe-aws.js";
import { SdkAgentProjectTools } from "../../../src/cli/agent-project-adapter.js";
import { CliConsole } from "../../../src/cli/console.js";
import { warningsLogger } from "../../../src/cli/run.js";
import type { CliServices } from "../../../src/cli/services.js";
import { FakeTerminal } from "./terminal.js";

const RENEWAL = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "fleet-recipe",
      "renewal.json",
    ),
    "utf8",
  ),
) as { cliWarningLine: string };

describe("CLI warnings", () => {
  test("the renewal the recipe asked for is printed like the Python CLI", () => {
    const terminal = new FakeTerminal();
    warnRecipe(warningsLogger(new CliConsole(terminal)), RECIPE_RENEWAL_IGNORED);
    expect(terminal.stderrText).toBe(`${RENEWAL.cliWarningLine}\n`);
  });

  test("without --verbose only warnings and errors are written", () => {
    const logger = warningsLogger(new CliConsole(new FakeTerminal()));
    expect(logger.info).toBeUndefined();
    expect(logger.debug).toBeUndefined();
    expect(logger.warn).toBeDefined();
  });

  test("only codes, ids and error classes reach the line", () => {
    const terminal = new FakeTerminal();
    const logger = warningsLogger(new CliConsole(terminal));
    logger.warn?.("aviso", { sandboxId: "mvm-1", path: "/home/x/secreto", message: "texto" });
    logger.error?.("fallo");
    expect(terminal.stderrText).toBe("aviso: mvm-1\nfallo\n");
  });

  test("status and stop never ask the fleet to warn about renewal", async () => {
    const connect = vi
      .spyOn(AgentFleet, "connect")
      .mockRejectedValue(new Error("detenido en la prueba"));
    const services = async () =>
      ({ region: "us-east-1", controlPlane: {} }) as unknown as CliServices;
    const tools = new SdkAgentProjectTools(services);
    await expect(
      tools.connectFleet("support-bot", "token", { renewGatewayKeys: false }),
    ).rejects.toThrow("detenido");
    expect(connect.mock.calls[0]?.[1]?.renewGatewayKeys).toBe(false);
    connect.mockRestore();
  });

  test("a fleet the CLI reconnects gets the CLI logger", async () => {
    const logger = warningsLogger(new CliConsole(new FakeTerminal()));
    const connect = vi
      .spyOn(AgentFleet, "connect")
      .mockRejectedValue(new Error("detenido en la prueba"));
    const services = async () =>
      ({ region: "us-east-1", controlPlane: {} }) as unknown as CliServices;
    const tools = new SdkAgentProjectTools(services, undefined, logger);
    await expect(tools.connectFleet("support-bot", "token")).rejects.toThrow("detenido");
    expect(connect.mock.calls[0]?.[1]?.logger).toBe(logger);
    connect.mockRestore();
  });
});
