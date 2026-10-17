/**
 * Lo común a `agent-zero-config.e2e.test.ts`: los agentes de ejemplo
 * (`testdata/agent-samples/`) preparados para desplegarlos sin tocar su código,
 * con un nombre propio de la ejecución, y el libro de gasto que corta la
 * ejecución antes de pasar del tope. Espejo de
 * `clients/python/tests/e2e/agent_zero_config_support.py`; las variables de
 * entorno y el coste están en la cabecera de ese fichero.
 */

import {
  appendFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

export const REPO_ROOT = join(import.meta.dirname, "../../../..");
export const SAMPLES_DIR = join(REPO_ROOT, "testdata", "agent-samples");

export interface Sample {
  readonly id: string;
  readonly language: string;
  readonly dir: string;
  readonly toolUse: boolean;
  readonly expected: {
    readonly framework: string;
    readonly kind: string;
    readonly protocol: string;
  };
  readonly app?: { readonly health: string; readonly invoke: string };
}

interface Manifest {
  readonly prompt: string;
  readonly expectedAnswer: string;
  readonly custom: {
    readonly memoryMib: number;
    readonly maxSessions: number;
    readonly minVms: number;
    readonly maxVms: number;
    readonly warmVms: number;
  };
  readonly samples: readonly Sample[];
}

export const MANIFEST = JSON.parse(
  readFileSync(join(SAMPLES_DIR, "manifest.json"), "utf8"),
) as Manifest;
export const RUN_ID = process.env.RAYITO_E2E_RUN_ID ?? "";
export const BUCKET = process.env.RAYITO_E2E_ZC_BUCKET ?? "";
export const BASE = process.env.RAYITO_E2E_ZC_BASE ?? "";
export const BEDROCK_SECRET = process.env.RAYITO_E2E_BEDROCK_SECRET ?? "";
export const BEDROCK_REGION =
  process.env.RAYITO_E2E_BEDROCK_REGION ?? process.env.AWS_REGION ?? "us-east-1";
export const LEDGER_PATH = process.env.RAYITO_E2E_ZC_LEDGER ?? "";
export const GATEWAY_NAME = "bedrock";
export const IGNORED_DIRS = [".venv", "node_modules", "__pycache__", ".git"];
export const MS_PER_SECOND = 1000;
export const TEST_TIMEOUT_MS = 1800 * MS_PER_SECOND;
export const IDLE_SECONDS = 60;
export const QUEUE_MAX = 2;
export const QUEUE_TIMEOUT_SECONDS = 60;
export const PARALLEL_CALLS = 5;
export const SUSPEND_WAIT_MS = 150 * MS_PER_SECOND;
export const RECOVERY_TIMEOUT_MS = 90 * MS_PER_SECOND;
export const TERMINATION_WAIT_MS = 180 * MS_PER_SECOND;
export const POLL_MS = 3 * MS_PER_SECOND;
const VM_USD_PER_SECOND_2GB = 0.126 / 3600;
const IMAGE_BUILD_USD = 0.07;
const MODEL_USD_PER_RUN = 0.01;
const MEMORY_BASE_MIB = 2048;
export const COST_STOP_USD = 8;
export const PROMPT = MANIFEST.prompt;
export const EXPECTED_ANSWER = MANIFEST.expectedAnswer;
export const LONG_PROMPT = "Write a story of about 300 words about a lighthouse keeper.";

/** Lo que despliega el espejo de TypeScript: los ejemplos de TypeScript, uno de
 * Python y OpenCode (el guest y el host se prueban una vez, en Python). */
const TS_RUN_EXTRA = new Set(["python-strands", "opencode"]);

export function selectedSamples(): Sample[] {
  const wanted = new Set((process.env.RAYITO_E2E_ZC_SAMPLES ?? "").split(",").filter(Boolean));
  return MANIFEST.samples.filter(
    (sample) =>
      (sample.language === "typescript" || TS_RUN_EXTRA.has(sample.id)) &&
      (wanted.size === 0 || wanted.has(sample.id)),
  );
}

/** `zc-<ejecución>-t<n><variante>`: corto, único por ejecución (la `t` evita
 * chocar con los nombres del espejo de Python) y válido como nombre de imagen. */
export function projectName(sample: Sample, variant: string): string {
  const index = MANIFEST.samples.findIndex((entry) => entry.id === sample.id);
  return `zc-${RUN_ID}-t${String(index).padStart(2, "0")}${variant}`;
}

export function projectFile(name: string, variant: string): string {
  const lines = [
    'schema = "rayito.agent-project/1"',
    `name = "${name}"`,
    "",
    "[image]",
    `base = "${BASE}"`,
  ];
  if (variant === "c") {
    lines.push(`memory_mib = ${MANIFEST.custom.memoryMib}`);
  }
  if (variant !== "") {
    lines.push(
      "",
      "[serve]",
      `max_concurrency = ${MANIFEST.custom.maxSessions}`,
      `queue_max = ${QUEUE_MAX}`,
      `queue_timeout_seconds = ${QUEUE_TIMEOUT_SECONDS}`,
      `idle_seconds = ${IDLE_SECONDS}`,
      "",
      "[scaling]",
      `min_vms = ${MANIFEST.custom.minVms}`,
      `max_vms = ${MANIFEST.custom.maxVms}`,
      `warm_vms = ${MANIFEST.custom.warmVms}`,
      `sessions = "${variant === "d" ? "dedicated" : "shared"}"`,
    );
  }
  return `${lines.join("\n")}\n`;
}

/** Una copia del ejemplo (sin entornos instalados) con su fichero mínimo. */
export function stagedSample(sample: Sample, root: string, variant = ""): string {
  const name = projectName(sample, variant);
  const target = join(root, name);
  mkdirSync(root, { recursive: true });
  cpSync(join(SAMPLES_DIR, sample.dir), target, {
    recursive: true,
    filter: (source) => !IGNORED_DIRS.some((ignored) => source.split("/").includes(ignored)),
  });
  writeFileSync(join(target, "rayito-agent.toml"), projectFile(name, variant));
  return target;
}

export function ledgerAdd(entry: Record<string, unknown>): void {
  if (LEDGER_PATH !== "") {
    appendFileSync(LEDGER_PATH, `${JSON.stringify({ t: Date.now() / MS_PER_SECOND, ...entry })}\n`);
  }
}

export function projectedUsd(): number {
  if (LEDGER_PATH === "" || !existsSync(LEDGER_PATH)) {
    return 0;
  }
  let total = 0;
  for (const line of readFileSync(LEDGER_PATH, "utf8").split("\n").filter(Boolean)) {
    const entry = JSON.parse(line) as {
      kind: string;
      seconds?: number;
      memory_mib?: number;
      calls?: number;
    };
    if (entry.kind === "build") {
      total += IMAGE_BUILD_USD;
    } else if (entry.kind === "vm") {
      total +=
        ((entry.seconds ?? 0) * VM_USD_PER_SECOND_2GB * (entry.memory_mib ?? MEMORY_BASE_MIB)) /
        MEMORY_BASE_MIB;
    } else if (entry.kind === "model") {
      total += MODEL_USD_PER_RUN * (entry.calls ?? 1);
    }
  }
  return total;
}

export function guard(): void {
  const spent = projectedUsd();
  if (spent > COST_STOP_USD) {
    throw new Error(
      `el gasto proyectado (${spent.toFixed(2)} USD) pasa del tope de parada (${COST_STOP_USD} USD)`,
    );
  }
}
