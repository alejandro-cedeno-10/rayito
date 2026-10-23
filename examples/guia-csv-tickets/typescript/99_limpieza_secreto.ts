// Borra el secreto del laboratorio.
// Por defecto programa el borrado con ventana de recuperación (7 días, se puede deshacer con
// `aws secretsmanager restore-secret`). Con --forzar lo borra sin recuperación: úsalo sólo con
// secretos creados para esta prueba.
import { DeleteSecretCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { SecretStore } from "rayito";

const NAME = process.env.MODEL_SECRET ?? "bedrock-key"; // queda como rayito/<NAME>
const REGION = process.env.AWS_REGION ?? "us-east-1";
const RECOVERY_WINDOW_DAYS = 7;
// Mensajes fijos: no se imprime nada leído del entorno.
const SECRETO = "rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)";

if (process.argv.includes("--forzar")) {
  const borrado = await new SecretStore({ region: REGION }).destroy(NAME); // ForceDeleteWithoutRecovery
  console.log(borrado ? `${SECRETO}: borrado sin recuperación` : `${SECRETO}: no existía`);
} else {
  const sm = new SecretsManagerClient({ region: REGION });
  try {
    await sm.send(new DeleteSecretCommand({ SecretId: `rayito/${NAME}`, RecoveryWindowInDays: RECOVERY_WINDOW_DAYS }));
  } catch (err) {
    const name = err instanceof Error ? err.name : "";
    if (name !== "ResourceNotFoundException" && name !== "InvalidRequestException") throw err;
    console.log(`${SECRETO}: no existía o ya tenía el borrado programado`);
    process.exit(0);
  }
  console.log(`${SECRETO}: borrado programado en 7 días (recuperable con restore-secret)`);
}
