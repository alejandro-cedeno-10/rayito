/**
 * `index` en el SDK TypeScript (M14) contra el plano de control falso del
 * pool (que arranca un `rayd` falso por VM) y un DynamoDB falso: `create()`
 * escribe una fila condicional, un `PutItem` que falla termina el VM (o sólo
 * avisa), `list()`/`paginate()` con `metadata` e índice devuelven sandboxes
 * `SUSPENDED` sin ningún `get-microvm`, token ni `Health`, el pool escribe al
 * rellenar y el shim `rayito/e2b` lo usa con `query.state: ["paused"]`. Sin
 * `index` no se carga `@aws-sdk/client-dynamodb`. Espejo de
 * `test_index_sync.py`, `test_index_pool.py` y `test_index_e2b.py`.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { E2B, Sandbox as E2BSandbox, UnimplementedError } from "../../src/e2b/index.js";
import {
  DynamoDbIndex,
  IndexWriteError,
  InvalidArgumentError,
  Sandbox,
  SandboxIndexError,
  SandboxPool,
} from "../../src/index.js";
import * as optional from "../../src/optional.js";
import { IMAGE_ARN } from "./fake/control-plane.js";
import { FakeClock, FakePoolControlPlane } from "./fake/pool-plane.js";
import { RecordingLogger, waitUntil } from "./helpers.js";
import { fakeIndex, TABLE } from "./index-fake.js";

const PROBES = ["GetMicrovm", "CreateMicrovmAuthToken"];
const TRANSPORT = { scheme: "http" as const, pingIdleConnection: false };

const planes: FakePoolControlPlane[] = [];

afterEach(async () => {
  vi.restoreAllMocks();
  for (const plane of planes.splice(0)) {
    plane.releaseAll();
    await plane.close();
  }
});

function newPlane(): FakePoolControlPlane {
  const plane = new FakePoolControlPlane({ clock: new FakeClock() });
  planes.push(plane);
  return plane;
}

async function create(
  plane: FakePoolControlPlane,
  options: Parameters<typeof Sandbox.create>[0] = {},
): Promise<Sandbox> {
  const sandbox = await Sandbox.create({
    template: IMAGE_ARN,
    idle: null,
    controlPlane: plane,
    transport: TRANSPORT,
    readyTimeoutMs: 10_000,
    ...options,
  });
  sandbox.close();
  return sandbox;
}

function probesSince(plane: FakePoolControlPlane, since: number): string[] {
  return plane.calls
    .slice(since)
    .map((call) => call.operation)
    .filter((operation) => PROBES.includes(operation));
}

async function collect<T>(items: AsyncIterable<T>): Promise<T[]> {
  const all: T[] = [];
  for await (const item of items) {
    all.push(item);
  }
  return all;
}

describe("create({ index })", () => {
  test("writes one conditional row without the access token", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    const sandbox = await create(plane, { metadata: { user: "42" }, index });
    const [put] = api.calls("putItem");
    expect(put?.ConditionExpression).toBe("attribute_not_exists(pk)");
    expect(put?.Item.pk).toEqual({ S: sandbox.sandboxId });
    expect(put?.Item.metadata).toEqual({ M: { user: { S: "42" } } });
    expect(JSON.stringify(put)).not.toContain(sandbox.accessToken);
  });

  test("a failed put terminates the VM before minting a token and raises", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    api.putError = "AccessDeniedException";
    await expect(create(plane, { index })).rejects.toBeInstanceOf(IndexWriteError);
    expect(plane.callsTo("TerminateMicrovm")).toHaveLength(1);
    expect(plane.callsTo("CreateMicrovmAuthToken")).toHaveLength(0);
    expect(plane.liveIds).toEqual([]);
  });

  test("keepOnFailure keeps the VM but still raises", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    api.putError = "ResourceNotFoundException";
    await expect(create(plane, { index, keepOnFailure: true })).rejects.toBeInstanceOf(
      IndexWriteError,
    );
    expect(plane.callsTo("TerminateMicrovm")).toHaveLength(0);
    expect(plane.liveIds).toHaveLength(1);
  });

  test("onWriteFailure: warn logs without metadata and returns the sandbox", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex({ onWriteFailure: "warn" });
    api.putError = "AccessDeniedException";
    const logger = new RecordingLogger();
    await create(plane, { index, logger, metadata: { user: "value-never-logged" } });
    expect(logger.at("warn").some((line) => line.message.includes("índice"))).toBe(true);
    expect(logger.dump()).not.toContain("value-never-logged");
    expect(plane.callsTo("TerminateMicrovm")).toHaveLength(0);
  });

  test("create({ pool, index }) is rejected", async () => {
    const { index } = fakeIndex();
    const pool = Object.create(SandboxPool.prototype) as SandboxPool;
    await expect(Sandbox.create({ pool, index })).rejects.toThrow(/PoolConfig\.index/);
    await expect(Sandbox.create({ pool, index })).rejects.toBeInstanceOf(InvalidArgumentError);
  });

  test("a missing DynamoDB peer fails before run-microvm", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockRejectedValue(
      new InvalidArgumentError("npm install @aws-sdk/client-dynamodb"),
    );
    const plane = newPlane();
    const index = new DynamoDbIndex({ tableName: TABLE, region: "eu-west-1" });
    await expect(create(plane, { metadata: { user: "42" }, index })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(plane.calls.map((call) => call.operation)).not.toContain("RunMicrovm");
  });

  test("an index without a region fails before run-microvm", async () => {
    vi.stubEnv("AWS_REGION", "");
    vi.stubEnv("AWS_DEFAULT_REGION", "");
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    const plane = newPlane();
    const index = new DynamoDbIndex({ tableName: TABLE });
    try {
      await expect(create(plane, { metadata: { user: "42" }, index })).rejects.toThrow(
        /región del índice/,
      );
    } finally {
      vi.unstubAllEnvs();
    }
    expect(plane.calls.map((call) => call.operation)).not.toContain("RunMicrovm");
    expect(loader).not.toHaveBeenCalled();
  });

  test("without index the DynamoDB peer is never loaded", async () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    const plane = newPlane();
    const sandbox = await create(plane, { metadata: { user: "42" } });
    await collect(Sandbox.list({ controlPlane: plane }));
    await sandbox.kill();
    expect(loader.mock.calls.map(([specifier]) => specifier)).not.toContain(
      "@aws-sdk/client-dynamodb",
    );
  });
});

describe("list/paginate with index", () => {
  async function threeSuspended() {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    const created = [];
    for (let n = 0; n < 3; n += 1) {
      created.push(await create(plane, { metadata: { user: "42" }, index }));
    }
    const other = await create(plane, { metadata: { user: "7" }, index });
    for (const sandbox of [...created.slice(0, 2), other]) {
      plane.setState(sandbox.sandboxId, "SUSPENDED");
    }
    return { plane, index, api, created, other };
  }

  test("returns the suspended matches without probing", async () => {
    const { plane, index, api, created, other } = await threeSuspended();
    const since = plane.calls.length;
    const found = await collect(
      Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index, controlPlane: plane }),
    );
    expect(found.map((item) => item.sandboxId).sort()).toEqual(
      created
        .slice(0, 2)
        .map((s) => s.sandboxId)
        .sort(),
    );
    expect(new Set(found.map((item) => item.state))).toEqual(new Set(["SUSPENDED"]));
    expect(found.every((item) => item.metadata?.user === "42")).toBe(true);
    expect(probesSince(plane, since)).toEqual([]);
    const [batch] = api.calls("batchGetItem");
    expect(new Set(batch?.RequestItems[TABLE]?.Keys.map((key) => key.pk.S))).toEqual(
      new Set([created[0]?.sandboxId, created[1]?.sandboxId, other.sandboxId]),
    );
  });

  test("sandboxes created without the index are excluded", async () => {
    const plane = newPlane();
    const { index } = fakeIndex();
    const indexed = await create(plane, { metadata: { user: "42" }, index });
    await create(plane, { metadata: { user: "42" } });
    const found = await collect(
      Sandbox.list({ metadata: { user: "42" }, index, controlPlane: plane }),
    );
    expect(found.map((item) => item.sandboxId)).toEqual([indexed.sandboxId]);
  });

  test("index without metadata never touches DynamoDB", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    await create(plane);
    expect(await collect(Sandbox.list({ index, controlPlane: plane }))).toHaveLength(1);
    expect(api.requests).toEqual([]);
  });

  test("paginate resumes from its token, bound to the table", async () => {
    const { plane, index, created } = await threeSuspended();
    const first = Sandbox.paginate({
      metadata: { user: "42" },
      index,
      limit: 2,
      controlPlane: plane,
    });
    const page = (await first.nextItems()).map((item) => item.sandboxId);
    expect(first.hasNext).toBe(true);
    const second = Sandbox.paginate({
      metadata: { user: "42" },
      index,
      limit: 2,
      nextToken: first.nextToken,
      controlPlane: plane,
    });
    const rest = (await second.nextItems()).map((item) => item.sandboxId);
    expect([...page, ...rest].sort()).toEqual(created.map((s) => s.sandboxId).sort());
    await expect(
      Sandbox.paginate({
        metadata: { user: "42" },
        limit: 2,
        nextToken: first.nextToken,
        controlPlane: plane,
      }).nextItems(),
    ).rejects.toThrow(/next_token/);
  });

  test("read failures surface instead of a partial list", async () => {
    const { plane, index, api } = await threeSuspended();
    api.batchError = "ResourceNotFoundException";
    await expect(
      collect(Sandbox.list({ metadata: { user: "42" }, index, controlPlane: plane })),
    ).rejects.toBeInstanceOf(SandboxIndexError);
  });

  test("kill never touches the index", async () => {
    const plane = newPlane();
    const { index, api } = fakeIndex();
    const sandbox = await create(plane, { metadata: { user: "42" }, index });
    await sandbox.kill();
    expect(api.requests.map((request) => request.operation)).toEqual(["putItem"]);
  });
});

describe("PoolConfig.index", () => {
  test("the pool writes each slot row and parked slots are listable", async () => {
    const clock = new FakeClock();
    const plane = new FakePoolControlPlane({ clock });
    planes.push(plane);
    const { index, api } = fakeIndex();
    const pool = new SandboxPool(
      {
        size: 2,
        template: IMAGE_ARN,
        timeoutMs: 7_200_000,
        minRemainingMs: 3_600_000,
        readyTimeoutMs: 10_000,
        metadata: { pool: "a" },
        index,
      },
      {
        controlPlane: plane,
        transport: TRANSPORT,
        monotonic: () => clock.seconds() * 1000,
        sleep: async () => undefined,
        random: () => 0.5,
      },
    );
    try {
      await pool.start();
      await waitUntil(() => pool.stats().ready === 2, 15_000, "el pool no se llenó");
      const puts = api.calls("putItem");
      expect(puts).toHaveLength(2);
      expect(
        puts.every((put) => JSON.stringify(put.Item.metadata) === '{"M":{"pool":{"S":"a"}}}'),
      ).toBe(true);
      const since = plane.calls.length;
      const found = await collect(
        Sandbox.list({
          metadata: { pool: "a" },
          states: ["SUSPENDED"],
          index,
          controlPlane: plane,
        }),
      );
      expect(found.map((item) => item.sandboxId).sort()).toEqual(
        puts.map((put) => (put.Item.pk as { S: string }).S).sort(),
      );
      expect(probesSince(plane, since)).toEqual([]);
    } finally {
      await pool.close();
    }
  });

  test("PoolConfig.index must be a DynamoDbIndex", () => {
    expect(
      () => new SandboxPool({ size: 1, index: "tabla" as never }, { controlPlane: newPlane() }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("rayito/e2b with index", () => {
  async function pausedRig() {
    const plane = newPlane();
    const { index } = fakeIndex();
    const created = [];
    for (let n = 0; n < 3; n += 1) {
      created.push(await create(plane, { metadata: { user: "42" }, index }));
    }
    for (const sandbox of created.slice(0, 2)) {
      plane.setState(sandbox.sandboxId, "SUSPENDED");
    }
    return {
      plane,
      index,
      paused: created
        .slice(0, 2)
        .map((s) => s.sandboxId)
        .sort(),
    };
  }

  test("query.state paused with index returns paused sandboxes without probing", async () => {
    const { plane, index, paused } = await pausedRig();
    const since = plane.calls.length;
    const items = await E2BSandbox.list({
      query: { metadata: { user: "42" }, state: ["paused"] },
      index,
      controlPlane: plane,
    }).nextItems();
    expect(items.map((item) => item.sandboxId).sort()).toEqual(paused);
    expect(new Set(items.map((item) => item.state))).toEqual(new Set(["paused"]));
    expect(probesSince(plane, since)).toEqual([]);
  });

  test("new E2B({ index }) binds the index for list", async () => {
    const { plane, index, paused } = await pausedRig();
    const client = new E2B({ controlPlane: plane, index });
    const items = await client.Sandbox.list({
      query: { metadata: { user: "42" }, state: ["paused"] },
    }).nextItems();
    expect(items.map((item) => item.sandboxId).sort()).toEqual(paused);
  });

  test("without index the paused query names the option", () => {
    expect(() =>
      E2BSandbox.list({ query: { metadata: { user: "42" }, state: ["paused"] } }),
    ).toThrow(UnimplementedError);
    try {
      E2BSandbox.list({ query: { metadata: { user: "42" }, state: ["paused"] } });
    } catch (error) {
      expect((error as Error).message).toContain("index: new DynamoDbIndex");
      expect((error as Error).message).toContain("optional-features");
    }
  });
});
