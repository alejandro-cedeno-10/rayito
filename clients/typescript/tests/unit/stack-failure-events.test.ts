/**
 * `firstFailedResource` / `describeFailedResource` (dominio puro) contra los
 * vectores compartidos con Python (`testdata/stacks/failure-events.json`): el
 * recurso más antiguo que falló en la última operación, nunca uno de un
 * despliegue anterior.
 */

import { describe, expect, it } from "vitest";
import {
  describeFailedResource,
  firstFailedResource,
  type StackEvent,
} from "../../src/stacks/model.js";
import { loadTestdata } from "./cli/paths.js";

interface RawEvent {
  readonly logicalResourceId: string | null;
  readonly resourceStatus: string | null;
  readonly resourceStatusReason: string | null;
}

interface FailureCase {
  readonly name: string;
  readonly stackName: string;
  readonly events: RawEvent[];
  readonly expected: RawEvent | null;
  readonly expectedText: string | null;
}

function eventOf(raw: RawEvent): StackEvent {
  return {
    logicalResourceId: raw.logicalResourceId ?? undefined,
    resourceStatus: raw.resourceStatus ?? undefined,
    resourceStatusReason: raw.resourceStatusReason ?? undefined,
  };
}

const { cases } = loadTestdata<{ cases: FailureCase[] }>("stacks", "failure-events.json");

describe("firstFailedResource: vectores compartidos", () => {
  it.each(cases)("$name", (testCase) => {
    const found = firstFailedResource(testCase.events.map(eventOf), testCase.stackName);
    if (testCase.expected === null) {
      expect(found).toBeUndefined();
      expect(testCase.expectedText).toBeNull();
      return;
    }
    expect(found).toEqual(eventOf(testCase.expected));
    expect(describeFailedResource(found as StackEvent)).toBe(testCase.expectedText);
  });
});
