/**
 * `AgentProject.export()`: un contexto de build de contenedor `linux/arm64`
 * para llevar el agente fuera de Rayito (ADR-028, D6), sin ninguna llamada de
 * red. Espejo de `rayito._agent._export`: escribe el `Dockerfile` (base fijada
 * por digest, los pasos de instalación del runtime para contenedor, el host
 * del agente, su configuración con el modelo cableado directo, el código del
 * usuario), un `.dockerignore` y un `README.md` con las órdenes de build y de
 * ejecución. El contenedor sirve `rayito.agent-service/1` en
 * `AGENT_SERVICE_EXPORT_PORT` (8080, el puerto que AgentCore espera).
 *
 * Fuera de Rayito no hay pasarela de secretos: la credencial del modelo la
 * pone la plataforma (variables de entorno, rol de IAM) y el agente puede
 * leerla. La garantía de que el agente nunca ve la credencial sólo vale
 * dentro de un sandbox de Rayito; el README generado lo dice.
 */

import { mkdir, readdir, writeFile } from "node:fs/promises";
import { dirname, join, posix } from "node:path";
import {
  AWS_CONFIG_NAME,
  awsConfigText,
  awsProfileEnvs,
  usesCredentialProcess,
  usesEnvCredentials,
} from "../agentcore/env-credentials.js";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
  AGENT_EXPORT_BASE_IMAGE,
  AGENT_EXPORT_BASE_IMAGE_DIGEST,
  AGENT_SERVICE_EXPORT_PORT,
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
} from "../limits.js";
import {
  AWS_CREDENTIALS_REQUIREMENTS,
  AWS_ENV_CREDENTIALS_SCRIPT,
} from "./assets/template-assets.gen.js";
import { canonicalJson } from "./opencode.js";
import type { AgentProject } from "./project.js";
import {
  AGENT_HOST_SCHEMA,
  hostDocument,
  installableRuntime,
  isRawApp,
  servableRuntime,
} from "./project-service.js";
import {
  AGENT_APP_CONTEXT_DIR,
  AGENT_APP_DIR,
  AGENT_HOST_CONTEXT_DIR,
  AGENT_HOST_DIR,
  AGENT_INSTALL_DIR,
  AGENT_RAYITO_DIR,
  agentHostFiles,
  hashedVenvInstall,
} from "./recipes.js";
import {
  copyStep,
  directWiring,
  type HostConfig,
  type InstallTarget,
  installTarget,
  runStep,
  STEP_COPY,
  STEP_RUN,
  type TemplateStep,
} from "./runtime.js";
import { appFiles } from "./template.js";

export const EXPORT_TARGETS = ["docker", "agentcore"] as const;
export type ExportTarget = (typeof EXPORT_TARGETS)[number];
/** La plataforma de la imagen exportada: la de AgentCore y la de las
 * imágenes de Rayito. */
export const EXPORT_PLATFORM = "linux/arm64";
/** La imagen base con su digest: lo que se construye hoy es lo que se
 * construirá mañana. */
export const BASE_IMAGE_REFERENCE = `${AGENT_EXPORT_BASE_IMAGE}@${AGENT_EXPORT_BASE_IMAGE_DIGEST}`;
const AGENT_USER = "user";
const AGENT_UID = 1000;
/** `~/.rayito`: lo que `user` debe poder escribir (sesiones del agente). */
const RAYITO_HOME_DIR = `${DEFAULT_AGENT_WORKDIR}/.rayito`;
const HOST_CONFIG_NAME = "host.json";
const HOST_CONFIG_PATH = `${AGENT_RAYITO_DIR}/${HOST_CONFIG_NAME}`;
const RUNTIME_FILES_CONTEXT_DIR = "config";
const DOCKERFILE_NAME = "Dockerfile";
const DOCKERIGNORE_NAME = ".dockerignore";
const README_NAME = "README.md";
const DOCKERIGNORE_CONTENT = `${README_NAME}\n${DOCKERIGNORE_NAME}\n${DOCKERFILE_NAME}\n`;
/** Variable con la que un contenedor exige `Authorization: Bearer` en
 * `/invocations` (`agent_host/serve.py`); no se fija en la imagen. */
