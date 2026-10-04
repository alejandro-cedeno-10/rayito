/**
 * `m15-efs-volumes` (ADR-018, experimental): `EfsVolume` validation,
 * `VolumeStore` CRUD over a fake `EfsApi`, and `requireVolumeSupport`'s
 * pre-launch gate. Espejo de `test_m15_efs_volumes_domain.py` /
 * `test_m15_efs_volumes_store.py` / `test_m15_efs_volumes_section.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError, VolumeNotFoundError } from "../../src/errors.js";
import { EfsVolume } from "../../src/volumes/domain.js";
import {
  type DescribedAccessPoint,
  type EfsApi,
  LIST_VISIBILITY_BUDGET_MS,
  LIST_VISIBILITY_POLL_MS,
} from "../../src/volumes/efs.js";
import { requireVolumeSupport } from "../../src/volumes/section.js";
import { VolumeStore } from "../../src/volumes/store.js";

function awsError(name: string): Error {
  const error = new Error("redacted");
  error.name = name;
  return error;
}

class FakeEfsApi implements EfsApi {
  readonly accessPoints = new Map<
    string,
    DescribedAccessPoint & { name: string; clientToken: string }
  >();
  readonly calls: string[] = [];
  /** Simula el listado eventualmente consistente de EFS (Q125): los access
   * points de `unlisted` existen pero `describeAccessPoints` aún no los
   * devuelve, y los de `stale` ya se borraron pero sí los devuelve. */
  readonly unlisted = new Set<string>();
  readonly stale = new Map<string, DescribedAccessPoint & { name: string; clientToken: string }>();
  #nextId = 1;

  /** Como EFS de verdad: un `ClientToken` ya usado por un access point que
   * sigue vivo es `AccessPointAlreadyExists` (409), nunca el access point
   * devuelto directamente — eso lo resuelve `VolumeStore.create` llamando a
   * `get(name)`. */
  async createAccessPoint(input: {
    ClientToken: string;
    FileSystemId: string;
    Tags: Array<{ Key: string; Value: string }>;
  }): Promise<DescribedAccessPoint> {
    this.calls.push("createAccessPoint");
    for (const existing of this.accessPoints.values()) {
      if (existing.clientToken === input.ClientToken) {
        throw awsError("AccessPointAlreadyExists");
      }
    }
    const name = input.Tags.find((t) => t.Key === "rayito:volume")?.Value ?? "";
    const accessPointId = `fsap-${(this.#nextId++).toString().padStart(8, "0")}`;
    const described = {
      AccessPointId: accessPointId,
      FileSystemId: input.FileSystemId,
      name,
      clientToken: input.ClientToken,
    };
    this.accessPoints.set(accessPointId, described);
    return described;
  }

  async describeAccessPoints(input: {
    FileSystemId: string;
  }): Promise<{ AccessPoints?: DescribedAccessPoint[] }> {
    this.calls.push("describeAccessPoints");
    const points = [...this.accessPoints.values(), ...this.stale.values()]
      .filter(
        (ap) =>
          ap.FileSystemId === input.FileSystemId && !this.unlisted.has(ap.AccessPointId ?? ""),
      )
      .map((ap) => ({
        AccessPointId: ap.AccessPointId,
        FileSystemId: ap.FileSystemId,
        Tags: [{ Key: "rayito:volume", Value: ap.name }],
      }));
    return { AccessPoints: points };
  }

  async deleteAccessPoint(input: { AccessPointId: string }): Promise<unknown> {
    this.calls.push("deleteAccessPoint");
    if (!this.accessPoints.has(input.AccessPointId)) {
      throw awsError("AccessPointNotFound");
    }
    this.accessPoints.delete(input.AccessPointId);
    return {};
  }
}

const FILE_SYSTEM_ID = "fs-0123abcd";

