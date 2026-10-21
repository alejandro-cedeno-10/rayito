/**
 * El arnés de TypeScript del contrato `testdata/agentcore-cdk/` (ADR-032
 * D12.2): construye cada caso como dice `README.md` (App con el contexto del
 * contrato, stack `RayitoCdkCase`, fixture comprimida con la regla de
 * `caseFormat`), sintetiza y normaliza con las reglas de
 * `contract.normalisation`, reimplementadas aquí (un test comprueba que
 * coinciden con los ejemplos). Es el espejo de
 * `clients/python/tests/cdk_contract_harness.py`.
 */

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { App, NestedStack, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import type { Construct } from "constructs";
import {
  AgentImageBuild,
  MicrovmImageArtifact,
  RayitoAgent,
  type RayitoAgentProps,
  RayitoMicrovmImage,
  type RayitoMicrovmImageProps,
} from "../../src/cdk/index.js";
import { writeZipEntries } from "../../src/templates/zip-node.js";

export const TESTDATA_ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
export const CDK_TESTDATA = join(TESTDATA_ROOT, "agentcore-cdk");
export const CASES_DIR = join(CDK_TESTDATA, "cases");
export const EXPECTED_FILE = "expected.template.json";
export const STACK_NAME = "RayitoCdkCase";

const ASSET_HASH = /[0-9a-f]{64}/g;
const ASSET_HASH_PLACEHOLDER = "<ASSET_HASH>";
const BOOTSTRAP_QUALIFIER = "hnb659fds";
const QUALIFIER_PLACEHOLDER = "<QUALIFIER>";
const SDK_VERSION_TAG = "rayito:sdk-version";
const SDK_VERSION_PLACEHOLDER = "<SDK_VERSION>";
const REMOVED_SECTIONS: ReadonlyArray<readonly [string, string]> = [
  ["Resources", "CDKMetadata"],
  ["Conditions", "CDKMetadataAvailable"],
  ["Parameters", "BootstrapVersion"],
  ["Rules", "CheckBootstrapVersion"],
];

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface CdkContract {
  readonly caseFormat: { readonly appContext: string; readonly stackName: string };
  readonly sdk: {
    readonly typescript: { readonly exports: string[] };
    readonly propNames: Record<string, string>;
  };
  readonly constants: Record<string, unknown>;
  readonly errors: { readonly messages: Record<string, string> };
  readonly cliRefusals: {
    readonly agentDeploy: { readonly message: string };
    readonly imagePublish: { readonly message: string };
  };
  readonly constructIds: Record<string, Record<string, string>>;
}

export interface CdkCase {
  readonly construct: "RayitoAgent" | "RayitoMicrovmImage";
  readonly stack: { readonly account: string | null; readonly region: string | null };
  readonly id: string;
  readonly props: Record<string, unknown>;
  readonly project?: string;
  readonly build?: "codeBuild" | "localDocker" | { readonly codeBuild: { timeoutMinutes: number } };
  readonly artifact?: { readonly fixture: string; readonly sha256?: string };
  readonly expected:
    | "template"
    | {
        readonly error: {
          readonly code: string;
          readonly exception: string;
          readonly fields: Record<string, unknown>;
        };
      };
}

export interface SynthesizedCase {
  readonly root: Record<string, Json>;
  readonly nested: Record<string, Record<string, Json>>;
}

export function loadContract(): CdkContract {
  return JSON.parse(readFileSync(join(CDK_TESTDATA, "contract.json"), "utf8")) as CdkContract;
}

export function loadCases(): Array<[string, CdkCase]> {
  return readdirSync(CASES_DIR)
    .sort()
    .map((name) => [
      name,
      JSON.parse(readFileSync(join(CASES_DIR, name, "input.json"), "utf8")) as CdkCase,
    ]);
}

function withoutSdkVersion(value: Json): Json {
  if (Array.isArray(value)) {
    return value.map(withoutSdkVersion);
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (value.Key === SDK_VERSION_TAG && "Value" in value) {
    return { ...value, Value: SDK_VERSION_PLACEHOLDER };
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      key === SDK_VERSION_TAG ? SDK_VERSION_PLACEHOLDER : withoutSdkVersion(item),
    ]),
  );
}

function replaceStrings(value: Json): Json {
  if (typeof value === "string") {
    return value
      .replace(ASSET_HASH, ASSET_HASH_PLACEHOLDER)
      .replaceAll(BOOTSTRAP_QUALIFIER, QUALIFIER_PLACEHOLDER);
  }
  if (Array.isArray(value)) {
    return value.map(replaceStrings);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        replaceStrings(key) as string,
        replaceStrings(item),
      ]),
    );
  }
  return value;
}

