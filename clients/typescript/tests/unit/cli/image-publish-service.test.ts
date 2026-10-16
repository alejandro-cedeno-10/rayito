/**
 * `rayito image publish` (servicio) contra un `PublishPorts` en memoria: la
 * subida por contenido, crear o actualizar, el gate de tres estados, el reuso
 * de una versión igual, un build fallido con su motivo y sus logs, los
 * artefactos que no casan con la variante y `--sizes`. Espejo de
 * `test_publish.py`.
 */

import { createHash } from "node:crypto";
import { afterEach, describe, expect, it } from "vitest";
import { fileArtifacts } from "../../../src/images/artifact-node.js";
import { DEFAULT_MEMORY_MIB } from "../../../src/images/gateway.js";
import {
  BASELINE_MEMORY_ENV_VAR,
  type PublishSettings,
} from "../../../src/images/publish-domain.js";
import {
  ImagePublishError,
  type PublishDeps,
  publish,
  publishWithSizes,
} from "../../../src/images/publish-service.js";
import { BUCKET, BUILD_ROLE, FakePublishPorts, ImageWorkspace, imageArnOf } from "./image-fakes.js";

const NOW = new Date("2026-09-15T16:02:07Z");
const MS_PER_SECOND = 1000;
const workspaces: ImageWorkspace[] = [];

afterEach(() => {
  for (const workspace of workspaces.splice(0)) {
    workspace.dispose();
  }
});

function workspace(): ImageWorkspace {
  const created = new ImageWorkspace();
  workspaces.push(created);
  return created;
}

function settingsFor(artifact: string, overrides: Partial<PublishSettings> = {}): PublishSettings {
  return {
    artifact,
    imageName: "rayito-base",
    variant: "full",
    bucket: BUCKET,
    stackName: "stack",
    buildRoleArn: BUILD_ROLE,
    baseImageVersion: "1",
    memoryMib: DEFAULT_MEMORY_MIB,
    force: false,
    timeoutMs: 600 * MS_PER_SECOND,
    osCapabilities: undefined,
    environmentVariables: {},
    withEfs: false,
    artifactRunId: undefined,
    ...overrides,
  };
}

interface Harness {
  readonly ports: FakePublishPorts;
  readonly lines: string[];
  readonly sleeps: number[];
  readonly deps: PublishDeps;
}

function harness(ports = new FakePublishPorts()): Harness {
  const lines: string[] = [];
  const sleeps: number[] = [];
  let clock = 0;
  return {
    ports,
    lines,
    sleeps,
    deps: {
      ports,
      artifacts: fileArtifacts,
      sleep: async (ms) => {
        sleeps.push(ms);
        clock += ms;
      },
      now: () => clock,
      emit: (message) => lines.push(message),
      utcNow: () => NOW,
    },
  };
}

function keyOf(artifact: string, runId?: string): string {
  const digest = createHash("sha256").update(fileArtifacts.read(artifact)).digest("hex");
  const prefix = runId === undefined ? "rayito/images" : `rayito/images/runs/${runId}`;
  return `${prefix}/rayd-${digest.slice(0, 12)}.zip`;
}

