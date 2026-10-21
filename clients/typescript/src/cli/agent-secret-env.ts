/**
 * Lo que `rayito agent deploy` dice de `secretEnv` antes de llamar al SDK
 * (ADR-033 D4). Espejo de `rayito.cli._agent_secret_env`: el secreto que
 * `--create-secrets-from` creará o actualizará y con qué variables (sólo
 * nombres, nunca valores), los avisos `env_missing` y la confirmación que pide
 * porque cuesta. Sin AWS ni SDK del agente: sólo textos.
 */

export const CREATE_SECRETS_LINE =
  "secreto {name}: se crea o se actualiza con {variables} (cuesta $0,40 al mes hasta que lo " +
  "borres)";
export const CREATE_SECRETS_NEEDS_CONFIRMATION =
  "--create-secrets-from crea un secreto que cuesta dinero: confírmalo con --yes (o desde una " +
  "terminal)";
export const ENV_WARNING_LINE = "aviso: {warning}";

/** La línea del secreto de `--create-secrets-from`: su nombre y sus variables. */
export function envFileSecretLine(secret: {
  readonly name: string;
  readonly variables: readonly string[];
}): string {
  return CREATE_SECRETS_LINE.replace("{name}", secret.name).replace(
    "{variables}",
    secret.variables.join(", "),
  );
}

export function envWarningLine(warning: string): string {
  return ENV_WARNING_LINE.replace("{warning}", warning);
}
