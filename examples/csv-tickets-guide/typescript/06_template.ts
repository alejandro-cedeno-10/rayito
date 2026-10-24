/**
 * Una imagen propia sobre rayito-base. Ejecútalo desde la carpeta examples/csv-tickets-guide/:
 * `copy("app/")` se resuelve contra el directorio actual. `RAYITO_BUCKET` es el bucket de
 * artefactos que imprimió `rayito setup`. Cada versión nueva factura almacenamiento de snapshot
 * (mínimo una semana).
 */
import { existsSync } from "node:fs";
import { Sandbox, Template, waitForPort } from "rayito";
import { localCsv, METADATA, SANDBOX_CSV } from "./common.ts";

const BUCKET = process.env.RAYITO_BUCKET;
const NAME = process.env.TEMPLATE_NAME ?? "tickets-demo";
const SANDBOX_TIMEOUT_MS = 300_000;
const COMMAND_TIMEOUT_MS = 30_000;
const APP_TIMEOUT_MS = 60_000;
const SERVER_PORT = 8000;
const BASE_IMAGE = "rayito-base";
const APP_FILE = "summary.py";

const template = new Template()
  .fromBaseImage(BASE_IMAGE)
  .pipInstall(["tabulate==0.9.0"])
  .copy("app/", "/srv/app/")
  .setEnvs({ MODE: "prod" })
  .setStartCmd(`python3 -m http.server ${SERVER_PORT} -d /srv/app`, waitForPort(SERVER_PORT));

/** Comprueba la dependencia, el servidor y la app dentro del sandbox nuevo. */
async function checkImage(sbx: Sandbox): Promise<void> {
  console.log(`imagen ${NAME}, versión ${(await sbx.getInfo()).templateVersion}`);
  const version = await sbx.commands.run("python3 -c 'import tabulate; print(tabulate.__version__)'", {
    timeoutMs: COMMAND_TIMEOUT_MS,
  });
  console.log(`tabulate ${version.stdout.trim()}`);
  const listing = (await sbx.commands.run(`curl -s localhost:${SERVER_PORT}/`, { timeoutMs: COMMAND_TIMEOUT_MS }))
    .stdout;
  console.log(`servidor en :${SERVER_PORT} sirve ${APP_FILE}:`, listing.includes(APP_FILE));
  await sbx.files.write(SANDBOX_CSV, localCsv());
  const app = await sbx.commands.run(`python3 /srv/app/${APP_FILE} ${SANDBOX_CSV}`, { timeoutMs: APP_TIMEOUT_MS });
  process.stdout.write(app.stdout);
}

async function main(): Promise<void> {
  if (!BUCKET) throw new Error("falta RAYITO_BUCKET (lo imprime rayito setup)");
  if (!existsSync(`app/${APP_FILE}`)) {
    throw new Error(`ejecuta desde la carpeta examples/csv-tickets-guide/ (falta app/${APP_FILE})`);
  }
  const info = await Template.build(template, NAME, { bucket: BUCKET });
  await using sbx = await Sandbox.create({
    template: info.templateId,
    timeoutMs: SANDBOX_TIMEOUT_MS,
    metadata: METADATA,
  });
  await checkImage(sbx);
}

await main();
