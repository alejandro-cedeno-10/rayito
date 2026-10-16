/**
 * El plan de instalación de un agente que ya existe (0.14, D1 y D3). Espejo de
 * `rayito._agent._install_plan`: cómo se llevan a la imagen el intérprete
 * (CPython o Node con sha256 fijado), el gestor de paquetes (uv, pip, npm, pnpm
 * o yarn) y las dependencias del proyecto, a partir de lo que se sabe de él.
 *
 * Puro: sin E/S, sin reloj, sin red; sólo compone `TemplateStep`s. Los pines
 * (versiones, sha256 y la etiqueta de la release de python-build-standalone)
 * llegan de `limits.json`. Los vectores compartidos con Python son
 * `testdata/agent/install-plans/*.json`.
 *
 * - Python: sin `pythonVersion`, o con la versión del intérprete de la imagen
 *   base (`AGENT_DEFAULT_PYTHON_MINOR`), no se descarga nada. Con otra, se baja
 *   el tarball `install_only` de python-build-standalone (linux arm64) y se
 *   comprueba su sha256.
 * - Node: tarball oficial de nodejs.org (linux arm64) con su sha256; `pnpm` y
 *   `yarn` se instalan con `npm install -g --ignore-scripts` a la versión
 *   fijada.
 */

import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_DEFAULT_NODE_MAJOR,
  AGENT_DEFAULT_PYTHON_MINOR,
  AGENT_NODE_BUILD_SHA256,
  AGENT_NODE_BUILD_VERSIONS,
  AGENT_PNPM_VERSION,
  AGENT_PYTHON_BUILD_SHA256,
  AGENT_PYTHON_BUILD_TAG,
  AGENT_PYTHON_BUILD_VERSIONS,
  AGENT_UV_SHA256,
  AGENT_UV_VERSION,
  AGENT_YARN_CLASSIC_VERSION,
} from "../limits.js";
import { containerPackagesStep, userRequirementsSteps } from "./host-runtime.js";
import { copyStep, type InstallTarget, runStep, type TemplateStep } from "./runtime.js";

export const PYTHON_INSTALLERS = ["pip", "uv"] as const;
export type PythonInstaller = (typeof PYTHON_INSTALLERS)[number];
export const NODE_PACKAGE_MANAGERS = ["npm", "pnpm", "yarn"] as const;
export type NodePackageManager = (typeof NODE_PACKAGE_MANAGERS)[number];
export const PACKAGE_MANAGERS = [...PYTHON_INSTALLERS, ...NODE_PACKAGE_MANAGERS] as const;
/** Cómo se describen las dependencias de Python de un proyecto: un fichero de
 * requisitos, `pyproject.toml` más `uv.lock`, o `pyproject.toml` solo. */
export const PYTHON_MANIFESTS = ["requirements", "uv-lock", "pyproject"] as const;
export type PythonManifest = (typeof PYTHON_MANIFESTS)[number];
/** Lockfile que cada gestor de Node lee junto a `package.json`. */
export const NODE_LOCKFILES: Readonly<Record<NodePackageManager, string>> = {
  npm: "package-lock.json",
  pnpm: "pnpm-lock.yaml",
  yarn: "yarn.lock",
};
export const NODE_MANIFEST = "package.json";
export const PYPROJECT_MANIFEST = "pyproject.toml";
export const UV_LOCKFILE = "uv.lock";
/** Carpetas de lo que instala el plan, bajo `installDir`. */
export const NODE_DIR_NAME = "node";
export const UV_DIR_NAME = "uv";
export const PYTHON_DIR_PREFIX = "python-";
/** El directorio que trae el tarball `install_only` de python-build-standalone. */
export const PYTHON_TARBALL_ROOT = "python";
export const PYTHON_TARBALL_PATH = "/tmp/pinned-python.tar.gz";
export const NODE_TARBALL_PATH = "/tmp/node.tar.gz";
export const UV_TARBALL_PATH = "/tmp/uv.tar.gz";
/** Dónde se descarga cada herramienta: los hosts oficiales del proyecto, con el
 * sha256 de `limits.json` como comprobación. */
export const PYTHON_BUILD_URL =
  "https://github.com/astral-sh/python-build-standalone/releases/download";
export const NODE_DIST_URL = "https://nodejs.org/dist";
export const UV_RELEASE_URL = "https://github.com/astral-sh/uv/releases/download";
export const UV_ARCHIVE = "uv-aarch64-unknown-linux-gnu";
/** Herramientas de descarga que una imagen Debian de contenedor no trae. */
export const DOWNLOAD_PACKAGES = ["curl", "ca-certificates"] as const;
/** `--hash=` en un fichero de requisitos activa `--require-hashes`. */
export const UV_PIP_HASH_FLAG = "--require-hashes";

