import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

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
];

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

/**
 * Verifica que `pnpm pack` produce un tarball con los ficheros publicables
 * (build, tipos, README, `LICENSE` y `NOTICE`). `pnpm pack --dry-run` no existe
 * en pnpm 9, así que se empaqueta de verdad en `.pack/` y se lista con `tar`.
 * Rutas relativas: GNU tar toma `D:\...` por un host remoto. Además (M13b)
 * comprueba que ningún `.mjs`/`.cjs` de `dist/` (incluidos los chunks
 * compartidos) importa `@opentelemetry/api` en tiempo de ejecución.
 */
function main() {
  assertNoRuntimeOpenTelemetryImport();
  rmSync(PACK_DIR, { recursive: true, force: true });
  mkdirSync(PACK_DIR);
  try {
    const entries = packedEntries();
    const missing = REQUIRED_ENTRIES.filter((entry) => !entries.includes(entry));
    console.log(entries.join("\n"));
    if (missing.length > 0) {
      throw new Error(`faltan en el tarball: ${missing.join(", ")}`);
    }
  } finally {
    rmSync(PACK_DIR, { recursive: true, force: true });
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
