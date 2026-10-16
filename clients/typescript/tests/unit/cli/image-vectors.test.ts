/**
 * Los contratos puros de `rayito image publish`, `prune` y `zip` frente a
 * `testdata/images/*.json`, que la CLI de Python genera y comprueba con
 * `tests/unit/cli/test_image_vectors.py` (ADR-027): mismas combinaciones,
 * mismas claves, misma configuración deseada, mismos planes y mismas
 * entradas de zip, en el mismo orden y con el mismo modo.
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import * as artifact from "../../../src/images/artifact.js";
import { inspectArtifact, writeImageZip } from "../../../src/images/artifact-node.js";
import { DEFAULT_MEMORY_MIB, VersionGate } from "../../../src/images/gateway.js";
import { IMAGE_ARTIFACT_KEY_PREFIX } from "../../../src/images/managed.js";
import * as prune from "../../../src/images/prune-domain.js";
import * as publishDomain from "../../../src/images/publish-domain.js";
import { MAX_CONCURRENT_IMAGE_BUILDS } from "../../../src/limits.js";
import { EFS_IMAGE_NAME } from "../../../src/role-policy.js";
import type { SizeName } from "../../../src/sizing/sizing.js";
import { readZipEntries } from "../../../src/templates/zip-node.js";
import { loadTestdata } from "./paths.js";

interface Outcome<T> {
  value?: T;
  error?: string;
  hint?: string;
}

interface PublishVectors {
  defaultNames: Array<{ variant: string; withEfs: boolean } & Outcome<string>>;
  efsCombinations: Array<{ variant: string; osCapabilities: string | null; error: string | null }>;
  runIds: Array<{ runId: string; error?: string }>;
  artifactKeys: Array<{ payload: string; sha256: string; runId: string | null } & Outcome<string>>;
  configurations: Array<{
    name: string;
    settings: Record<string, unknown>;
    region: string;
    buildRoleArn: string;
    artifactUri: string;
    expected: Record<string, unknown>;
  }>;
  logGroups: Array<{ imageName: string; expected: string }>;
  sized: Array<{
    size: SizeName;
    base: { environment_variables: Record<string, string> };
    imageName: string;
    memoryMib: number;
    environmentVariables: Record<string, string>;
  }>;
  sizeEnvVars: Array<{ size: string; expected: string }>;
  summaries: Array<{
    name: string;
    uploaded: boolean;
    buildSeconds: number | null;
    build: Record<string, unknown>;
    gate: publishDomain.GateFacts | null;
    expected: Record<string, unknown>;
  }>;
  aggregates: Array<{
    name: string;
    baseline: publishDomain.PublishSummary;
    sized: Record<string, publishDomain.PublishSummary>;
    expected: Record<string, unknown>;
  }>;
  sizes: Array<{ raw: string | null } & Outcome<string[]>>;
  baselineMemory: Array<{ memoryMib: number; sizes: string[]; error?: string; hint?: string }>;
  environment: Array<{ raw: string[] } & Outcome<Record<string, string>>>;
  variants: string[];
  osCapabilities: string[];
  constants: {
    defaultStackName: string;
    buildRoleOutputKey: string;
    baselineMemoryEnvVar: string;
    maxConcurrentImageBuilds: number;
    hooks: Record<string, unknown>;
    efsImageName: string;
    hooksPort: number;
    runScopedKeySegment: string;
    imageArtifactKeyPrefix: string;
    defaultMemoryMib: number;
  };
}

interface PruneVectors {
  plans: Array<{
    name: string;
    keep: number;
    live: string[];
    versions: Array<{ imageVersion: string; state: string; status: string; createdAt: string }>;
    kept: Array<[string, string]>;
    delete: string[];
    rows: string[][];
    table: string[];
  }>;
  constants: {
    defaultKeep: number;
    defaultWaitTimeoutSeconds: number;
    pollIntervalSeconds: number;
    retryBackoffSeconds: number[];
    inFlightStates: string[];
    busyImageStates: string[];
    liveMicrovmExcludedStates: string[];
    tableHeader: string[];
  };
}

interface ArtifactCases {
  tree: Record<string, string>;
  constants: {
    variants: string[];
    fixedDateTime: number[];
    warmupMarker: string;
    kernelsMarker: string;
    efsMarker: string;
    slimContent: string;
    polyContent: string;
    efsContent: string;
    excludedSuffixes: string[];
    excludedDirectories: string[];
    excludedFiles: string[];
    rayd: string;
    noticesDirectory: string;
    noticeFiles: string[];
    requirementsFiles: string[];
  };
  cases: Array<{
    name: string;
    variant: string;
    withEfs: boolean;
    tree: string | string[];
    count?: number;
    entries?: Array<{ name: string; mode: number; content: string }>;
    dates?: number[][];
    markerVariant?: string;
    hasEfs?: boolean;
    errorKind?: string;
  }>;
}

const publishVectors = loadTestdata<PublishVectors>("images", "publish-vectors.json");
const pruneVectors = loadTestdata<PruneVectors>("images", "prune-vectors.json");
const artifactCases = loadTestdata<ArtifactCases>("images", "artifact-cases.json");
const MS_PER_SECOND = 1000;

function messageOf(compute: () => unknown): { message: string; hint: string | undefined } {
  try {
    compute();
  } catch (error) {
    if (error instanceof publishDomain.ImageSettingsError) {
      return { message: error.message, hint: error.hint };
    }
    throw error;
  }
  throw new Error("no lanzó");
}

function gateOf(raw: publishDomain.GateFacts | null): publishDomain.GateFacts | undefined {
  if (raw === null) {
    return undefined;
  }
  const gate = new VersionGate(
    raw.imageState,
    raw.versionState,
    raw.versionStatus,
    raw.stateReason ?? undefined,
  );
  return {
    imageState: gate.imageState,
    versionState: gate.versionState,
    versionStatus: gate.versionStatus,
    stateReason: gate.stateReason,
    launchable: gate.launchable,
  };
}

function settingsFrom(overrides: Record<string, unknown>): publishDomain.PublishSettings {
  const camel: Record<string, unknown> = {};
  const aliases: Record<string, string> = {
    os_capabilities: "osCapabilities",
    image_name: "imageName",
    environment_variables: "environmentVariables",
    memory_mib: "memoryMib",
    base_image_version: "baseImageVersion",
  };
  for (const [key, value] of Object.entries(overrides)) {
    camel[aliases[key] ?? key] = value;
  }
  return {
    artifact: "image/rayito-image.zip",
    imageName: "rayito-base",
    variant: "full",
    bucket: "bucket",
    stackName: "stack",
    buildRoleArn: "arn:aws:iam::123456789012:role/build",
    baseImageVersion: "1",
    memoryMib: 2048,
    force: false,
    timeoutMs: 10_000,
    osCapabilities: undefined,
    environmentVariables: {},
    withEfs: false,
    artifactRunId: undefined,
    ...camel,
  } as publishDomain.PublishSettings;
}

describe("publish: contratos compartidos con Python", () => {
  it.each(publishVectors.defaultNames)(
    "nombre por defecto de $variant (efs: $withEfs)",
    (vector) => {
      if (vector.error !== undefined) {
        expect(
          messageOf(() => publishDomain.defaultImageName(vector.variant, vector.withEfs)).message,
        ).toBe(vector.error);
      } else {
        expect(publishDomain.defaultImageName(vector.variant, vector.withEfs)).toBe(vector.value);
      }
    },
  );

  it.each(publishVectors.efsCombinations)(
    "--with-efs con $variant y capabilities $osCapabilities",
    (vector) => {
      const capabilities = vector.osCapabilities ?? undefined;
      if (vector.error === null) {
        publishDomain.requireEfsCombination(vector.variant, capabilities);
      } else {
        expect(
          messageOf(() => publishDomain.requireEfsCombination(vector.variant, capabilities))
            .message,
        ).toBe(vector.error);
      }
    },
  );

  it.each(publishVectors.runIds)("id de ejecución $runId", (vector) => {
    if (vector.error === undefined) {
      expect(publishDomain.validateRunId(vector.runId)).toBe(vector.runId);
    } else {
      expect(messageOf(() => publishDomain.validateRunId(vector.runId)).message).toBe(vector.error);
    }
  });

  it.each(publishVectors.artifactKeys)("clave de $payload con ejecución $runId", (vector) => {
    const runId = vector.runId ?? undefined;
    if (vector.error !== undefined) {
      expect(() => publishDomain.artifactKey(vector.sha256, runId)).toThrow(
        publishDomain.ImageSettingsError,
      );
    } else {
      expect(publishDomain.artifactKey(vector.sha256, runId)).toBe(vector.value);
    }
  });

  it.each(publishVectors.configurations)("configuración deseada: $name", (vector) => {
    expect(
      publishDomain.desiredConfiguration({
        region: vector.region,
        settings: settingsFrom(vector.settings),
        artifactUri: vector.artifactUri,
        buildRoleArn: vector.buildRoleArn,
      }),
    ).toEqual(vector.expected);
  });

  it.each(publishVectors.logGroups)("grupo de logs de $imageName", (vector) => {
    expect(publishDomain.logGroupOf({ imageName: vector.imageName })).toBe(vector.expected);
  });

  it.each(publishVectors.sized)("ajustes del tamaño $size", (vector) => {
    const sized = publishDomain.sizedSettings(
      settingsFrom({ environment_variables: vector.base.environment_variables }),
      vector.size,
    );
    expect(sized.imageName).toBe(vector.imageName);
    expect(sized.memoryMib).toBe(vector.memoryMib);
    expect(sized.environmentVariables).toEqual(vector.environmentVariables);
  });

  it.each(publishVectors.sizeEnvVars)("variable de salida de $size", (vector) => {
    expect(publishDomain.sizeEnvVar(vector.size)).toBe(vector.expected);
  });

  it.each(publishVectors.summaries)("resumen $name", (vector) => {
    const summary = publishDomain.publishSummary({
      arn: "arn:x",
      version: "3.0",
      artifact: { uri: "s3://bucket/rayito/images/rayd-abc.zip", uploaded: vector.uploaded },
      buildSeconds: vector.buildSeconds ?? undefined,
      build: vector.build,
      gate: gateOf(vector.gate),
    });
    expect(summary).toEqual(vector.expected);
  });

  it.each(publishVectors.aggregates)("documento agregado $name", (vector) => {
    expect(publishDomain.aggregateSummary(vector.baseline, vector.sized)).toEqual(vector.expected);
  });

  it.each(publishVectors.sizes)("--sizes $raw", (vector) => {
    const raw = vector.raw ?? undefined;
    if (vector.error !== undefined) {
      const failure = messageOf(() => publishDomain.validateSizes(raw));
      expect(failure.message).toBe(vector.error);
      expect(failure.hint).toBe(vector.hint);
    } else {
      expect(publishDomain.validateSizes(raw)).toEqual(vector.value);
    }
  });

  it.each(publishVectors.baselineMemory)("--memory-mib $memoryMib con tamaños $sizes", (vector) => {
    if (vector.error !== undefined) {
      const failure = messageOf(() =>
        publishDomain.validateBaselineMemory(vector.memoryMib, vector.sizes),
      );
      expect(failure.message).toBe(vector.error);
      expect(failure.hint).toBe(vector.hint);
    } else {
      publishDomain.validateBaselineMemory(vector.memoryMib, vector.sizes);
    }
  });

  it.each(publishVectors.environment)("--env $raw", (vector) => {
    if (vector.error !== undefined) {
      const failure = messageOf(() => publishDomain.parseEnvironmentAssignments(vector.raw));
      expect(failure.message).toBe(vector.error);
      expect(failure.hint).toBe(vector.hint);
    } else {
      expect(publishDomain.parseEnvironmentAssignments(vector.raw)).toEqual(vector.value);
    }
  });

  it("las constantes son las de Python", () => {
    const { constants } = publishVectors;
    expect(publishDomain.DEFAULT_STACK_NAME).toBe(constants.defaultStackName);
    expect(publishDomain.BUILD_ROLE_OUTPUT_KEY).toBe(constants.buildRoleOutputKey);
    expect(publishDomain.BASELINE_MEMORY_ENV_VAR).toBe(constants.baselineMemoryEnvVar);
    expect(MAX_CONCURRENT_IMAGE_BUILDS).toBe(constants.maxConcurrentImageBuilds);
    expect(publishDomain.IMAGE_HOOKS).toEqual(constants.hooks);
    expect(EFS_IMAGE_NAME).toBe(constants.efsImageName);
    expect(publishDomain.RUN_SCOPED_KEY_SEGMENT).toBe(constants.runScopedKeySegment);
    expect(IMAGE_ARTIFACT_KEY_PREFIX).toBe(constants.imageArtifactKeyPrefix);
    expect(DEFAULT_MEMORY_MIB).toBe(constants.defaultMemoryMib);
    expect([...publishDomain.IMAGE_VARIANTS]).toEqual(publishVectors.variants);
    expect(publishDomain.OS_CAPABILITY_CHOICES).toEqual(publishVectors.osCapabilities);
  });
});

describe("prune: planes compartidos con Python", () => {
  it.each(pruneVectors.plans)("$name", (vector) => {
    const versions = vector.versions.map((version) => ({
      imageVersion: version.imageVersion,
      state: version.state,
      status: version.status,
      createdAt: new Date(version.createdAt),
    }));
    const plan = prune.buildPlan(versions, new Set(vector.live), vector.keep);
    expect(plan.keep.map(([version, why]) => [version.imageVersion, why])).toEqual(vector.kept);
    expect(plan.delete.map((version) => version.imageVersion)).toEqual(vector.delete);
    expect(prune.planRows(plan).map((row) => [...row])).toEqual(vector.rows);
    expect(prune.formatTable(plan)).toEqual(vector.table);
  });

  it("las constantes son las de Python", () => {
    const { constants } = pruneVectors;
    expect(prune.DEFAULT_KEEP).toBe(constants.defaultKeep);
    expect(prune.DEFAULT_WAIT_TIMEOUT_MS / MS_PER_SECOND).toBe(constants.defaultWaitTimeoutSeconds);
    expect(prune.POLL_INTERVAL_MS / MS_PER_SECOND).toBe(constants.pollIntervalSeconds);
    expect(prune.RETRY_BACKOFF_MS.map((ms) => ms / MS_PER_SECOND)).toEqual(
      constants.retryBackoffSeconds,
    );
    expect([...prune.IN_FLIGHT_STATES].sort()).toEqual(constants.inFlightStates);
    expect([...prune.BUSY_IMAGE_STATES].sort()).toEqual(constants.busyImageStates);
    expect(["TERMINATED", "TERMINATING"]).toEqual(constants.liveMicrovmExcludedStates.sort());
    expect([...prune.TABLE_HEADER]).toEqual(constants.tableHeader);
  });
});

const folders: string[] = [];

afterEach(() => {
  for (const folder of folders.splice(0)) {
    rmSync(folder, { recursive: true, force: true });
  }
});

function buildTree(tree: Record<string, string>): { imageDir: string; root: string } {
  const root = mkdtempSync(join(tmpdir(), "rayito-artifact-"));
  folders.push(root);
  const imageDir = join(root, "image");
  for (const [relative, text] of Object.entries(tree)) {
    const path = join(imageDir, relative);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text, "utf8");
  }
  return { imageDir, root };
}

/** `external_attr >> 16` de cada entrada del directorio central, en orden. */
function readEntryModes(archive: Uint8Array): Map<string, number> {
  const buffer = Buffer.from(archive);
  const eocd = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  const count = buffer.readUInt16LE(eocd + 10);
  let cursor = buffer.readUInt32LE(eocd + 16);
  const modes = new Map<string, number>();
  for (let index = 0; index < count; index += 1) {
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const name = buffer.toString("utf8", cursor + 46, cursor + 46 + nameLength);
    modes.set(name, buffer.readUInt32LE(cursor + 38) >>> 16);
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return modes;
}

describe("zip: entradas compartidas con Python", () => {
  it.each(artifactCases.cases)("$name", (vector) => {
    const tree =
      vector.tree === "default"
        ? artifactCases.tree
        : Object.fromEntries(
            (vector.tree as string[]).map((name) => [name, artifactCases.tree[name] ?? ""]),
          );
    const { imageDir, root } = buildTree(tree);
    const destination = join(root, "out.zip");
    if (vector.errorKind !== undefined) {
      expect(() =>
        writeImageZip(imageDir, destination, vector.variant, { withEfs: vector.withEfs }),
      ).toThrow(expect.objectContaining({ kind: vector.errorKind }));
      return;
    }
    const count = writeImageZip(imageDir, destination, vector.variant, { withEfs: vector.withEfs });
    expect(count).toBe(vector.count);
    const bytes = new Uint8Array(readFileSync(destination));
    const entries = readZipEntries(bytes);
    const modes = readEntryModes(bytes);
    expect(
      [...entries.entries()].map(([name, content]) => ({
        name,
        mode: modes.get(name),
        content: new TextDecoder().decode(content),
      })),
    ).toEqual(vector.entries);
    expect(inspectArtifact(destination)).toEqual({
      variant: vector.markerVariant,
      withEfs: vector.hasEfs,
    });
  });

  it("las constantes son las de Python", () => {
    const { constants } = artifactCases;
    expect([...artifact.IMAGE_VARIANTS]).toEqual(constants.variants);
    expect(artifact.WARMUP_MARKER_ENTRY).toBe(constants.warmupMarker);
    expect(artifact.KERNELS_MARKER_ENTRY).toBe(constants.kernelsMarker);
    expect(artifact.EFS_MARKER_ENTRY).toBe(constants.efsMarker);
    expect(artifact.syntheticEntries("slim", false)).toEqual([
      [constants.warmupMarker, constants.slimContent],
    ]);
    expect(artifact.syntheticEntries("poly", false)).toEqual([
      [constants.kernelsMarker, constants.polyContent],
    ]);
    expect(artifact.syntheticEntries("full", true)).toEqual([
      [constants.efsMarker, constants.efsContent],
    ]);
    expect(artifact.EXCLUDED_SUFFIXES).toEqual(constants.excludedSuffixes);
    expect([...artifact.EXCLUDED_DIRECTORIES].sort()).toEqual(constants.excludedDirectories);
    expect([...artifact.EXCLUDED_FILES].sort()).toEqual(constants.excludedFiles);
    expect(artifact.RAYD_ENTRY).toBe(constants.rayd);
    expect(artifact.NOTICES_DIRECTORY).toBe(constants.noticesDirectory);
    expect(artifact.RAYD_NOTICE_FILES).toEqual(constants.noticeFiles);
    expect(artifact.REQUIREMENTS_FILES).toEqual(constants.requirementsFiles);
  });
});
