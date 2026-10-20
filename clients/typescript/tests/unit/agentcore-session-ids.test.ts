/**
 * El id de sesión de AgentCore (ADR-031, D7): los vectores de
 * `testdata/agentcore-deploy/session-ids.json`, que Python comparte, y la
 * forma de un id nuevo.
 */

import { describe, expect, it } from "vitest";
import { agentcoreSessionId } from "../../src/agentcore/identifiers.js";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  AGENTCORE_SESSION_ID_MAX_LENGTH,
  AGENTCORE_SESSION_ID_MIN_LENGTH,
} from "../../src/limits.js";
import { sessionVectors } from "./agentcore-vectors.js";

describe("id de sesión de AgentCore", () => {
  for (const testCase of sessionVectors.cases) {
    it(`${testCase.kind}: ${testCase.input.slice(0, 24)}`, () => {
      expect(agentcoreSessionId(testCase.input)).toBe(testCase.expected);
    });
  }

  it("sin id usa uno nuevo con la forma del contrato", () => {
    const first = agentcoreSessionId(undefined);
    expect(first).toMatch(new RegExp(sessionVectors.newPattern));
    expect(agentcoreSessionId(undefined)).not.toBe(first);
  });

  it("un id vacío es un error de argumento", () => {
    expect(() => agentcoreSessionId("")).toThrow(InvalidArgumentError);
  });

  it("todo id que se devuelve cabe en lo que AWS acepta", () => {
    for (const testCase of sessionVectors.cases) {
      const used = agentcoreSessionId(testCase.input);
      expect(used.length).toBeGreaterThanOrEqual(AGENTCORE_SESSION_ID_MIN_LENGTH);
      expect(used.length).toBeLessThanOrEqual(AGENTCORE_SESSION_ID_MAX_LENGTH);
    }
  });

  it("devolver el id usado continúa la misma sesión", () => {
    const used = agentcoreSessionId("user-42");
    expect(agentcoreSessionId(used)).toBe(used);
  });
});
