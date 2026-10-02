/**
 * `Volume` de `rayito/e2b` (`m15-efs-volumes`, ADR-018, experimental):
 * unbound, cada estático lanza `UnimplementedError("Volume")` en el acto
 * (nunca dentro de la promesa que devuelve); ligado vía
 * `new E2B({ volumeStore })`, el CRUD delega en el `VolumeStore`; las
 * operaciones de contenido (`readFile`/`writeFile`/`makeDir`/`list`/
 * `remove`/`updateMetadata`) son siempre `UnimplementedError`, ligado o no.
 * Espejo de `test_m15_efs_volumes_shim.py`.
 */

import { describe, expect, test } from "vitest";
import { E2B, UnimplementedError, Volume } from "../../src/e2b/index.js";
import { resolveVolumeMounts } from "../../src/e2b/volume.js";
import { InvalidArgumentError } from "../../src/errors.js";
import type { DescribedAccessPoint, EfsApi } from "../../src/volumes/efs.js";
import { VolumeStore } from "../../src/volumes/store.js";

const FILE_SYSTEM_ID = "fs-0123abcd";
const CONTENT_METHODS = [
  "readFile",
  "writeFile",
  "makeDir",
  "list",
  "remove",
  "updateMetadata",
] as const;

function awsError(name: string): Error {
  const error = new Error("redacted");
  error.name = name;
  return error;
}

class FakeEfsApi implements EfsApi {
  readonly accessPoints = new Map<string, DescribedAccessPoint & { name: string }>();
  #nextId = 1;

  async createAccessPoint(input: {
    FileSystemId: string;
    Tags: Array<{ Key: string; Value: string }>;
  }): Promise<DescribedAccessPoint> {
    const name = input.Tags.find((t) => t.Key === "rayito:volume")?.Value ?? "";
    const accessPointId = `fsap-${(this.#nextId++).toString().padStart(8, "0")}`;
    const described = { AccessPointId: accessPointId, FileSystemId: input.FileSystemId, name };
    this.accessPoints.set(accessPointId, described);
    return described;
  }

  async describeAccessPoints(input: {
    FileSystemId: string;
  }): Promise<{ AccessPoints?: DescribedAccessPoint[] }> {
    const points = [...this.accessPoints.values()]
      .filter((ap) => ap.FileSystemId === input.FileSystemId)
      .map((ap) => ({
        AccessPointId: ap.AccessPointId,
        FileSystemId: ap.FileSystemId,
        Tags: [{ Key: "rayito:volume", Value: ap.name }],
      }));
    return { AccessPoints: points };
  }

  async deleteAccessPoint(input: { AccessPointId: string }): Promise<unknown> {
    if (!this.accessPoints.has(input.AccessPointId)) {
      throw awsError("AccessPointNotFound");
    }
    this.accessPoints.delete(input.AccessPointId);
    return {};
  }
}

function storeWith(api: FakeEfsApi): VolumeStore {
  return new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: api });
}

describe("unbound Volume", () => {
  test("throws UnimplementedError on every static", async () => {
    for (const call of [
      () => Volume.create("x"),
      () => Volume.connect("x"),
      () => Volume.getInfo("x"),
      () => Volume.list(),
      () => Volume.destroy("x"),
    ]) {
      expect(call).toThrow(UnimplementedError);
    }
  });

  test.each(CONTENT_METHODS)("%s is refused unbound", (method) => {
    const instance = new Volume("fsap-0123abcd", "x");
    expect(() => (instance[method] as (...args: unknown[]) => unknown)()).toThrow(
      UnimplementedError,
    );
  });
});

describe("bound Volume", () => {
  test("is bound per client", async () => {
    const firstApi = new FakeEfsApi();
    const secondApi = new FakeEfsApi();
    const first = new E2B({ volumeStore: storeWith(firstApi) });
    const second = new E2B({ volumeStore: storeWith(secondApi) });
    await first.Volume.create("only-on-first");
    await second.Volume.create("only-on-second");
    expect([...firstApi.accessPoints.values()].map((v) => v.name)).toEqual(["only-on-first"]);
    expect([...secondApi.accessPoints.values()].map((v) => v.name)).toEqual(["only-on-second"]);
  });

  test("create/connect/getInfo/list/destroy delegate to the store", async () => {
    const api = new FakeEfsApi();
    const client = new E2B({ volumeStore: storeWith(api) });
    const created = await client.Volume.create("datos-agente-7");
    const connected = await client.Volume.connect("datos-agente-7");
    expect(connected.volumeId).toBe(created.volumeId);
    expect((await client.Volume.getInfo("datos-agente-7")).volumeId).toBe(created.volumeId);
    expect((await client.Volume.list()).map((v) => v.name)).toEqual(["datos-agente-7"]);
    expect(await client.Volume.destroy("datos-agente-7")).toBe(true);
    expect(await client.Volume.destroy("datos-agente-7")).toBe(false);
  });

  test.each(CONTENT_METHODS)("%s is refused even when bound", async (method) => {
    const client = new E2B({ volumeStore: storeWith(new FakeEfsApi()) });
    const vol = await client.Volume.create("datos-agente-7");
    expect(() => (vol[method] as (...args: unknown[]) => unknown)()).toThrow(UnimplementedError);
  });
});

describe("resolveVolumeMounts", () => {
  test("resolves a bound Volume instance without any AWS call", async () => {
    const api = new FakeEfsApi();
    const store = storeWith(api);
    const vol = new Volume("fsap-0123abcd", "datos-agente-7");
    const resolved = await resolveVolumeMounts({ "/mnt/v": vol }, store);
    expect(resolved["/mnt/v"]?.accessPointId).toBe("fsap-0123abcd");
    expect(api.accessPoints.size).toBe(0);
  });

  test("resolves a plain name through the store", async () => {
    const api = new FakeEfsApi();
    const store = storeWith(api);
    await store.create("datos-agente-7");
    const resolved = await resolveVolumeMounts({ "/mnt/v": "datos-agente-7" }, store);
    expect(resolved["/mnt/v"]?.name).toBe("datos-agente-7");
  });

  test("rejects an unsupported value type", async () => {
    const store = storeWith(new FakeEfsApi());
    await expect(resolveVolumeMounts({ "/mnt/v": 123 }, store)).rejects.toThrow(
      InvalidArgumentError,
    );
  });
});