describe("publish", () => {
  it("la primera vez sube el zip por contenido, crea la imagen y espera el gate", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps, lines, sleeps } = harness();
    const summary = await publish(deps, settingsFor(artifact));
    const key = keyOf(artifact);
    expect(ports.calls.slice(0, 3)).toEqual([
      `headObject:${BUCKET}/${key}`,
      `putObject:${BUCKET}/${key}`,
      "getMicrovmImage",
    ]);
    expect(ports.calls).toContain("createMicrovmImage");
    expect(ports.calls).not.toContain("updateMicrovmImage");
    expect(summary).toMatchObject({
      imageArn: imageArnOf("rayito-base"),
      imageVersion: "1.0",
      artifact: `s3://${BUCKET}/${key}`,
      artifactUploaded: true,
      buildState: "SUCCESSFUL",
      chipsetGeneration: "3",
      launchable: true,
      gate: { imageState: "UPDATED", versionState: "SUCCESSFUL", versionStatus: "ACTIVE" },
    });
    expect(sleeps).toEqual([10_000]);
    expect(lines.some((line) => line.startsWith("subido s3://"))).toBe(true);
    expect(lines).toContain("create-microvm-image aceptado: versión 1.0");
    expect(lines.some((line) => line.includes("image=UPDATING version=1.0"))).toBe(true);
  });

  it("el cuerpo de create lleva la configuración deseada y una descripción con la hora", async () => {
    const files = workspace();
    const { ports, deps } = harness();
    await publish(deps, settingsFor(files.zip(), { osCapabilities: "ALL" }));
    const request = ports.requests[0]?.body ?? {};
    expect(request).toMatchObject({
      baseImageArn: "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
      baseImageVersion: "1",
      buildRoleArn: BUILD_ROLE,
      additionalOsCapabilities: ["ALL"],
      logging: { cloudWatch: { logGroup: "/rayito/rayito-base" } },
      description: "rayd 2026-09-15T16:02:07Z",
    });
  });

  it("si la imagen existe pero ninguna versión casa, actualiza", async () => {
    const files = workspace();
    const { ports, deps, lines } = harness();
    await publish(deps, settingsFor(files.zip(), { baseImageVersion: "1" }));
    ports.calls.length = 0;
    await publish(deps, settingsFor(files.zip(), { baseImageVersion: "2" }));
    expect(ports.calls).toContain("updateMicrovmImage");
    expect(ports.calls).not.toContain("createMicrovmImage");
    expect(lines).toContain("update-microvm-image aceptado: versión 2.0");
  });

  it("reutiliza una versión igual sin construir ni esperar", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps, lines } = harness();
    await publish(deps, settingsFor(artifact));
    ports.calls.length = 0;
    const reused = await publish(deps, settingsFor(artifact));
    expect(ports.calls).not.toContain("createMicrovmImage");
    expect(ports.calls).not.toContain("updateMicrovmImage");
    expect(reused).toMatchObject({ imageVersion: "1.0", launchable: true, buildSeconds: null });
    expect(reused).not.toHaveProperty("gate");
    expect(reused).toMatchObject({ artifactUploaded: false });
    expect(lines.some((line) => line.includes("se reutiliza"))).toBe(true);
  });

  it("--force construye aunque una versión coincida", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps } = harness();
    await publish(deps, settingsFor(artifact));
    ports.calls.length = 0;
    const rebuilt = await publish(deps, settingsFor(artifact, { force: true }));
    expect(ports.calls).toContain("updateMicrovmImage");
    expect(rebuilt).toMatchObject({ imageVersion: "2.0" });
  });

  it("no reutiliza una versión con otras variables de entorno ni con otras capabilities", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps } = harness();
    await publish(deps, settingsFor(artifact));
    ports.calls.length = 0;
    await publish(deps, settingsFor(artifact, { environmentVariables: { A: "1" } }));
    expect(ports.calls).toContain("updateMicrovmImage");
    ports.calls.length = 0;
    await publish(deps, settingsFor(artifact, { osCapabilities: "ALL" }));
    expect(ports.calls).toContain("updateMicrovmImage");
  });

  it("un build fallido informa el motivo y la cola de logs, y no es lanzable", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.failNextBuild = true;
    ports.recent = {
      kind: "streams",
      streams: [
        {
          name: "stream-1",
          events: [{ timestamp: 1_790_000_000_000, message: "Step 3/5 falló  \n" }],
        },
      ],
    };
    const { deps, lines } = harness(ports);
    const summary = await publish(deps, settingsFor(files.zip()));
    expect(summary.launchable).toBe(false);
    expect(summary).toMatchObject({
      gate: { versionState: "FAILED", versionStatus: "INACTIVE", launchable: false },
    });
    expect(lines.some((line) => line.includes("el build no quedó lanzable"))).toBe(true);
    expect(lines.some((line) => line.includes("stateReason='boom'"))).toBe(true);
    expect(lines).toContain("===== /rayito/rayito-base / stream-1 (1 eventos)");
    expect(lines).toContain("1790000000000 Step 3/5 falló");
  });

  it("sin logs legibles lo dice con el código de AWS", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.failNextBuild = true;
    ports.recent = { kind: "unavailable", code: "ResourceNotFoundException" };
    const { deps, lines } = harness(ports);
    await publish(deps, settingsFor(files.zip()));
    expect(lines).toContain("sin logs de build en /rayito/rayito-base: ResourceNotFoundException");
  });

  it("un build que no se asienta en el plazo termina como no lanzable", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.neverSettle = true;
    const { deps, sleeps } = harness(ports);
    const summary = await publish(deps, settingsFor(files.zip(), { timeoutMs: 25_000 }));
    expect(summary.launchable).toBe(false);
    expect(sleeps).toEqual([10_000, 10_000, 10_000]);
  });
});

