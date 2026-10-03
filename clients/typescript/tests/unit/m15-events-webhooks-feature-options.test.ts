/**
 * `planFeatures`' `events` branch, espejo del test Python: la opción debe
 * ser un `LifecycleEvents` y `logging` debe llegar a CloudWatch (validado
 * con el mismo resolver que usa `run-microvm`), ambos antes de cualquier
 * llamada a AWS. Con ambos bien la opción queda planeada
 * (`FeaturePlan.events`) y se envía tras `run-microvm`: ver
 * `m15-events-create-wiring.test.ts`.
 */

import { describe, expect, it } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { planFeatures } from "../../src/feature-options.js";
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
    "plans a valid option as FeaturePlan.events (%o)",
    (logging) => {
      const events = new LifecycleEvents();
      expect(planFeatures({ events }, undefined, logging).events).toBe(events);
    },
  );

  it("ignores logging without events", () => {
    expect(() => planFeatures({}, undefined, "disabled")).not.toThrow();
  });
});
