/**
 * `rayito/e2b`: el shim de compatibilidad con el SDK JS de E2B 2.x (contrato:
 * `e2b` 2.51.0 y `@e2b/code-interpreter`). Un programa de E2B cambia sólo el
 * import:
 *
 *   import Sandbox from "rayito/e2b";               // antes: from "e2b"
 *   import { Sandbox } from "rayito/e2b";           // antes: from "@e2b/code-interpreter"
 *
 * Lo que Lambda MicroVMs no puede dar (fork, snapshots, templates, volúmenes,
 * secretos, MCP, IAM con audiencia) lanza o rechaza `UnimplementedError` con
 * el motivo; ver docs/site/docs/e2b-compat.md. Los alias de errores son las
 * clases nativas, así `instanceof` vale entre `rayito` y `rayito/e2b`.
 *
 * Node 20.0–20.3 no define `Symbol.asyncDispose`; el polyfill de E2B hace que
 * `await using sbx = await Sandbox.create()` funcione en todo Node >= 20.
 */

(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");

import { Sandbox } from "./sandbox.js";

export {
  AuthenticationError,
  CapacityError as ServiceBusyError,
  CommandExitError,
  DiskFullError as NotEnoughSpaceError,
  FileNotFoundError,
  FileUploadError,
  GitAuthError,
  GitUpstreamError,
  InvalidArgumentError,
  NotFoundError,
  RateLimitError,
  SandboxError,
  SandboxNotFoundError,
  TimeoutError,
  UnimplementedError,
} from "../errors.js";
export type { Logger } from "../logger.js";
export {
  ALL_TRAFFIC,
  type CodeContext as Context,
  type CommandResult,
  type EntryInfo,
  type EntryInfo as WriteInfo,
  Execution,
  type ExecutionError,
  type FilesystemEvent,
  FilesystemEventType,
  FileType,
  type Logs,
  type OutputMessage,
  Result,
} from "../models.js";
export type { CommandHandle } from "../sandbox/commands.js";
export { Git } from "../sandbox/git.js";
export type {
  GitBranches,
  GitFileStatus,
  GitResetMode,
  GitStatus,
} from "../sandbox/git-args.js";
export { E2B, type E2BClientOpts } from "./client.js";
export { ConnectionConfig, type ConnectionOpts, type Username } from "./connection.js";
export { BuildError, TemplateError } from "./errors.js";
export {
  DEFAULT_WATCH_TIMEOUT_MS,
  Filesystem,
  type WatchEventCallback,
  type WatchOpts,
} from "./filesystem.js";
export {
  Pty,
  type PtyConnectOpts,
  type PtyCreateOpts,
  type PtyOutputCallback,
  type PtySize,
} from "./pty.js";
export { getSignature, Secret, Template, Volume } from "./resources.js";
export { Sandbox, type SandboxInstanceConnectOpts, SandboxPaginator } from "./sandbox.js";
export type {
  SandboxConnectOpts,
  SandboxInfo,
  SandboxInfoLifecycle,
  SandboxLifecycle,
  SandboxListOpts,
  SandboxMetrics,
  SandboxMetricsOpts,
  SandboxNetworkInfo,
  SandboxNetworkOpts,
  SandboxNetworkSelector,
  SandboxNetworkUpdate,
  SandboxOnResume,
  SandboxOnTimeout,
  SandboxOpts,
  SandboxPauseOpts,
  SandboxState,
  SandboxUrlOpts,
} from "./types.js";

export default Sandbox;
