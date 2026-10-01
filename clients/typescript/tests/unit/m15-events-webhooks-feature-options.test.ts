/**
 * `planFeatures`'s `events` branch (m15-events-webhooks): validated before
 * any AWS call, requires `logging: "cloudwatch"`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { planFeatures } from "../../src/feature-options.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";

const TEMPLATE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";

describe("planFeatures events branch", () => {
  test("events without cloudwatch logging is invalid argument", () => {
    expect(() => planFeatures({ events: {} }, undefined, undefined)).toThrow(InvalidArgumentError);
  });

  test("events with disabled logging is also invalid argument", () => {
    expect(() => planFeatures({ events: {} }, undefined, "disabled")).toThrow(InvalidArgumentError);
  });

  test("events with cloudwatch logging does not throw", () => {
    expect(() => planFeatures({ events: {} }, undefined, "cloudwatch")).not.toThrow();
  });

  test("no events option ignores logging entirely", () => {
    expect(() => planFeatures({}, undefined, undefined)).not.toThrow();
  });

  test("other branches still raise unimplemented", () => {
    expect(() => planFeatures({ mounts: {} }, undefined, "cloudwatch")).toThrow(UnimplementedError);
  });

  test("Sandbox.create rejects events without cloudwatch logging before any control plane", async () => {
    await expect(Sandbox.create({ template: TEMPLATE, events: {} })).rejects.toThrow(
      InvalidArgumentError,
    );
  });
});
