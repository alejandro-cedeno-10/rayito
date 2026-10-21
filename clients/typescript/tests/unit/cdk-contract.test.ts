/**
 * El contrato compartido `testdata/agentcore-cdk/` (ADR-032 D12.2): cada caso
 * con plantilla sintetiza exactamente su `expected.template.json` (el mismo
 * que produce Python) y cada caso de error lanza la excepción, el código y
 * los campos del caso sin dejar nada en el stack. También fija la superficie
 * pública de `rayito/cdk` y los mensajes del contrato.
 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DefaultStackSynthesizer } from "aws-cdk-lib";
import { afterEach, describe, expect, it } from "vitest";
import { CDK_MANAGED_MESSAGE } from "../../src/agentcore/messages.js";
import { RAYITO_CDK_ERROR_MESSAGES, RayitoCdkError } from "../../src/cdk/errors.js";
import * as cdk from "../../src/cdk/index.js";
import { AgentCoreError } from "../../src/errors.js";
import { CDK_MANAGED_IMAGE_MESSAGE } from "../../src/images/publish-domain.js";
import {
  addCaseConstruct,
  CASES_DIR,
  caseStack,
  EXPECTED_FILE,
  type Json,
  loadCases,
  loadContract,
  normalisedDocument,
  normaliseTemplate,
  renderDocument,
  synthesizeCase,
} from "./cdk-contract-harness.js";

const contract = loadContract();
const cases = loadCases();
const scratches: string[] = [];
const EXCEPTIONS: Record<string, new (...args: never[]) => Error> = {
  RayitoCdkException: RayitoCdkError,
  AgentCoreException: AgentCoreError,
};

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-cdk-case-"));
  scratches.push(directory);
  return directory;
}

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe("contrato agentcore-cdk: plantillas", () => {
  const templateCases = cases.filter(([, testCase]) => testCase.expected === "template");

  it("hay casos con plantilla y cada uno tiene su golden", () => {
    expect(templateCases.length).toBeGreaterThan(0);
    for (const [name] of templateCases) {
      expect(existsSync(join(CASES_DIR, name, EXPECTED_FILE)), name).toBe(true);
    }
  });

  for (const [name, testCase] of templateCases) {
    it(`${name} sintetiza su expected.template.json`, () => {
      const expected = readFileSync(join(CASES_DIR, name, EXPECTED_FILE), "utf8");
      const actual = renderDocument(normalisedDocument(synthesizeCase(testCase, scratch())));
      expect(actual).toBe(expected);
    });
  }
});

describe("contrato agentcore-cdk: errores", () => {
  for (const [name, testCase] of cases) {
    if (testCase.expected === "template") {
      continue;
    }
    const { error } = testCase.expected;
    it(`${name} lanza ${error.code} antes de añadir nada`, () => {
      const { stack } = caseStack(testCase);
      let thrown: unknown;
      try {
        addCaseConstruct(stack, testCase, scratch());
      } catch (caught) {
        thrown = caught;
      }
      const exception = EXCEPTIONS[error.exception];
      expect(exception, error.exception).toBeDefined();
      expect(thrown).toBeInstanceOf(exception as new (...args: never[]) => Error);
      const typed = thrown as { code: string; fields: Record<string, unknown> };
      expect(typed.code).toBe(error.code);
      for (const [field, value] of Object.entries(error.fields)) {
        expect(String(typed.fields[field]), field).toBe(String(value));
      }
      expect(stack.node.tryFindChild(testCase.id)).toBeUndefined();
    });
  }
});

describe("contrato agentcore-cdk: superficie y mensajes", () => {
  it("rayito/cdk exporta los valores del contrato", () => {
    const values = contract.sdk.typescript.exports.filter((name) => !name.startsWith("type "));
    expect(Object.keys(cdk).sort()).toEqual([...values].sort());
  });

  it("los mensajes de RayitoCdkError son los del contrato", () => {
    expect(RAYITO_CDK_ERROR_MESSAGES).toEqual(contract.errors.messages);
  });

  it("los rechazos de la CLI usan los mensajes del contrato", () => {
    expect(CDK_MANAGED_MESSAGE).toBe(contract.cliRefusals.agentDeploy.message);
    expect(CDK_MANAGED_IMAGE_MESSAGE).toBe(contract.cliRefusals.imagePublish.message);
  });
});

describe("normalización (contract.normalisation)", () => {
  const raw: Record<string, Json> = {
    Conditions: { CDKMetadataAvailable: { "Fn::Equals": ["a", "a"] } },
    Parameters: { BootstrapVersion: { Type: "String" } },
    Rules: { CheckBootstrapVersion: { Assertions: [] }, Keep: { Assertions: [] } },
    Resources: {
      CDKMetadata: { Type: "AWS::CDK::Metadata" },
      Build: { Properties: { ImageTag: "src-0123456789abcdef", Uri: "repo:src-0123456789abcdef" } },
      Bucket: {
        Type: "AWS::S3::Bucket",
        Metadata: { "aws:cdk:path": "Stack/Bucket" },
        Properties: {
          Name: `cdk-${DefaultStackSynthesizer.DEFAULT_QUALIFIER}-assets/${"a".repeat(64)}.zip`,
          Tags: [
            { Key: "rayito:sdk-version", Value: "0.15.0" },
            { Key: "team", Value: "demo" },
          ],
          TagMap: { "rayito:sdk-version": "0.15.0" },
        },
      },
    },
  };

  it("aplica las reglas en orden y no toca la entrada", () => {
    const before = JSON.stringify(raw);
    expect(normaliseTemplate(raw)).toEqual({
      Rules: { Keep: { Assertions: [] } },
      Resources: {
        Build: { Properties: { ImageTag: "src-<SOURCE_HASH>", Uri: "repo:src-<SOURCE_HASH>" } },
        Bucket: {
          Type: "AWS::S3::Bucket",
          Properties: {
            Name: "cdk-<QUALIFIER>-assets/<ASSET_HASH>.zip",
            Tags: [
              { Key: "rayito:sdk-version", Value: "<SDK_VERSION>" },
              { Key: "team", Value: "demo" },
            ],
            TagMap: { "rayito:sdk-version": "<SDK_VERSION>" },
          },
        },
      },
    });
    expect(JSON.stringify(raw)).toBe(before);
  });

  it("serializa como gen_cdk_vectors.py: claves ordenadas, sangría 2 y salto final", () => {
    expect(renderDocument({ b: [1, { d: "ñ", c: null }], a: {} })).toBe(
      '{\n  "a": {},\n  "b": [\n    1,\n    {\n      "c": null,\n      "d": "ñ"\n    }\n  ]\n}\n',
    );
  });
});
