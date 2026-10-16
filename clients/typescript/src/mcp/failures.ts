/**
 * Cómo se describe al modelo un fallo del SDK de AWS que no es de Rayito:
 * sólo el código y el mensaje redactado (`redactAwsText`), nunca un mensaje
 * crudo.
 */

import { awsCode } from "../aws/optional-client.js";
import { redactAwsText } from "../aws/sanitize.js";

const CREDENTIALS_ERROR_NAME = "CredentialsProviderError";

/** Un error del SDK de AWS v3: lleva `$metadata`/`$fault`, o es la ausencia de credenciales. */
export function isAwsSdkFailure(error: Error): boolean {
  return error.name === CREDENTIALS_ERROR_NAME || "$metadata" in error || "$fault" in error;
}

export function describeAwsFailure(error: Error): string {
  return `${awsCode(error) ?? error.name}: ${redactAwsText(error.message)}`;
}
