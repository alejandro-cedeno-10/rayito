import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DIST_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");

/**
 * Declaraciones publicadas que activan algo con coste AWS (ADR-014). Cada una
 * debe llevar su PROPIO bloque "Coste y activación" en el TSDoc que la
 * precede dentro de `dist/*.d.mts`: el hover del IDE sólo enseña ese
 * comentario, nunca el bloque de módulo (que tsdown descarta).
 */
// `cost-declarations/*.json` as a drop-in glob (so a feature's own entry
// needs no edit here) was deferred by m15-foundations (see its proposal's
// "Non-blocking follow-ups"); this array stays hard-coded for now.
// m15-events-webhooks only adds its own line, same as any feature would.
export const COST_DECLARATIONS = [
  { name: "class SecretStore", pattern: /^(export )?declare class SecretStore\b/ },
  { name: "class SecretCache", pattern: /^(export )?declare class SecretCache\b/ },
  { name: "class Secret (e2b)", pattern: /^(export )?declare class Secret\b/ },
  { name: "opción secrets", pattern: /^\s*readonly secrets\?: SecretsInput\b/ },
  { name: "opción secretCache", pattern: /^\s*readonly secretCache\?: SecretCache\b/ },
  { name: "class LifecycleEvents", pattern: /^(export )?declare class LifecycleEvents\b/ },
];

export const REQUIRED_HEADINGS = [
  "Coste y activación",
  "Activa:",
  "Recursos y llamadas AWS:",
  "Coste aproximado:",
  "IAM:",
  "Cómo apagarla:",
  "Ejemplo:",
];

/** El TSDoc (`/** ... *\/`) que termina justo antes de la línea `index`, o `""`. */
export function docBefore(lines, index) {
  let end = index - 1;
  while (end >= 0 && lines[end].trim() === "") {
    end -= 1;
  }
  if (end < 0 || !lines[end].trim().endsWith("*/")) {
    return "";
  }
  let start = end;
  while (start >= 0 && !lines[start].trim().startsWith("/**")) {
    start -= 1;
  }
  return start < 0 ? "" : lines.slice(start, end + 1).join("\n");
}

/** Los fallos (`fichero:línea: qué falta`) de un `.d.mts`; vacío si está bien. */
export function checkDeclarations(file, text) {
  const failures = [];
  const found = new Set();
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    for (const declaration of COST_DECLARATIONS) {
      if (!declaration.pattern.test(line)) {
        continue;
      }
      found.add(declaration.name);
      const doc = docBefore(lines, index);
      const missing = REQUIRED_HEADINGS.filter((heading) => !doc.includes(heading));
      if (missing.length > 0) {
        failures.push(`${file}:${index + 1}: ${declaration.name} sin ${missing.join(", ")}`);
      }
    }
  });
  return { failures, found };
}

function main() {
  const files = readdirSync(DIST_DIR).filter((name) => name.endsWith(".d.mts"));
  if (files.length === 0) {
    throw new Error("no hay dist/*.d.mts: ejecuta `pnpm build` antes");
  }
  const failures = [];
  const found = new Set();
  for (const name of files) {
    const result = checkDeclarations(name, readFileSync(join(DIST_DIR, name), "utf8"));
    failures.push(...result.failures);
    for (const declaration of result.found) {
      found.add(declaration);
    }
  }
  for (const declaration of COST_DECLARATIONS) {
    if (!found.has(declaration.name)) {
      failures.push(`ningún dist/*.d.mts declara ${declaration.name}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(`bloques "Coste y activación" incompletos:\n${failures.join("\n")}`);
  }
  console.log(`check-dts-cost-blocks: OK (${COST_DECLARATIONS.length} declaraciones)`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main();
}