const NODE_INSTALL_COMMANDS: Readonly<Record<NodePackageManager, string>> = {
  npm: "npm ci --ignore-scripts --omit=dev --no-audit --no-fund",
  pnpm: "pnpm install --frozen-lockfile --prod --ignore-scripts",
  yarn: "yarn install --frozen-lockfile --production --ignore-scripts",
};
const NODE_MANAGER_VERSIONS: Readonly<Partial<Record<NodePackageManager, string>>> = {
  pnpm: AGENT_PNPM_VERSION,
  yarn: AGENT_YARN_CLASSIC_VERSION,
};

const REQUIREMENTS_PATH_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*$/;

/** `uv-lock`, `pyproject` o `requirements` según el nombre del fichero de
 * dependencias de `source`. */
export function pythonManifest(requirements: string): PythonManifest {
  if (requirements === UV_LOCKFILE) {
    return "uv-lock";
  }
  return requirements === PYPROJECT_MANIFEST ? "pyproject" : "requirements";
}

/** `requirements` es una ruta relativa dentro de `source`, y `pyproject.toml` o
 * `uv.lock` sólo con `uv`; si no, `InvalidArgumentError` con el nombre de la
 * opción (`label`). */
export function checkRequirementsPath(
  label: string,
  requirements: string,
  packageManager: string | undefined,
): void {
  if (!REQUIREMENTS_PATH_PATTERN.test(requirements) || requirements.split("/").includes("..")) {
    throw new InvalidArgumentError(`${label}.requirements es una ruta relativa dentro de source`);
  }
  if (pythonManifest(requirements) !== "requirements" && packageManager !== "uv") {
    throw new InvalidArgumentError(
      `${label}.requirements=${JSON.stringify(requirements)} necesita packageManager 'uv'`,
    );
  }
}

/** El ecosistema del gestor: `python` (pip, uv) o `node` (npm, pnpm, yarn). */
export function ecosystem(packageManager: string): "python" | "node" {
  if ((PYTHON_INSTALLERS as readonly string[]).includes(packageManager)) {
    return "python";
  }
  if ((NODE_PACKAGE_MANAGERS as readonly string[]).includes(packageManager)) {
    return "node";
  }
  throw new InvalidArgumentError(
    `package_manager debe ser uno de ${JSON.stringify(PACKAGE_MANAGERS)}, recibido ${JSON.stringify(packageManager)}`,
  );
}

/** `version` (la menor de CPython, `3.12`) si hay un build fijado. */
export function checkPythonVersion(version: string): string {
  if (!(version in AGENT_PYTHON_BUILD_VERSIONS)) {
    throw new InvalidArgumentError(
      `python_version debe ser una de ${JSON.stringify(Object.keys(AGENT_PYTHON_BUILD_VERSIONS).sort())}, recibida ${JSON.stringify(version)}`,
    );
  }
  return version;
}

/** `version` (la mayor de Node, `22`) si hay un build fijado. */
export function checkNodeVersion(version: string): string {
  if (!(version in AGENT_NODE_BUILD_VERSIONS)) {
    throw new InvalidArgumentError(
      `node_version debe ser una de ${JSON.stringify(Object.keys(AGENT_NODE_BUILD_VERSIONS).sort())}, recibida ${JSON.stringify(version)}`,
    );
  }
  return version;
}

/** El `RUN` que baja el CPython fijado de la versión menor `minor` a
 * `parentDir/python`, comprobando su sha256. */
export function pythonDownloadCommand(
  minor: string,
  parentDir: string,
  tarballPath: string,
): string {
  checkPythonVersion(minor);
  const version = AGENT_PYTHON_BUILD_VERSIONS[minor];
  const asset = `cpython-${version}%2B${AGENT_PYTHON_BUILD_TAG}-aarch64-unknown-linux-gnu-install_only`;
  const url = `${PYTHON_BUILD_URL}/${AGENT_PYTHON_BUILD_TAG}/${asset}.tar.gz`;
  return (
    `mkdir -p ${parentDir}` +
    ` && curl -fsSL --retry 3 -o ${tarballPath} ${url}` +
    ` && echo '${AGENT_PYTHON_BUILD_SHA256[minor]}  ${tarballPath}' | sha256sum -c -` +
    ` && tar -xzf ${tarballPath} -C ${parentDir}` +
    ` && rm -f ${tarballPath}`
  );
}

function usesImagePython(pythonVersion: string | undefined): boolean {
  return pythonVersion === undefined || pythonVersion === AGENT_DEFAULT_PYTHON_MINOR;
}

