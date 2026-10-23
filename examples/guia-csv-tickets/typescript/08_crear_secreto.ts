// Guarda la clave del modelo en Secrets Manager leyendo el entorno; nunca imprime el valor.
import { SecretStore } from "rayito";

const NAME = process.env.MODEL_SECRET ?? "bedrock-key"; // queda como rayito/<NAME>
const REGION = process.env.AWS_REGION ?? "us-east-1";
// Mensaje fijo: no se imprime nada leído del entorno (ni el nombre ni, por supuesto, el valor).
const SALIDA = "en Secrets Manager: rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)";
const raw = process.env.AWS_BEARER_TOKEN_BEDROCK;
if (!raw) throw new Error("define AWS_BEARER_TOKEN_BEDROCK en el entorno");
const value = raw.startsWith("Bearer ") ? raw : `Bearer ${raw}`;

const store = new SecretStore({ region: REGION });
if (await store.exists(NAME)) {
  await store.update(NAME, value);
  console.log(`secreto actualizado ${SALIDA}`);
} else {
  await store.create(NAME, value);
  console.log(`secreto creado ${SALIDA}`);
}
