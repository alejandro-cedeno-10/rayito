import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { compareDirectories, SOURCE_DIR } from "./dashboard-static.mjs";

const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DIST_DIR = join(PACKAGE_DIR, "dist");
const PACK_DIR_NAME = ".pack";
const PACK_DIR = join(PACKAGE_DIR, PACK_DIR_NAME);
const REQUIRED_ENTRIES = [
  "package/package.json",
  "package/README.md",
  "package/LICENSE",
  "package/NOTICE",
  "package/dist/index.mjs",
  "package/dist/index.cjs",
  "package/dist/index.d.mts",
  "package/dist/index.d.cts",
  "package/dist/e2b.mjs",
  "package/dist/e2b.cjs",
  "package/dist/e2b.d.mts",
  "package/dist/e2b.d.cts",
  "package/dist/cdk.mjs",
  "package/dist/cdk.cjs",
  "package/dist/cdk.d.mts",
  "package/dist/cdk.d.cts",
  "package/dist/cli.mjs",
  "package/dist/mcp.mjs",
  "package/dist/dashboard-static/index.html",
];
const SHEBANG = "#!/usr/bin/env node";
const EXTRACT_DIR_NAME = "extract";
const PACKED_STATIC_ENTRY = "package/dist/dashboard-static";

// `@opentelemetry/api` (M13b) sólo se importa con `import type` en
// `src/otel.ts`, que `tsdown`/rolldown borran del build: ningún fichero
// publicado debe traer un `import`/`require`/`import()` en tiempo de
// ejecución de ese paquete. Comprueba la sintaxis real, no una substring:
// el JSDoc del bundle menciona `@opentelemetry/api` entre comillas simples
// de Markdown (no de JS), así que no debe casar aquí.
const RUNTIME_OPENTELEMETRY_IMPORT = new RegExp(
  "require\\(\\s*[\"']@opentelemetry/api[\"']\\s*\\)" +
    "|from\\s*[\"']@opentelemetry/api[\"']" +
    "|import\\(\\s*[\"']@opentelemetry/api[\"']\\s*\\)",
);

// `rayito/cdk` (ADR-032, D8): `aws-cdk-lib` y `constructs` son peers
// opcionales que sólo carga la entrada `cdk`. Ningún otro fichero de `dist/`
// (las entradas `index`, `e2b`, `cli` y `mcp` y los chunks que comparten) puede
// importarlos: quien no usa los constructos no los instala.
const RUNTIME_CDK_IMPORT = new RegExp(
  "require\\(\\s*[\"'](?:aws-cdk-lib|constructs)(?:/[^\"']*)?[\"']\\s*\\)" +
    "|from\\s*[\"'](?:aws-cdk-lib|constructs)(?:/[^\"']*)?[\"']" +
    "|import\\(\\s*[\"'](?:aws-cdk-lib|constructs)(?:/[^\"']*)?[\"']\\s*\\)",
);
const CDK_ENTRY = /^cdk\.(?:mjs|cjs)$/;

/**
 * Extensión de los tipos y del JavaScript de cada condición del mapa
 * `exports`. TypeScript toma la primera condición que casa y da por hecho que
 * el `.d.*` que encuentra describe el formato del fichero en tiempo de
 * ejecución (TypeScript Handbook, "Modules reference", `exports`): un `types`
 * hermano de `import`/`require` casaría antes que ambos y serviría tipos ESM a
 * un consumidor CommonJS ("Masquerading as ESM" de arethetypeswrong).
 */
const CONDITION_EXTENSIONS = {
  import: { types: ".d.mts", default: ".mjs" },
  require: { types: ".d.cts", default: ".cjs" },
};

/**
 * Verifica que `pnpm pack` produce un tarball con los ficheros publicables
 * (build, tipos, README, `LICENSE` y `NOTICE`). `pnpm pack --dry-run` no existe
 * en pnpm 9, así que se empaqueta de verdad en `.pack/` y se lista con `tar`.
 * Rutas relativas: GNU tar toma `D:\...` por un host remoto. Además (M13b)
 * comprueba que ningún `.mjs`/`.cjs` de `dist/` (incluidos los chunks
 * compartidos) importa `@opentelemetry/api` en tiempo de ejecución, y que cada
 * entrada de `exports` declara sus tipos dentro de `import` y de `require`.
 */
function main() {
  assertNoRuntimeOpenTelemetryImport();
  assertCdkImportsOnlyInCdkEntry();
  assertTypesMatchEachCondition();
  assertBinaryEntriesAreExecutable();
  rmSync(PACK_DIR, { recursive: true, force: true });
  mkdirSync(PACK_DIR);
  try {
    const entries = packedEntries();
    const missing = REQUIRED_ENTRIES.filter((entry) => !entries.includes(entry));
    console.log(entries.join("\n"));
    if (missing.length > 0) {
      throw new Error(`faltan en el tarball: ${missing.join(", ")}`);
    }
    assertPackedDashboardMatchesSource();
  } finally {
    rmSync(PACK_DIR, { recursive: true, force: true });
  }
}

/**
 * Cada `bin` de `package.json` (ADR-027, D1) apunta a un fichero de `dist/`
 * ESM que arranca con el shebang de Node: sin él, `npx rayito` lo ejecutaría
 * el shell. Un ejecutable nunca es una condición de `exports`: la biblioteca
 * no lo importa.
 */
