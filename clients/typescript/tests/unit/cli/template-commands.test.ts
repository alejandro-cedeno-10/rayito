/**
 * `rayito template build | status | logs` y `rayito agent template build`:
 * las órdenes sólo parsean argumentos y delegan en `TemplateTools`; lo que
 * se comprueba es el cableado, la salida y los códigos de salida, igual que
 * `test_m15_template_cli.py`.
 */

import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SdkTemplateTools } from "../../../src/cli/ops-adapters.js";
import { TemplateSpecError } from "../../../src/cli/ops-ports.js";
import { BuildError } from "../../../src/errors.js";
import { Template } from "../../../src/templates/dsl.js";
import { buildWorld } from "./case-world.js";
import { BUILD_INFO, FakeTemplateTools } from "./ops-fakes.js";
import { runWithServices } from "./run-case.js";

const FIXTURES = join(import.meta.dirname, "fixtures");
const MS_PER_SECOND = 1000;
const DEFAULT_TIMEOUT_SECONDS = 1800;

async function invoke(tools: FakeTemplateTools, args: string[]) {
  const built = buildWorld({});
  return runWithServices({ args, services: { ...built.services, templates: tools } });
}

describe("template build", () => {
  it("imprime el log del build y los ids, y reenvía las opciones", async () => {
    const tools = new FakeTemplateTools();
    const outcome = await invoke(tools, [
      "template",
      "build",
      "spec.mjs",
      "--name",
      "mi-template",
      "--bucket",
      "mi-bucket",
      "--force",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe(
      "Step 1/2 : FROM base\nStep 2/2 : RUN pip install pandas\n" +
        `template_id=${BUILD_INFO.templateId}\nbuild_id=${BUILD_INFO.buildId}\n`,
    );
    expect(tools.calls).toEqual([
      ["loadSpec", "spec.mjs"],
      ["build", "mi-template", "mi-bucket", 2048, true, DEFAULT_TIMEOUT_SECONDS * MS_PER_SECOND],
    ]);
  });

  it("--json es un solo documento y no imprime el log", async () => {
    const outcome = await invoke(new FakeTemplateTools(), [
      "--json",
      "template",
      "build",
      "spec.mjs",
      "--name",
      "mi-template",
      "--bucket",
      "mi-bucket",
      "--memory-mb",
      "4096",
      "--timeout",
      "60",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toEqual({
      templateId: BUILD_INFO.templateId,
      buildId: BUILD_INFO.buildId,
      alias: BUILD_INFO.alias,
    });
  });

  it("un build fallido sale con 1 y lo dice", async () => {
    const tools = new FakeTemplateTools();
    tools.buildFailure = new BuildError("el build terminó en imagen=UPDATE_FAILED");
    const outcome = await invoke(tools, [
      "template",
      "build",
      "spec.mjs",
      "--name",
      "n",
      "--bucket",
      "b",
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain(
      "rayito: build fallido: el build terminó en imagen=UPDATE_FAILED",
    );
  });

  it("un spec que no existe o no exporta `template` sale con 1", async () => {
    const tools = new FakeTemplateTools();
    tools.specFailure = new TemplateSpecError("no existe: spec.mjs");
    const outcome = await invoke(tools, [
      "template",
      "build",
      "spec.mjs",
      "--name",
      "n",
      "--bucket",
      "b",
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("rayito: no existe: spec.mjs");
    expect(tools.calls).toEqual([["loadSpec", "spec.mjs"]]);
  });

  it("--name y --bucket son obligatorios (salida 2)", async () => {
    const outcome = await invoke(new FakeTemplateTools(), ["template", "build", "spec.mjs"]);
    expect(outcome.exitCode).toBe(2);
  });
});

describe("template status", () => {
  it("imprime estado, ids y error", async () => {
    const tools = new FakeTemplateTools();
    const ok = await invoke(tools, ["template", "status", "mi-template", "--version", "3.0"]);
    expect(ok.stdout).toBe(
      `state=SUCCESSFUL\ntemplate_id=${BUILD_INFO.templateId}\nbuild_id=${BUILD_INFO.buildId}\n`,
    );
    expect(tools.calls).toEqual([["status", "mi-template", "3.0"]]);
    tools.statusResult = { state: "FAILED", errorMessage: "imagen=UPDATE_FAILED" };
    const failed = await invoke(tools, ["template", "status", "mi-template"]);
    expect(failed.stdout).toBe("state=FAILED\nerror=imagen=UPDATE_FAILED\n");
    expect(tools.calls.at(-1)).toEqual(["status", "mi-template", undefined]);
  });

  it("--json lleva null donde no hay valor", async () => {
    const tools = new FakeTemplateTools();
    tools.statusResult = { state: "IN_PROGRESS" };
    const outcome = await invoke(tools, ["--json", "template", "status", "mi-template"]);
    expect(JSON.parse(outcome.stdout)).toEqual({
      state: "IN_PROGRESS",
      templateId: null,
      buildId: null,
      errorMessage: null,
    });
  });
});

describe("template logs", () => {
  it("imprime las líneas y pasa el límite", async () => {
    const tools = new FakeTemplateTools();
    const outcome = await invoke(tools, ["template", "logs", "mi-template", "--limit", "10"]);
    expect(outcome.stdout).toBe("linea 1\nlinea 2\n");
    expect(tools.calls).toEqual([["logs", "mi-template", 10]]);
    const json = await invoke(tools, ["--json", "template", "logs", "mi-template"]);
    expect(JSON.parse(json.stdout)).toEqual({ lines: ["linea 1", "linea 2"] });
    expect(tools.calls.at(-1)).toEqual(["logs", "mi-template", 500]);
  });
});

describe("agent template build", () => {
  const BaseArgs = ["agent", "template", "build", "--bucket", "mi-bucket"];

  function agentOf(tools: FakeTemplateTools): Record<string, unknown> {
    const call = tools.calls.find((candidate) => candidate[0] === "buildAgent");
    return call?.[1] as Record<string, unknown>;
  }

  it("por defecto instala los dos runtimes con precarga y sin calentar el kernel", async () => {
    const tools = new FakeTemplateTools();
    const outcome = await invoke(tools, BaseArgs);
    expect(outcome.exitCode).toBe(0);
    expect(agentOf(tools)).toEqual({
      name: "rayito-agent",
      base: "rayito-base-caps",
      runtimes: ["opencode", "deepagents"],
      prefetch: true,
      kernelWarmup: false,
      memoryMib: 2048,
    });
    expect(outcome.stdout).toContain(`template_id=${BUILD_INFO.templateId}`);
  });

  it("--no-deepagents, --no-prefetch y --kernel-warmup cambian la receta", async () => {
    const tools = new FakeTemplateTools();
    await invoke(tools, [...BaseArgs, "--no-deepagents", "--no-prefetch", "--kernel-warmup"]);
    expect(agentOf(tools)).toMatchObject({
      runtimes: ["opencode"],
      prefetch: false,
      kernelWarmup: true,
    });
  });

  it("las banderas con negación aceptan también la forma positiva", async () => {
    const tools = new FakeTemplateTools();
    await invoke(tools, [...BaseArgs, "--deepagents", "--prefetch", "--no-kernel-warmup"]);
    expect(agentOf(tools)).toMatchObject({
      runtimes: ["opencode", "deepagents"],
      prefetch: true,
      kernelWarmup: false,
    });
  });

  it("una memoria por debajo del mínimo del agente sale con 1 antes de construir", async () => {
    const tools = new FakeTemplateTools();
    const outcome = await invoke(tools, [...BaseArgs, "--memory-mb", "512"]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("memoryMib debe ser >= 2048");
    expect(tools.calls).toEqual([]);
  });

  it("un build fallido sale con 1", async () => {
    const tools = new FakeTemplateTools();
    tools.buildFailure = new BuildError("boom");
    const outcome = await invoke(tools, BaseArgs);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("rayito: build fallido: boom");
  });

  it("--json no imprime el log", async () => {
    const outcome = await invoke(new FakeTemplateTools(), ["--json", ...BaseArgs]);
    expect(JSON.parse(outcome.stdout)).toEqual(BUILD_INFO);
  });
});

describe("SdkTemplateTools.loadSpec", () => {
  const tools = new SdkTemplateTools("us-east-1", undefined);

  it("importa el módulo y devuelve su `template`", async () => {
    const loaded = await tools.loadSpec(join(FIXTURES, "template-spec.ts"));
    expect(loaded).toBeInstanceOf(Template);
    expect(loaded.spec.base?.name).toBe("rayito-base");
  });

  it("un fichero ausente es TemplateSpecError con el camino que se pasó", async () => {
    await expect(tools.loadSpec("no-existe.mjs")).rejects.toThrow(
      new TemplateSpecError("no existe: no-existe.mjs"),
    );
  });

  it("un módulo sin `template` lo dice", async () => {
    await expect(tools.loadSpec(join(FIXTURES, "template-spec-without-export.ts"))).rejects.toThrow(
      /exporta una variable `template`/,
    );
  });
});
