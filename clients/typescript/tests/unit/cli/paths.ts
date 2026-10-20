/** Rutas de los datos compartidos con la CLI de Python (`testdata/cli/`). */

import { readFileSync } from "node:fs";
import { join } from "node:path";

const TESTDATA_CLI = join(import.meta.dirname, "..", "..", "..", "..", "..", "testdata", "cli");

export function loadCliTestdata<T>(name: string): T {
  return JSON.parse(readFileSync(join(TESTDATA_CLI, name), "utf8")) as T;
}

const TESTDATA_ROOT = join(import.meta.dirname, "..", "..", "..", "..", "..", "testdata");

/** Un fichero JSON de `testdata/<carpeta>/` compartido con la CLI de Python. */
export function loadTestdata<T>(folder: string, name: string): T {
  return JSON.parse(readFileSync(join(TESTDATA_ROOT, folder, name), "utf8")) as T;
}

/** Un fichero JSON de `testdata/account-setup/` (el contrato y los casos de `rayito setup`). */
export function loadAccountSetupTestdata<T>(name: string): T {
  return loadTestdata<T>("account-setup", name);
}
