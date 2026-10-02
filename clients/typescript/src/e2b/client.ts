/**
 * `E2B` de E2B JS 2.51: un cliente cuyas opciones de conexión quedan ligadas
 * a `client.Sandbox` (gana la llamada salvo `undefined`; `headers` no se
 * fusiona). Las opciones ignoradas avisan una vez, al construir el cliente.
 * `client.Secret` usa su `region` (Secrets Manager en esa cuenta); `Template`
 * lanza `UnimplementedError` al leerlo. `client.Volume` (m15-efs-volumes,
 * experimental) sólo funciona con `volumeStore` en las opciones del
 * constructor — sin él, también `UnimplementedError`.
 */

import { type DynamoDbIndex, validateIndex } from "../index/dynamodb.js";
import type { VolumeStore } from "../volumes/store.js";
import { emitIgnoredWarnings, IGNORED_CONNECTION_OPTS, splitConnectionOpts } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { bindSandbox, type Sandbox } from "./sandbox.js";
import { bindSecret, type Secret } from "./secret.js";
import { unimplemented } from "./unimplemented.js";
import { bindVolume, type Volume } from "./volume.js";

/**
 * Las opciones de `new E2B({...})`: las de conexión y, como extensión de
 * Rayito, `index` (M14, `undefined` por defecto), que `client.Sandbox.list`
 * usa cuando la llamada no pasa otro (ver `DynamoDbIndex`, "Coste y
 * activación"). Ninguna otra llamada lo usa.
 */
export type E2BClientOpts = ConnectionOpts & {
  readonly index?: DynamoDbIndex | undefined;
  /** m15-efs-volumes, experimental: liga `client.Volume`/`client.AsyncVolume` a este store. */
  readonly volumeStore?: VolumeStore | undefined;
};

function withoutIgnored(opts: ConnectionOpts): ConnectionOpts {
  return Object.fromEntries(
    Object.entries(opts).filter(([name]) => !Object.hasOwn(IGNORED_CONNECTION_OPTS, name)),
  ) as ConnectionOpts;
}

// biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
export class E2B {
  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  readonly Sandbox: typeof Sandbox;
  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  readonly Secret: typeof Secret;
  readonly #volumeStore: VolumeStore | undefined;

  constructor(opts: E2BClientOpts = {}) {
    const { ignored } = splitConnectionOpts(opts);
    emitIgnoredWarnings(ignored);
    validateIndex(opts.index);
    this.Sandbox = bindSandbox(withoutIgnored(opts), opts.volumeStore);
    this.Secret = bindSecret(opts.region === undefined ? {} : { region: opts.region });
    this.#volumeStore = opts.volumeStore;
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Template(): never {
    throw unimplemented("Template");
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Volume(): typeof Volume {
    if (this.#volumeStore === undefined) {
      throw unimplemented("Volume");
    }
    return bindVolume(this.#volumeStore);
  }
}
