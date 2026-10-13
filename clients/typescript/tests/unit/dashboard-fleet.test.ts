/**
 * `Fleet`: inventario con memoria de imagen, imágenes y versiones (adaptador
 * con un cliente falso), historial de eventos y acciones masivas (sólo con
 * `allowActions: true`, al ritmo de la cuota de TPS, sin que un fallo
 * detenga la cola y sin repetir el mensaje de AWS).
 */

import { describe, expect, it } from "vitest";
import {
  LambdaMicrovmsImageCatalog,
  listImageSummaries,
  listImageVersionSummaries,
  type Sender,
} from "../../src/dashboard/aws.js";
import type { BulkProgress } from "../../src/dashboard/domain.js";
import { Fleet, type FleetOptions } from "../../src/dashboard/fleet.js";
import {
  AuthenticationError,
  CapacityError,
  DashboardError,
  InvalidArgumentError,
  QuotaExceededError,
  SandboxError,
} from "../../src/errors.js";
import type { EventRecord } from "../../src/lifecycle-events/domain.js";
import { ConventionCatalog } from "../../src/sizing/catalog.js";
import { IMAGE_ARN } from "./fake/control-plane.js";
import {
  awsSdkError,
  FakeClock,
  FakeEvents,
  FakeImageCatalog,
  FakePlane,
  listItem,
  OTHER_IMAGE_ARN,
  RaisingError,
  sandboxId,
} from "./fake/dashboard.js";

function fleet(plane: FakePlane, extra: FleetOptions = {}, clock = new FakeClock()): Fleet {
  return new Fleet({
    controlPlane: plane,
    catalog: new ConventionCatalog(),
    monotonic: clock.monotonic,
    sleep: clock.sleep,
    ...extra,
  });
}

function runningFleet(count = 3): FakePlane {
  return new FakePlane(Array.from({ length: count }, (_, index) => listItem(index + 1)));
}

describe("Fleet.sandboxes", () => {
  it("carries the image memory and never envs or hook payloads", async () => {
    const plane = new FakePlane([listItem(1), listItem(2, { imageArn: OTHER_IMAGE_ARN })], {
      [`${IMAGE_ARN}@1.0`]: 2048,
      [`${OTHER_IMAGE_ARN}@1.0`]: 8192,
    });

    const views = await fleet(plane).sandboxes();

    expect(views.map((v) => [v.sandboxId, v.memoryMib, v.templateName])).toEqual([
      [sandboxId(1), 2048, "rayito-base-2gb"],
      [sandboxId(2), 8192, "rayito-base-8gb"],
    ]);
    const fields = Object.keys(views[0] ?? {});
    for (const forbidden of ["envs", "runHookPayload", "token", "jwe", "endpoint"]) {
      expect(fields).not.toContain(forbidden);
    }
  });

  it("filters by image name and state", async () => {
    const plane = new FakePlane(
      [
        listItem(1),
        listItem(2, { state: "SUSPENDED" }),
        listItem(3, { imageArn: OTHER_IMAGE_ARN }),
      ],
      { [`${IMAGE_ARN}@1.0`]: 2048, [`${OTHER_IMAGE_ARN}@1.0`]: 8192 },
    );
    const views = await fleet(plane).sandboxes({ states: ["SUSPENDED"], image: "rayito-base-2gb" });
    expect(views.map((v) => v.sandboxId)).toEqual([sandboxId(2)]);
    expect(plane.calls).toContainEqual(["resolveTemplateArn", "rayito-base-2gb"]);
  });
});

describe("Fleet images and events", () => {
  it("uses the catalog port and resolves names", async () => {
    const catalog = new FakeImageCatalog();
    const service = fleet(runningFleet(), { images: catalog });
    expect((await service.images({ nameFilter: "rayito" })).map((i) => i.name)).toEqual([
      "rayito-base",
    ]);
    expect((await service.imageVersions("rayito-base")).map((v) => v.imageVersion)).toEqual([
      "2.0",
    ]);
    expect(catalog.calls).toEqual([
      ["listImages", "rayito"],
      ["listVersions", IMAGE_ARN],
    ]);
  });

  it("needs a history and passes the query through", async () => {
    await expect(fleet(runningFleet()).events()).rejects.toThrow(/historial de eventos/);
    const record: EventRecord = {
      eventId: "e1",
      sandboxId: sandboxId(1),
      kind: "created",
      killReason: undefined,
      generation: 0,
      occurredAtMs: 1,
      imageArn: IMAGE_ARN,
      imageVersion: "1.0",
    };
    const history = new FakeEvents([record]);
    const events = await fleet(runningFleet(), { events: history }).events({
      sandboxId: sandboxId(1),
      types: ["sandbox.lifecycle.created"],
      limit: 5,
      order: "asc",
    });
    expect(events).toEqual([record]);
    expect(history.calls).toEqual([
      {
        sandboxId: sandboxId(1),
        types: ["sandbox.lifecycle.created"],
        limit: 5,
        order: "asc",
      },
    ]);
  });
});