const SERVICE_TOKEN_ENV = "RAYITO_AGENT_SERVICE_TOKEN";
const HEALTHCHECK_INTERVAL = "30s";
const HEALTHCHECK_COMMAND =
  'python3 -c "import urllib.request; ' +
  `urllib.request.urlopen('http://127.0.0.1:${AGENT_SERVICE_EXPORT_PORT}/ping')"`;
const PERMISSIONS_CMD = `chown -R root:root ${AGENT_INSTALL_DIR} && chmod -R a+rX,go-w ${AGENT_INSTALL_DIR}`;
/** El argumento de build con el que CodeBuild toma la misma imagen base (mismo digest) de ECR Public (`constants.dockerfileBaseImageArg`). */
export const AGENT_BASE_IMAGE_ARG = "AGENT_BASE_IMAGE";
/** El helper que lee las credenciales del rol de AgentCore para los adaptadores que sólo las leen de variables de entorno (`constants.export.awsCredentials`). */
export const AWS_CREDENTIALS_REQUIREMENTS_NAME = "requirements-aws-credentials.txt";
export const AWS_ENV_CREDENTIALS_SCRIPT_NAME = "aws_env_credentials.py";
export const AWS_CREDENTIALS_REQUIREMENTS_PATH = `${AGENT_INSTALL_DIR}/${AWS_CREDENTIALS_REQUIREMENTS_NAME}`;
export const AWS_CREDENTIALS_VENV_DIR = `${AGENT_RAYITO_DIR}/aws-credentials`;
export const AWS_ENV_CREDENTIALS_SCRIPT_PATH = `${AGENT_RAYITO_DIR}/${AWS_ENV_CREDENTIALS_SCRIPT_NAME}`;
export const AWS_CONFIG_PATH = `${AGENT_RAYITO_DIR}/${AWS_CONFIG_NAME}`;
export const HOST_AWS_ENV_CREDENTIALS_KEY = "aws_env_credentials";
const DEFAULT_FILE_MODE = 0o644;
const OCTAL = 8;
const MODE_DIGITS = 4;

/** Lo que `export()` escribió: el directorio, los ficheros (rutas
 * relativas, ordenadas), el puerto en el que sirve el contenedor y su
 * plataforma. */
export interface AgentExport {
  readonly path: string;
  readonly files: readonly string[];
  readonly port: number;
  readonly platform: string;
}

export interface AgentExportOptions {
  readonly target?: string | undefined;
  readonly outDir: string;
  readonly overwrite?: boolean | undefined;
}

/** Una cadena JSON como la escribe `json.dumps` de Python (ASCII puro), para
 * que los dos SDK produzcan el mismo `Dockerfile` byte a byte. */
