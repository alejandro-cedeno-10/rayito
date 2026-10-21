/**
 * El puerto `ProjectReader` (ADR-032 D7): la ruta síncrona de los
 * constructos CDK (`planAgentCoreSync`, `renderExportSync`,
 * `detectProjectSync`, `collectContextFilesSync`) da exactamente lo mismo
 * que la asíncrona del SDK, con las mismas reglas y los mismos errores,
 * porque las dos ejecutan los mismos pasos.
 */

import { cpSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { renderExport, renderExportSync } from "../../src/agent/export.js";
import { detectProject, detectProjectSync } from "../../src/agent/project-file.js";
import { runWithSyncReader, type SyncProjectReader } from "../../src/agent/project-reader.js";
import { planAgentCoreSync, planAgentCoreWith } from "../../src/agentcore/facade.js";
import { BuildError } from "../../src/errors.js";
import { collectContextFiles, collectContextFilesSync } from "../../src/templates/context-node.js";
import { TESTDATA_ROOT } from "./cdk-contract-harness.js";

const SAMPLES = [
  join("python", "strands"),
  join("python", "flask-custom"),
  join("typescript", "vercel-ai-sdk"),
];
const REGION = "us-east-1";
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-reader-"));
  scratches.push(directory);
  return directory;
}

describe("ProjectReader: la ruta síncrona es la asíncrona", () => {
  for (const sample of SAMPLES) {
    it(`planAgentCoreSync de ${sample} es planAgentCoreWith`, async () => {
      const path = join(TESTDATA_ROOT, "agent-samples", sample);
      const args = { target: "agentcore" as const, region: REGION };
      const asynchronous = await planAgentCoreWith({ environ: {} }, path, args);
      const synchronous = planAgentCoreSync(path, args, { environ: {} });
      expect(synchronous.plan).toEqual(asynchronous.plan);
      expect([...synchronous.tree.entries()]).toEqual([...asynchronous.tree.entries()]);
      expect(synchronous.report).toEqual(asynchronous.report);
      const [project] = detectProjectSync(path, undefined, { region: REGION });
      expect(renderExportSync(project, "agentcore")).toEqual(
        await renderExport(project, "agentcore"),
      );
    });
  }

  it("un rayito-agent.toml se lee igual con el lector síncrono", async () => {
    const directory = scratch();
    cpSync(join(TESTDATA_ROOT, "agent-samples", "python", "strands"), directory, {
      recursive: true,
    });
    writeFileSync(
      join(directory, "rayito-agent.toml"),
      'schema = "rayito.agent-project/1"\nname = "toml-agent"\n',
    );
    const [asynchronous] = await detectProject(directory, undefined, { region: REGION });
    const [synchronous] = detectProjectSync(directory, undefined, { region: REGION });
    expect(synchronous.name).toBe("toml-agent");
    expect(synchronous.toObject()).toEqual(asynchronous.toObject());
  });

  it("un error de un paso llega igual a las dos rutas", async () => {
    const missing = join(scratch(), "nada");
    const asynchronous = await detectProject(missing).catch((error: unknown) => error);
    const synchronous = (() => {
      try {
        detectProjectSync(missing);
        return undefined;
      } catch (error) {
        return error;
      }
    })();
    expect(synchronous).toEqual(asynchronous);
    expect((synchronous as { code?: string }).code).toBe("file_not_found");
  });

  it("el error de una lectura vuelve al paso que la pidió", () => {
    const failing: SyncProjectReader = {
      readText: () => {
        throw new Error("sin disco");
      },
      tomlParser: () => ({ parse: () => ({}) }),
      appFiles: () => [],
    };
    function* steps() {
      try {
        yield { kind: "text" as const, file: "x" };
        return "leído";
      } catch (error) {
        return `capturado: ${(error as Error).message}`;
      }
    }
    expect(runWithSyncReader(steps(), failing)).toBe("capturado: sin disco");
  });
});

describe("collectContextFilesSync", () => {
  it("da los mismos ficheros que collectContextFiles", async () => {
    const root = join(TESTDATA_ROOT, "agent-samples", "python", "strands");
    const copies = [{ kind: "copy" as const, src: ".", dst: "/app" }];
    expect(collectContextFilesSync(root, copies, ["uv.lock"])).toEqual(
      await collectContextFiles(root, copies, ["uv.lock"]),
    );
  });

  it("rechaza igual un enlace que sale del contexto", async () => {
    const root = scratch();
    const outside = scratch();
    writeFileSync(join(outside, "secret"), "x");
    mkdirSync(join(root, "app"));
    symlinkSync(outside, join(root, "app", "link"));
    const copies = [{ kind: "copy" as const, src: "app/link", dst: "/x" }];
    const asynchronous = await collectContextFiles(root, copies).catch((error: unknown) => error);
    expect(asynchronous).toBeInstanceOf(BuildError);
    expect(() => collectContextFilesSync(root, copies)).toThrow(
      (asynchronous as BuildError).message,
    );
  });
});