describe("Fleet bulk actions", () => {
  it("are off unless explicitly allowed", async () => {
    const plane = runningFleet();
    const service = fleet(plane);
    expect(service.allowActions).toBe(false);
    await expect(service.runBulk("kill", [sandboxId(1)])).rejects.toThrow(/allowActions/);
    expect(plane.operations()).toEqual([]);
  });

  it("suspend runs at the quota rate and reports progress", async () => {
    const plane = runningFleet(6);
    const clock = new FakeClock();
    const progress: BulkProgress[] = [];
    const service = fleet(plane, { allowActions: true }, clock);
    const start = clock.now;

    const result = await service.runBulk(
      "suspend",
      Array.from({ length: 6 }, (_, index) => sandboxId(index + 1)),
      { onProgress: (p) => progress.push(p) },
    );

    expect(result.plan.tps).toBe(2);
    expect(result.items.map((r) => r.outcome)).toEqual(Array(6).fill("done"));
    expect(result.count("done")).toBe(6);
    expect(plane.operations()).toEqual(Array(6).fill("suspendMicrovm"));
    expect(progress.map((p) => p.done)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(new Set(progress.map((p) => p.total))).toEqual(new Set([6]));
    expect(clock.now - start).toBeCloseTo(2, 9);
  });

  it("use a lower applied rate and never exceed the API TPS", () => {
    const service = fleet(runningFleet(), { allowActions: true });
    expect(service.planBulk("suspend", 10, { appliedRate: 1 }).tps).toBe(1);
    expect(service.planBulk("suspend", 10, { appliedRate: 50 }).tps).toBe(2);
  });

  it("a failure does not stop the queue and only the code is reported", async () => {
    const plane = runningFleet(4);
    plane.fail.set(`terminateMicrovm:${sandboxId(2)}`, new RaisingError("ConflictException"));
    plane.fail.set(
      `terminateMicrovm:${sandboxId(3)}`,
      new AuthenticationError("arn:aws:... secreto", { awsCode: "AccessDeniedException" }),
    );
    plane.unchanged.add(`terminateMicrovm:${sandboxId(4)}`);

    const result = await fleet(plane, { allowActions: true }).runBulk(
      "kill",
      [1, 2, 3, 4].map(sandboxId),
    );

    expect(result.items.map((r) => [r.outcome, r.code])).toEqual([
      ["done", undefined],
      ["failed", "ConflictException"],
      ["failed", "AccessDeniedException"],
      ["unchanged", undefined],
    ]);
    expect(JSON.stringify(result)).not.toContain("secreto");
    expect(JSON.stringify(result)).not.toContain("mensaje");
  });

  it("quota and capacity failures do not stop the queue", async () => {
    const plane = runningFleet(4);
    plane.fail.set(
      `resumeMicrovm:${sandboxId(2)}`,
      new QuotaExceededError("cuota", { quotaCode: "L-1" }),
    );
    plane.fail.set(`resumeMicrovm:${sandboxId(3)}`, new CapacityError("sin capacidad"));

    const result = await fleet(plane, { allowActions: true }).runBulk(
      "resume",
      [1, 2, 3, 4].map(sandboxId),
    );

    expect(result.items.map((r) => [r.outcome, r.code])).toEqual([
      ["done", undefined],
      ["failed", "ServiceQuotaExceededException"],
      ["failed", "InsufficientCapacityException"],
      ["done", undefined],
    ]);
  });

  it("a failure without an AWS code uses the language-neutral code", async () => {
    const plane = runningFleet(1);
    plane.fail.set(`terminateMicrovm:${sandboxId(1)}`, new SandboxError("x"));
    const result = await fleet(plane, { allowActions: true }).runBulk("kill", [sandboxId(1)]);
    expect(result.items[0]?.code).toBe("UnknownError");
  });

  it("an unexpected error is not swallowed", async () => {
    const plane = runningFleet(1);
    plane.fail.set(`terminateMicrovm:${sandboxId(1)}`, new TypeError("bug"));
    await expect(
      fleet(plane, { allowActions: true }).runBulk("kill", [sandboxId(1)]),
    ).rejects.toThrow(TypeError);
  });

  it("validate before any call and dedupe ids", async () => {
    const plane = runningFleet();
    const service = fleet(plane, { allowActions: true });
    for (const bad of [[], [""], ["x".repeat(257)]]) {
      await expect(service.runBulk("kill", bad)).rejects.toBeInstanceOf(InvalidArgumentError);
    }
    await expect(service.runBulk("delete" as unknown as "kill", [sandboxId(1)])).rejects.toThrow(
      /action/,
    );
    expect(plane.operations()).toEqual([]);
    const result = await service.runBulk("resume", [sandboxId(1), sandboxId(1)]);
    expect(result.items).toHaveLength(1);
  });
});

interface Recorded {
  readonly name: string;
  readonly input: Record<string, unknown>;
}

function recordingSender(responses: unknown[]): { sender: Sender; sent: Recorded[] } {
  const sent: Recorded[] = [];
  const sender: Sender = {
    send: async (command) => {
      const { input } = command as { input: object };
      sent.push({ name: (command as object).constructor.name, input: { ...input } });
      return responses[sent.length - 1];
    },
  };
  return { sender, sent };
}

describe("image catalog adapter", () => {
  const created = new Date(Date.UTC(2026, 9, 1));

  it("lists images with the name filter and follows nextToken", async () => {
    const { sender, sent } = recordingSender([
      {
        items: [
          {
            name: "rayito-base",
            imageArn: IMAGE_ARN,
            state: "CREATED",
            latestActiveImageVersion: "2",
            createdAt: created,
          },
        ],
        nextToken: "t1",
      },
      { items: [] },
    ]);
    const images = await listImageSummaries(sender, "rayito");
    expect(images.map((i) => [i.name, i.latestActiveVersion, i.latestFailedVersion])).toEqual([
      ["rayito-base", "2", undefined],
    ]);
    expect(sent.map((s) => [s.name, s.input])).toEqual([
      ["ListMicrovmImagesCommand", { nameFilter: "rayito" }],
      ["ListMicrovmImagesCommand", { nameFilter: "rayito", nextToken: "t1" }],
    ]);
  });

  it("sorts versions newest first and keeps only the artifact file name", async () => {
    const item = (version: string, day: number, withMemory: boolean) => ({
      imageVersion: version,
      state: "SUCCESSFUL",
      status: "ACTIVE",
      baseImageVersion: "1",
      codeArtifact: { uri: "s3://amzn-s3-demo-bucket/rayito/artifact-abc.zip" },
      ...(withMemory ? { resources: [{ minimumMemoryInMiB: 2048 }] } : {}),
      createdAt: new Date(Date.UTC(2026, 9, day)),
    });
    const { sender, sent } = recordingSender([
      { items: [item("1", 1, false), item("2", 9, true)] },
    ]);

    const versions = await listImageVersionSummaries(sender, IMAGE_ARN);

    expect(versions.map((v) => [v.imageVersion, v.memoryMib])).toEqual([
      ["2", 2048],
      ["1", undefined],
    ]);
    expect(versions[0]?.artifact).toBe("artifact-abc.zip");
    expect(JSON.stringify(versions)).not.toContain("amzn-s3-demo-bucket");
    expect(sent[0]?.input).toEqual({ imageIdentifier: IMAGE_ARN });
  });

  it("maps errors by code through the catalog class", async () => {
    const failing: Sender = {
      send: async () => {
        throw awsSdkError("arn:aws:... secreto", "ValidationException");
      },
    };
    const catalog = new LambdaMicrovmsImageCatalog(() => failing);
    const error = await catalog.listImages().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(DashboardError);
    expect((error as Error).message).not.toContain("secreto");
    expect((error as DashboardError).awsCode).toBe("ValidationException");
  });
});
