/**
 * `Sandbox.paginate` y `Sandbox.list` sobre páginas guionizadas (design
 * D8–D10, la paridad con `test_listing_sync.py`): `limit`, reanudar con
 * `nextToken`, `order`, filtros de cliente, el filtro de metadatos contra un
 * `rayd` falso por sandbox con sus transportes cerrados, y el fin del
 * paginador.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import { afterEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError, SandboxError, SandboxNotFoundError } from "../../src/errors.js";
import { type SandboxListItem, sandboxListItem } from "../../src/models.js";
import type { ListOrder } from "../../src/sandbox/listing.js";
import type { SandboxListPaginator } from "../../src/sandbox/paginator.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { deadlineFromHeaders } from "./fake/common.js";
import { FakeControlPlane, IMAGE_ARN, listedItem, STARTED_AT } from "./fake/control-plane.js";
import { type FakeMicrovm, FakePoolControlPlane } from "./fake/pool-plane.js";
import type { FakeRayd } from "./fake/server.js";
import { sleep } from "./helpers.js";

const OTHER_IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other";
const GOLDEN_IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const GOLDEN_SECOND_ID = "microvm-00000000-0000-0000-0000-000000000002";
const GOLDEN_KEY_TOKEN =
  "eyJmIjoiMWYxYjEyM2IxYjc0NGMzYSIsImsiOlsxNzkwMDAwMDAwMDAwLCJtaWNyb3ZtLTAwMDAwMDAwLTAwMDAtMDAw" +
  "MC0wMDAwLTAwMDAwMDAwMDAwMiJdLCJ2IjoxfQ";
const LOOPBACK_TRANSPORT = { scheme: "http", pingIdleConnection: false } as const;

function plane(): FakeControlPlane {
  return new FakeControlPlane({ endpoint: "127.0.0.1" });
}

async function walk(paginator: SandboxListPaginator): Promise<string[][]> {
  const pages: string[][] = [];
  while (paginator.hasNext) {
    pages.push((await paginator.nextItems()).map((entry) => entry.sandboxId));
  }
  return pages;
}

async function collect(listing: AsyncIterable<SandboxListItem>): Promise<string[]> {
  const ids: string[] = [];
  for await (const entry of listing) {
    ids.push(entry.sandboxId);
  }
  return ids;
}

function ids(items: readonly SandboxListItem[]): string[] {
  return items.map((entry) => entry.sandboxId);
}

describe("Sandbox.paginate over scripted pages", () => {
  test("limit 1 walks every item exactly once", async () => {
    const fake = plane();
    fake.scriptPages([
      listedItem("a"),
      listedItem("b"),
      listedItem("c"),
      listedItem("dead", "TERMINATED"),
    ]);
    const paginator = Sandbox.paginate({ limit: 1, controlPlane: fake });
    expect(paginator.hasNext).toBe(true);
    expect(paginator.nextToken).toBeUndefined();
    const pages = await walk(paginator);
    expect(pages.flat()).toEqual(["a", "b", "c"]);
    expect(pages.every((page) => page.length <= 1)).toBe(true);
    expect(paginator.hasNext).toBe(false);
    expect(paginator.nextToken).toBeUndefined();
    expect(new Set(fake.pageRequests.map((request) => request.maxResults))).toEqual(new Set([50]));
  });

  test("a fresh paginator resumes by identity on the cursor's page", async () => {
    const fake = plane();
    fake.scriptPages([
      listedItem("a"),
      listedItem("b"),
      listedItem("c"),
      listedItem("dead", "TERMINATED"),
    ]);
    const first = Sandbox.paginate({ limit: 1, controlPlane: fake });
    expect(ids(await first.nextItems())).toEqual(["a"]);
    const token = first.nextToken as string;
    expect(token).toBeTypeOf("string");
    expect(first.hasNext).toBe(true);

    fake.scriptPages([
      listedItem("new"),
      listedItem("a"),
      listedItem("b"),
      listedItem("c"),
      listedItem("dead", "TERMINATED"),
    ]);
    fake.pageRequests.length = 0;
    const resumed = Sandbox.paginate({ limit: 1, nextToken: token, controlPlane: fake });
    expect(resumed.nextToken).toBe(token);
    expect((await walk(resumed)).flat()).toEqual(["new", "b", "c"]);
    expect(fake.pageRequests[0]?.nextToken).toBeUndefined();
  });

  test("resuming after a whole page continues on the next AWS page", async () => {
    const fake = plane();
    fake.scriptPages([listedItem("a"), listedItem("b")], [listedItem("c")]);
    const first = Sandbox.paginate({ limit: 2, controlPlane: fake });
    expect(ids(await first.nextItems())).toEqual(["a", "b"]);
    expect(first.hasNext).toBe(true);
    fake.pageRequests.length = 0;
    const resumed = Sandbox.paginate({ nextToken: first.nextToken, controlPlane: fake });
    expect(ids(await resumed.nextItems())).toEqual(["c"]);
    expect(fake.pageRequests.map((request) => request.nextToken)).toEqual([undefined, "page-1"]);
    expect(resumed.hasNext).toBe(false);
  });

  test("order sorts by startedAt and resumes with a key cursor", async () => {
    const fake = plane();
    fake.scriptPages(
      [listedItem("thirty", "RUNNING", 30), listedItem("ten", "RUNNING", 10)],
      [listedItem("twenty", "RUNNING", 20)],
    );
    const ascending = Sandbox.paginate({ order: "asc", limit: 2, controlPlane: fake });
    expect(ids(await ascending.nextItems())).toEqual(["ten", "twenty"]);
    expect(ascending.hasNext).toBe(true);
    const resumed = Sandbox.paginate({
      order: "asc",
      limit: 2,
      nextToken: ascending.nextToken,
      controlPlane: fake,
    });
    expect(ids(await resumed.nextItems())).toEqual(["thirty"]);
    expect(resumed.hasNext).toBe(false);
    expect(resumed.nextToken).toBeUndefined();
    const descending = Sandbox.paginate({ order: "desc", controlPlane: fake });
    expect(ids(await descending.nextItems())).toEqual(["thirty", "twenty", "ten"]);
  });

  test("an ordered token is byte-identical to the Python golden vector", async () => {
    const fake = plane();
    const golden = (sandboxId: string, startedAtMs: number) =>
      sandboxListItem({
        sandboxId,
        state: "RUNNING",
        template: GOLDEN_IMAGE,
        templateVersion: "1",
        startedAt: new Date(startedAtMs),
      });
    fake.scriptPages([
      golden("older", 1_789_999_999_000),
      golden(GOLDEN_SECOND_ID, 1_790_000_000_000),
    ]);
    const paginator = Sandbox.paginate({
      template: GOLDEN_IMAGE,
      order: "desc",
      limit: 1,
      controlPlane: fake,
    });
    expect(ids(await paginator.nextItems())).toEqual([GOLDEN_SECOND_ID]);
    expect(paginator.nextToken).toBe(GOLDEN_KEY_TOKEN);
  });

  test("states and startedAfter filter on the client", async () => {
    const fake = plane();
    fake.scriptPages([
      listedItem("thirty", "RUNNING", 30),
      listedItem("ten", "SUSPENDED", 10),
      listedItem("twenty", "RUNNING", 20),
    ]);
    const suspended = Sandbox.paginate({ states: ["SUSPENDED"], controlPlane: fake });
    expect(ids(await suspended.nextItems())).toEqual(["ten"]);
    const recent = Sandbox.paginate({
      startedAfter: new Date(STARTED_AT.getTime() + 15_000),
      controlPlane: fake,
    });
    expect(ids(await recent.nextItems())).toEqual(["thirty", "twenty"]);
  });

  test("template travels as the server-side image filter", async () => {
    const fake = plane();
    fake.scriptPages([listedItem("a"), listedItem("x", "RUNNING", 0, OTHER_IMAGE_ARN)]);
    const items = await Sandbox.paginate({
      template: "rayito-base-2gb",
      templateVersion: "1.0",
      controlPlane: fake,
    }).nextItems();
    expect(ids(items)).toEqual(["a"]);
    expect(fake.pageRequests[0]).toMatchObject({ imageArn: IMAGE_ARN, imageVersion: "1.0" });
    expect(fake.callsTo("resolveTemplateArn")).toHaveLength(1);
  });

  test("nextItems past the end raises SandboxError", async () => {
    const fake = plane();
    fake.scriptPages([listedItem("a")]);
    const paginator = Sandbox.paginate({ controlPlane: fake });
    expect(ids(await paginator.nextItems())).toEqual(["a"]);
    expect(paginator.hasNext).toBe(false);
    await expect(paginator.nextItems()).rejects.toBeInstanceOf(SandboxError);
    await expect(paginator.nextItems()).rejects.toThrow(/hasNext/);
  });

  test("paginate validates synchronously before any AWS call", () => {
    const fake = plane();
    const invalid = [
      { limit: 0 },
      { order: "sideways" as ListOrder },
      { metadata: { a: "1" }, states: ["SUSPENDED"] },
      { nextToken: "%%%" },
    ];
    for (const options of invalid) {
      expect(() => Sandbox.paginate({ controlPlane: fake, ...options })).toThrow(
        InvalidArgumentError,
      );
    }
    Sandbox.paginate({ template: "rayito-base-2gb", limit: 3, controlPlane: fake });
    expect(fake.calls).toEqual([]);
  });

  test("a token from other filters fails before any ListMicrovms call", async () => {
    const fake = plane();
    fake.scriptPages([listedItem("a"), listedItem("b")]);
    const first = Sandbox.paginate({ limit: 1, controlPlane: fake });
    await first.nextItems();
    fake.pageRequests.length = 0;
    const foreign = Sandbox.paginate({
      states: ["RUNNING"],
      nextToken: first.nextToken,
      controlPlane: fake,
    });
    await expect(foreign.nextItems()).rejects.toThrow(/no corresponde a estos filtros/);
    expect(fake.pageRequests).toEqual([]);
  });

  test("an AWS rejection of a stale nextToken surfaces as InvalidArgumentError", async () => {
    const fake = plane();
    fake.listMicrovmsError = new InvalidArgumentError("Invalid nextToken");
    await expect(Sandbox.paginate({ controlPlane: fake }).nextItems()).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
  });
});

describe("Sandbox.list", () => {
  test("accepts order and startedAfter", async () => {
    const fake = plane();
    fake.scriptPages(
      [listedItem("thirty", "RUNNING", 30), listedItem("ten", "RUNNING", 10)],
      [listedItem("twenty", "RUNNING", 20)],
    );
    const ordered = Sandbox.list({
      order: "desc",
      startedAfter: new Date(STARTED_AT.getTime() + 15_000),
      controlPlane: fake,
    });
    expect(await collect(ordered)).toEqual(["thirty", "twenty"]);
  });

  test("without the new options it streams the same pages lazily", async () => {
    const fake = plane();
    fake.scriptPages(
      [listedItem("a"), listedItem("dead", "TERMINATED")],
      [listedItem("b", "SUSPENDED")],
    );
    const iterator = Sandbox.list({ controlPlane: fake })[Symbol.asyncIterator]();
    expect(fake.pageRequests).toEqual([]);
    expect((await iterator.next()).value?.sandboxId).toBe("a");
    expect(fake.pageRequests).toHaveLength(1);
    expect((await iterator.next()).value?.sandboxId).toBe("b");
    expect((await iterator.next()).done).toBe(true);
    expect(fake.pageRequests.map((request) => [request.maxResults, request.nextToken])).toEqual([
      [50, undefined],
      [50, "page-1"],
    ]);
  });

  test("validates eagerly", () => {
    const fake = plane();
    expect(() => Sandbox.list({ order: "up" as ListOrder, controlPlane: fake })).toThrow(/order/);
    expect(fake.calls).toEqual([]);
  });
});

describe("the metadata filter, one fake rayd per sandbox", () => {
  const planes: FakePoolControlPlane[] = [];

  afterEach(async () => {
    for (const fake of planes.splice(0)) {
      await fake.close();
    }
  });

  function poolPlane(): FakePoolControlPlane {
    const fake = new FakePoolControlPlane();
    planes.push(fake);
    return fake;
  }

  function openConnections(rayd: FakeRayd): Promise<number> {
    return new Promise((resolve, reject) => {
      rayd.server.getConnections((error, count) => (error ? reject(error) : resolve(count)));
    });
  }

  async function expectAllClosed(vms: readonly FakeMicrovm[]): Promise<void> {
    const deadline = performance.now() + 5000;
    for (const vm of vms) {
      while ((await openConnections(vm.rayd)) !== 0) {
        if (performance.now() > deadline) {
          throw new Error(`el transporte de la sonda de ${vm.sandboxId} sigue abierto`);
        }
        await sleep(20);
      }
    }
  }

  function healthCalls(vm: FakeMicrovm): number {
    return vm.rayd.health.healthCalls.length;
  }

  test("a subset match walks only the matching sandbox with its metadata", async () => {
    const fake = poolPlane();
    const vms = [
      await fake.addListedSandbox({ metadata: { env: "ci", run: "1" } }),
      await fake.addListedSandbox({ metadata: { env: "ci", run: "2" } }),
      await fake.addListedSandbox({ metadata: {} }),
    ];
    const paginator = Sandbox.paginate({
      metadata: { env: "ci", run: "2" },
      limit: 1,
      controlPlane: fake,
      transport: LOOPBACK_TRANSPORT,
    });
    const served: SandboxListItem[] = [];
    while (paginator.hasNext) {
      served.push(...(await paginator.nextItems()));
    }
    expect(ids(served)).toEqual([vms[1]?.sandboxId]);
    expect(served[0]?.metadata).toEqual({ env: "ci", run: "2" });
    expect(fake.callsTo("CreateMicrovmAuthToken")).toHaveLength(3);
    expect(vms.map(healthCalls)).toEqual([1, 1, 1]);
    expect(vms.every((vm) => vm.rayd.health.healthCalls[0]?.["x-access-token"] === undefined)).toBe(
      true,
    );
    await expectAllClosed(vms);
  });

  test("with a limit it probes only what it consumes", async () => {
    const fake = poolPlane();
    const vms = [];
    for (let index = 0; index < 4; index += 1) {
      vms.push(await fake.addListedSandbox({ metadata: { env: "ci" } }));
    }
    const paginator = Sandbox.paginate({
      metadata: { env: "ci" },
      limit: 1,
      controlPlane: fake,
      transport: LOOPBACK_TRANSPORT,
    });
    const items = await paginator.nextItems();
    expect(ids(items)).toEqual([vms[0]?.sandboxId]);
    expect(items[0]?.metadata).toEqual({ env: "ci" });
    expect(vms.map(healthCalls)).toEqual([1, 0, 0, 0]);
    expect(paginator.hasNext).toBe(true);
    await expectAllClosed(vms);
  });

  test("list skips suspended, vanished, not-ready and non-matching sandboxes", async () => {
    const fake = poolPlane();
    const wanted = await fake.addListedSandbox({ metadata: { env: "ci", run: "2" } });
    const other = await fake.addListedSandbox({ metadata: { env: "ci", run: "1" } });
    const paused = await fake.addListedSandbox({
      metadata: { env: "ci", run: "2" },
      state: "SUSPENDED",
    });
    const booting = await fake.addListedSandbox({ metadata: { run: "2" } });
    booting.rayd.health.notReadyCalls = 1;
    const vanished = await fake.addListedSandbox({ metadata: { run: "2" } });
    fake.fail("GetMicrovm", 4, new SandboxNotFoundError("ya no existe"));
    const listed = await collect(
      Sandbox.list({ metadata: { run: "2" }, controlPlane: fake, transport: LOOPBACK_TRANSPORT }),
    );
    expect(listed).toEqual([wanted.sandboxId]);
    expect([paused, vanished].map(healthCalls)).toEqual([0, 0]);
    expect([wanted, other, booting].map(healthCalls)).toEqual([1, 1, 1]);
    await expectAllClosed([wanted, other, booting]);
  });

  test("a sandbox suspended between ListMicrovms and GetMicrovm is skipped without a probe or a mint", async () => {
    const fake = poolPlane();
    const vm = await fake.addListedSandbox({ metadata: { env: "ci" } });
    const listPage = fake.listMicrovmsPage.bind(fake);
    vi.spyOn(fake, "listMicrovmsPage").mockImplementation(async (options) => {
      const page = await listPage(options);
      fake.setState(vm.sandboxId, "SUSPENDED");
      return page;
    });
    const listed = await collect(
      Sandbox.list({ metadata: { env: "ci" }, controlPlane: fake, transport: LOOPBACK_TRANSPORT }),
    );
    expect(listed).toEqual([]);
    expect(fake.callsTo("GetMicrovm")).toHaveLength(1);
    expect(healthCalls(vm)).toBe(0);
    expect(fake.callsTo("CreateMicrovmAuthToken")).toHaveLength(0);
  });

  test("a probe honours requestTimeoutMs (5000 ms by default)", async () => {
    const fake = poolPlane();
    const first = await fake.addListedSandbox({ metadata: { env: "ci" } });
    await collect(
      Sandbox.list({ metadata: { env: "ci" }, controlPlane: fake, transport: LOOPBACK_TRANSPORT }),
    );
    expect(deadlineFromHeaders(first.rayd.health.healthCalls[0] ?? {})).toBe(5000);
    await collect(
      Sandbox.list({
        metadata: { env: "ci" },
        requestTimeoutMs: 1234,
        controlPlane: fake,
        transport: LOOPBACK_TRANSPORT,
      }),
    );
    expect(deadlineFromHeaders(first.rayd.health.healthCalls[1] ?? {})).toBe(1234);
  });

  test("a proxy 403 during a probe re-mints once", async () => {
    const fake = poolPlane();
    const vm = await fake.addListedSandbox({ metadata: { env: "ci" } });
    vm.rayd.forbidNext(1);
    const listed = await collect(
      Sandbox.list({ metadata: { env: "ci" }, controlPlane: fake, transport: LOOPBACK_TRANSPORT }),
    );
    expect(listed).toEqual([vm.sandboxId]);
    expect(fake.callsTo("CreateMicrovmAuthToken")).toHaveLength(2);
    await expectAllClosed([vm]);
  });

  test("a failing Health names the sandbox instead of leaving a silent hole", async () => {
    const fake = poolPlane();
    const vm = await fake.addListedSandbox({ metadata: { env: "ci" } });
    vm.rayd.health.failNext.push(new ConnectError("boom", Code.Internal));
    const error = await Sandbox.paginate({
      metadata: { env: "ci" },
      controlPlane: fake,
      transport: LOOPBACK_TRANSPORT,
    })
      .nextItems()
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SandboxError);
    expect((error as Error).message).toContain(vm.sandboxId);
    expect((error as Error).message).toContain("Health no respondió");
    expect((error as Error).message).not.toContain("ci");
    await expectAllClosed([vm]);
  });
});
