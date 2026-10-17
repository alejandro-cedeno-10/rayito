/**
 * `AgentProject` (ADR-028, `ai-agent-project`): la descripción tipada, el
 * fichero `rayito-agent.toml` / `.json` y sus vectores compartidos con Python
 * (`testdata/agent-project/`). Todo es puro: ninguna prueba crea un cliente
 * de AWS.
 */

import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parse as parseToml } from "smol-toml";
import { describe, expect, test } from "vitest";
import { AgentProjectError, RuntimeVersions } from "../../src/agent/project.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import {
  AgentLimits,
  AgentModel,
  AgentProject,
  AgentSpec,
  anthropicGateway,
  InvalidArgumentError,
  SecretGateway,
  ServeOptions,
  UnimplementedError,
} from "../../src/index.js";
import { AGENT_PROJECT_FILE_NAMES, MAX_DURATION_SECONDS } from "../../src/limits.js";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-project");

interface InvalidCase {
  readonly case: string;
  readonly file: string;
  readonly path: string;
  readonly code: string;
  readonly schema: "rejects" | "semantic";
}

const valid = (await readdir(join(ROOT, "valid")))
  .filter((name) => name.endsWith(".toml"))
  .sort()
  .map((name) => name.slice(0, -".toml".length));
const invalid = (
  JSON.parse(await readFile(join(ROOT, "invalid", "cases.json"), "utf8")) as {
    cases: InvalidCase[];
  }
).cases;
const validator = new Ajv2020({ strict: false }).compile(
  JSON.parse(await readFile(join(ROOT, "schema.json"), "utf8")),
);

function spec(gateway = "anthropic"): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({ provider: "anthropic", id: "claude-haiku-4-5", gateway }),
  });
}

function project(overrides: Record<string, unknown> = {}): AgentProject {
  return new AgentProject({
    name: "demo",
    runtime: "opencode",
    spec: spec(),
    gateways: { anthropic: anthropicGateway("rayito/anthropic-key") },
    ...overrides,
  });
}

async function expectProjectError(
  action: () => Promise<unknown> | unknown,
): Promise<AgentProjectError> {
  try {
    await action();
  } catch (error) {
    expect(error).toBeInstanceOf(AgentProjectError);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    return error as AgentProjectError;
  }
  throw new Error("no lanzó");
}

