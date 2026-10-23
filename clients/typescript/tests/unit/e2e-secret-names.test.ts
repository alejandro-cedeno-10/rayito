/**
 * Los nombres de secreto de los e2e de despliegue agnóstico
 * (`tests/e2e/agnostic-deploy-support.ts`): únicos por llamada y por ámbito, y
 * siempre bajo `rayito-e2e/<RAYITO_E2E_RUN_ID>/`, para que AgentCore y MicroVM
 * no choquen en la misma ejecución (`ResourceExistsException` con un secreto
 * pendiente de borrado). No llama a AWS.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const RUN_ID = "unit-run-0001";
const CALLS = 50;
const SECRET_NAME_PATTERN = /^[A-Za-z0-9/_+=.@-]+$/;

type SupportModule = typeof import("../e2e/agnostic-deploy-support.js");

async function support(): Promise<SupportModule> {
  vi.resetModules();
  return import("../e2e/agnostic-deploy-support.js");
}

describe("secretName de los e2e", () => {
  beforeEach(() => {
    vi.stubEnv("RAYITO_E2E_RUN_ID", RUN_ID);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("queda bajo rayito-e2e/<run>/ con el ámbito y el tipo", async () => {
    const { secretName, secretPrefix } = await support();
    expect(secretPrefix()).toBe(`rayito-e2e/${RUN_ID}/`);
    const name = secretName("microvm-senv", "ts-db-url");
    expect(name.startsWith(`rayito-e2e/${RUN_ID}/microvm-senv-ts-db-url-`)).toBe(true);
    expect(name).toMatch(/-[0-9a-f]{8}$/);
    expect(name).toMatch(SECRET_NAME_PATTERN);
  });

  it("no repite nombre entre llamadas con el mismo ámbito y tipo", async () => {
    const { secretName } = await support();
    const names = Array.from({ length: CALLS }, () => secretName("agentcore-senv", "ts-db-url"));
    expect(new Set(names).size).toBe(CALLS);
  });

  it("AgentCore y MicroVM con el mismo tipo nunca comparten nombre", async () => {
    const { secretName } = await support();
    const agentcore = secretName("agentcore-senv", "ts-tickets");
    const microvm = secretName("microvm-senv", "ts-tickets");
    expect(agentcore).not.toBe(microvm);
    expect(agentcore.includes("/agentcore-senv-")).toBe(true);
    expect(microvm.includes("/microvm-senv-")).toBe(true);
  });
});
