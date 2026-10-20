/**
 * Los ARN del rol del runtime (ADR-031, D11): los vectores de
 * `testdata/agentcore-deploy/model-arns.json`, que Python comparte.
 */

import { describe, expect, it } from "vitest";
import { bedrockResourceArns, secretResourceArns } from "../../src/agentcore/arns.js";
import { modelArnVectors } from "./agentcore-vectors.js";

describe("ARN de Bedrock del rol del runtime", () => {
  for (const testCase of modelArnVectors.bedrock) {
    it(testCase.name, () => {
      expect(
        bedrockResourceArns(testCase.models, {
          modelRegion: testCase.modelRegion,
          account: testCase.account,
          partition: testCase.partition,
        }),
      ).toEqual(testCase.expected);
    });
  }
});

describe("ARN de Secrets Manager del rol del runtime", () => {
  for (const testCase of modelArnVectors.secrets) {
    it(testCase.name, () => {
      expect(
        secretResourceArns(testCase.secrets, {
          region: testCase.region,
          account: testCase.account,
          partition: testCase.partition,
        }),
      ).toEqual(testCase.expected);
    });
  }

  it("sin secretos ni modelos no da ningún permiso", () => {
    expect(secretResourceArns([], { region: "us-east-1", account: "123456789012" })).toEqual([]);
    expect(bedrockResourceArns([], { modelRegion: "us-east-1", account: "123456789012" })).toEqual(
      [],
    );
  });
});
