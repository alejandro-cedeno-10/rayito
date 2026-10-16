/**
 * `rayito image publish | prune | zip` de punta a punta con la CLI: validación
 * de opciones antes de tocar AWS (salida 2), el documento por stdout y el
 * progreso por stderr con `--json`, las líneas `RAYITO_TEMPLATE=` y los
 * códigos de salida. Los puertos de AWS son los dobles en memoria.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { buildWorld } from "./case-world.js";
import {
  BUCKET,
  FakePrunePorts,
  FakePublishPorts,
  ImageWorkspace,
  imageArnOf,
  versionOf,
} from "./image-fakes.js";
import { runWithServices } from "./run-case.js";

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

interface Run {
  readonly publishPorts?: FakePublishPorts;
  readonly prunePorts?: FakePrunePorts;
  readonly env?: Record<string, string>;
}

async function invoke(args: string[], options: Run = {}) {
  const built = buildWorld({});
  const services = {
    ...built.services,
    ...(options.publishPorts === undefined ? {} : { publishPorts: options.publishPorts }),
    ...(options.prunePorts === undefined ? {} : { prunePorts: options.prunePorts }),
  };
  return runWithServices({ args, env: options.env, services });
}

function publishArgs(artifact: string, ...extra: string[]): string[] {
  return [
    "image",
    "publish",
    "--artifact",
    artifact,
    "--base-image-version",
    "1",
    "--bucket",
    BUCKET,
    "--build-role-arn",
    "arn:aws:iam::123456789012:role/build",
    ...extra,
  ];
}

describe("image publish", () => {
  it("imprime el resumen y la línea RAYITO_TEMPLATE", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const outcome = await invoke(publishArgs(files.zip()), { publishPorts: ports });
    expect(outcome.exitCode).toBe(0);
    const start = outcome.stdout.indexOf("{\n");
    const end = outcome.stdout.lastIndexOf("\n}\n") + "\n}".length;
    const line = outcome.stdout.slice(end + 1);
    expect(JSON.parse(outcome.stdout.slice(start, end))).toMatchObject({
      imageArn: imageArnOf("rayito-base"),
      imageVersion: "1.0",
      launchable: true,
    });
    expect(line).toBe(`RAYITO_TEMPLATE=${imageArnOf("rayito-base")}\n`);
    expect(outcome.stderr).toBe("");
    expect(outcome.stdout).toContain("subido s3://");
  });

  it("--json deja en stdout sólo el documento y manda el progreso a stderr", async () => {
    const files = workspace();
    const outcome = await invoke(["--json", ...publishArgs(files.zip())], {
      publishPorts: new FakePublishPorts(),
    });
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toMatchObject({ imageVersion: "1.0" });
    expect(outcome.stdout).not.toContain("RAYITO_TEMPLATE");
    expect(outcome.stderr).toContain("create-microvm-image aceptado: versión 1.0");
  });

  it("un build fallido sale con 1 y no imprime RAYITO_TEMPLATE (con --json, sí el documento)", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    ports.failNextBuild = true;
    const text = await invoke(publishArgs(files.zip()), { publishPorts: ports });
    expect(text.exitCode).toBe(1);
    expect(text.stdout).not.toContain("RAYITO_TEMPLATE");
    const failing = new FakePublishPorts();
    failing.failNextBuild = true;
    const json = await invoke(["--json", ...publishArgs(files.zip())], { publishPorts: failing });
    expect(json.exitCode).toBe(1);
    expect(JSON.parse(json.stdout)).toMatchObject({ launchable: false });
  });

  it("--sizes agrupa todo: un RAYITO_TEMPLATE y un RAYITO_TEMPLATE_<SIZE> por tamaño", async () => {
    const files = workspace();
    const outcome = await invoke(publishArgs(files.zip(), "--sizes", "512mb,4gb"), {
      publishPorts: new FakePublishPorts(),
    });
    expect(outcome.exitCode).toBe(0);
    const lines = outcome.stdout.split("\n").filter((line) => line.startsWith("RAYITO_TEMPLATE"));
    expect(lines).toEqual([
      `RAYITO_TEMPLATE=${imageArnOf("rayito-base")}`,
      `RAYITO_TEMPLATE_512MB=${imageArnOf("rayito-base-512mb")}`,
      `RAYITO_TEMPLATE_4GB=${imageArnOf("rayito-base-4gb")}`,
    ]);
    expect(outcome.stdout).not.toContain('"imageArn"');
  });

  it("--sizes --json es un único documento con `sizes`", async () => {
    const files = workspace();
    const outcome = await invoke(["--json", ...publishArgs(files.zip(), "--sizes", "4gb")], {
      publishPorts: new FakePublishPorts(),
    });
    expect(JSON.parse(outcome.stdout)).toMatchObject({
      imageArn: imageArnOf("rayito-base"),
      sizes: { "4gb": { imageArn: imageArnOf("rayito-base-4gb") } },
    });
  });

  it("--sizes con un tamaño fallido sale con 1 sin líneas RAYITO_TEMPLATE", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const artifact = files.zip();
    await invoke(publishArgs(artifact), { publishPorts: ports });
    ports.failNextBuild = true;
    const outcome = await invoke(publishArgs(artifact, "--sizes", "512mb"), {
      publishPorts: ports,
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stdout).not.toContain("RAYITO_TEMPLATE");
  });

  it("el nombre por defecto sigue a la variante y a --with-efs, y --image-name lo sustituye", async () => {
    const files = workspace();
    const slim = new FakePublishPorts();
    await invoke(publishArgs(files.zip("slim", false, "slim.zip"), "--variant", "slim"), {
      publishPorts: slim,
    });
    expect([...slim.images.keys()]).toEqual([imageArnOf("rayito-base-slim")]);
    const efs = new FakePublishPorts();
    await invoke(
      publishArgs(files.zip("full", true, "efs.zip"), "--with-efs", "--os-capabilities", "ALL"),
      { publishPorts: efs },
    );
    expect([...efs.images.keys()]).toEqual([imageArnOf("rayito-base-caps-efs")]);
    const named = new FakePublishPorts();
    await invoke(publishArgs(files.zip(), "--image-name", "mi-imagen"), { publishPorts: named });
    expect([...named.images.keys()]).toEqual([imageArnOf("mi-imagen")]);
  });

  it("--bucket sale de RAYITO_BUCKET y falta en ambos es un error de uso", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const args = [
      "image",
      "publish",
      "--artifact",
      files.zip(),
      "--base-image-version",
      "1",
      "--build-role-arn",
      "arn:aws:iam::123456789012:role/build",
    ];
    const fromEnv = await invoke(args, {
      publishPorts: ports,
      env: { RAYITO_BUCKET: "desde-env" },
    });
    expect(fromEnv.exitCode).toBe(0);
    expect(ports.calls.some((call) => call.startsWith("putObject:desde-env/"))).toBe(true);
    const missing = new FakePublishPorts();
    const refused = await invoke(args, { publishPorts: missing });
    expect(refused.exitCode).toBe(2);
    expect(refused.stderr).toContain("--bucket: pasa --bucket o exporta RAYITO_BUCKET");
    expect(missing.calls).toEqual([]);
  });

  it.each([
    ["--variant", "bogus", "--variant: admitidas: full, slim, poly"],
    ["--os-capabilities", "NONE", "--os-capabilities: admitidas: ALL"],
    ["--sizes", "2gb", "--sizes: '2gb' ya es el baseline"],
    ["--sizes", "bogus", "--sizes: 'bogus': admitidos"],
    ["--env", "sin-igual", "--env: formato K=V esperado: 'sin-igual'"],
    ["--env", "1A=x", "--env: '1A': nombre de variable inválido"],
    ["--artifact-run-id", "a/b", "--artifact-run-id inválido"],
  ])("%s %s es un error de uso y no toca AWS", async (flagName, value, message) => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const outcome = await invoke(publishArgs(files.zip(), flagName, value), {
      publishPorts: ports,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain(message);
    expect(ports.calls).toEqual([]);
  });

  it("--sizes con otra memoria que la del baseline es un error de uso", async () => {
    const files = workspace();
    const outcome = await invoke(
      publishArgs(files.zip(), "--sizes", "4gb", "--memory-mib", "4096"),
      {
        publishPorts: new FakePublishPorts(),
      },
    );
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain(
      "--memory-mib: --sizes publica el baseline sin sufijo a 2048 MiB",
    );
  });

  it("--with-efs sin la variante full o sin capabilities es un error de uso", async () => {
    const files = workspace();
    const slim = await invoke(
      publishArgs(
        files.zip("slim", true, "a.zip"),
        "--with-efs",
        "--variant",
        "slim",
        "--os-capabilities",
        "ALL",
      ),
      { publishPorts: new FakePublishPorts() },
    );
    expect(slim.exitCode).toBe(2);
    expect(slim.stderr).toContain("--with-efs: --with-efs sólo con --variant full");
    const noCaps = await invoke(publishArgs(files.zip("full", true, "b.zip"), "--with-efs"), {
      publishPorts: new FakePublishPorts(),
    });
    expect(noCaps.exitCode).toBe(2);
    expect(noCaps.stderr).toContain("--with-efs necesita --os-capabilities ALL");
  });

  it("un artefacto de otra variante sale con 1 antes de llamar a AWS", async () => {
    const files = workspace();
    const ports = new FakePublishPorts();
    const outcome = await invoke(publishArgs(files.zip("full"), "--variant", "slim"), {
      publishPorts: ports,
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("es un artefacto full pero se pasó --variant slim");
    expect(ports.calls).toEqual([]);
  });

  it("--artifact y --base-image-version son obligatorios (salida 2)", async () => {
    const outcome = await invoke(["image", "publish"], { publishPorts: new FakePublishPorts() });
    expect(outcome.exitCode).toBe(2);
  });
});

describe("image prune", () => {
  const ports = (): FakePrunePorts => {
    const fake = new FakePrunePorts(Array.from({ length: 8 }, (_, index) => versionOf(index + 1)));
    fake.live = new Set(["2.0"]);
    return fake;
  };

  it("--dry-run: el documento por stdout, la tabla por stderr con --json", async () => {
    const fake = ports();
    const outcome = await invoke(["--json", "image", "prune", "--keep", "3", "--dry-run"], {
      prunePorts: fake,
    });
    expect(outcome.exitCode).toBe(0);
    const summary = JSON.parse(outcome.stdout) as Record<string, unknown>;
    expect(summary).toMatchObject({
      kept: ["8.0", "7.0", "6.0", "2.0"],
      planned: ["1.0", "3.0", "4.0", "5.0"],
      dryRun: true,
    });
    expect(outcome.stderr).toContain("keep (live microvm)");
    expect(fake.calls.some((call) => call.startsWith("deleteVersion"))).toBe(false);
  });

  it("sin --json la tabla va a stdout y el documento también", async () => {
    const outcome = await invoke(["image", "prune", "--keep", "3", "--dry-run"], {
      prunePorts: ports(),
    });
    expect(outcome.stdout).toContain("version  state");
    expect(outcome.stdout).toContain('"dryRun": true');
    expect(outcome.stderr).toBe("");
  });

  it("borra de verdad y sale con 0", async () => {
    const fake = new FakePrunePorts([versionOf(3), versionOf(2), versionOf(1)]);
    const outcome = await invoke(["--json", "image", "prune", "--keep", "2"], { prunePorts: fake });
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toMatchObject({ deleted: ["1.0"], dryRun: false });
  });

  it("un rechazo sale con 1", async () => {
    const fake = new FakePrunePorts([versionOf(2), versionOf(1)]);
    fake.refusals = [{ code: "AccessDeniedException", message: "nope" }];
    const outcome = await invoke(["--json", "image", "prune", "--keep", "1"], { prunePorts: fake });
    expect(outcome.exitCode).toBe(1);
    expect(JSON.parse(outcome.stdout)).toMatchObject({ deleted: [] });
  });

  it("--keep por debajo de uno es un error de uso sin tocar AWS", async () => {
    const fake = ports();
    const outcome = await invoke(["image", "prune", "--keep", "0"], { prunePorts: fake });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--keep");
    expect(fake.calls).toEqual([]);
  });

  it("--image-name elige la imagen", async () => {
    const fake = ports();
    await invoke(["image", "prune", "--image-name", "otra", "--dry-run"], { prunePorts: fake });
    const outcome = await invoke(
      ["--json", "image", "prune", "--image-name", "otra", "--dry-run"],
      {
        prunePorts: fake,
      },
    );
    expect(JSON.parse(outcome.stdout)).toMatchObject({ imageArn: imageArnOf("otra") });
  });
});

describe("image zip", () => {
  it("imprime la línea con los ficheros, los bytes y el sha256", async () => {
    const files = workspace();
    const destination = join(files.root, "out.zip");
    const outcome = await invoke(["image", "zip", files.imageDir, destination]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toMatch(
      new RegExp(
        `^${destination.replaceAll("/", "\\/")}: 5 files, \\d+ bytes \\(variant full\\) sha256=[0-9a-f]{64}\\n$`,
      ),
    );
    expect(readFileSync(destination).length).toBeGreaterThan(0);
  });

  it("--json es el documento con las mismas claves que Python", async () => {
    const files = workspace();
    const destination = join(files.root, "out.zip");
    const outcome = await invoke([
      "--json",
      "image",
      "zip",
      files.imageDir,
      destination,
      "--variant",
      "poly",
      "--with-efs",
    ]);
    const document = JSON.parse(outcome.stdout) as Record<string, unknown>;
    expect(Object.keys(document)).toEqual([
      "destination",
      "files",
      "bytes",
      "variant",
      "withEfs",
      "sha256",
      "sidecarFiles",
      "seconds",
    ]);
    expect(document).toMatchObject({
      files: 7,
      variant: "poly",
      withEfs: true,
      sidecarFiles: null,
    });
  });

  it("--sidecar copia primero el kernel-sidecar y lo cuenta", async () => {
    const files = workspace();
    const sidecar = join(files.root, "sidecar");
    for (const name of ["requirements.txt", "requirements-poly.txt", "sidecar.py"]) {
      files.write(`../sidecar/${name}`, `${name}\n`);
    }
    const destination = join(files.root, "out.zip");
    const outcome = await invoke([
      "image",
      "zip",
      files.imageDir,
      destination,
      "--sidecar",
      sidecar,
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(`${files.imageDir}/kernel-sidecar: 3 files\n`);
    expect(outcome.stdout).toContain("8 files");
  });

  it("una variante desconocida es un error de uso", async () => {
    const files = workspace();
    const outcome = await invoke([
      "image",
      "zip",
      files.imageDir,
      join(files.root, "o.zip"),
      "--variant",
      "bogus",
    ]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--variant: admitidas: full, slim, poly");
  });

  it("un directorio sin Dockerfile sale con 1", async () => {
    const files = workspace();
    const outcome = await invoke([
      "image",
      "zip",
      join(files.root, "vacio"),
      join(files.root, "o.zip"),
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("falta Dockerfile");
  });

  it("acepta --artifact-run-id sin efecto, por paridad con publish", async () => {
    const files = workspace();
    const outcome = await invoke([
      "image",
      "zip",
      files.imageDir,
      join(files.root, "o.zip"),
      "--artifact-run-id",
      "run-1",
    ]);
    expect(outcome.exitCode).toBe(0);
  });
});
