/**
 * `src/templates/artifact.ts` (m15-templates): los ficheros de contexto van
 * bajo `__rayito_context/`, nunca sobre una entrada del zip base (T26).
 * Espejo de `test_m15_templates_artifact.py`.
 */

import { describe, expect, test } from "vitest";
import { assembleArtifact } from "../../src/templates/artifact.js";
import { Template } from "../../src/templates/dsl.js";
import { readZipEntries } from "../../src/templates/zip-node.js";
import { makeBaseZip } from "./m15-fake-build-clients.js";

const BASE_DOCKERFILE =
  'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n';
const encode = (text: string): Uint8Array => new TextEncoder().encode(text);

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
});