function pythonJsonString(value: string): string {
  return JSON.stringify(value).replace(
    /[\u007f-￿]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function fromLines(target: string): string[] {
  if (target !== "agentcore") {
    return [`FROM --platform=${EXPORT_PLATFORM} ${BASE_IMAGE_REFERENCE}`];
  }
  return [
    `ARG ${AGENT_BASE_IMAGE_ARG}=${BASE_IMAGE_REFERENCE}`,
    `FROM --platform=${EXPORT_PLATFORM} \${${AGENT_BASE_IMAGE_ARG}}`,
  ];
}

function stepLines(steps: readonly TemplateStep[]): {
  readonly lines: string[];
  readonly executables: string[];
} {
  const lines: string[] = [];
  const executables: string[] = [];
  for (const step of steps) {
    const args = step.args ?? {};
    if (step.op === STEP_RUN) {
      lines.push(`RUN ${String(args.cmd)}`);
    } else if (step.op === STEP_COPY) {
      lines.push(`COPY ${String(args.src)} ${String(args.dst)}`);
      if (args.executable === true) {
        executables.push(String(args.dst));
      }
    }
  }
  return { lines, executables };
}

function envLines(envs: Readonly<Record<string, string>>): string[] {
  return Object.keys(envs)
    .sort()
    .map((name) => `ENV ${name}=${pythonJsonString(envs[name] as string)}`);
}

function hostConfigBytes(project: AgentProject, host: HostConfig, target: string): Uint8Array {
  const { ready_marker: _ready, ...document } = hostDocument(project, host);
  const credentials = needsAwsEnvCredentials(project, target)
    ? {
        [HOST_AWS_ENV_CREDENTIALS_KEY]: {
          python: `${AWS_CREDENTIALS_VENV_DIR}/bin/python`,
          script: AWS_ENV_CREDENTIALS_SCRIPT_PATH,
        },
      }
    : {};
  return canonicalJson({
    ...document,
    ...credentials,
    schema: AGENT_HOST_SCHEMA,
  });
}

/** Si el adaptador del proyecto sólo lee las credenciales de AWS de variables de entorno, en AgentCore el host se las pone leídas del rol del runtime. */
function needsAwsEnvCredentials(project: AgentProject, target: string): boolean {
  return target === "agentcore" && usesEnvCredentials(project.runtime);
}

function awsCredentialSteps(install: InstallTarget): readonly TemplateStep[] {
  return [
    copyStep(AWS_CREDENTIALS_REQUIREMENTS_NAME, AWS_CREDENTIALS_REQUIREMENTS_PATH),
    runStep(
      hashedVenvInstall(install, {
        venvDir: AWS_CREDENTIALS_VENV_DIR,
        requirementsPath: AWS_CREDENTIALS_REQUIREMENTS_PATH,
        requirementsSha256: AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
      }),
    ),
    copyStep(AWS_ENV_CREDENTIALS_SCRIPT_NAME, AWS_ENV_CREDENTIALS_SCRIPT_PATH),
  ];
}

function dockerfile(
  project: AgentProject,
  host: HostConfig,
  steps: readonly TemplateStep[],
  target: string,
): string {
  const { lines: installLines, executables } = stepLines(steps);
  const configDirs = [...new Set(host.files.files.map((file) => posix.dirname(file.path)))].sort();
  const copyFiles = host.files.files.map((file, index) => {
    const mode = (file.mode ?? DEFAULT_FILE_MODE).toString(OCTAL).padStart(MODE_DIGITS, "0");
    return (
      `COPY --chown=${AGENT_USER}:${AGENT_USER} --chmod=${mode} ` +
      `${RUNTIME_FILES_CONTEXT_DIR}/${index} ${file.path}`
    );
  });
  const permissions =
    PERMISSIONS_CMD + (executables.length > 0 ? ` && chmod 0755 ${executables.join(" ")}` : "");
  const command = `[${[
    host.python,
    AGENT_HOST_DIR,
    "serve",
    HOST_CONFIG_PATH,
    "--port",
    String(AGENT_SERVICE_EXPORT_PORT),
  ]
    .map(pythonJsonString)
    .join(", ")}]`;
  const lines = [
    "# syntax=docker/dockerfile:1",
    `# Agente \`${project.name}\` exportado por Rayito. Construye con:`,
    `#   docker buildx build --platform ${EXPORT_PLATFORM} -t ${project.name} .`,
    ...fromLines(target),
    `RUN useradd --create-home --uid ${AGENT_UID} --shell /bin/bash ${AGENT_USER}` +
      ` && mkdir -p ${AGENT_STATE_DIR} ${configDirs.join(" ")}` +
      ` && chown -R ${AGENT_USER}:${AGENT_USER} ${RAYITO_HOME_DIR}`,
    ...installLines,
    `COPY ${AGENT_HOST_CONTEXT_DIR} ${AGENT_HOST_DIR}`,
    `COPY ${HOST_CONFIG_NAME} ${HOST_CONFIG_PATH}`,
    ...(project.source === undefined ? [] : [`COPY ${AGENT_APP_CONTEXT_DIR} ${AGENT_APP_DIR}`]),
    `RUN ${permissions}`,
    ...copyFiles,
    ...envLines(host.envs),
    `EXPOSE ${AGENT_SERVICE_EXPORT_PORT}`,
    `USER ${AGENT_USER}`,
    `WORKDIR ${DEFAULT_AGENT_WORKDIR}`,
    `HEALTHCHECK --interval=${HEALTHCHECK_INTERVAL} CMD ${HEALTHCHECK_COMMAND}`,
    `CMD ${command}`,
  ];
  return `${lines.join("\n")}\n`;
}

function agentCoreReadmeLines(port: number): string[] {
  return [
    "",
    "## Amazon Bedrock AgentCore Runtime",
    "",
    `AgentCore espera una imagen \`${EXPORT_PLATFORM}\` que sirva \`/ping\` y \`/invocations\``,
    `en el puerto ${port}, como esta. La forma más corta de desplegarla es`,
    "`rayito agent deploy <directorio> --target agentcore`: construye la imagen con",
    "CodeBuild (sin Docker local), la sube a Amazon ECR, crea el rol del runtime con",
    "el mínimo privilegio y crea o actualiza el runtime. A mano: sube la imagen a ECR,",
    "crea el runtime en la consola o con la CLI de AgentCore y activa MMDSv2",
    "(`requireMMDSV2` en `UpdateAgentRuntime`), sin el que AgentCore rechaza las",
    "invocaciones. AgentCore autentica las invocaciones por sí mismo (SigV4): no",
    `fijes \`${SERVICE_TOKEN_ENV}\`.`,
    "",
    `El argumento de build \`${AGENT_BASE_IMAGE_ARG}\` toma la misma imagen base, con el mismo`,
    "digest, de otro registro (por ejemplo `public.ecr.aws/docker/library/python`).",
    "",
    "### Seguridad: sin pasarela de secretos",
    "",
    "En AgentCore no hay pasarela de secretos de Rayito: el agente obtiene las",
    "credenciales del rol del runtime por el servicio de metadatos de la microVM, y",
    "cualquier código que corra en la sesión puede leerlas. Da al rol sólo lo que el",
    "agente necesita: los modelos de Bedrock que usa y, si hace falta, los secretos",
    "que debe leer.",
  ];
}

function readme(project: AgentProject, target: string): string {
  const port = AGENT_SERVICE_EXPORT_PORT;
  const lines = [
    `# ${project.name}`,
    "",
    "Contexto de build exportado por Rayito (`AgentProject.export()`). El contenedor",
    `sirve el contrato \`rayito.agent-service/1\` en el puerto ${port}: \`GET /ping\` y`,
    "`POST /invocations`.",
    "",
    "## Construir y probar en local",
    "",
    "```bash",
    `docker buildx build --platform ${EXPORT_PLATFORM} -t ${project.name} .`,
    `docker run --rm --platform ${EXPORT_PLATFORM} -p ${port}:${port} \\`,
    `  -e ${SERVICE_TOKEN_ENV}=<token> ${project.name}`,
    `curl http://127.0.0.1:${port}/ping`,
    `curl -X POST http://127.0.0.1:${port}/invocations \\`,
    "  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \\",
    '  -d \'{"prompt": "hola"}\'',
    "```",
    "",
    "Con `Accept: text/event-stream` la respuesta llega como un mensaje SSE por evento.",
    `\`${SERVICE_TOKEN_ENV}\` exige \`Authorization: Bearer <token>\` en \`/invocations\`.`,
    "",
    "## Credenciales",
    "",
    "Fuera de un sandbox de Rayito no hay pasarela de secretos: la credencial del",
    "modelo la pone la plataforma donde corras el contenedor (variables de entorno o",
    "rol de IAM) y el agente puede leerla. La garantía de que el agente nunca ve la",
    "credencial sólo vale dentro de Rayito.",
  ];
  if (target === "agentcore") {
    lines.push(...agentCoreReadmeLines(port));
  }
  return `${lines.join("\n")}\n`;
}

function containerHost(project: AgentProject): HostConfig {
  return servableRuntime(project).hostConfig(project.spec, {
    wiring: directWiring(),
    workdir: DEFAULT_AGENT_WORKDIR,
  });
}

/** `true` si el proyecto es una app con `protocol: "raw"`: no cumple el contrato de AgentCore (`/ping` y `/invocations`), así que no se despliega allí. */
export function usesRawProtocol(project: AgentProject): boolean {
  return isRawApp(containerHost(project));
}

/** El árbol de ficheros del export (ruta relativa -> contenido). Puro salvo
 * la lectura del código del usuario. */
export async function renderExport(
  project: AgentProject,
  target = "docker",
): Promise<Map<string, string | Uint8Array>> {
  const runtime = installableRuntime(project);
  const install = installTarget({ platform: "container" });
  const credentialProcess = target === "agentcore" && usesCredentialProcess(project.runtime);
  const base = containerHost(project);
  const host = credentialProcess
    ? { ...base, envs: { ...base.envs, ...awsProfileEnvs(AWS_CONFIG_PATH) } }
    : base;
  if (target === "agentcore" && isRawApp(host)) {
    throw new InvalidArgumentError(
      "una app con protocol: 'raw' no cumple el contrato de AgentCore (/ping + /invocations)",
    );
  }
  const tree = new Map<string, string | Uint8Array>();
  for (const [name, data] of Object.entries(runtime.contextFiles())) {
    tree.set(name, data);
  }
  for (const [relative, data] of Object.entries(agentHostFiles())) {
    tree.set(`${AGENT_HOST_CONTEXT_DIR}/${relative}`, data);
  }
  host.files.files.forEach((file, index) => {
    tree.set(`${RUNTIME_FILES_CONTEXT_DIR}/${index}`, file.data);
  });
  tree.set(HOST_CONFIG_NAME, hostConfigBytes(project, host, target));
  const envCredentials = needsAwsEnvCredentials(project, target);
  if (envCredentials || credentialProcess) {
    tree.set(AWS_CREDENTIALS_REQUIREMENTS_NAME, AWS_CREDENTIALS_REQUIREMENTS);
    tree.set(AWS_ENV_CREDENTIALS_SCRIPT_NAME, AWS_ENV_CREDENTIALS_SCRIPT);
  }
  if (credentialProcess) {
    tree.set(
      AWS_CONFIG_NAME,
      awsConfigText(`${AWS_CREDENTIALS_VENV_DIR}/bin/python`, AWS_ENV_CREDENTIALS_SCRIPT_PATH),
    );
  }
  if (project.source !== undefined) {
    for (const [relative, data] of await appFiles(project.source, project.exclude)) {
      tree.set(`${AGENT_APP_CONTEXT_DIR}/${relative}`, data);
    }
  }
  const steps = [
    ...runtime.installSteps(install),
    ...(envCredentials || credentialProcess ? awsCredentialSteps(install) : []),
    ...(credentialProcess ? [copyStep(AWS_CONFIG_NAME, AWS_CONFIG_PATH)] : []),
  ];
  tree.set(DOCKERFILE_NAME, dockerfile(project, host, steps, target));
  tree.set(DOCKERIGNORE_NAME, DOCKERIGNORE_CONTENT);
  tree.set(README_NAME, readme(project, target));
  return new Map([...tree.entries()].sort(([left], [right]) => (left < right ? -1 : 1)));
}

async function isNonEmptyDirectory(path: string): Promise<boolean> {
  try {
    return (await readdir(path)).length > 0;
  } catch {
    return false;
  }
}

/** Renderiza y escribe el contexto de build de `project` en `outDir`. Falla
 * con `InvalidArgumentError`, antes de escribir nada, si `target` no existe,
 * el runtime no se puede instalar ni servir, el proyecto es una app `raw`
 * para `agentcore` o `outDir` no está vacío (sin `overwrite`). */
export async function exportProject(
  project: AgentProject,
  options: AgentExportOptions,
): Promise<AgentExport> {
  const target = options.target ?? "docker";
  if (!(EXPORT_TARGETS as readonly string[]).includes(target)) {
    throw new InvalidArgumentError(`target debe ser uno de ${JSON.stringify(EXPORT_TARGETS)}`);
  }
  const tree = await renderExport(project, target);
  if (options.overwrite !== true && (await isNonEmptyDirectory(options.outDir))) {
    throw new InvalidArgumentError("outDir no está vacío; usa otro directorio u overwrite: true");
  }
  for (const [relative, data] of tree) {
    const file = join(options.outDir, relative);
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, data);
  }
  return Object.freeze({
    path: options.outDir,
    files: Object.freeze([...tree.keys()]),
    port: AGENT_SERVICE_EXPORT_PORT,
    platform: EXPORT_PLATFORM,
  });
}
