/**
 * El contexto de Docker que se exporta de cada agente de ejemplo
 * (`testdata/agent-samples/expected/export/`): el `Dockerfile` y el `host.json`
 * son los mismos en Python y en TypeScript. Los ficheros del host y el README
 * cambian con cada versión y los cubre `testdata/agent/export/`.
 */

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, test } from "vitest";
import { AgentProject } from "../../src/agent/project.js";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-samples");
const EXPECTED = join(ROOT, "expected", "export");
const REGION = "us-east-1";
const COMPARED = ["Dockerfile", "host.json"] as const;

const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8")) as {
  samples: Array<{ id: string; dir: string }>;
};
const work = mkdtempSync(join(tmpdir(), "samples-export-"));
afterAll(() => rmSync(work, { recursive: true, force: true }));

describe("exported sample contexts", () => {
  test.each(manifest.samples)("$id matches the shared files", async (sample) => {
    const [project] = await AgentProject.detect(join(ROOT, sample.dir), undefined, {
      region: REGION,
    });
    const out = join(work, sample.id);
    await project.export({ target: "docker", outDir: out });
    for (const name of COMPARED) {
      expect(readFileSync(join(out, name), "utf8")).toBe(
        readFileSync(join(EXPECTED, sample.id, name), "utf8"),
      );
    }
  });
});
