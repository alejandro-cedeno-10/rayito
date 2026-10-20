/**
 * Traducción de errores a una línea y un código de salida, en un solo sitio
 * (espejo de `translated_failures` de `rayito.cli._console`): el uso y el
 * entorno incorrectos salen con 2, una operación fallida con 1. Los mensajes
 * de los errores del SDK ya vienen sin URL, bucket, clave ni mensaje crudo de
 * AWS; un error crudo del SDK de AWS pasa por `redactAwsText`.
 */

import { awsCode, isAwsSdkError } from "../aws/optional-client.js";
import { redactAwsText } from "../aws/sanitize.js";
import {
  AgentCoreError,
  AuthenticationError,
  CapacityError,
  QuotaExceededError,
  SandboxError,
  UnimplementedError,
} from "../errors.js";
import { AbortedError } from "./console.js";
import { EXIT_FAILURE, EXIT_USAGE } from "./exit-codes.js";

export const NO_REGION_MESSAGE =
  "sin región: pasa --region o exporta AWS_REGION (o AWS_DEFAULT_REGION)";
export const NO_CREDENTIALS_MESSAGE =
  "sin credenciales de AWS: configura un perfil (--profile / AWS_PROFILE), " +
  "variables de entorno o el rol de la máquina";
const CREDENTIALS_ERROR_NAME = "CredentialsProviderError";
const UNEXPECTED_FAILURE_PREFIX = "error inesperado";

/** Error de uso o de entorno (código 2): región, perfil, credenciales o token ausentes. */
export class UsageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UsageError";
  }
}

export interface TranslatedFailure {
  /** La línea completa que va a stderr. */
  readonly line: string;
  readonly code: number;
}

function sdkFailureTypes(error: unknown): boolean {
  return (
    error instanceof SandboxError ||
    error instanceof UnimplementedError ||
    error instanceof AuthenticationError ||
    error instanceof QuotaExceededError ||
    error instanceof CapacityError
  );
}

function rayitoLine(message: string): string {
  return `rayito: ${message}`;
}

/** La línea y el código de salida de `error`; con `verbose`, un error ajeno añade su pila. */
export function translateFailure(error: unknown, verbose = false): TranslatedFailure {
  if (error instanceof UsageError) {
    return { line: rayitoLine(error.message), code: EXIT_USAGE };
  }
  if (error instanceof AbortedError) {
    return { line: error.message, code: EXIT_FAILURE };
  }
  if (error instanceof Error && error.name === CREDENTIALS_ERROR_NAME) {
    return { line: rayitoLine(NO_CREDENTIALS_MESSAGE), code: EXIT_USAGE };
  }
  if (error instanceof AgentCoreError) {
    return { line: rayitoLine(error.message), code: error.exitCode };
  }
  if (sdkFailureTypes(error)) {
    return { line: rayitoLine((error as Error).message), code: EXIT_FAILURE };
  }
  if (isAwsSdkError(error)) {
    const code = awsCode(error) ?? "Unknown";
    return {
      line: rayitoLine(`AWS error ${code}: ${redactAwsText(error.message)}`),
      code: EXIT_FAILURE,
    };
  }
  const name = error instanceof Error ? error.name : typeof error;
  const detail = verbose && error instanceof Error ? `\n${error.stack ?? ""}` : "";
  return {
    line: rayitoLine(`${UNEXPECTED_FAILURE_PREFIX} (${name})${detail}`),
    code: EXIT_FAILURE,
  };
}
