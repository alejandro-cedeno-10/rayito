/**
 * `Template` de E2B (m15-templates): un subtipo delgado de `rayito.Template`
 * — hereda el DSL sin copiarlo — que adapta la firma de build de E2B JS
 * (`Template.build(template, { alias, cpuCount, memoryMB, skipCache,
 * onBuildLogs })`, o `Template.build(template, alias, {...})`) a la nativa
 * (`Template.build(template, name, { bucket, memoryMb, force })`) y añade
 * los cuatro métodos de etiquetado que 0.6 no implementa
 * (`aliasExists`/`assignTags`/`removeTags`/`getTags`): lanzan
 * `UnimplementedError` por la tabla D14, nunca `TypeError` por método
 * inexistente. Espejo de `rayito.e2b._template`.
 *
 * `bucket`, `region` y `credentials` llegan de la llamada o del cliente
 * `new E2B({ region, bucket })` (`bindTemplate`, como `bindSecret`); sin
 * `bucket` en ninguno de los dos, `InvalidArgumentError` nombra la opción.
 * `memoryMB` se redondea al tamaño soportado siguiente con
 * `RayitoCompatWarning`; `cpuCount` no se puede fijar (la CPU sale de la
 * memoria, Q87) y avisa si se da.
 */

import { InvalidArgumentError } from "../errors.js";
import { SUPPORTED_MEMORY_MIB } from "../limits.js";
import type { BuildHandle, BuildInfo, BuildOptions } from "../templates/build.js";
import { Template as NativeTemplate } from "../templates/dsl.js";
import { COMPAT_WARNING_TYPE, emitCompatWarning, IGNORED_CONNECTION_OPTS } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { unimplemented } from "./unimplemented.js";

/** Opciones de build de E2B JS (las de conexión avisan y se ignoran) más
 * las de Rayito (`bucket`, `region`, `credentials`, ...). */
export interface E2BTemplateBuildOptions
  extends Partial<BuildOptions>,
    Omit<ConnectionOpts, "region"> {
  readonly alias?: string;
  readonly cpuCount?: number;
  readonly memoryMB?: number;
  readonly skipCache?: boolean;
}

/** Lo que `new E2B({...})` vincula a `client.Template`. */
export interface TemplateBoundOpts {
  readonly bucket?: string | undefined;
  readonly region?: string | undefined;
  readonly credentials?: BuildOptions["credentials"];
}

const NATIVE_OPTIONS = new Set([
  "bucket",
  "region",
  "credentials",
  "timeoutMs",
  "baseImageVersion",
  "contextDir",
  "force",
  "onBuildLogs",
]);
const E2B_BUILD_OPTIONS = new Set(["alias", "cpuCount", "memoryMB", "skipCache", "memoryMb"]);
const CPU_COUNT_IGNORED =
  "Lambda MicroVMs deriva la CPU de memoryMB (Q87, AWS_API_NOTES.md §4); sube memoryMB para " +
  "tener más CPU";

/** El tamaño soportado igual o inmediatamente mayor que `memoryMb`, con
 * `RayitoCompatWarning` si hubo que redondear; por encima del máximo,
 * `InvalidArgumentError`. Espejo de `_template.resolve_memory_mb`. */
export function resolveMemoryMb(memoryMb: number | undefined): number | undefined {
  if (memoryMb === undefined) {
    return undefined;
  }
  const supported = SUPPORTED_MEMORY_MIB.find((size) => memoryMb <= size);
  if (supported === undefined) {
    throw new InvalidArgumentError(
      `memoryMB=${memoryMb} supera el máximo soportado (${SUPPORTED_MEMORY_MIB.at(-1)} MiB, RES-1)`,
    );
  }
  if (supported !== memoryMb) {
    process.emitWarning(
      `memoryMB=${memoryMb} redondeado a ${supported} (tamaños soportados: ` +
        `${SUPPORTED_MEMORY_MIB.join(", ")})`,
      { type: COMPAT_WARNING_TYPE },
    );
  }
  return supported;
}

