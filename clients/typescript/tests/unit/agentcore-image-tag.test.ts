/**
 * La etiqueta de imagen por contenido de `--target agentcore` (ADR-031, D6):
 * los vectores de `testdata/agentcore-deploy/image-tags.json`, que Python
 * comparte, y que sólo el contenido la cambia.
 */

import { describe, expect, it } from "vitest";
import { imageTagFor } from "../../src/agentcore/identifiers.js";
import { imageTagVectors } from "./agentcore-vectors.js";

describe("etiqueta de imagen de AgentCore", () => {
  for (const testCase of imageTagVectors) {
    it(testCase.name, () => {
      expect(imageTagFor(new Map(Object.entries(testCase.files)))).toBe(testCase.expected);
    });
  }

  it("da lo mismo con un contenido de texto o de bytes", () => {
    const text = imageTagFor(new Map([["a.txt", "ñ"]]));
    const bytes = imageTagFor(new Map([["a.txt", new TextEncoder().encode("ñ")]]));
    expect(bytes).toBe(text);
  });
});