describe("shared vectors", () => {
  test.each(valid)("%s has the expected canonical form", async (name) => {
    const expected = JSON.parse(
      await readFile(join(ROOT, "valid", `${name}.expected.json`), "utf8"),
    );
    const loaded = await AgentProject.load(join(ROOT, "valid", `${name}.toml`));
    expect(loaded.toObject()).toEqual(expected);
  });

  test.each(valid)("%s round-trips through fromObject and JSON", async (name) => {
    const loaded = await AgentProject.load(join(ROOT, "valid", `${name}.toml`));
    const canonical = loaded.toObject();
    expect(AgentProject.fromObject(canonical, { baseDir: loaded.baseDir }).toObject()).toEqual(
      canonical,
    );
    const dir = await mkdtemp(join(tmpdir(), "rayito-project-"));
    try {
      await writeFile(join(dir, "rayito-agent.json"), JSON.stringify(canonical));
      expect((await AgentProject.load(dir)).toObject()).toEqual(canonical);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test.each(valid)("the schema accepts %s", async (name) => {
    const document = parseToml(await readFile(join(ROOT, "valid", `${name}.toml`), "utf8"));
    expect(validator(document), JSON.stringify(validator.errors)).toBe(true);
  });

  test.each(invalid.map((c) => [c.case, c] as const))(
    "%s fails with its path and code",
    async (_n, c) => {
      const error = await expectProjectError(() =>
        AgentProject.load(join(ROOT, "invalid", c.file)),
      );
      expect(error.path).toBe(c.path);
      expect(error.code).toBe(c.code);
      expect(error.message).toContain(c.path);
    },
  );

  test.each(invalid.map((c) => [c.case, c] as const))("the schema judges %s", async (_n, c) => {
    const document = parseToml(await readFile(join(ROOT, "invalid", c.file), "utf8"));
    expect(validator(document) === false).toBe(c.schema === "rejects");
  });
});

describe("loading", () => {
  test("a directory prefers toml over json", async () => {
    expect([...AGENT_PROJECT_FILE_NAMES]).toEqual(["rayito-agent.toml", "rayito-agent.json"]);
    const dir = await mkdtemp(join(tmpdir(), "rayito-project-"));
    try {
      await writeFile(
        join(dir, "rayito-agent.toml"),
        await readFile(join(ROOT, "valid", "minimal.toml")),
      );
      await writeFile(join(dir, "rayito-agent.json"), "{}");
      expect((await AgentProject.load(dir)).name).toBe("hello-agent");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("a file path loads and a missing one is file_not_found", async () => {
    expect((await AgentProject.load(join(ROOT, "valid", "minimal.toml"))).name).toBe("hello-agent");
    const dir = await mkdtemp(join(tmpdir(), "rayito-project-"));
    try {
      expect((await expectProjectError(() => AgentProject.load(dir))).code).toBe("file_not_found");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test.each([
    ["a.toml", "name = ["],
    ["b.json", "[1, 2]"],
  ])("an unreadable document %s is an invalid value", async (name, text) => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-project-"));
    try {
      await writeFile(join(dir, name), text);
      expect((await expectProjectError(() => AgentProject.load(join(dir, name)))).code).toBe(
        "invalid_value",
      );
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("the error of a bad document never carries its values", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-project-"));
    try {
      await writeFile(join(dir, "rayito-agent.toml"), 'name = "hunter2-secret"\nnope = [');
      const error = await expectProjectError(() => AgentProject.load(dir));
      expect(error.message).not.toContain("hunter2");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  const document = {
    schema: "rayito.agent-project/1",
    name: "demo",
    runtime: { kind: "deepagents" },
    model: { provider: "anthropic", id: "claude-haiku-4-5", gateway: "a" },
    gateways: { a: { preset: "anthropic", secret: "rayito/key" } },
  };

  test("without a file reader instructions_file is an error", async () => {
    const error = await expectProjectError(() =>
      AgentProject.fromObject(
        { ...document, agent: { instructions_file: "AGENTS.md" } },
        { baseDir: "/proj" },
      ),
    );
    expect(error.path).toBe("agent.instructions_file");
  });

  test("fromObject resolves paths without touching the disk", () => {
    const loaded = AgentProject.fromObject(
      {
        ...document,
        source: { path: "code", requirements: "code/requirements.txt" },
      },
      { baseDir: "/nonexistent/project" },
    );
    expect(loaded.source).toBe("/nonexistent/project/code");
    expect(loaded.requirements).toBe("/nonexistent/project/code/requirements.txt");
    expect(loaded.toObject().source).toEqual({
      path: "code",
      requirements: "code/requirements.txt",
    });
  });
});

describe("construction", () => {
  test("validates before calling anyone", () => {
    expect(() => project({ name: "Not A Name" })).toThrow(InvalidArgumentError);
    expect(() => project({ gateways: {} })).toThrow(/pasarela/);
    expect(() => project({ memoryMib: 1024 })).toThrow(InvalidArgumentError);
    expect(() => project({ spec: "no es un AgentSpec" })).toThrow(InvalidArgumentError);
    expect(() => project({ allowInternetAccess: "si" })).toThrow(InvalidArgumentError);
    expect(() => project({ gateways: { anthropic: "no es una pasarela" } })).toThrow(
      InvalidArgumentError,
    );
    expect(() => project({ runtime: "crewai" })).toThrow(UnimplementedError);
    expect(() => project({ runtime: "custom" })).toThrow(/CustomRuntime/);
  });

  test("defaults come from the platform constants", () => {
    const built = project();
    expect(built.serve).toBeInstanceOf(ServeOptions);
    expect(built.limits).toBeInstanceOf(AgentLimits);
    expect(built.allowInternetAccess).toBe(false);
    expect(built.egress).toBeUndefined();
    expect(built.toObject().runtime).toEqual({ kind: "opencode" });
    expect(Object.keys(built.toObject()).sort()).toEqual(
      ["gateways", "model", "name", "runtime", "schema"].sort(),
    );
  });

  test.each([8080, 9000, 0, 70000, "8088" as never])("serve port %s is rejected", (port) => {
    expect(() => new ServeOptions({ port })).toThrow(InvalidArgumentError);
  });

  test.each([0, -1, (MAX_DURATION_SECONDS + 1) * 1000])(
    "serve timeout %s is capped at the platform maximum",
    (timeoutMs) => {
      expect(() => new ServeOptions({ timeoutMs })).toThrow(InvalidArgumentError);
    },
  );

  test("serve options are frozen and copy their metadata", () => {
    const metadata = { team: "support" };
    const serve = new ServeOptions({ metadata });
    metadata.team = "other";
    expect(serve.metadata).toEqual({ team: "support" });
    expect(Object.isFrozen(serve.metadata)).toBe(true);
  });

  test("gateways are secret-free in the canonical form", () => {
    const built = project({ gateways: { anthropic: anthropicGateway("rayito/anthropic-key") } });
    expect(built.gateways.anthropic).toBeInstanceOf(SecretGateway);
    const text = JSON.stringify(built.toObject());
    expect(text).toContain("rayito/anthropic-key");
    expect(text).not.toContain("Bearer");
  });

  test("loading and writing make no AWS call", async () => {
    const calls: string[] = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (() => {
      calls.push("fetch");
      throw new Error("fetch");
    }) as typeof fetch;
    try {
      for (const name of valid) {
        (await AgentProject.load(join(ROOT, "valid", `${name}.toml`))).toObject();
      }
      project().toObject();
    } finally {
      globalThis.fetch = originalFetch;
    }
    expect(calls).toEqual([]);
  });
});

describe("fleet options", () => {
  test("a project defaults to one VM without extras", () => {
    const built = project();
    expect(built.scaling).toEqual(new ScalingOptions());
    expect(built.env).toEqual({});
    expect(built.network).toBeUndefined();
    expect([built.region, built.bucket, built.baseImageVersion]).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
    expect([built.language, built.framework]).toEqual([undefined, undefined]);
    expect(built.versions.isDefault).toBe(true);
  });

  test("they survive the canonical form", () => {
    const built = project({
      scaling: { minVms: 1, maxVms: 3, warmVms: 1, sessions: "dedicated" },
      env: { LOG_LEVEL: "info" },
      network: { allowOut: ["example.com"], denyOut: ["0.0.0.0/0"] },
      region: "us-east-1",
      bucket: "amzn-s3-demo-bucket",
      baseImageVersion: "latest",
      language: "python",
      framework: "langgraph",
      versions: { python: "3.13" },
      serve: new ServeOptions({ queueMax: 4, queueTimeoutMs: 12_000 }),
    });
    const again = AgentProject.fromObject(built.toObject(), { baseDir: built.baseDir });
    expect(again.toObject()).toEqual(built.toObject());
    expect(again.scaling).toEqual(built.scaling);
    expect(again.network).toEqual(built.network);
    expect(again.versions).toEqual(built.versions);
    expect(again.serve.queueMax).toBe(4);
  });

  test.each([
    { scaling: "dedicated" },
    { versions: { python: "3.12.4" } },
    { env: { API_KEY: "x" } },
    { env: { GITHUB_TOKEN: "x" } },
    { env: { GREETING: "Bearer demo" } },
    { env: { "1BAD": "x" } },
    { env: { COUNT: 3 } },
    { network: { allowOut: [""] } },
    { language: "go" },
    { region: "" },
    { bucket: "has space" },
    { baseImageVersion: "" },
    { serve: new ServeOptions({ maxConcurrency: 16 }) },
  ])("invalid %j fails before calling anyone", (overrides) => {
    expect(() => project(overrides)).toThrow(InvalidArgumentError);
  });

  test("the session cap grows with the memory of the VM", () => {
    expect(
      project({ serve: new ServeOptions({ maxConcurrency: 15 }), memoryMib: 4096 }).memoryMib,
    ).toBe(4096);
    expect(() => project({ serve: new ServeOptions({ maxConcurrency: 8 }) })).toThrow(
      InvalidArgumentError,
    );
  });

  test("RuntimeVersions only accepts a pinned build", () => {
    for (const version of ["2.7", "3", "3.12.4"]) {
      expect(() => new RuntimeVersions({ python: version })).toThrow(InvalidArgumentError);
    }
  });

  test("the env of a project is copied and frozen", () => {
    const env = { LOG_LEVEL: "info" };
    const built = project({ env });
    env.LOG_LEVEL = "debug";
    expect(built.env.LOG_LEVEL).toBe("info");
    expect(Object.isFrozen(built.env)).toBe(true);
  });
});
