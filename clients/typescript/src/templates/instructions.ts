/**
 * Dominio puro de `Template` (m15-templates). Espejo de
 * `rayito._templates._instructions`: las cinco instrucciones de cable
 * (investigación §3.1: COPY, ENV, RUN, WORKDIR, USER), la referencia a la
 * imagen base y el `TemplateSpec` inmutable que agrupa todo. Nada aquí
 * importa un SDK de AWS, ni abre un fichero.
 */

/** Marca de versión del `/etc/rayito/template.json` que lee `rayd`. */
export const TEMPLATE_SPEC_VERSION = "rayito.template/1";

/** Usuario por defecto del start/ready cmd (investigación §3.5). */
export const DEFAULT_TEMPLATE_USER = "1000";

/**
 * Único tipo de imagen base que `fromBaseImage()` sabe componer en 0.6: el
 * zip de `codeArtifact` publicado por `rayito image publish`. `fromImage`/
 * `fromTemplate` quedan fuera de alcance (ver `docs-delta.md`): inyectar
 * `rayd` y sus hooks en una imagen externa no tiene aún un camino
 * soportado.
 */
export const BASE_IMAGE_KIND = "rayito-base";

export interface BaseImageRef {
  readonly kind: string;
  readonly name: string;
  readonly version?: string | undefined;
}

export interface CopyStep {
  readonly kind: "copy";
  readonly src: string;
  readonly dst: string;
}

export interface EnvStep {
  readonly kind: "env";
  readonly key: string;
  readonly value: string;
}

export interface RunStep {
  readonly kind: "run";
  readonly cmd: string;
}

export interface WorkdirStep {
  readonly kind: "workdir";
  readonly path: string;
}

export interface UserStep {
  readonly kind: "user";
  readonly user: string;
}

export type WireStep = CopyStep | EnvStep | RunStep | WorkdirStep | UserStep;

export interface ReadyPoll {
  readonly intervalSeconds: number;
  readonly timeoutSeconds: number;
}

/** Lo que horneado en `/etc/rayito/template.json` lee `rayd` (mismo
 * esquema que `rayd_core::template::StartSpec`). */
export interface StartSpec {
  readonly startCmd: string;
  readonly readyCmd?: string | undefined;
  readonly user: string;
  readonly workdir?: string | undefined;
  readonly envs: Readonly<Record<string, string>>;
  readonly readyPoll?: ReadyPoll | undefined;
}

/** El estado inmutable que acumula el builder `Template` (`dsl.ts`). */
export interface TemplateSpec {
  readonly base?: BaseImageRef | undefined;
  readonly steps: readonly WireStep[];
  readonly start?: StartSpec | undefined;
  readonly skipCache: boolean;
}

export const EMPTY_TEMPLATE_SPEC: TemplateSpec = { steps: [], skipCache: false };
