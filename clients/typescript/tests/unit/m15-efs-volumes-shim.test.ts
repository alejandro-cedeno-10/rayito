/**
 * `Volume` de `rayito/e2b` (`m15-efs-volumes`, ADR-018, experimental):
 * unbound, cada estático lanza `UnimplementedError("Volume")` en el acto
 * (nunca dentro de la promesa que devuelve); ligado vía
 * `new E2B({ volumeStore })`, el CRUD delega en el `VolumeStore`; las
 * operaciones de contenido (`readFile`/`writeFile`/`makeDir`/`list`/
 * `remove`/`updateMetadata`) son siempre `UnimplementedError("volume.content")`,
 * ligado o no. `volumeId` es el nombre lógico en todas partes (el ida y
 * vuelta `destroy(vol.volumeId)` de E2B funciona) y `volumeMounts` pasa por
 * una puerta sin I/O (store, `volumeConnectorArn`, sin internet) y después
 * se monta con el `volumes` nativo, con el conector como único egress. Espejo de
 * `test_m15_efs_volumes_shim.py`.
 */

import { describe, expect, test, vi } from "vitest";
import { E2B, Sandbox, UnimplementedError, Volume } from "../../src/e2b/index.js";
import { planVolumeMounts, resolveVolumeMounts } from "../../src/e2b/volume.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { Sandbox as NativeSandbox } from "../../src/sandbox/sandbox.js";
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
  readonly calls: string[] = [];
  #nextId = 1;

  async createAccessPoint(input: {
    FileSystemId: string;
    Tags: Array<{ Key: string; Value: string }>;
  }): Promise<DescribedAccessPoint> {
    this.calls.push("createAccessPoint");
    const name = input.Tags.find((t) => t.Key === "rayito:volume")?.Value ?? "";
    const accessPointId = `fsap-${(this.#nextId++).toString().padStart(8, "0")}`;
    const described = { AccessPointId: accessPointId, FileSystemId: input.FileSystemId, name };
    this.accessPoints.set(accessPointId, described);
    return described;
  }

  async describeAccessPoints(input: {
    FileSystemId: string;
  }): Promise<{ AccessPoints?: DescribedAccessPoint[] }> {
    this.calls.push("describeAccessPoints");
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
    this.calls.push("deleteAccessPoint");
    if (!this.accessPoints.has(input.AccessPointId)) {
      throw awsError("AccessPointNotFound");
    }
    this.accessPoints.delete(input.AccessPointId);
    return {};
  }
}

function caught(call: () => unknown): unknown {
  try {
    call();
  } catch (error) {
    return error;
  }
  throw new Error("expected a throw");
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
    const instance = new Volume("datos-agente-7");
    const error = caught(() => (instance[method] as (...args: unknown[]) => unknown)());
    expect(error).toBeInstanceOf(UnimplementedError);
    expect((error as UnimplementedError).feature).toBe("volume.content");
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

  test("volumeId round-trips through connect, getInfo and destroy", async () => {
    const api = new FakeEfsApi();
    const client = new E2B({ volumeStore: storeWith(api) });
    const vol = await client.Volume.create("ws");
    expect(vol.volumeId).toBe("ws");
    expect(vol.name).toBe("ws");
    expect(vol.accessPointId).toMatch(/^fsap-/);
    expect((await client.Volume.connect(vol.volumeId)).accessPointId).toBe(vol.accessPointId);
    expect((await client.Volume.getInfo(vol.volumeId)).volumeId).toBe("ws");
    expect(await client.Volume.destroy(vol.volumeId)).toBe(true);
    expect(api.accessPoints.size).toBe(0);
  });

  test.each(CONTENT_METHODS)("%s is refused even when bound", async (method) => {
    const client = new E2B({ volumeStore: storeWith(new FakeEfsApi()) });
    const vol = await client.Volume.create("datos-agente-7");
    expect(() => (vol[method] as (...args: unknown[]) => unknown)()).toThrow(UnimplementedError);
  });
});

// Marcador de documentación (cuenta ficticia).
const CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs";
const ROLE = "arn:aws:iam::123456789012:role/rayito-execution";

function config(api: FakeEfsApi, connectorArn: string | undefined = CONNECTOR) {
  return { store: storeWith(api), connectorArn };
}

describe("planVolumeMounts", () => {
  test("without a bound store it is UnimplementedError('Volume')", () => {
    const error = caught(() => planVolumeMounts({ "/mnt/v": "x" }, undefined, undefined));
    expect((error as UnimplementedError).feature).toBe("Volume");
  });

  test.each([
    ["plain name", "datos-agente-7"],
    ["bound volume", new Volume("datos-agente-7", "fsap-00000001")],
  ])("%s: validated without calling AWS", (_label, value) => {
    const api = new FakeEfsApi();
    const planned = planVolumeMounts({ "/mnt/v": value }, config(api), undefined);
    expect(planned.connectorArn).toBe(CONNECTOR);
    expect(planned.entries).toEqual([["/mnt/v", value]]);
    expect(api.calls).toEqual([]);
  });

  test("without volumeConnectorArn it names the option and the one-connector rule", () => {
    const error = caught(() =>
      planVolumeMounts({ "/mnt/v": "datos" }, { store: storeWith(new FakeEfsApi()) }, undefined),
    );
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect((error as Error).message).toContain("volumeConnectorArn");
    expect((error as Error).message).toContain("NAT");
  });

  test("allowInternetAccess: true is refused (one egress connector per MicroVM)", () => {
    expect(() => planVolumeMounts({ "/mnt/v": "datos" }, config(new FakeEfsApi()), true)).toThrow(
      /un solo conector/,
    );
    expect(() =>
      planVolumeMounts({ "/mnt/v": "datos" }, config(new FakeEfsApi()), false),
    ).not.toThrow();
  });

  test.each([[{}], [{ "/mnt/v": 123 }], [{ "/mnt/v": "no valid name!" }], [["x"]]])(
    "rejects a malformed request %#",
    (mounts) => {
      expect(() => planVolumeMounts(mounts, config(new FakeEfsApi()), undefined)).toThrow(
        InvalidArgumentError,
      );
    },
  );
});

