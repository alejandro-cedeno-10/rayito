/**
 * `E2B` de E2B JS 2.51: un cliente cuyas opciones de conexión quedan ligadas
 * a `client.Sandbox` (gana la llamada salvo `undefined`; `headers` no se
 * fusiona). Las opciones ignoradas avisan una vez, al construir el cliente.
 * `client.Secret` usa su `region` (Secrets Manager en esa cuenta);
 * `client.Template` (m15-templates) usa su `region` y `bucket` (extensión
 * de Rayito: el bucket de artefactos de `Template.build`). `client.Volume`
 * (m15-efs-volumes, experimental) sólo funciona con `volumeStore` en las
 * opciones del constructor — sin él, `UnimplementedError`.
 */

import { type DynamoDbIndex, validateIndex } from "../index/dynamodb.js";
import type { VolumeStore } from "../volumes/store.js";
import { emitIgnoredWarnings, IGNORED_CONNECTION_OPTS, splitConnectionOpts } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { bindSandbox, type Sandbox } from "./sandbox.js";
import { bindSecret, type Secret } from "./secret.js";
import { bindTemplate, type Template } from "./template.js";
import { unimplemented } from "./unimplemented.js";
import { bindVolume, type Volume } from "./volume.js";

/**
 * Las opciones de `new E2B({...})`: las de conexión y, como extensiones de
 * Rayito, `index` (M14, `undefined` por defecto), que `client.Sandbox.list`
 * usa cuando la llamada no pasa otro (ver `DynamoDbIndex`, "Coste y
 * activación"), y `bucket` (m15-templates, `undefined` por defecto), el
 * bucket de artefactos que `client.Template.build` usa si la llamada no
 * pasa otro. Ninguna otra llamada los usa.
 */
export type E2BClientOpts = ConnectionOpts & {
  readonly index?: DynamoDbIndex | undefined;
  readonly bucket?: string | undefined;
  /** m15-efs-volumes, experimental: liga `client.Volume` a este store. */
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
  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  readonly Template: typeof Template;
  readonly #volumeStore: VolumeStore | undefined;

  constructor(opts: E2BClientOpts = {}) {
    const { ignored } = splitConnectionOpts(opts);
    emitIgnoredWarnings(ignored);
    validateIndex(opts.index);
    this.Sandbox = bindSandbox(withoutIgnored(opts), opts.volumeStore);
    this.Secret = bindSecret(opts.region === undefined ? {} : { region: opts.region });
    this.Template = bindTemplate({
      ...(opts.region === undefined ? {} : { region: opts.region }),
      ...(opts.bucket === undefined ? {} : { bucket: opts.bucket }),
    });
    this.#volumeStore = opts.volumeStore;
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Volume(): typeof Volume {
    if (this.#volumeStore === undefined) {
      throw unimplemented("Volume");
    }
    return bindVolume(this.#volumeStore);
  }
}
