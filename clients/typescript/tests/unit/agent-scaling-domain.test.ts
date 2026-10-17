/**
 * Lógica pura del escalado de agentes: los vectores compartidos con Python
 * (`testdata/agent-fleet/routing.json`) y las invariantes de `ScalingOptions`,
 * `ServeOptions`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { ServeOptions } from "../../src/agent/project.js";
import {
  decodeHandle,
  encodeHandle,
  isUnhealthy,
  maxSessionsForMemory,
  replacementsNeeded,
  ScalingOptions,
  validateCallerSessionId,
} from "../../src/agent/scaling-domain.js";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  AGENT_FLEET_UNHEALTHY_PINGS,
  AGENT_SCALING_MAX_VMS_DEFAULT,
  AGENT_SCALING_MIN_VMS_DEFAULT,
  AGENT_SCALING_WARM_VMS_DEFAULT,
  POOL_SIZE_MAX,
} from "../../src/limits.js";

interface Vectors {
  readonly memory: ReadonlyArray<{ memory_mib: number; max_sessions: number }>;
  readonly handles: {
    readonly encode: ReadonlyArray<{ sandbox_id: string; session_id: string; handle: string }>;
    readonly decode: ReadonlyArray<{
      value: string;
      handle: { sandbox_id: string; session_id: string } | null;
    }>;
    readonly encode_invalid: ReadonlyArray<{ sandbox_id: string; session_id: string }>;
    readonly caller_valid: readonly string[];
    readonly caller_invalid: readonly string[];
  };
  readonly health: {
    readonly threshold_offsets: ReadonlyArray<{ offset: number; unhealthy: boolean }>;
    readonly replacements: ReadonlyArray<{
      healthy_running: number;
      min_vms: number;
      needed: number;
    }>;
  };
}

const vectors = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-fleet", "routing.json"),
    "utf8",
  ),
) as Vectors;

describe("session cap", () => {
  test.each(vectors.memory)("follows the connection cap of the VM: $memory_mib MiB", (entry) => {
    expect(maxSessionsForMemory(entry.memory_mib)).toBe(entry.max_sessions);
  });

  test.each([0, -1, 2.5, Number.NaN])("rejects the memory %s", (value) => {
    expect(() => maxSessionsForMemory(value)).toThrow(InvalidArgumentError);
  });
});

describe("session handles", () => {
  test.each(vectors.handles.encode)("encodes $handle", (entry) => {
    const handle = encodeHandle(entry.sandbox_id, entry.session_id);
    expect(handle).toBe(entry.handle);
    expect(decodeHandle(handle)).toEqual({
      sandboxId: entry.sandbox_id,
      sessionId: entry.session_id,
    });
  });

  test.each(vectors.handles.decode)("decodes $value", (entry) => {
    const expected =
      entry.handle === null
        ? undefined
        : { sandboxId: entry.handle.sandbox_id, sessionId: entry.handle.session_id };
    expect(decodeHandle(entry.value)).toEqual(expected);
  });

  test.each(vectors.handles.encode_invalid)("cannot encode an unroutable handle", (entry) => {
    expect(() => encodeHandle(entry.sandbox_id, entry.session_id)).toThrow(InvalidArgumentError);
  });

  test.each(vectors.handles.caller_valid)("accepts the caller id %s", (value) => {
    expect(validateCallerSessionId(value)).toBe(value);
  });

  test.each(vectors.handles.caller_invalid)("rejects the caller id %j", (value) => {
    expect(() => validateCallerSessionId(value)).toThrow(InvalidArgumentError);
  });
});

describe("health", () => {
  test.each(vectors.health.threshold_offsets)("ping threshold offset %j", (entry) => {
    expect(isUnhealthy(AGENT_FLEET_UNHEALTHY_PINGS + entry.offset)).toBe(entry.unhealthy);
  });

  test.each(vectors.health.replacements)("replacements for %j", (entry) => {
    const options = new ScalingOptions({ minVms: entry.min_vms, maxVms: entry.min_vms });
    expect(replacementsNeeded(entry.healthy_running, options)).toBe(entry.needed);
  });
});

describe("ScalingOptions", () => {
  test("defaults to a single VM", () => {
    const options = new ScalingOptions();
    expect([options.minVms, options.maxVms, options.warmVms]).toEqual([
      AGENT_SCALING_MIN_VMS_DEFAULT,
      AGENT_SCALING_MAX_VMS_DEFAULT,
      AGENT_SCALING_WARM_VMS_DEFAULT,
    ]);
    expect(options.sessions).toBe("shared");
    expect(options.routesByHandle).toBe(false);
  });

  test("routes by handle with more than one VM", () => {
    expect(new ScalingOptions({ maxVms: 2 }).routesByHandle).toBe(true);
  });

  test.each([
    { minVms: -1 },
    { minVms: 2, maxVms: 1 },
    { maxVms: 0 },
    { maxVms: POOL_SIZE_MAX + 1 },
    { minVms: 1, maxVms: 2, warmVms: 2 },
    { sessions: "exclusive" },
    { scaleInAfterMs: 1000 },
    { maxVms: 2.5 },
  ])("validates %j", (input) => {
    expect(() => new ScalingOptions(input as never)).toThrow(InvalidArgumentError);
  });
});

describe("ServeOptions queue", () => {
  test("defaults keep the zero-queue behaviour", () => {
    expect(new ServeOptions().queueMax).toBe(0);
  });

  test.each([{ queueMax: -1 }, { queueMax: 1.5 }, { queueTimeoutMs: 0 }])(
    "validates %j",
    (input) => {
      expect(() => new ServeOptions(input)).toThrow(InvalidArgumentError);
    },
  );
});
