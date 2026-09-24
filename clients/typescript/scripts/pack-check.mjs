import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
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

/**
 * Verifica que `pnpm pack` produce un tarball con los ficheros publicables
 * (build, tipos, README, `LICENSE` y `NOTICE`). `pnpm pack --dry-run` no existe
 * en pnpm 9, así que se empaqueta de verdad en `.pack/` y se lista con `tar`.
 * Rutas relativas: GNU tar toma `D:\...` por un host remoto.
 */
function main() {
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
