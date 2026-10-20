/**
 * Lo común a `agentcore-deploy.e2e.test.ts`: los agentes de ejemplo
 * (`testdata/agent-samples/`) preparados para desplegarlos en AgentCore con un
 * nombre propio de la ejecución, las etiquetas de las SCP y la simulación de
 * IAM del rol del runtime. Variables (ninguna con un valor por defecto de un
 * entorno concreto; todas llegan en tiempo de ejecución):
 *
 * - `RAYITO_E2E=1` y `RAYITO_E2E_AGENTCORE=1`: sin las dos no se ejecuta nada.
 * - `AWS_REGION`: la región (`us-east-1` para el perfil `us.` de Haiku 4.5).
 * - `RAYITO_BUCKET`: el bucket de artefactos de `rayito setup`.
 * - `RAYITO_E2E_RUN_ID`: 8 minúsculas o dígitos que identifican la ejecución; todo
 *   lo que crea la prueba los lleva y sólo eso se borra.
 * - `RAYITO_E2E_TAGS`: `K=V,K=V` para las organizaciones que exigen etiquetas.
 * - `RAYITO_E2E_ZC_SAMPLES`: ids de ejemplo separados por comas (por defecto, los
 *   de TypeScript más `python-strands`).
 *
 * Coste: unos $0,05 por ejemplo (un build de CodeBuild de unos 8 minutos, la
 * imagen un día y unas pocas invocaciones de Haiku 4.5); a $0,0034 por minuto de
 * build tras los 100 minutos gratis del mes.
 */

import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { stackNameFor } from "../../src/agentcore/names.js";
import { CloudFormationProvisioner } from "../../src/stacks/cloudformation.js";

export const REPO_ROOT = join(import.meta.dirname, "../../../..");
export const SAMPLES_DIR = join(REPO_ROOT, "testdata", "agent-samples");
export const RUN_ID = process.env.RAYITO_E2E_RUN_ID ?? "";
export const BUCKET = process.env.RAYITO_BUCKET ?? "";
export const REGION = process.env.AWS_REGION ?? "";
export const MS_PER_SECOND = 1000;
export const TEST_TIMEOUT_MS = 2400 * MS_PER_SECOND;
export const IGNORED_DIRS = [".venv", "node_modules", "__pycache__", ".git"];
export const ROLE_OUTPUT = "RuntimeRoleArn";
export const OTHER_MODEL_ARN_TEMPLATE =
  "arn:aws:bedrock:{region}::foundation-model/amazon.nova-micro-v1:0";
export const SECRET_ARN_PATTERN_TEMPLATE = "arn:aws:secretsmanager:{region}:*:secret:*";
const DEFAULT_SAMPLES = [
  "typescript-langgraph-js",
  "typescript-deepagents-js",
  "typescript-strands-ts",
  "typescript-strands-harness-ts",
  "typescript-vercel-ai-sdk",
  "typescript-express-custom",
  "python-strands",
];

export interface Sample {
  readonly id: string;
  readonly dir: string;
}

const manifest = JSON.parse(readFileSync(join(SAMPLES_DIR, "manifest.json"), "utf8")) as {
  prompt: string;
  expectedAnswer: string;
  samples: readonly Sample[];
};

export const PROMPT = manifest.prompt;
export const EXPECTED_ANSWER = manifest.expectedAnswer;

export function agentcoreE2eReady(): boolean {
  return (
    process.env.RAYITO_E2E === "1" &&
    process.env.RAYITO_E2E_AGENTCORE === "1" &&
    Boolean(BUCKET && RUN_ID && REGION)
  );
}

export function selectedSamples(): Sample[] {
  const wanted = new Set(
    (process.env.RAYITO_E2E_ZC_SAMPLES ?? DEFAULT_SAMPLES.join(",")).split(",").filter(Boolean),
  );
  return manifest.samples.filter((sample) => wanted.has(sample.id));
}

/** `<id>-<ejecución>`: único por ejecución y válido como nombre de agente (hasta 41 caracteres). */
export function agentNameOf(sample: Sample): string {
  return `${sample.id.replace(/^(typescript|python)-/, (_all, language: string) => `${language.slice(0, 2)}-`)}-${RUN_ID}`;
}

/** Las etiquetas de `RAYITO_E2E_TAGS` (`K=V,K=V`). */
export function tagsFromEnvironment(): Record<string, string> {
  const tags: Record<string, string> = {};
  for (const pair of (process.env.RAYITO_E2E_TAGS ?? "").split(",").filter(Boolean)) {
    const separator = pair.indexOf("=");
    tags[pair.slice(0, separator)] = pair.slice(separator + 1);
  }
  return tags;
}

/** Una copia del ejemplo (sin entornos instalados) con su `rayito-agent.toml`, que fija el nombre del agente y, opcionalmente, su entorno. */
export function stagedAgent(
  sample: Sample,
  root: string,
  environment: Readonly<Record<string, string>> = {},
): string {
  const name = agentNameOf(sample);
  const target = join(root, name);
  mkdirSync(root, { recursive: true });
  cpSync(join(SAMPLES_DIR, sample.dir), target, {
    recursive: true,
    filter: (source) => !IGNORED_DIRS.some((ignored) => source.split("/").includes(ignored)),
  });
  const lines = [
    'schema = "rayito.agent-project/1"',
    `name = "${name}"`,
    "",
    "[image]",
    `template = "${name}"`,
  ];
  if (Object.keys(environment).length > 0) {
    lines.push(
      "",
      "[env]",
      ...Object.entries(environment).map(([key, value]) => `${key} = "${value}"`),
    );
  }
  writeFileSync(join(target, "rayito-agent.toml"), `${lines.join("\n")}\n`);
  return target;
}

/** Cambia un fichero del código del agente sin cambiar lo que hace: el contenido del contexto de build cambia y con él la etiqueta de la imagen. */
export function touchSource(directory: string, file: string, marker: string): void {
  const path = join(directory, file);
  writeFileSync(path, `${readFileSync(path, "utf8")}\n${marker}\n`);
}

/** El ARN del rol del runtime de un agente: la salida de su pila. */
export async function runtimeRoleOf(name: string): Promise<string> {
  const status = await new CloudFormationProvisioner({ region: REGION }).describe(
    stackNameFor(name),
  );
  const arn = status?.outputs[ROLE_OUTPUT];
  if (arn === undefined) {
    throw new Error("la pila del agente no tiene la salida del rol del runtime");
  }
  return arn;
}

interface SimulationModule {
  readonly IAMClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly SimulatePrincipalPolicyCommand: new (input: object) => unknown;
}

/** La decisión de IAM de `action` sobre `resource` para `roleArn` (`SimulatePrincipalPolicy`). */
export async function simulatedDecision(
  roleArn: string,
  action: string,
  resource: string,
): Promise<string> {
  const sdk = (await import("@aws-sdk/client-iam")) as unknown as SimulationModule;
  const client = new sdk.IAMClient({ region: REGION });
  const output = (await client.send(
    new sdk.SimulatePrincipalPolicyCommand({
      PolicySourceArn: roleArn,
      ActionNames: [action],
      ResourceArns: [resource],
    }),
  )) as { EvaluationResults?: ReadonlyArray<{ EvalDecision?: string }> };
  return output.EvaluationResults?.[0]?.EvalDecision ?? "";
}
