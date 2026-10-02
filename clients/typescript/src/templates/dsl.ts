/**
 * `Template`: el builder fluido e inmutable de m15-templates (DSL de E2B
 * v2, investigación §3.1 y §3.6). Espejo de `rayito._templates._dsl`. Cada
 * método devuelve un `Template` nuevo: encadenar `.pipInstall(...).copy(...)`
 * construye un `TemplateSpec` paso a paso sin mutar una rama anterior del
 * mismo builder. Puro: ninguna llamada de red, ningún fichero se abre aquí
 * (`build.ts` los lee al ensamblar el zip).
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import { DEFAULT_BASE_IMAGE_NAME } from "../images/gateway.js";
import {
  type BuildHandle,
  type BuildInfo,
  type BuildOptions,
  type BuildStatus,
  build as runBuild,
  buildInBackground as runBuildInBackground,
  getBuildStatus as runGetBuildStatus,
  templateExists as runTemplateExists,
} from "./build.js";
import { renderAppendedLayer } from "./dockerfile.js";
import {
  BASE_IMAGE_KIND,
  type BaseImageRef,
  DEFAULT_TEMPLATE_USER,
  type ReadyPoll,
  type StartSpec,
  type TemplateSpec,
  type WireStep,
} from "./instructions.js";
import type { ReadyCommand } from "./ready-cmds.js";

export { DEFAULT_BASE_IMAGE_NAME };

const UNSUPPORTED_BASE_REASON = (method: string): string =>
  `Template.${method}() necesita inyectar rayd y sus hooks en la imagen de base; en 0.6 sólo ` +
  "fromBaseImage() sabe componer eso (siempre sobre un zip de codeArtifact ya publicado con " +
  "rayito image publish). Para una imagen externa, publícala primero con rayito image publish " +
  "sobre un Dockerfile `FROM <esa imagen>@sha256:...`.";

export interface SetStartCmdOptions {
  readonly user?: string;
  readonly workdir?: string;
  readonly envs?: Readonly<Record<string, string>>;
}

/**
 * Templates declarativos (m15-templates, ADR-022): DSL -> Dockerfile
 * compuesto sobre una imagen `rayito-base` ya publicada -> zip determinista
 * en S3 -> `create`/`update-microvm-image`. Apagado por defecto
 * (ADR-014): construir un `Template` no llama a AWS; sólo `build()`/
 * `buildInBackground()` lo hacen.
 *
 * Coste y activación
 * -------------------
 * Activa: `Template.build()`/`buildInBackground()`. El builder y
 *     `toDockerfile()`/`toJSON()` son puros.
 * Recursos y llamadas AWS: `GetMicrovmImageVersion`, `CreateMicrovmImage`/
 *     `UpdateMicrovmImage`, `GetMicrovmImage`, `ListMicrovmImageVersions`;
 *     `s3:GetObject` (zip base), `HeadObject`/`PutObject` (subir el
 *     artefacto nuevo, sólo si no existe ya por hash); `logs:DescribeLogStreams`/
 *     `GetLogEvents` sólo si el build falla. Ver `AWS_API_NOTES.md` §27.
 * Coste aproximado: cada versión de imagen nueva cuesta almacenamiento de
 *     snapshot (~$0,04/semana por versión, investigación §3.4, consultado
 *     2026-10-01); el build en sí no se factura aparte.
 * IAM: `RayitoTemplateBuilder` (`infra/templates.yaml`): `lambda:CreateMicrovmImage`/
 *     `UpdateMicrovmImage`/`GetMicrovmImage*`/`ListMicrovmImageVersions`,
 *     `iam:PassRole` sobre el rol de build, `s3:PutObject`/`GetObject`/
 *     `HeadObject`, y lectura del grupo de logs de la imagen.
 * Cómo apagarla: no llames a `Template.build()`. Borra versiones de imagen
 *     con `rayito image` (no las borra `Template`).
 * Ejemplo:
 *     const t = new Template().fromBaseImage("rayito-base").pipInstall(["pandas"]);
 *     const info = await Template.build(t, "mi-template", { bucket: "<bucket>" });
 */
export class Template {
  readonly spec: TemplateSpec;

  constructor(spec?: TemplateSpec) {
    this.spec = spec ?? { steps: [], skipCache: false };
  }

  protected withSpec(spec: TemplateSpec): this {
    return new (this.constructor as new (spec: TemplateSpec) => this)(spec);
  }

  // -- imagen base -------------------------------------------------------

  /** Parte del zip de `codeArtifact` de `name` (ya publicada con `rayito
   * image publish`): `Template.build()` lo descarga, le añade las
   * instrucciones compiladas y vuelve a subir el resultado. */
  fromBaseImage(name: string = DEFAULT_BASE_IMAGE_NAME, version?: string): this {
    if (!name) {
      throw new InvalidArgumentError("fromBaseImage: el nombre de la imagen no puede ser vacío");
    }
    const base: BaseImageRef = { kind: BASE_IMAGE_KIND, name, version };
    return this.withSpec({ ...this.spec, base });
  }

  fromImage(..._args: unknown[]): never {
    throw new UnimplementedError("Template.fromImage", UNSUPPORTED_BASE_REASON("fromImage"));
  }

  fromTemplate(..._args: unknown[]): never {
    throw new UnimplementedError("Template.fromTemplate", UNSUPPORTED_BASE_REASON("fromTemplate"));
  }

  fromDockerfile(..._args: unknown[]): never {
    throw new UnimplementedError(
      "Template.fromDockerfile",
      "el compilador de 0.6 construye el Dockerfile desde el DSL; parsear uno existente llega " +
        "en un cambio posterior (ver docs-delta.md de m15-templates)",
    );
  }

