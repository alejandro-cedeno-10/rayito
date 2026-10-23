// Un comando, un fichero de ida y vuelta y una celda con estado.
import { Sandbox } from "rayito";
import { METADATA, TEMPLATE, runCell } from "./comun.ts";

// await using llama a kill() al salir del bloque, también si algo lanza
await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: 600_000, metadata: METADATA });
process.stdout.write((await sbx.commands.run("echo hola", { timeoutMs: 30_000 })).stdout);
await sbx.files.write("/home/user/a.txt", "contenido");
console.log(await sbx.files.read("/home/user/a.txt"));
console.log((await runCell(sbx, "x = 40; x + 2", "celda con estado")).text);