describe("la subida del artefacto", () => {
  it("se salta si la clave ya existe", async () => {
    const files = workspace();
    const artifact = files.zip();
    const ports = new FakePublishPorts();
    ports.objects.set(`${BUCKET}/${keyOf(artifact)}`, new Uint8Array());
    const { deps, lines } = harness(ports);
    const summary = await publish(deps, settingsFor(artifact));
    expect(ports.calls.some((call) => call.startsWith("putObject"))).toBe(false);
    expect(summary).toMatchObject({ artifactUploaded: false });
    expect(lines.some((line) => line.startsWith("el artefacto ya está en S3"))).toBe(true);
  });

  it("--artifact-run-id acota la clave a esa ejecución", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps } = harness();
    const summary = await publish(deps, settingsFor(artifact, { artifactRunId: "run-1" }));
    expect(ports.calls).toContain(`putObject:${BUCKET}/${keyOf(artifact, "run-1")}`);
    expect(summary).toMatchObject({
      artifact: `s3://${BUCKET}/${keyOf(artifact, "run-1")}`,
      artifactUploaded: true,
    });
  });
});

describe("el rol de build", () => {
  it("sale de la pila si no se pasa", async () => {
    const files = workspace();
    const { ports, deps } = harness();
    await publish(
      deps,
      settingsFor(files.zip(), { buildRoleArn: undefined, stackName: "mi-pila" }),
    );
    expect(ports.calls).toContain("buildRoleArnOfStack:mi-pila");
    expect(ports.requests[0]?.body.buildRoleArn).toBe(BUILD_ROLE);
  });

  it("sin la salida BuildRoleArn pide --build-role-arn", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.stackRole = undefined;
    const { deps } = harness(ports);
    await expect(
      publish(deps, settingsFor(files.zip(), { buildRoleArn: undefined, stackName: "mi-pila" })),
    ).rejects.toThrow(/mi-pila no tiene la salida BuildRoleArn: pasa --build-role-arn/);
  });
});

describe("el artefacto tiene que casar con la variante y con EFS, antes de cualquier llamada", () => {
  async function refused(
    artifact: string,
    overrides: Partial<PublishSettings>,
  ): Promise<{ error: ImagePublishError; ports: FakePublishPorts }> {
    const { ports, deps } = harness();
    const error = await publish(deps, settingsFor(artifact, overrides)).then(
      () => new ImagePublishError("no se rechazó"),
      (caught: unknown) => caught as ImagePublishError,
    );
    return { error, ports };
  }

  it("un zip ausente", async () => {
    const { error, ports } = await refused("/no/existe.zip", {});
    expect(error).toBeInstanceOf(ImagePublishError);
    expect(error.message).toContain("no existe");
    expect(ports.calls).toEqual([]);
  });

  it("un zip full publicado como slim, y al revés", async () => {
    const files = workspace();
    const slimOnFull = await refused(files.zip("full"), { variant: "slim" });
    expect(slimOnFull.error.message).toContain("es un artefacto full pero se pasó --variant slim");
    expect(slimOnFull.ports.calls).toEqual([]);
    const fullOnSlim = await refused(files.zip("slim", false, "slim.zip"), { variant: "full" });
    expect(fullOnSlim.error.message).toContain("es un artefacto slim");
  });

  it("un artefacto con el marcador de EFS exige --with-efs, y --with-efs exige el marcador", async () => {
    const files = workspace();
    const withoutFlag = await refused(files.zip("full", true), {});
    expect(withoutFlag.error.message).toContain("publícalo con --with-efs");
    const withoutMarker = await refused(files.zip("full", false, "plain.zip"), {
      withEfs: true,
      osCapabilities: "ALL",
    });
    expect(withoutMarker.error.message).toContain("no lleva el marcador de amazon-efs-utils");
    expect(withoutMarker.ports.calls).toEqual([]);
  });

  it("--with-efs sin --os-capabilities ALL se rechaza", async () => {
    const files = workspace();
    const outcome = await refused(files.zip("full", true), { withEfs: true });
    expect(outcome.error.message).toContain("--with-efs necesita --os-capabilities ALL");
    expect(outcome.ports.calls).toEqual([]);
  });

  it("un artefacto EFS con --with-efs y capabilities se publica", async () => {
    const files = workspace();
    const { deps } = harness();
    const summary = await publish(
      deps,
      settingsFor(files.zip("full", true), {
        withEfs: true,
        osCapabilities: "ALL",
        imageName: "rayito-base-caps-efs",
      }),
    );
    expect(summary.launchable).toBe(true);
  });
});

