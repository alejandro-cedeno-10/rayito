/**
 * Lógica pura del escalado de agentes: los vectores compartidos con Python
 * (`testdata/agent-fleet/routing.json`) y las invariantes de `ScalingOptions`,
 * `ServeOptions` y `DeployOptions`. Los vectores hablan en segundos; aquí se
 * convierten a milisegundos.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { BASE_IMAGE_VERSION_LATEST, DeployOptions } from "../../src/agent/deploy-domain.js";
import { ServeOptions } from "../../src/agent/project.js";
import {
  decideQueue,
  decodeHandle,
  encodeHandle,
  expiredVms,
  isUnhealthy,
  maxSessionsForMemory,
  placeSession,
  replacementsNeeded,
  ScalingOptions,
  scaleInDeadlines,
  type VmView,
  validateCallerSessionId,
} from "../../src/agent/scaling-domain.js";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  AGENT_FLEET_UNHEALTHY_PINGS,
  AGENT_SCALING_MAX_VMS_DEFAULT,
  AGENT_SCALING_MIN_VMS_DEFAULT,
  AGENT_SCALING_WARM_VMS_DEFAULT,
  AGENT_SERVICE_QUEUE_MAX,
  POOL_SIZE_MAX,
} from "../../src/limits.js";

const MS_PER_SECOND = 1000;

interface ScalingFixture {
  readonly min_vms: number;
  readonly max_vms: number;
  readonly warm_vms: number;
  readonly sessions: "shared" | "dedicated";
  readonly scale_in_after_seconds: number;
}

interface VmFixture {
  readonly sandbox_id: string;
  readonly active: number;
  readonly served: number;
  readonly healthy: boolean;
  readonly busy: boolean;
  readonly warm: boolean;
}

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
  readonly placement: ReadonlyArray<{
    case: string;
    scaling: ScalingFixture;
    max_concurrency: number;
    session_id: string | null;
    vms: VmFixture[];
    expect: { action: string; sandbox_id: string | null };
  }>;
  readonly queue: ReadonlyArray<{
    case: string;
    waiting: number;
    elapsed_seconds: number;
    queue_max: number;
    queue_timeout_seconds: number;
    retry_after_seconds: number;
    expect: { action: string; wait_seconds: number };
  }>;
  readonly scale_in: ReadonlyArray<{
    case: string;
    scaling: ScalingFixture;
    usages: Array<{ sandbox_id: string; created_at: number; last_used_at: number }>;
    now: number;
    deadlines: Record<string, number>;
    expired: string[];
  }>;
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

function scaling(fixture: ScalingFixture): ScalingOptions {
  return new ScalingOptions({
    minVms: fixture.min_vms,
    maxVms: fixture.max_vms,
    warmVms: fixture.warm_vms,
    sessions: fixture.sessions,
    scaleInAfterMs: fixture.scale_in_after_seconds * MS_PER_SECOND,
  });
}

function view(fixture: VmFixture): VmView {
  return {
    sandboxId: fixture.sandbox_id,
    active: fixture.active,
    served: fixture.served,
    healthy: fixture.healthy,
    busy: fixture.busy,
    warm: fixture.warm,
  };
}

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

describe("placement", () => {
  test.each(vectors.placement)("$case", (entry) => {
    const placement = placeSession(
      entry.session_id ?? undefined,
      entry.vms.map(view),
      scaling(entry.scaling),
      { maxConcurrency: entry.max_concurrency },
    );
    expect({ action: placement.action, sandbox_id: placement.sandboxId ?? null }).toEqual(
      entry.expect,
    );
  });
});

describe("queue", () => {
  test.each(vectors.queue)("$case", (entry) => {
    const decision = decideQueue({
      waiting: entry.waiting,
      elapsedMs: entry.elapsed_seconds * MS_PER_SECOND,
      queueMax: entry.queue_max,
      queueTimeoutMs: entry.queue_timeout_seconds * MS_PER_SECOND,
      retryAfterMs: entry.retry_after_seconds * MS_PER_SECOND,
    });
    expect({ action: decision.action, wait_seconds: decision.waitMs / MS_PER_SECOND }).toEqual(
      entry.expect,
    );
  });

  test("checks the queue bound", () => {
    expect(() =>
      decideQueue({
        waiting: 0,
        elapsedMs: 0,
        queueMax: AGENT_SERVICE_QUEUE_MAX + 1,
        queueTimeoutMs: 30_000,
        retryAfterMs: 1000,
      }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("scale in", () => {
  test.each(vectors.scale_in)("$case", (entry) => {
    const deadlines = scaleInDeadlines(
      entry.usages.map((usage) => ({
        sandboxId: usage.sandbox_id,
        createdAtMs: usage.created_at * MS_PER_SECOND,
        lastUsedAtMs: usage.last_used_at * MS_PER_SECOND,
      })),
      scaling(entry.scaling),
    );
    expect(
      Object.fromEntries(Object.entries(deadlines).map(([id, ms]) => [id, ms / MS_PER_SECOND])),
    ).toEqual(entry.deadlines);
    expect(expiredVms(deadlines, entry.now * MS_PER_SECOND)).toEqual(entry.expired);
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

describe("DeployOptions", () => {
  test("defaults to the latest base and asks for nothing extra", () => {
    const options = new DeployOptions();
    expect(options.baseImageVersion).toBe(BASE_IMAGE_VERSION_LATEST);
    expect([options.createSecretFromEnv, options.dryRun, options.ensureBaseImage]).toEqual([
      false,
      false,
      true,
    ]);
  });

  test("keeps the access token out of enumeration", () => {
    const token = "t".repeat(32);
    const options = new DeployOptions({ accessToken: token });
    expect(options.accessToken).toBe(token);
    expect(JSON.stringify(options)).not.toContain(token);
    expect({ ...options }).not.toHaveProperty("accessToken");
  });

  test.each([
    { bucket: "no" },
    { bucket: "has space" },
    { bucket: "a/b/c" },
    { baseImageVersion: "" },
    { secretNames: { OpenAI: "name" } },
    { secretNames: { openai: "has space" } },
    { dryRun: "yes" },
    { accessToken: "short" },
  ])("validates %j", (input) => {
    expect(() => new DeployOptions(input as never)).toThrow(InvalidArgumentError);
  });
});
