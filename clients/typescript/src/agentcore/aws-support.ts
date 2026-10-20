/**
 * Lo común de los adaptadores de AWS de `--target agentcore` (ADR-031, D8):
 * cargar el peer opcional de un servicio en su primer uso, mandar un comando y
 * traducir los errores del SDK por su código (`awsCode`, nunca por el mensaje)
 * a `AgentCoreError`. El `cause` pasa por `sanitizeAwsError`. Construir un
 * adaptador no carga ningún peer ni llama a AWS.
 */

import { type AwsClientSettings, clientConfig } from "../aws/control-plane.js";
import { awsCode, isAwsSdkError, type OptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { AgentCoreError } from "../errors.js";
import { loadOptionalPeer } from "../optional.js";

export interface AwsSettings extends AwsClientSettings {
  readonly region: string;
}

export const ACCESS_DENIED_CODES: ReadonlySet<string> = new Set([
  "AccessDeniedException",
  "AccessDenied",
]);
export const NOT_FOUND_CODE = "ResourceNotFoundException";
export const CONFLICT_CODE = "ConflictException";
const UNKNOWN_CODE = "Unknown";

type ClientClass = new (config: object) => { send(command: unknown): Promise<unknown> };
type Send = <T>(command: unknown, options?: { readonly abortSignal?: AbortSignal }) => Promise<T>;

export interface LoadedClient<M> {
  readonly sdk: M;
  readonly send: Send;
}

/** Importa el peer `peer` y construye su cliente con la configuración común del SDK más `extra` (por ejemplo un `requestHandler` con otro plazo). */
export async function loadClient<M>(
  peer: string,
  feature: string,
  clientClass: (sdk: M) => ClientClass,
  settings: AwsSettings,
  extra: object = {},
): Promise<LoadedClient<M>> {
  const sdk = await loadOptionalPeer<M>(peer, feature);
  const Client = clientClass(sdk);
  const client = new Client({
    ...clientConfig(settings.region, {
      ...(settings.credentials === undefined ? {} : { credentials: settings.credentials }),
      ...(settings.proxy === undefined ? {} : { proxy: settings.proxy }),
    }),
    ...extra,
  });
  return {
    sdk,
    send: <T>(command: unknown, options?: { readonly abortSignal?: AbortSignal }) =>
      (client.send as (command: unknown, options?: object) => Promise<T>)(command, options),
  };
}

/** Un cliente que se carga en la primera llamada; si falla (p. ej. falta el peer), la siguiente vuelve a intentarlo. */
export class LazyClient<M> {
  readonly #load: () => Promise<LoadedClient<M>>;
  #pending: Promise<LoadedClient<M>> | undefined;

  constructor(load: () => Promise<LoadedClient<M>>) {
    this.#load = load;
  }

  get(): Promise<LoadedClient<M>> {
    if (this.#pending === undefined) {
      const pending = this.#load();
      pending.catch(() => {
        if (this.#pending === pending) {
          this.#pending = undefined;
        }
      });
      this.#pending = pending;
    }
    return this.#pending;
  }
}

export function codeOf(error: unknown): string | undefined {
  return awsCode(error);
}

/**
 * El error de una operación de AWS sin traducción propia: `access_denied` si es
 * un acceso denegado, `aws_error` con la operación y el código si es otro
 * error del servicio, y el mismo error si no lo es (credenciales, red).
 */
export function awsFailure(operation: string, error: unknown): unknown {
  if (error instanceof AgentCoreError || !isAwsSdkError(error)) {
    return error;
  }
  const code = awsCode(error) ?? UNKNOWN_CODE;
  const cause = sanitizeAwsError(error, { includeMessage: false });
  return new AgentCoreError(
    ACCESS_DENIED_CODES.has(code) ? "access_denied" : "aws_error",
    { operation, awsCode: code },
    { cause },
  );
}

export type { OptionalSdkClient };
