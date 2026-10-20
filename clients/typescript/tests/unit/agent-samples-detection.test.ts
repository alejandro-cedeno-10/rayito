/**
 * La detección (`ai-agent-detection`) sobre los dieciséis agentes de ejemplo de
 * `testdata/agent-samples/`: cada uno se decide sin ninguna opción, igual que
 * lo dice el manifiesto que comparten los dos SDK. Sin AWS ni red: sólo se
 * leen ficheros.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { detect } from "../../src/agent/detect-domain.js";
import { LocalProjectTree } from "../../src/agent/detect-fs.js";
import { AgentProject, AgentProjectError } from "../../src/agent/project.js";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-samples");
const REGION = "us-east-1";
const HTTP_ALTERNATIVE_WARNING = "http_app_alternative";

interface Sample {
  readonly id: string;
  readonly language: string;
  readonly dir: string;
  readonly lockfile: string | null;
  readonly expected: {
    readonly framework: string;
    readonly kind: string;
    readonly protocol: string;
    readonly entry: string | null;
    readonly installManager: string | null;
  };
  readonly httpAlternative?: { readonly framework: string };
  readonly app?: {
    readonly health: string;
    readonly invoke: string;
    readonly requestField: string;
    readonly responseField: string | null;
  };
}

const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8")) as {
  samples: Sample[];
};
const reportOf = (sample: Sample) => detect(new LocalProjectTree(join(ROOT, sample.dir)));

describe.each(manifest.samples)("agent sample $id", (sample) => {
  test("is decided without options as the manifest says", () => {
    const report = reportOf(sample);
    expect(report.framework).toBe(sample.expected.framework);
    expect(report.kind).toBe(sample.expected.kind);
    expect(report.protocol).toBe(sample.expected.protocol);
    expect(report.entrypoint ?? null).toBe(sample.expected.entry);
    expect(report.install?.manager ?? null).toBe(sample.expected.installManager);
  });

  test("has the language of its lockfile", () => {
    const report = reportOf(sample);
    expect(report.language ?? null).toBe(sample.lockfile === null ? null : sample.language);
  });

  test("lists its HTTP app as an alternative when a framework object wins", () => {
    if (sample.httpAlternative === undefined) return;
    const report = reportOf(sample);
    expect(report.app).toBeUndefined();
    expect(report.warnings.map((warning) => warning.code)).toContain(HTTP_ALTERNATIVE_WARNING);
    expect(report.alternatives.map((alternative) => alternative.framework)).toContain(
      sample.httpAlternative.framework,
    );
  });

  test("reports the routes of a custom HTTP app", () => {
    if (sample.app === undefined) return;
    const app = reportOf(sample).app;
    expect(app?.healthPath).toBe(sample.app.health);
    expect(app?.invokePath).toBe(sample.app.invoke);
    expect(app?.requestField).toBe(sample.app.requestField);
    expect(app?.responseField ?? null).toBe(sample.app.responseField);
  });
});

describe("detected project", () => {
  test.each(manifest.samples)("$id is the shared document", async (sample) => {
    const [project] = await AgentProject.detect(join(ROOT, sample.dir), undefined, {
      region: REGION,
    });
    const expected = JSON.parse(
      readFileSync(join(ROOT, "expected", `${sample.id}.json`), "utf8"),
    ) as unknown;
    expect(project.toObject()).toEqual(expected);
  });

  test("a project without a model id names the key that fixes it", async () => {
    const dir = mkdtempSync(join(tmpdir(), "detect-"));
    try {
      writeFileSync(join(dir, "package-lock.json"), "{}");
      writeFileSync(
        join(dir, "package.json"),
        JSON.stringify({ name: "x", type: "module", dependencies: { "@strands-agents/sdk": "1" } }),
      );
      writeFileSync(
        join(dir, "agent.js"),
        'import { Agent } from "@strands-agents/sdk";\nexport const agent = new Agent({});\n',
      );
      const failure = await AgentProject.detect(dir, undefined, { region: REGION }).catch(
        (error: unknown) => error,
      );
      expect(failure).toBeInstanceOf(AgentProjectError);
      expect((failure as AgentProjectError).code).toBe("not_detected");
      expect((failure as AgentProjectError).path).toBe("model.id");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("the region is required by a Bedrock gateway", async () => {
    const previous = { region: process.env.AWS_REGION, fallback: process.env.AWS_DEFAULT_REGION };
    delete process.env.AWS_REGION;
    delete process.env.AWS_DEFAULT_REGION;
    try {
      const failure = await AgentProject.detect(join(ROOT, "python", "strands")).catch(
        (error: unknown) => error,
      );
      expect((failure as AgentProjectError).path).toBe("deploy.region");
    } finally {
      if (previous.region !== undefined) process.env.AWS_REGION = previous.region;
      if (previous.fallback !== undefined) process.env.AWS_DEFAULT_REGION = previous.fallback;
    }
  });
});
