/**
 * `planFeatures`' `events` branch, espejo del test Python: la opción debe
 * ser un `LifecycleEvents` y `logging` debe llegar a CloudWatch (validado
 * con el mismo resolver que usa `run-microvm`); con ambos bien sigue
 * lanzando `UnimplementedError` nombrando el envío de `ConfigureSandbox`
 * que falta, antes de cualquier llamada a AWS.
 */

import { describe, expect, it } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { EVENTS_CHANGE, planFeatures } from "../../src/feature-options.js";
import { LifecycleEvents } from "../../src/lifecycle-events/service.js";

describe("planFeatures events", () => {
  it("rejects anything that is not a LifecycleEvents", () => {
    expect(() => planFeatures({ events: {} }, undefined, "cloudwatch")).toThrow(
      InvalidArgumentError,
    );
  });

  it.each([undefined, "disabled", { disabled: {} }])(
    "needs logging that reaches CloudWatch (%o)",
    (logging) => {
      expect(() => planFeatures({ events: new LifecycleEvents() }, undefined, logging)).toThrow(
        /cloudwatch/,
      );
    },
  );

  it.each(["cloudwatch", { cloudWatch: { logGroup: "/custom/group" } }])(
    "is still unimplemented with a valid option, naming what is missing (%o)",
    (logging) => {
      let raised: unknown;
      try {
        planFeatures({ events: new LifecycleEvents() }, undefined, logging);
      } catch (error) {
        raised = error;
      }
      expect(raised).toBeInstanceOf(UnimplementedError);
      expect(String((raised as Error).message)).toContain(EVENTS_CHANGE);
      expect(String((raised as Error).message)).toContain("ConfigureSandbox");
    },
  );

  it("ignores logging without events", () => {
    expect(() => planFeatures({}, undefined, "disabled")).not.toThrow();
  });
});
