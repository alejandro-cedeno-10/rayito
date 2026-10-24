/**
 * Borra el secreto del laboratorio.
 * Por defecto programa el borrado con ventana de recuperación (7 días, se puede deshacer con
 * `aws secretsmanager restore-secret`). Con --force lo borra sin recuperación
 * (`ForceDeleteWithoutRecovery`): úsalo sólo con secretos creados para esta prueba. Los mensajes
 * son fijos: no se imprime nada leído del entorno.
 */
import { DeleteSecretCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { SecretStore } from "rayito";

const NAME = process.env.MODEL_SECRET ?? "bedrock-key";
const REGION = process.env.AWS_REGION ?? "us-east-1";
const FORCE_FLAG = "--force";
const RECOVERY_WINDOW_DAYS = 7;
const TOLERATED_ERRORS = new Set(["ResourceNotFoundException", "InvalidRequestException"]);
const ENTRY_LABEL = "rayito/<MODEL_SECRET> (por defecto rayito/bedrock-key)";

/** Borra el secreto sin ventana de recuperación. */
async function forceDelete(): Promise<void> {
  const deleted = await new SecretStore({ region: REGION }).destroy(NAME);
  console.log(deleted ? `${ENTRY_LABEL}: borrado sin recuperación` : `${ENTRY_LABEL}: no existía`);
}

/** Programa el borrado con la ventana de recuperación. */
async function scheduleDelete(): Promise<void> {
  const client = new SecretsManagerClient({ region: REGION });
  try {
    await client.send(
      new DeleteSecretCommand({ SecretId: `rayito/${NAME}`, RecoveryWindowInDays: RECOVERY_WINDOW_DAYS }),
    );
  } catch (err) {
    if (!(err instanceof Error) || !TOLERATED_ERRORS.has(err.name)) throw err;
    console.log(`${ENTRY_LABEL}: no existía o ya tenía el borrado programado`);
    return;
  }
  console.log(`${ENTRY_LABEL}: borrado programado en ${RECOVERY_WINDOW_DAYS} días (recuperable con restore-secret)`);
}

await (process.argv.includes(FORCE_FLAG) ? forceDelete() : scheduleDelete());
