// Volver a un sandbox con su id y su access token (el token es un secreto).
import { Sandbox } from "rayito";
import { METADATA, TEMPLATE } from "./comun.ts";

const sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: 600_000, metadata: METADATA });
try {
  await sbx.files.write("/home/user/estado.txt", "paso 1 hecho");
  const { sandboxId, accessToken } = sbx; // nunca lo imprimas
  const again = await Sandbox.connect(sandboxId, { accessToken }); // desde otro proceso, igual
  console.log(await again.files.read("/home/user/estado.txt"));
} finally {
  await sbx.kill();
}
