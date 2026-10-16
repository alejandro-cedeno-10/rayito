/**
 * La detección (`ai-agent-detection`) sobre los quince agentes de ejemplo de
 * `testdata/agent-samples/`: cada uno se decide sin ninguna opción, igual que
 * lo dice el manifiesto que comparten los dos SDK. Sin AWS ni red: sólo se
 * leen ficheros.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { detect } from "../../src/agent/detect-domain.js";
import { LocalProjectTree } from "../../src/agent/detect-fs.js";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-samples");
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
