/**
 * La etiqueta de imagen por contenido de `--target agentcore` (ADR-031, D6):
 * los vectores de `testdata/agentcore-deploy/image-tags.json`, que Python
 * comparte, y que sólo el contenido la cambia.
 */

import { describe, expect, it } from "vitest";
import { clientTokenFor, imageTagFor } from "../../src/agentcore/identifiers.js";
import { clientTokenVectors, imageTagVectors } from "./agentcore-vectors.js";

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

describe("clientToken de AgentCore", () => {
  for (const testCase of clientTokenVectors) {
    it(testCase.name, () => {
      expect(clientTokenFor(testCase.request, testCase.baseVersion)).toBe(testCase.expected);
    });
  }

  it("volver a una configuración anterior en otra versión no repite el token", () => {
    const request = { agentRuntimeId: "rayito_demo-abc1234567", description: "igual" };
    expect(clientTokenFor(request, "2")).toBe(clientTokenFor(request, "2"));
    expect(clientTokenFor(request, "2")).not.toBe(clientTokenFor(request, "3"));
    expect(clientTokenFor(request, "2")).not.toBe(clientTokenFor(request));
  });
});