function assertBinaryEntriesAreExecutable() {
  const manifest = JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf-8"));
  const problems = Object.entries(manifest.bin ?? {}).flatMap(([name, target]) => {
    const file = join(PACKAGE_DIR, String(target));
    if (!String(target).endsWith(".mjs")) {
      return [`bin ${name}: ${target} debe ser un .mjs (sólo ESM)`];
    }
    return readFileSync(file, "utf-8").startsWith(SHEBANG)
      ? []
      : [`bin ${name}: ${target} no empieza por ${SHEBANG}`];
  });
  if (problems.length > 0) {
    throw new Error(`bin de package.json mal formado: ${problems.join("; ")}`);
  }
}

function assertNoRuntimeOpenTelemetryImport() {
  const offenders = readdirSync(DIST_DIR)
    .filter((name) => name.endsWith(".mjs") || name.endsWith(".cjs"))
    .filter((name) =>
      RUNTIME_OPENTELEMETRY_IMPORT.test(readFileSync(join(DIST_DIR, name), "utf-8")),
    );
  if (offenders.length > 0) {
    throw new Error(
      `dist/ importa @opentelemetry/api en tiempo de ejecución (debería ser sólo \`import type\`, borrado en el build): ${offenders.join(", ")}`,
    );
  }
}

function assertCdkImportsOnlyInCdkEntry() {
  const scripts = readdirSync(DIST_DIR).filter(
    (name) => name.endsWith(".mjs") || name.endsWith(".cjs"),
  );
  const offenders = scripts
    .filter((name) => !CDK_ENTRY.test(name))
    .filter((name) => RUNTIME_CDK_IMPORT.test(readFileSync(join(DIST_DIR, name), "utf-8")));
  if (offenders.length > 0) {
    throw new Error(
      `dist/ importa aws-cdk-lib o constructs fuera de la entrada cdk: ${offenders.join(", ")}`,
    );
  }
  const entries = scripts.filter((name) => CDK_ENTRY.test(name));
  const missing = entries.filter(
    (name) => !RUNTIME_CDK_IMPORT.test(readFileSync(join(DIST_DIR, name), "utf-8")),
  );
  if (entries.length === 0 || missing.length > 0) {
    throw new Error(
      "la entrada cdk de dist/ no importa aws-cdk-lib: la comprobación no prueba nada",
    );
  }
}

/**
 * Cada subruta de `exports` que no es un fichero suelto lleva exactamente las
 * condiciones `import` y `require`, cada una con `types` y `default` de la
 * extensión de su formato (`CONDITION_EXTENSIONS`) y ningún `types` hermano.
 */
function assertTypesMatchEachCondition() {
  const manifest = JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf-8"));
  const problems = Object.entries(manifest.exports)
    .filter(([, target]) => typeof target === "object")
    .flatMap(([subpath, target]) => conditionProblems(subpath, target));
  if (problems.length > 0) {
    throw new Error(`exports de package.json mal formado: ${problems.join("; ")}`);
  }
}

function conditionProblems(subpath, target) {
  const expected = Object.keys(CONDITION_EXTENSIONS);
  const unexpected = Object.keys(target).filter((condition) => !expected.includes(condition));
  const keyProblems = unexpected.map((condition) => `${subpath}: condición "${condition}" sobra`);
  const fileProblems = expected.flatMap((condition) =>
    Object.entries(CONDITION_EXTENSIONS[condition])
      .filter(([field, extension]) => !String(target[condition]?.[field]).endsWith(extension))
      .map(([field, extension]) => `${subpath}: ${condition}.${field} debe acabar en ${extension}`),
  );
  return [...keyProblems, ...fileProblems];
}

/**
 * La SPA del tarball es la canónica del paquete de Python, byte a byte
 * (ADR-027, D4): falla si falta un fichero, sobra uno o alguno difiere. El
 * build la copia de `SOURCE_DIR`, así que una diferencia es un tarball hecho
 * con un `dist/` viejo o una copia tocada a mano.
 */
function assertPackedDashboardMatchesSource() {
  const extractDir = join(PACK_DIR, EXTRACT_DIR_NAME);
  mkdirSync(extractDir);
  const tarball = readdirSync(PACK_DIR).find((name) => name.endsWith(".tgz"));
  execFileSync(
    "tar",
    [
      "-xzf",
      join(PACK_DIR_NAME, tarball),
      "-C",
      join(PACK_DIR_NAME, EXTRACT_DIR_NAME),
      PACKED_STATIC_ENTRY,
    ],
    {
      cwd: PACKAGE_DIR,
    },
  );
  const problems = compareDirectories(SOURCE_DIR, join(extractDir, PACKED_STATIC_ENTRY));
  if (problems.length > 0) {
    throw new Error(`la SPA del tarball no es idéntica a la de Python: ${problems.join(", ")}`);
  }
}

function packedEntries() {
  execFileSync("pnpm", ["pack", "--pack-destination", PACK_DIR_NAME], {
    cwd: PACKAGE_DIR,
    stdio: ["ignore", "ignore", "inherit"],
    shell: process.platform === "win32",
  });
  const tarball = readdirSync(PACK_DIR).find((name) => name.endsWith(".tgz"));
  if (tarball === undefined) {
    throw new Error("pnpm pack no produjo ningún .tgz");
  }
  const listing = execFileSync("tar", ["-tzf", join(PACK_DIR_NAME, tarball)], {
    cwd: PACKAGE_DIR,
    encoding: "utf8",
  });
  return listing
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .sort();
}

main();