/** `contract.normalisation.rules`, en su orden; no modifica `template`. */
export function normaliseTemplate(template: Record<string, Json>): Record<string, Json> {
  const result = JSON.parse(JSON.stringify(template)) as Record<string, Json>;
  for (const [section, key] of REMOVED_SECTIONS) {
    const entries = result[section];
    if (entries !== null && typeof entries === "object" && !Array.isArray(entries)) {
      delete entries[key];
      if (Object.keys(entries).length === 0) {
        delete result[section];
      }
    }
  }
  const resources = result.Resources;
  if (resources !== null && typeof resources === "object" && !Array.isArray(resources)) {
    for (const resource of Object.values(resources)) {
      if (resource !== null && typeof resource === "object" && !Array.isArray(resource)) {
        delete resource.Metadata;
      }
    }
  }
  return replaceStrings(withoutSdkVersion(result)) as Record<string, Json>;
}

function sortedJson(value: Json): Json {
  if (Array.isArray(value)) {
    return value.map(sortedJson);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sortedJson(value[key] as Json)]),
    );
  }
  return value;
}

/** Como `render` de `gen_cdk_vectors.py`: claves ordenadas, sangría 2 y salto de línea final. */
export function renderDocument(document: Json): string {
  return `${JSON.stringify(sortedJson(document), null, 2)}\n`;
}

function filesUnder(root: string): string[] {
  return readdirSync(root)
    .flatMap((name) => {
      const path = join(root, name);
      return statSync(path).isDirectory() ? filesUnder(path) : [path];
    })
    .sort();
}

/** El zip de una fixture con la regla de `caseFormat.fields.artifact` (entradas ordenadas, sin compresión). */
export function writeFixtureZip(fixture: string, destination: string): string {
  const entries = filesUnder(fixture).map((path) => ({
    name: relative(fixture, path).split(sep).join("/"),
    content: new Uint8Array(readFileSync(path)),
  }));
  writeFileSync(destination, writeZipEntries(entries, { compress: false, unixCreator: true }));
  return destination;
}

function buildOf(build: CdkCase["build"]): AgentImageBuild {
  if (build === "localDocker") {
    return AgentImageBuild.localDocker();
  }
  if (build === undefined || build === "codeBuild") {
    return AgentImageBuild.codeBuild();
  }
  return AgentImageBuild.codeBuild(build.codeBuild);
}

/** La app y el stack vacío de un caso. */
export function caseStack(testCase: CdkCase): { readonly app: App; readonly stack: Stack } {
  const app = new App({
    context: JSON.parse(loadContract().caseFormat.appContext) as Record<string, unknown>,
  });
  const { account, region } = testCase.stack;
  const stack =
    region === null
      ? new Stack(app, STACK_NAME)
      : new Stack(app, STACK_NAME, {
          env: account === null ? { region } : { account, region },
        });
  return { app, stack };
}

/** Añade el constructo del caso a `stack` (lanza el error del caso, si lo hay). */
export function addCaseConstruct(stack: Stack, testCase: CdkCase, scratch: string): Construct {
  if (testCase.construct === "RayitoAgent") {
    const props = {
      ...testCase.props,
      project: join(TESTDATA_ROOT, testCase.project ?? ""),
      build: buildOf(testCase.build),
    } as RayitoAgentProps;
    return new RayitoAgent(stack, testCase.id, props);
  }
  const fixture = testCase.artifact?.fixture ?? "";
  const zip = writeFixtureZip(join(CDK_TESTDATA, fixture), join(scratch, "artifact.zip"));
  const artifact = MicrovmImageArtifact.fromFile(zip, { sha256: testCase.artifact?.sha256 });
  const props = { ...testCase.props, artifact } as unknown as RayitoMicrovmImageProps;
  return new RayitoMicrovmImage(stack, testCase.id, props);
}

/** Sintetiza un caso: `{ root, nested }` sin normalizar, como `synthesize_case` de Python. */
export function synthesizeCase(testCase: CdkCase, scratch: string): SynthesizedCase {
  const { stack } = caseStack(testCase);
  addCaseConstruct(stack, testCase, scratch);
  const nested: Record<string, Record<string, Json>> = {};
  for (const child of stack.node.findAll()) {
    if (child instanceof NestedStack && child.nestedStackParent === stack) {
      const logicalId = stack.resolve(child.nestedStackResource?.logicalId) as string;
      nested[logicalId] = Template.fromStack(child).toJSON() as Record<string, Json>;
    }
  }
  return { root: Template.fromStack(stack).toJSON() as Record<string, Json>, nested };
}

/** El documento normalizado de un caso, el que se compara con `expected.template.json`. */
export function normalisedDocument(synthesized: SynthesizedCase): Json {
  return {
    root: normaliseTemplate(synthesized.root),
    nested: Object.fromEntries(
      Object.keys(synthesized.nested)
        .sort()
        .map((key) => [key, normaliseTemplate(synthesized.nested[key] ?? {})]),
    ),
  };
}
