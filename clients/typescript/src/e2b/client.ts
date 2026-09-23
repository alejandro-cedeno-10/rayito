/**
 * `E2B` de E2B JS 2.51: un cliente cuyas opciones de conexión quedan ligadas
 * a `client.Sandbox` (gana la llamada salvo `undefined`; `headers` no se
 * fusiona). Las opciones ignoradas avisan una vez, al construir el cliente.
 * `Template`, `Volume` y `Secret` lanzan `UnimplementedError` al leerlos.
 */

import { emitIgnoredWarnings, IGNORED_CONNECTION_OPTS, splitConnectionOpts } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { bindSandbox, type Sandbox } from "./sandbox.js";
import { unimplemented } from "./unimplemented.js";

export type E2BClientOpts = ConnectionOpts;

function withoutIgnored(opts: ConnectionOpts): ConnectionOpts {
  return Object.fromEntries(
    Object.entries(opts).filter(([name]) => !Object.hasOwn(IGNORED_CONNECTION_OPTS, name)),
  ) as ConnectionOpts;
}

// biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
export class E2B {
  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  readonly Sandbox: typeof Sandbox;

  constructor(opts: E2BClientOpts = {}) {
    const { ignored } = splitConnectionOpts(opts);
    emitIgnoredWarnings(ignored);
    this.Sandbox = bindSandbox(withoutIgnored(opts));
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Template(): never {
    throw unimplemented("Template");
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Volume(): never {
    throw unimplemented("Volume");
  }

  // biome-ignore lint/style/useNamingConvention: nombre público de E2B JS
  get Secret(): never {
    throw unimplemented("Secret");
  }
}
