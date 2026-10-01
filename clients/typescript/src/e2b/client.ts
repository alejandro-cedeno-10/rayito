/**
 * `E2B` de E2B JS 2.51: un cliente cuyas opciones de conexión quedan ligadas
 * a `client.Sandbox` (gana la llamada salvo `undefined`; `headers` no se
 * fusiona). Las opciones ignoradas avisan una vez, al construir el cliente.
 * `client.Secret` usa su `region` (Secrets Manager en esa cuenta);
 * `client.Template` es la clase real (m15-templates); `Volume` sigue
 * lanzando `UnimplementedError` al leerla.
 */

import { type DynamoDbIndex, validateIndex } from "../index/dynamodb.js";
import { emitIgnoredWarnings, IGNORED_CONNECTION_OPTS, splitConnectionOpts } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { bindSandbox, type Sandbox } from "./sandbox.js";
import { bindSecret, type Secret } from "./secret.js";
import { Template } from "./template.js";
import { unimplemented } from "./unimplemented.js";

/**
 * Las opciones de `new E2B({...})`: las de conexión y, como extensión de
 * Rayito, `index` (M14, `undefined` por defecto), que `client.Sandbox.list`
 * usa cuando la llamada no pasa otro (ver `DynamoDbIndex`, "Coste y
 * activación"). Ninguna otra llamada lo usa.
 */
export type E2BClientOpts = ConnectionOpts & { readonly index?: DynamoDbIndex | undefined };

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

  constructor(opts: E2BClientOpts = {}) {
    const { ignored } = splitConnectionOpts(opts);
    emitIgnoredWarnings(ignored);
    validateIndex(opts.index);
    this.Sandbox = bindSandbox(withoutIgnored(opts));
    this.Secret = bindSecret(opts.region === undefined ? {} : { region: opts.region });
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Template(): typeof Template {
    return Template;
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Volume(): never {
    throw unimplemented("Volume");
  }
}
