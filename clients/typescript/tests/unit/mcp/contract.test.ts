/**
 * Las herramientas del servidor MCP (nombre, descripción, anotaciones,
 * argumentos y campos de salida) frente al golden compartido
 * `testdata/mcp/tools.json`, el mismo que comprueba el servidor de Python
 * (`test_mcp_contract.py`).
 */

import { describe, expect, it } from "vitest";
import { VERSION } from "../../../src/version.js";
import { describeServer, GOLDEN } from "../../shared/mcp-golden.js";
import { startFixture } from "./support.js";

describe("contrato compartido de herramientas", () => {
  it("las herramientas coinciden con testdata/mcp/tools.json", async () => {
    const fixture = await startFixture();
    const client = await fixture.connect();
    expect(await describeServer(client)).toEqual(GOLDEN);
  });

  it("el servidor anuncia la versión del paquete", async () => {
    const fixture = await startFixture();
    const client = await fixture.connect();
    expect(client.getServerVersion()?.version).toBe(VERSION);
  });
});