  fromGcpRegistry(..._args: unknown[]): never {
    throw new UnimplementedError(
      "Template.fromGcpRegistry",
      "Rayito publica sobre Lambda MicroVMs (AWS): no hay un análogo de GCP Artifact Registry " +
        "que create-microvm-image pueda usar",
    );
  }

  // -- instrucciones de cable ---------------------------------------------

  copy(src: string, dst: string): this {
    if (!src || !dst) {
      throw new InvalidArgumentError("copy: src y dst no pueden ser vacíos");
    }
    if (!dst.startsWith("/")) {
      throw new InvalidArgumentError(`copy: dst debe ser una ruta absoluta en la imagen: ${dst}`);
    }
    return this.withStep({ kind: "copy", src, dst });
  }

  runCmd(cmd: string): this {
    if (!cmd.trim()) {
      throw new InvalidArgumentError("runCmd: el comando no puede ser vacío");
    }
    return this.withStep({ kind: "run", cmd });
  }

  pipInstall(packages: string | readonly string[], extraArgs = ""): this {
    const names = typeof packages === "string" ? [packages] : [...packages];
    if (names.length === 0) {
      throw new InvalidArgumentError("pipInstall: la lista de paquetes no puede ser vacía");
    }
    const args = extraArgs ? ` ${extraArgs}` : "";
    return this.runCmd(`pip install --no-cache-dir${args} ${names.join(" ")}`);
  }

  aptInstall(..._args: unknown[]): never {
    throw new UnimplementedError(
      "Template.aptInstall",
      "rayito-base es Amazon Linux 2023 (dnf, no apt): usa runCmd('dnf install -y ...')",
    );
  }

  setEnvs(envs: Readonly<Record<string, string>>): this {
    let spec = this.spec;
    for (const [key, value] of Object.entries(envs)) {
      if (!key) {
        throw new InvalidArgumentError("setEnvs: una clave no puede ser vacía");
      }
      spec = { ...spec, steps: [...spec.steps, { kind: "env", key, value }] };
    }
    return this.withSpec(spec);
  }

  workdir(path: string): this {
    if (!path.startsWith("/")) {
      throw new InvalidArgumentError(`workdir: debe ser una ruta absoluta: ${path}`);
    }
    return this.withStep({ kind: "workdir", path });
  }

  setUser(user: string): this {
    if (!user) {
      throw new InvalidArgumentError("setUser: el usuario no puede ser vacío");
    }
    return this.withStep({ kind: "user", user });
  }

  /** 0.6 no tiene caché de capas propia: esto hace que `Template.build()`/
   * `buildInBackground()` se comporten como con `{ force: true }` — envían un
   * build nuevo aunque ya exista una versión con la misma configuración. */
  skipCache(value = true): this {
    return this.withSpec({ ...this.spec, skipCache: value });
  }

  // -- start / ready -------------------------------------------------------

  /** Hornea `/etc/rayito/template.json` (`rayito.template/1`). Al arrancar,
   * un `rayd` 0.6 o posterior lo lee y, tras el hook `/run`, lanza
   * `startCmd` como proceso gestionado (visible en `commands.list()`); si
   * hay `readyCmd`, `/ready` responde 503 hasta que el comando sale con 0, y
   * falla al agotarse `ReadyPoll.timeoutSeconds` (una cadena cruda se sondea
   * con la cadencia por defecto, 0,5 s durante 60 s). Requiere que la imagen
   * base se haya publicado con `rayd` 0.6 o posterior: un `rayd` anterior
   * ignora el fichero. */
  setStartCmd(
    startCmd: string,
    readyCmd?: ReadyCommand | string,
    options: SetStartCmdOptions = {},
  ): this {
    if (!startCmd.trim()) {
      throw new InvalidArgumentError("setStartCmd: startCmd no puede ser vacío");
    }
    let readyCmdText: string | undefined;
    let poll: ReadyPoll | undefined;
    if (readyCmd === undefined) {
      readyCmdText = undefined;
      poll = undefined;
    } else if (typeof readyCmd === "string") {
      readyCmdText = readyCmd;
      poll = undefined;
    } else {
      readyCmdText = readyCmd.cmd;
      poll = readyCmd.poll;
    }
    const start: StartSpec = {
      startCmd,
      readyCmd: readyCmdText,
      user: options.user ?? DEFAULT_TEMPLATE_USER,
      workdir: options.workdir,
      envs: options.envs ?? {},
      readyPoll: poll,
    };
    return this.withSpec({ ...this.spec, start });
  }

  // -- inspección -----------------------------------------------------------

  /** El fragmento de Dockerfile que este `Template` añade (sin `FROM`: eso
   * depende de la imagen base, resuelta sólo al construir). */
  toDockerfile(): string {
    return renderAppendedLayer(this.spec);
  }

  toJSON(): string {
    return JSON.stringify(this.spec, null, 2);
  }

  // -- construcción (delegada: ver build.ts) --------------------------------

  static build(template: Template, name: string, options: BuildOptions): Promise<BuildInfo> {
    return runBuild(template, name, options);
  }

  static buildInBackground(
    template: Template,
    name: string,
    options: BuildOptions,
  ): Promise<BuildHandle> {
    return runBuildInBackground(template, name, options);
  }

  static getBuildStatus(handle: BuildHandle): Promise<BuildStatus> {
    return runGetBuildStatus(handle);
  }

  static exists(
    name: string,
    options?: { region?: string; credentials?: BuildOptions["credentials"] },
  ): Promise<boolean> {
    return runTemplateExists(name, options);
  }

  private withStep(step: WireStep): this {
    return this.withSpec({ ...this.spec, steps: [...this.spec.steps, step] });
  }
}