describe("EfsVolume", () => {
  test("constructs with a well-formed file system and access point id", () => {
    const vol = new EfsVolume({ fileSystemId: FILE_SYSTEM_ID, accessPointId: "fsap-0123abcd" });
    expect(vol.readOnly).toBe(false);
  });

  test("rejects a malformed file system id", () => {
    expect(
      () => new EfsVolume({ fileSystemId: "not-an-id", accessPointId: "fsap-0123abcd" }),
    ).toThrow(InvalidArgumentError);
  });

  test("rejects a malformed access point id", () => {
    expect(
      () => new EfsVolume({ fileSystemId: FILE_SYSTEM_ID, accessPointId: "fs-0123abcd" }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("VolumeStore", () => {
  test("constructing it makes no call", () => {
    const fake = new FakeEfsApi();
    new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    expect(fake.calls).toEqual([]);
  });

  test("create is idempotent by name even though a repeated client token throws", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    const first = await store.create("datos-agente-7");
    fake.calls.length = 0;
    const second = await store.create("datos-agente-7");
    expect(second.accessPointId).toBe(first.accessPointId);
    expect(fake.accessPoints.size).toBe(1);
    expect(fake.calls).toEqual(["createAccessPoint", "describeAccessPoints"]);
  });

  test("create waits for an existing access point to be listed (Q125)", async () => {
    const fake = new FakeEfsApi();
    let now = 0;
    const sleeps: number[] = [];
    const listedAfterPolls = 3;
    const store = new VolumeStore({
      fileSystemId: FILE_SYSTEM_ID,
      client: fake,
      now: () => now,
      sleep: async (ms) => {
        sleeps.push(ms);
        now += ms;
        if (sleeps.length === listedAfterPolls) {
          fake.unlisted.clear();
        }
      },
    });
    const first = await store.create("datos-agente-7");
    fake.unlisted.add(first.accessPointId);
    const second = await store.create("datos-agente-7");
    expect(second.accessPointId).toBe(first.accessPointId);
    expect(sleeps).toEqual(Array(listedAfterPolls).fill(LIST_VISIBILITY_POLL_MS));
  });

  test("create gives up once the listing budget is spent", async () => {
    const fake = new FakeEfsApi();
    let now = 0;
    const store = new VolumeStore({
      fileSystemId: FILE_SYSTEM_ID,
      client: fake,
      now: () => now,
      sleep: async (ms) => {
        now += ms;
      },
    });
    const first = await store.create("datos-agente-7");
    fake.unlisted.add(first.accessPointId);
    await expect(store.create("datos-agente-7")).rejects.toBeInstanceOf(VolumeNotFoundError);
    expect(now).toBeGreaterThanOrEqual(LIST_VISIBILITY_BUDGET_MS);
  });

  test("destroy of a stale listed access point returns false (Q125)", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    const volume = await store.create("datos-agente-7");
    const described = fake.accessPoints.get(volume.accessPointId);
    expect(described).toBeDefined();
    if (described !== undefined) {
      fake.stale.set(volume.accessPointId, described);
    }
    fake.accessPoints.delete(volume.accessPointId);
    expect(await store.destroy("datos-agente-7")).toBe(false);
    expect(fake.calls.at(-1)).toBe("deleteAccessPoint");
  });

  test("the same name on two file systems does not share a client token", async () => {
    const fake = new FakeEfsApi();
    const firstStore = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    const secondStore = new VolumeStore({ fileSystemId: "fs-99999999", client: fake });
    const first = await firstStore.create("datos-agente-7");
    const second = await secondStore.create("datos-agente-7");
    expect(first.accessPointId).not.toBe(second.accessPointId);
    expect(fake.accessPoints.size).toBe(2);
  });

  test("recreating a destroyed name does not reuse a spent token", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    const first = await store.create("datos-agente-7");
    await store.destroy("datos-agente-7");
    const second = await store.create("datos-agente-7");
    expect(second.accessPointId).not.toBe(first.accessPointId);
  });

  test("get finds a volume by name and raises when missing", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    await store.create("datos-agente-7");
    const found = await store.get("datos-agente-7");
    expect(found.name).toBe("datos-agente-7");
    await expect(store.get("no-existe")).rejects.toThrow(VolumeNotFoundError);
  });

  test("list returns every tagged access point", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    await store.create("a");
    await store.create("b");
    const names = (await store.list()).map((v) => v.name).sort();
    expect(names).toEqual(["a", "b"]);
  });

  test("destroy returns false for a volume that never existed", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    expect(await store.destroy("ghost")).toBe(false);
  });

  test("destroy returns true and removes the access point", async () => {
    const fake = new FakeEfsApi();
    const store = new VolumeStore({ fileSystemId: FILE_SYSTEM_ID, client: fake });
    await store.create("datos-agente-7");
    expect(await store.destroy("datos-agente-7")).toBe(true);
    expect(fake.accessPoints.size).toBe(0);
  });
});

const VALID_VOLUME = {
  "/mnt/v": new EfsVolume({ fileSystemId: FILE_SYSTEM_ID, accessPointId: "fsap-0123abcd" }),
};

describe("requireVolumeSupport", () => {
  test("an empty object is invalid", () => {
    expect(() => requireVolumeSupport({}, undefined)).toThrow(InvalidArgumentError);
  });

  test("a non-EfsVolume value is invalid", () => {
    expect(() => requireVolumeSupport({ "/mnt/v": {} }, undefined)).toThrow(InvalidArgumentError);
  });

  test("an overlapping path is invalid", () => {
    const overlapping = {
      "/mnt/v": new EfsVolume({ fileSystemId: FILE_SYSTEM_ID, accessPointId: "fsap-0123abcd" }),
      "/mnt/v/sub": new EfsVolume({ fileSystemId: FILE_SYSTEM_ID, accessPointId: "fsap-0123abce" }),
    };
    expect(() => requireVolumeSupport(overlapping, undefined)).toThrow(InvalidArgumentError);
  });

  test("a non-caps image variant is rejected before the generic message", () => {
    expect(() => requireVolumeSupport(VALID_VOLUME, "base")).toThrow(UnimplementedError);
  });

  test("a well-formed request still raises UnimplementedError (experimental, pending EFS-1..EFS-20)", () => {
    expect(() => requireVolumeSupport(VALID_VOLUME, "base-caps")).toThrow(UnimplementedError);
    expect(() => requireVolumeSupport(VALID_VOLUME, undefined)).toThrow(UnimplementedError);
  });
});