function nativeOptions(
  bound: TemplateBoundOpts,
  opts: E2BTemplateBuildOptions,
  call: string,
): { alias: string; options: BuildOptions } {
  for (const [name, value] of Object.entries(opts)) {
    if (value === undefined || NATIVE_OPTIONS.has(name) || E2B_BUILD_OPTIONS.has(name)) {
      continue;
    }
    emitCompatWarning(name, IGNORED_CONNECTION_OPTS[name] ?? "Template.build no lo aplica");
  }
  if (opts.cpuCount !== undefined) {
    emitCompatWarning("cpuCount", CPU_COUNT_IGNORED);
  }
  if (opts.alias === undefined) {
    throw new InvalidArgumentError(`Template.${call}: falta alias (el nombre del template)`);
  }
  const bucket = opts.bucket ?? bound.bucket;
  if (bucket === undefined) {
    throw new InvalidArgumentError(
      `Template.${call}: falta el bucket de artefactos; pasa { bucket } o crea el cliente con ` +
        "new E2B({ bucket })",
    );
  }
  const memoryMb = resolveMemoryMb(opts.memoryMB ?? opts.memoryMb);
  const region = opts.region ?? bound.region;
  const credentials = opts.credentials ?? bound.credentials;
  const options: BuildOptions = {
    bucket,
    ...(memoryMb === undefined ? {} : { memoryMb }),
    ...(region === undefined ? {} : { region }),
    ...(credentials === undefined ? {} : { credentials }),
    ...(opts.timeoutMs === undefined ? {} : { timeoutMs: opts.timeoutMs }),
    ...(opts.baseImageVersion === undefined ? {} : { baseImageVersion: opts.baseImageVersion }),
    ...(opts.contextDir === undefined ? {} : { contextDir: opts.contextDir }),
    ...(opts.onBuildLogs === undefined ? {} : { onBuildLogs: opts.onBuildLogs }),
    force: (opts.force ?? false) || (opts.skipCache ?? false),
  };
  return { alias: opts.alias, options };
}

function withAlias(
  aliasOrOptions: string | E2BTemplateBuildOptions,
  options: E2BTemplateBuildOptions,
): E2BTemplateBuildOptions {
  return typeof aliasOrOptions === "string"
    ? { ...options, alias: aliasOrOptions }
    : { ...aliasOrOptions };
}

/**
 * `rayito/e2b`'s `Template`: the same builder as `rayito`'s `Template`
 * with E2B's build signature, plus four tag stubs that never call AWS.
 *
 * Coste y activación
 * -------------------
 * Activa: `Template.build()`/`buildInBackground()`, same as the native
 *     class — see `rayito`'s `Template` for the full block.
 * Recursos y llamadas AWS: same as the native `Template`.
 * Coste aproximado: same as the native `Template`.
 * IAM: same as the native `Template` (`RayitoTemplateBuilder`).
 * Cómo apagarla: no llames a `Template.build()`/`buildInBackground()`.
 * Ejemplo:
 *     import { E2B, Template } from "rayito/e2b";
 *     const client = new E2B({ region: "us-east-1", bucket: "<bucket>" });
 *     const t = new Template().fromBaseImage().pipInstall(["pandas"]);
 *     await client.Template.build(t, { alias: "mi-template", memoryMB: 2048 });
 */
export class Template extends NativeTemplate {
  static readonly boundOpts: TemplateBoundOpts = {};

  static override build(
    template: NativeTemplate,
    aliasOrOptions: string | E2BTemplateBuildOptions,
    options: E2BTemplateBuildOptions = {},
  ): Promise<BuildInfo> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Template`
    const bound = this.boundOpts;
    const { alias, options: native } = nativeOptions(
      bound,
      withAlias(aliasOrOptions, options),
      "build",
    );
    return NativeTemplate.build(template, alias, native);
  }

  static override buildInBackground(
    template: NativeTemplate,
    aliasOrOptions: string | E2BTemplateBuildOptions,
    options: E2BTemplateBuildOptions = {},
  ): Promise<BuildHandle> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Template`
    const bound = this.boundOpts;
    const { alias, options: native } = nativeOptions(
      bound,
      withAlias(aliasOrOptions, options),
      "buildInBackground",
    );
    return NativeTemplate.buildInBackground(template, alias, native);
  }

  static override exists(
    name: string,
    options: { region?: string; credentials?: BuildOptions["credentials"] } = {},
  ): Promise<boolean> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Template`
    const bound = this.boundOpts;
    const region = options.region ?? bound.region;
    const credentials = options.credentials ?? bound.credentials;
    return NativeTemplate.exists(name, {
      ...(region === undefined ? {} : { region }),
      ...(credentials === undefined ? {} : { credentials }),
    });
  }

  aliasExists(..._args: unknown[]): never {
    throw unimplemented("Template.aliasExists");
  }

  assignTags(..._args: unknown[]): never {
    throw unimplemented("Template.assignTags");
  }

  removeTags(..._args: unknown[]): never {
    throw unimplemented("Template.removeTags");
  }

  getTags(..._args: unknown[]): never {
    throw unimplemented("Template.getTags");
  }
}

/** `new E2B({...}).Template`: la clase con `bucket`/`region` del cliente. */
export function bindTemplate(opts: TemplateBoundOpts): typeof Template {
  const bound = Object.freeze({ ...opts });
  return class BoundTemplate extends Template {
    static override readonly boundOpts: TemplateBoundOpts = bound;
  };
}
