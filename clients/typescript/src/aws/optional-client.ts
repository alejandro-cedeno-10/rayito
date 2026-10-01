/**
 * Clientes del SDK de AWS v3 para funciones opcionales (ADR-014): el cliente
 * de un peer opcional (`@aws-sdk/client-secrets-manager`,
 * `@aws-sdk/client-dynamodb`) se carga y se construye en el primer uso,
 * nunca al construir el objeto de configuración (`SecretStore`,
 * `DynamoDbIndex`). Así, con la opción apagada no hay importación dinámica
 * ni llamada a AWS.
 */

import { InvalidArgumentError } from "../errors.js";
import { loadOptionalPeer } from "../optional.js";
import type { AwsClientSettings } from "./control-plane.js";
import { clientConfig } from "./control-plane.js";

type Credentials = AwsClientSettings["credentials"];

/** El código de error de AWS de una excepción del SDK v3 (su `name`). */
export function awsCode(error: unknown): string | undefined {
  const name = (error as { name?: unknown } | null)?.name;
  return typeof name === "string" ? name : undefined;
}

/** Un cliente del SDK v3 ya construido: `send` manda un `…Command` de `sdk`. */
export interface OptionalSdkClient<M> {
  readonly sdk: M;
  send<T>(command: unknown): Promise<T>;
}

type SdkClientClass = new (config: object) => { send(command: unknown): Promise<unknown> };

/**
 * Importa el peer opcional `peer` y construye su cliente (`clientClass`) con
 * la `clientConfig` del SDK.
 *
 * @param peer - paquete npm del cliente (`@aws-sdk/client-dynamodb`).
 * @param feature - la función que lo necesita, para el mensaje si falta.
 * @param clientClass - elige la clase del cliente en el módulo importado.
 */
export async function loadOptionalSdkClient<M>(
  peer: string,
  feature: string,
  clientClass: (sdk: M) => SdkClientClass,
  region: string,
  credentials: Credentials,
): Promise<OptionalSdkClient<M>> {
  const sdk = await loadOptionalPeer<M>(peer, feature);
  const Client = clientClass(sdk);
  const client = new Client(clientConfig(region, credentials === undefined ? {} : { credentials }));
  return { sdk, send: <T>(command: unknown) => client.send(command) as Promise<T> };
}

/**
 * Memoiza la construcción perezosa de un adaptador: `get()` lo construye en
 * la primera llamada (con la región de `region` o de `AWS_REGION` /
 * `AWS_DEFAULT_REGION`) y reutiliza la misma promesa después; si la
 * construcción falla (p. ej. falta el peer), el siguiente `get()` reintenta.
 */
export class LazyAwsApi<T> {
  readonly #region: string | undefined;
  readonly #missingRegion: string;
  readonly #build: (region: string) => Promise<T>;
  #pending: Promise<T> | undefined;

  /**
   * @param region - la región explícita, si la hay.
   * @param missingRegion - el mensaje de `InvalidArgumentError` si no hay región.
   * @param build - construye el adaptador para una región.
   * @param preset - un adaptador ya construido (p. ej. el `client` de tests).
   */
  constructor(
    region: string | undefined,
    missingRegion: string,
    build: (region: string) => Promise<T>,
    preset?: T,
  ) {
    this.#region = region;
    this.#missingRegion = missingRegion;
    this.#build = build;
    this.#pending = preset === undefined ? undefined : Promise.resolve(preset);
  }

  get(): Promise<T> {
    if (this.#pending === undefined) {
      const region = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
      if (!region) {
        return Promise.reject(new InvalidArgumentError(this.#missingRegion));
      }
      const pending = this.#build(region);
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
