/**
 * `src/templates/artifact.ts` (m15-templates): los ficheros de contexto van
 * bajo `__rayito_context/`, nunca sobre una entrada del zip base (T26).
 * Espejo de `test_m15_templates_artifact.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { assembleArtifact, templateEnvToJson } from "../../src/templates/artifact.js";
import { TEMPLATE_ENV_CONTEXT_PATH } from "../../src/templates/dockerfile.js";
import { Template } from "../../src/templates/dsl.js";
import { templateEnvs } from "../../src/templates/instructions.js";
import { readZipEntries } from "../../src/templates/zip-node.js";
import { makeBaseZip } from "./m15-fake-build-clients.js";

const BASE_DOCKERFILE =
  'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n';
const encode = (text: string): Uint8Array => new TextEncoder().encode(text);
const decode = (bytes: Uint8Array | undefined): string => new TextDecoder().decode(bytes);

interface TemplateEnvCase {
  readonly name: string;
  readonly steps: readonly [string, string][];
  readonly file: string;
  readonly envs: Record<string, string>;
}

const TEMPLATE_ENV = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "..", "..", "..", "..", "testdata", "templates", "template-env.json"),
    "utf8",
  ),
) as { cases: TemplateEnvCase[] };

describe("templates/artifact", () => {
  test("a context file named like a base entry never replaces it", () => {
    const agent = new Uint8Array([0x7f, 0x45, 0x4c, 0x46]);
    const baseZip = makeBaseZip(BASE_DOCKERFILE, { rayd: agent });
    const spec = new Template().copy(".", "/srv/app/").spec;

    const entries = readZipEntries(
      assembleArtifact(baseZip, spec, [
        ["Dockerfile", encode("FROM evil")],
        ["rayd", encode("not the agent")],
      ]),
    );

    expect(entries.get("rayd")).toEqual(agent);
    expect(new TextDecoder().decode(entries.get("Dockerfile")).startsWith("FROM scratch")).toBe(
      true,
    );
    expect(entries.get("__rayito_context/Dockerfile")).toEqual(encode("FROM evil"));
    expect(entries.get("__rayito_context/rayd")).toEqual(encode("not the agent"));
  });

  test.each(TEMPLATE_ENV.cases)("setEnvs bakes the template env file: $name", (item) => {
    let template = new Template();
    for (const [key, value] of item.steps) {
      template = template.setEnvs({ [key]: value });
    }
    const entries = readZipEntries(
      assembleArtifact(makeBaseZip(BASE_DOCKERFILE), template.spec, []),
    );

    expect(templateEnvs(template.spec)).toEqual(item.envs);
    expect(decode(templateEnvToJson(templateEnvs(template.spec)))).toBe(item.file);
    expect(decode(entries.get(TEMPLATE_ENV_CONTEXT_PATH))).toBe(item.file);
    expect(decode(entries.get("Dockerfile"))).toContain('"/etc/rayito/template-env.json"');
  });

  test("a template without setEnvs bakes no template env file", () => {
    const spec = new Template().pipInstall(["pandas"]).setStartCmd("run.sh").spec;
    const entries = readZipEntries(assembleArtifact(makeBaseZip(BASE_DOCKERFILE), spec, []));
    expect(entries.has(TEMPLATE_ENV_CONTEXT_PATH)).toBe(false);
    expect(decode(entries.get("Dockerfile"))).not.toContain("template-env.json");
  });
});
