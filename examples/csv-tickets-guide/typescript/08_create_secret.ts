/**
 * Guarda la clave del modelo en Secrets Manager leyendo `AWS_BEARER_TOKEN_BEDROCK` del entorno;
 * nunca imprime el valor. El secreto queda como `rayito/<MODEL_SECRET>`. El mensaje es fijo: no se
 * imprime nada leído del entorno (ni el nombre ni, por supuesto, el valor). A diferencia de la
 * versión de Python, no tiene `--generate`.
 */
import { SecretStore } from "rayito";

const NAME = process.env.MODEL_SECRET ?? "bedrock-key";
const REGION = process.env.AWS_REGION ?? "us-east-1";
const BEARER_PREFIX = "Bearer ";
const OUTPUT = "en Secrets Manager: rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)";

async function main(): Promise<void> {
  const raw = process.env.AWS_BEARER_TOKEN_BEDROCK;
  if (!raw) throw new Error("define AWS_BEARER_TOKEN_BEDROCK en el entorno");
  const value = raw.startsWith(BEARER_PREFIX) ? raw : `${BEARER_PREFIX}${raw}`;
  const store = new SecretStore({ region: REGION });
  if (await store.exists(NAME)) {
    await store.update(NAME, value);
    console.log(`secreto actualizado ${OUTPUT}`);
  } else {
    await store.create(NAME, value);
    console.log(`secreto creado ${OUTPUT}`);
  }
}

await main();