/** El intérprete con el que se crean los venvs: el de `target` si no se pide
 * otra versión menor, o el CPython fijado bajo `installDir`. */
export function pythonInterpreter(target: InstallTarget, pythonVersion?: string): string {
  if (usesImagePython(pythonVersion)) {
    return target.python;
  }
  checkPythonVersion(pythonVersion as string);
  return `${target.installDir}/${PYTHON_DIR_PREFIX}${pythonVersion}/${PYTHON_TARBALL_ROOT}/bin/python3`;
}

/** Descarga el CPython fijado de `pythonVersion`; ningún paso si es la versión
 * del intérprete de la imagen base. */
export function pythonRuntimeSteps(
  target: InstallTarget,
  pythonVersion?: string,
): readonly TemplateStep[] {
  if (usesImagePython(pythonVersion)) {
    return [];
  }
  const parent = `${target.installDir}/${PYTHON_DIR_PREFIX}${pythonVersion}`;
  return [
    ...containerPackagesStep(target, DOWNLOAD_PACKAGES),
    runStep(pythonDownloadCommand(pythonVersion as string, parent, PYTHON_TARBALL_PATH)),
  ];
}

export function uvBinary(target: InstallTarget): string {
  return `${target.installDir}/${UV_DIR_NAME}/uv`;
}

/** Instala `uv` (versión y sha256 fijados) en `installDir/uv/uv`. */
export function uvSteps(target: InstallTarget): TemplateStep[] {
  const directory = `${target.installDir}/${UV_DIR_NAME}`;
  const url = `${UV_RELEASE_URL}/${AGENT_UV_VERSION}/${UV_ARCHIVE}.tar.gz`;
  return [
    ...containerPackagesStep(target, DOWNLOAD_PACKAGES),
    runStep(
      `curl -fsSL --retry 3 -o ${UV_TARBALL_PATH} ${url}` +
        ` && echo '${AGENT_UV_SHA256}  ${UV_TARBALL_PATH}' | sha256sum -c -` +
        ` && mkdir -p ${directory}` +
        ` && tar -xzf ${UV_TARBALL_PATH} -C ${directory} --strip-components=1` +
        ` && rm -f ${UV_TARBALL_PATH}`,
    ),
  ];
}

export interface PythonDependencyOptions {
  readonly installer: string;
  readonly manifest: string;
  readonly path?: string | undefined;
  readonly python: string;
  readonly venvDir: string;
  readonly slug: string;
  /** `false` si el venv ya existe (el de un runtime con sus pines): `python` es
   * el suyo y las dependencias se añaden sin quitar lo que ya hay. */
  readonly createVenv?: boolean | undefined;
}

function checkPythonInputs(options: PythonDependencyOptions): void {
  if (!(PYTHON_INSTALLERS as readonly string[]).includes(options.installer)) {
    throw new InvalidArgumentError(
      `installer debe ser uno de ${JSON.stringify(PYTHON_INSTALLERS)}, recibido ${JSON.stringify(options.installer)}`,
    );
  }
  if (!(PYTHON_MANIFESTS as readonly string[]).includes(options.manifest)) {
    throw new InvalidArgumentError(
      `manifest debe ser uno de ${JSON.stringify(PYTHON_MANIFESTS)}, recibido ${JSON.stringify(options.manifest)}`,
    );
  }
  if (options.installer === "pip" && options.manifest !== "requirements") {
    throw new InvalidArgumentError(
      "pip sólo instala un fichero de requisitos: para pyproject.toml o uv.lock usa uv",
    );
  }
  if (options.manifest === "requirements" && !options.path) {
    throw new InvalidArgumentError("requirements necesita la ruta del fichero de requisitos");
  }
}

/**
 * Crea `venvDir` con `python` e instala las dependencias del proyecto.
 *
 * `manifest` es `requirements` (`path` es el fichero dentro de `source`),
 * `uv-lock` (`pyproject.toml` y `uv.lock` en la raíz de `source`) o `pyproject`
 * (sólo `pyproject.toml`). `pip` sólo instala `requirements`; `uv` instala los
 * tres (`uv sync --frozen` con el lockfile, `uv pip install -r` con los otros
 * dos).
 */
