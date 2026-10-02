import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DIST_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");
/** Un `<función>.json` por función 0.6 (`[{ name, pattern }]`, `pattern` en
 * texto de `RegExp`): cada una añade sus declaraciones sin editar este
 * fichero compartido (el registro drop-in que el plan M15 nombra). */
const DROP_IN_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..", "cost-declarations");

/**
 * Declaraciones publicadas que activan algo con coste AWS (ADR-014). Cada una
 * debe llevar su PROPIO bloque "Coste y activación" en el TSDoc que la
 * precede dentro de `dist/*.d.mts`: el hover del IDE sólo enseña ese
 * comentario, nunca el bloque de módulo (que tsdown descarta).
 */
export const COST_DECLARATIONS = [
  { name: "class SecretStore", pattern: /^(export )?declare class SecretStore\b/ },
  { name: "class SecretCache", pattern: /^(export )?declare class SecretCache\b/ },
  { name: "class Secret (e2b)", pattern: /^(export )?declare class Secret\b/ },
  { name: "opción secrets", pattern: /^\s*readonly secrets\?: SecretsInput\b/ },
  { name: "opción secretCache", pattern: /^\s*readonly secretCache\?: SecretCache\b/ },
];

/** Las declaraciones de `cost-declarations/*.json`, en orden de fichero. */
export function dropInDeclarations(dir = DROP_IN_DIR) {
  if (!existsSync(dir)) {
    return [];
  }
  return readdirSync(dir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .flatMap((name) =>
      JSON.parse(readFileSync(join(dir, name), "utf8")).map(({ name: label, pattern }) => ({
        name: label,
        pattern: new RegExp(pattern),
      })),
    );
}

/** `COST_DECLARATIONS` más las de cada función en `cost-declarations/`. */
export function allDeclarations() {
  return [...COST_DECLARATIONS, ...dropInDeclarations()];
}

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
export function checkDeclarations(file, text, declarations = allDeclarations()) {
  const failures = [];
  const found = new Set();
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    for (const declaration of declarations) {
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
  const declarations = allDeclarations();
  const failures = [];
  const found = new Set();
  for (const name of files) {
    const result = checkDeclarations(
      name,
      readFileSync(join(DIST_DIR, name), "utf8"),
      declarations,
    );
    failures.push(...result.failures);
    for (const declaration of result.found) {
      found.add(declaration);
    }
  }
  for (const declaration of declarations) {
    if (!found.has(declaration.name)) {
      failures.push(`ningún dist/*.d.mts declara ${declaration.name}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(`bloques "Coste y activación" incompletos:\n${failures.join("\n")}`);
  }
  console.log(`check-dts-cost-blocks: OK (${declarations.length} declaraciones)`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main();
}
