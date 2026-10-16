/**
 * Puertos que la CLI necesita del SDK y de AWS (capa de aplicación): el
 * plano de control, los sandboxes, las pilas, las imágenes y los logs. Los
 * comandos sólo conocen estas interfaces; `aws-services.ts` las cumple con el
 * SDK de AWS y los tests con dobles en memoria, nunca con AWS.
 */

import type { ControlPlane } from "../aws/control-plane.js";
import type { StsSender } from "../dashboard/assume.js";
import type { ImageCatalog } from "../dashboard/ports.js";
import type { PrunePorts } from "../images/prune-service.js";
import type { PublishPorts } from "../images/publish-service.js";
import type { Logger } from "../logger.js";
import type {
  CommandResult,
  PtySize,
  SandboxInfo,
  SandboxListItem,
  SandboxMetrics,
} from "../models.js";
import type { CommandHandle, CommandOptions } from "../sandbox/commands.js";
import type { LogsApi } from "../sandbox-logs/service.js";
import type { OptionalStacks } from "../stacks/service.js";
import type { DoctorAws } from "./doctor/ports.js";
import type {
  CustomDomainRequest,
  DomainDeployer,
  LifecycleEventsTool,
  TemplateTools,
} from "./ops-ports.js";

/** Un filtro de `rayito sandbox list`; `states` ausente es "los no terminados". */
export interface SandboxListQuery {
  readonly template: string | undefined;
  readonly templateVersion: string | undefined;
  readonly states: readonly string[] | undefined;
  readonly metadata: Readonly<Record<string, string>>;
  /** La tabla del índice de metadatos (opt-in); sin ella `metadata` sondea `Health`. */
  readonly indexTable: string | undefined;
}

export interface SandboxCreateRequest {
  readonly template: string | undefined;
  readonly timeoutMs: number;
  readonly metadata: Readonly<Record<string, string>>;
  readonly envs: Readonly<Record<string, string>>;
  readonly accessToken: string;
}

/** Lo que la CLI usa de una terminal (PTY) del sandbox. */
export interface RemotePtyHandle {
  sendInput(data: Uint8Array): Promise<void>;
  resize(size: PtySize): Promise<void>;
  kill(): Promise<boolean>;
  wait(): Promise<{ readonly exitCode: number }>;
}

export interface RemotePtyOptions {
  readonly size: PtySize;
  readonly user: string | undefined;
  readonly cwd: string | undefined;
  readonly envs: Readonly<Record<string, string>> | undefined;
  readonly onData: (chunk: Uint8Array) => void;
  readonly timeoutMs: number;
}

/** Lo que la CLI usa de un sandbox ya conectado. */
export interface RemoteSandbox {
  readonly sandboxId: string;
  readonly info: SandboxInfo;
  readonly commands: {
    run(cmd: string, options: CommandOptions): Promise<CommandResult | CommandHandle>;
  };
  readonly pty: { create(options: RemotePtyOptions): Promise<RemotePtyHandle> };
  getMetrics(): Promise<SandboxMetrics>;
  close(): void;
  kill(): Promise<boolean>;
}

export interface SandboxGateway {
  list(query: SandboxListQuery): AsyncIterable<SandboxListItem>;
  /** `get-microvm` y, con `readMetadata`, un `Health` sobre un sandbox `RUNNING`. */
  info(sandboxId: string, options: { readonly readMetadata: boolean }): Promise<SandboxInfo>;
  kill(sandboxId: string): Promise<boolean>;
  connect(sandboxId: string, accessToken: string): Promise<RemoteSandbox>;
  create(request: SandboxCreateRequest): Promise<RemoteSandbox>;
}

/** El artefacto de código (nombre del fichero) de una versión de imagen. */
export interface ImageArtifacts {
  activeArtifact(imageArn: string, imageVersion: string): Promise<string | undefined>;
}

/** La sesión de AWS de una invocación: todo se construye al usarlo, nunca al crearla. */
export interface CliServices {
  readonly region: string;
  readonly controlPlane: ControlPlane;
  readonly sandboxes: SandboxGateway;
  readonly stacks: OptionalStacks;
  readonly images: ImageCatalog;
  readonly imageArtifacts: ImageArtifacts;
  readonly logs: LogsApi;
  readonly doctor: DoctorAws;
  readonly publishPorts: PublishPorts;
  readonly prunePorts: PrunePorts;
  readonly templates: TemplateTools;
  /** `LifecycleEvents` de la pila `stackName`; construirlo no llama a AWS. */
  readonly lifecycleEvents: (stackName: string) => LifecycleEventsTool;
  readonly customDomain: (request: CustomDomainRequest) => DomainDeployer;
  /** El cliente de STS de la sesión, para asumir los roles de `rayito dashboard --account`. */
  readonly sts?: StsSender | undefined;
}

export interface SessionSettings {
  readonly profile: string | undefined;
  readonly region: string | undefined;
  readonly env: Readonly<Record<string, string | undefined>>;
  /** El logger de `--verbose`; sin él, el SDK calla. */
  readonly logger: Logger | undefined;
}

/** Construye la sesión; el adaptador real resuelve región y perfil, y un doble no necesita nada. */
export type ServicesFactory = (settings: SessionSettings) => Promise<CliServices>;
