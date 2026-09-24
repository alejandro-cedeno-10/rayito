/**
 * La validación pura de `retries` e `integration` que comparten el plano de
 * control y `rayito/e2b`, probada sin construir ningún cliente de AWS.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { validateIntegration, validateRetries } from "../../src/validation.js";

describe("client settings validation", () => {
  test("retries is an integer >= 0 or absent", () => {
    expect(validateRetries(undefined)).toBeUndefined();
    expect(validateRetries(0)).toBe(0);
    expect(validateRetries(3)).toBe(3);
    for (const retries of [-1, 1.5, true, "2", null]) {
      expect(() => validateRetries(retries)).toThrow(InvalidArgumentError);
    }
  });

  test("integration is printable ASCII without spaces or absent", () => {
    expect(validateIntegration(undefined)).toBeUndefined();
    expect(validateIntegration("acme/1.0")).toBe("acme/1.0");
    for (const integration of ["", "acme 1", "ñandú", 7]) {
      expect(() => validateIntegration(integration)).toThrow(InvalidArgumentError);
    }
  });
});
