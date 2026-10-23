// Borra el secreto del laboratorio.
// Por defecto programa el borrado con ventana de recuperación (7 días, se puede deshacer con
// `aws secretsmanager restore-secret`). Con --forzar lo borra sin recuperación: úsalo sólo con
// secretos creados para esta prueba.
import { DeleteSecretCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { SecretStore } from "rayito";

const NAME = process.env.MODEL_SECRET ?? "bedrock-key"; // queda como rayito/<NAME>
const REGION = process.env.AWS_REGION ?? "us-east-1";
const RECOVERY_WINDOW_DAYS = 7;

if (process.argv.includes("--forzar")) {
  const borrado = await new SecretStore({ region: REGION }).destroy(NAME); // ForceDeleteWithoutRecovery
  console.log(`rayito/${NAME}: ${borrado ? "borrado sin recuperación" : "no existía"}`);
} else {
  const sm = new SecretsManagerClient({ region: REGION });
  await sm.send(new DeleteSecretCommand({ SecretId: `rayito/${NAME}`, RecoveryWindowInDays: RECOVERY_WINDOW_DAYS }));
  console.log(`rayito/${NAME}: borrado programado en ${RECOVERY_WINDOW_DAYS} días (recuperable con restore-secret)`);
}