export function pythonDependencySteps(
  target: InstallTarget,
  options: PythonDependencyOptions,
): readonly TemplateStep[] {
  checkPythonInputs(options);
  const { python, venvDir, slug } = options;
  const createVenv = options.createVenv ?? true;
  if (options.installer === "pip") {
    return [
      ...(createVenv ? [runStep(`${python} -m venv ${venvDir}`)] : []),
      ...userRequirementsSteps(target, {
        venvDir,
        requirements: options.path as string,
        slug,
      }),
    ];
  }
  const uv = uvBinary(target);
  const venvPython = `${venvDir}/bin/python`;
  const steps = [
    ...uvSteps(target),
    ...(createVenv ? [runStep(`${uv} venv --python ${python} ${venvDir}`)] : []),
  ];
  const inexact = createVenv ? "" : " --inexact";
  if (options.manifest === "uv-lock") {
    const source = `${target.installDir}/${slug}-project`;
    steps.push(
      copyStep(`app/${PYPROJECT_MANIFEST}`, `${source}/${PYPROJECT_MANIFEST}`),
      copyStep(`app/${UV_LOCKFILE}`, `${source}/${UV_LOCKFILE}`),
      runStep(
        `cd ${source} && UV_PROJECT_ENVIRONMENT=${venvDir} UV_PYTHON_DOWNLOADS=never` +
          ` ${uv} sync --frozen --no-dev --no-install-project${inexact} --python ${python}`,
      ),
    );
    return steps;
  }
  const requirements =
    options.manifest === "requirements" ? (options.path as string) : PYPROJECT_MANIFEST;
  const destination =
    options.manifest === "requirements"
      ? `${target.installDir}/${slug}-${requirements.split("/").pop()}`
      : `${target.installDir}/${slug}-project/${PYPROJECT_MANIFEST}`;
  steps.push(
    copyStep(`app/${requirements}`, destination),
    runStep(
      `hashes=''; if grep -q -- '--hash=' ${destination};` +
        ` then hashes='${UV_PIP_HASH_FLAG}'; fi` +
        ` && ${uv} pip install --python ${venvPython} --no-cache $hashes -r ${destination}` +
        ` && ${uv} pip check --python ${venvPython}`,
    ),
  );
  return steps;
}

export function nodeDirectory(target: InstallTarget): string {
  return `${target.installDir}/${NODE_DIR_NAME}`;
}

export function nodeBin(target: InstallTarget): string {
  return `${nodeDirectory(target)}/bin`;
}

/** Node (tarball oficial linux arm64, sha256 fijado) en `installDir/node`, con
 * `node` enlazado en `/usr/local/bin`. */
export function nodeRuntimeSteps(
  target: InstallTarget,
  nodeVersion?: string,
): readonly TemplateStep[] {
  const major = checkNodeVersion(nodeVersion ?? AGENT_DEFAULT_NODE_MAJOR);
  const version = AGENT_NODE_BUILD_VERSIONS[major];
  const archive = `node-v${version}-linux-arm64.tar.gz`;
  const directory = nodeDirectory(target);
  return [
    ...containerPackagesStep(target, DOWNLOAD_PACKAGES),
    runStep(
      `curl -fsSL --retry 3 -o ${NODE_TARBALL_PATH} ${NODE_DIST_URL}/v${version}/${archive}` +
        ` && echo '${AGENT_NODE_BUILD_SHA256[major]}  ${NODE_TARBALL_PATH}' | sha256sum -c -` +
        ` && mkdir -p ${directory}` +
        ` && tar -xzf ${NODE_TARBALL_PATH} -C ${directory} --strip-components=1` +
        ` && rm -f ${NODE_TARBALL_PATH}` +
        ` && ln -sf ${nodeBin(target)}/node /usr/local/bin/node`,
    ),
  ];
}

/** `package.json` y el lockfile del gestor, que el build copia de `source`. */
export function nodePackageFiles(manager: string): readonly [string, string] {
  if (ecosystem(manager) !== "node") {
    throw new InvalidArgumentError(
      `${JSON.stringify(manager)} no es un gestor de paquetes de Node`,
    );
  }
  return [NODE_MANIFEST, NODE_LOCKFILES[manager as NodePackageManager]];
}

/** Copia `package.json` y el lockfile a `installDir` (de modo que
 * `node_modules` quede por encima del código copiado y Node lo resuelva) y
 * ejecuta la instalación congelada del gestor. */
export function nodeDependencySteps(
  target: InstallTarget,
  manager: string,
): readonly TemplateStep[] {
  const steps = nodePackageFiles(manager).map((name) =>
    copyStep(`app/${name}`, `${target.installDir}/${name}`),
  );
  const path = `PATH=${nodeBin(target)}:$PATH`;
  const key = manager as NodePackageManager;
  const version = NODE_MANAGER_VERSIONS[key];
  const prepare =
    version === undefined
      ? ""
      : `${path} npm install -g --ignore-scripts --no-audit --no-fund ${manager}@${version} && `;
  steps.push(runStep(`${prepare}cd ${target.installDir} && ${path} ${NODE_INSTALL_COMMANDS[key]}`));
  return steps;
}