describe("resolveVolumeMounts", () => {
  test("a name is looked up in the store, a Volume maps without AWS", async () => {
    const api = new FakeEfsApi();
    const store = storeWith(api);
    const created = await store.create("por-nombre");
    api.calls.length = 0;
    const volumes = await resolveVolumeMounts({
      store,
      connectorArn: CONNECTOR,
      entries: [
        ["/mnt/a", "por-nombre"],
        ["/mnt/b", new Volume("por-objeto", "fsap-0000000a")],
      ],
    });
    expect(volumes["/mnt/a"]?.accessPointId).toBe(created.accessPointId);
    expect(volumes["/mnt/b"]?.accessPointId).toBe("fsap-0000000a");
    expect(volumes["/mnt/b"]?.fileSystemId).toBe(FILE_SYSTEM_ID);
    expect(volumes["/mnt/b"]?.name).toBe("por-objeto");
    expect(api.calls).toEqual(["describeAccessPoints"]);
  });

  test("a Volume without accessPointId is refused", async () => {
    await expect(
      resolveVolumeMounts({
        store: storeWith(new FakeEfsApi()),
        connectorArn: CONNECTOR,
        entries: [["/mnt/a", new Volume("sin-id")]],
      }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
  });
});

describe("Sandbox.create({ volumeMounts })", () => {
  const launched = new Error("lanzamiento interceptado");

  test("launches the native create with volumes and the connector as the only egress", async () => {
    const api = new FakeEfsApi();
    const store = storeWith(api);
    await store.create("datos");
    const native = vi.spyOn(NativeSandbox, "create").mockRejectedValue(launched);
    try {
      const client = new E2B({ volumeStore: store, volumeConnectorArn: CONNECTOR });
      await expect(
        client.Sandbox.create("rayito-base-caps-efs", {
          executionRoleArn: ROLE,
          volumeMounts: { "/mnt/v": "datos" },
        }),
      ).rejects.toBe(launched);
      const [options] = native.mock.calls[0] ?? [];
      expect(options?.egress).toEqual([CONNECTOR]);
      expect(options?.allowInternetAccess).toBeUndefined();
      expect(options?.executionRoleArn).toBe(ROLE);
      expect(Object.keys(options?.volumes ?? {})).toEqual(["/mnt/v"]);
      expect(options?.volumes?.["/mnt/v"]?.fileSystemId).toBe(FILE_SYSTEM_ID);
    } finally {
      native.mockRestore();
    }
  });

  test("allowInternetAccess: false still reaches the native create", async () => {
    const native = vi.spyOn(NativeSandbox, "create").mockRejectedValue(launched);
    try {
      const client = new E2B({
        volumeStore: storeWith(new FakeEfsApi()),
        volumeConnectorArn: CONNECTOR,
      });
      await expect(
        client.Sandbox.create("rayito-base-caps-efs", {
          allowInternetAccess: false,
          volumeMounts: { "/mnt/v": new Volume("datos", "fsap-00000001") },
        }),
      ).rejects.toBe(launched);
      expect(native.mock.calls[0]?.[0]?.allowInternetAccess).toBe(false);
    } finally {
      native.mockRestore();
    }
  });

  test("mcp/iam are rejected first and nothing reaches AWS", async () => {
    const api = new FakeEfsApi();
    const client = new E2B({ volumeStore: storeWith(api), volumeConnectorArn: CONNECTOR });
    await expect(
      client.Sandbox.create("rayito-base-caps-efs", {
        mcp: { github: {} },
        volumeMounts: { "/mnt/v": "datos" },
      }),
    ).rejects.toMatchObject({ feature: "mcp" });
    expect(api.calls).toEqual([]);
  });

  test("without volumeConnectorArn nothing reaches AWS", async () => {
    const api = new FakeEfsApi();
    const client = new E2B({ volumeStore: storeWith(api) });
    await expect(
      client.Sandbox.create("rayito-base-caps-efs", { volumeMounts: { "/mnt/v": "datos" } }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(api.calls).toEqual([]);
  });

  test("a per-call volumeStore is not an option: the store is client-bound only", async () => {
    const api = new FakeEfsApi();
    const opts = { volumeMounts: { "/mnt/v": "datos" }, volumeStore: storeWith(api) };
    await expect(Sandbox.create("rayito-base-caps", opts)).rejects.toMatchObject({
      feature: "Volume",
    });
    expect(api.calls).toEqual([]);
  });
});