describe("--sizes", () => {
  it("publica el baseline y una imagen por tamaño desde el mismo artefacto", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps } = harness();
    const outcome = await publishWithSizes(deps, settingsFor(artifact), ["512mb", "4gb"]);
    expect(outcome.failed).toBe(false);
    expect(Object.keys(outcome.sized)).toEqual(["512mb", "4gb"]);
    expect(outcome.document).toMatchObject({
      imageArn: imageArnOf("rayito-base"),
      sizes: {
        "512mb": { imageArn: imageArnOf("rayito-base-512mb") },
        "4gb": { imageArn: imageArnOf("rayito-base-4gb") },
      },
    });
    const sizedRequest = ports.requests.find(
      (request) =>
        (request.body.logging as { cloudWatch: { logGroup: string } }).cloudWatch.logGroup ===
        "/rayito/rayito-base-4gb",
    );
    expect(sizedRequest?.body).toMatchObject({
      resources: [{ minimumMemoryInMiB: 4096 }],
      environmentVariables: { [BASELINE_MEMORY_ENV_VAR]: "4096" },
    });
    expect(ports.calls.filter((call) => call.startsWith("putObject"))).toHaveLength(1);
  });

  it("si el baseline no queda lanzable no publica ningún tamaño", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.failNextBuild = true;
    const { deps } = harness(ports);
    const outcome = await publishWithSizes(deps, settingsFor(files.zip()), ["512mb"]);
    expect(outcome.failed).toBe(true);
    expect(outcome.sized).toEqual({});
    expect(outcome.document).not.toHaveProperty("sizes");
    expect(ports.requests).toHaveLength(1);
  });

  it("un tamaño fallido marca el conjunto como fallido pero informa a todos", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const { deps } = harness(ports);
    await publish(deps, settingsFor(files.zip()));
    ports.failNextBuild = true;
    const outcome = await publishWithSizes(deps, settingsFor(files.zip()), ["512mb", "1gb"]);
    expect(outcome.failed).toBe(true);
    expect(Object.keys(outcome.sized)).toEqual(["512mb", "1gb"]);
    expect(outcome.sized["512mb"]?.launchable).toBe(false);
    expect(outcome.sized["1gb"]?.launchable).toBe(true);
  });

  it("reutiliza los tamaños ya publicados sin volver a construir", async () => {
    const files = workspace();
    const artifact = files.zip();
    const { ports, deps } = harness();
    await publishWithSizes(deps, settingsFor(artifact), ["512mb"]);
    const before = ports.requests.length;
    const outcome = await publishWithSizes(deps, settingsFor(artifact), ["512mb"]);
    expect(ports.requests).toHaveLength(before);
    expect(outcome.failed).toBe(false);
    expect(outcome.sized["512mb"]).toMatchObject({ buildSeconds: null });
  });
});
