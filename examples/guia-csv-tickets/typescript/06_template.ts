// Una imagen propia sobre rayito-base. Ejecútalo desde la carpeta examples/guia-csv-tickets/:
// copy("app/") se resuelve contra el directorio actual.
import { existsSync } from "node:fs";
import { Sandbox, Template, waitForPort } from "rayito";
import { CSV_SANDBOX, METADATA, csvLocal } from "./comun.ts";

const BUCKET = process.env.RAYITO_BUCKET; // el bucket de artefactos que imprimió rayito setup
const NAME = process.env.TEMPLATE_NAME ?? "tickets-demo";
if (!BUCKET) throw new Error("falta RAYITO_BUCKET (lo imprime rayito setup)");
if (!existsSync("app/resumen.py")) throw new Error("ejecuta desde la carpeta examples/guia-csv-tickets/ (falta app/resumen.py)");

const template = new Template()
  .fromBaseImage("rayito-base")
  .pipInstall(["tabulate==0.9.0"])
  .copy("app/", "/srv/app/")
  .setEnvs({ MODE: "prod" })
  .setStartCmd("python3 -m http.server 8000 -d /srv/app", waitForPort(8000));

const info = await Template.build(template, NAME, { bucket: BUCKET });
await using sbx = await Sandbox.create({ template: info.templateId, timeoutMs: 300_000, metadata: METADATA });
console.log(`imagen ${NAME}, versión ${(await sbx.getInfo()).templateVersion}`);
const version = await sbx.commands.run("python3 -c 'import tabulate; print(tabulate.__version__)'", { timeoutMs: 30_000 });
console.log(`tabulate ${version.stdout.trim()}`);
const listing = (await sbx.commands.run("curl -s localhost:8000/", { timeoutMs: 30_000 })).stdout;
console.log("servidor en :8000 sirve resumen.py:", listing.includes("resumen.py"));
await sbx.files.write(CSV_SANDBOX, csvLocal());
process.stdout.write((await sbx.commands.run(`python3 /srv/app/resumen.py ${CSV_SANDBOX}`, { timeoutMs: 60_000 })).stdout);
