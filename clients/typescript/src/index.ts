/**
 * `rayito`: sandboxes de ejecución para agentes de IA sobre AWS Lambda
 * MicroVMs, dentro de tu propia cuenta. La misma superficie que el SDK Python
 * en camelCase y milisegundos; los tipos generados del `.proto` no se
 * re-exportan (son un detalle de implementación).
 *
 * Node 20.0–20.3 no define `Symbol.asyncDispose`; el polyfill de E2B hace que
 * `await using sbx = await Sandbox.create()` funcione en todo Node >= 20.
 */

(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");

export {
  type CommandSender,
  type ControlPlane,
  LambdaMicrovmsControlPlane,
  type ListMicrovmsOptions,
  PortSpec,
} from "./aws/control-plane.js";
export {
  type BarChart,
  type BarData,
  type BoxAndWhiskerChart,
  type BoxAndWhiskerData,
  type Chart,
  ChartType,
  type LineChart,
  type PieChart,
  type PieData,
  type PointData,
  parseChart,
  ScaleType,
  type ScatterChart,
  type SuperChart,
} from "./charts.js";
export {
  AuthenticationError,
  CapacityError,
  CommandExitError,
  FileNotFoundError,
  InvalidArgumentError,
  NotFoundError,
  PersistenceError,
  type PersistenceErrorOptions,
  PoolClosedError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxLifetimeError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  SandboxStateError,
  TimeoutError,
} from "./errors.js";
export type { Logger } from "./logger.js";
export {
  type CodeContext,
  type CommandResult,
  defaultIdlePolicy,
  type EntryInfo,
  Execution,
  type ExecutionError,
  type FilesystemEvent,
  FilesystemEventType,
  FileType,
  HostAccess,
  type IdlePolicy,
  type IdlePolicyInput,
  type Logs,
  type OutputChunk,
  type OutputMessage,
  type ProcessInfo,
  type PtySize,
  Result,
  type SandboxHealth,
  type SandboxInfo,
  type SandboxListItem,
  type SandboxMetrics,
  type WriteData,
  type WriteEntry,
} from "./models.js";
export {
  InMemoryPoolBackend,
  JsonFilePoolBackend,
  type PoolBackend,
} from "./pool/backend.js";
export { type PoolConfig, type ResolvedPoolConfig, validatePoolConfig } from "./pool/config.js";
export type { PoolSlotInfo, PoolStats, SlotRecord } from "./pool/core.js";
export {
  type CloseOptions,
  SandboxPool,
  type SandboxPoolOptions,
  type TakeOptions,
} from "./pool/pool.js";
export {
  CodeClient,
  type ContextLike,
  type CreateContextOptions,
  type ErrorCallback,
  type ResultCallback,
  type RunCodeOptions,
  type StdoutCallback,
} from "./sandbox/code.js";
export {
  CommandHandle,
  type CommandOptions,
  Commands,
  type ConnectOptions,
  type OutputCallback,
  type RequestOptions,
} from "./sandbox/commands.js";
export {
  type EventCallback,
  type ExitCallback,
  Filesystem,
  type ListOptions,
  type ReadFormat,
  type ReadOptions,
  type RemoveOptions,
  type UserOptions,
  WatchHandle,
  type WatchOptions,
  type WriteOptions,
} from "./sandbox/filesystem.js";
export type { LoggingOption, PortLike } from "./sandbox/launch.js";
export {
  type CheckpointFilesOptions,
  type CheckpointProgress,
  type CheckpointProgressCallback,
  type CheckpointResult,
  DEFAULT_PERSIST_TIMEOUT_MS,
  type LaunchOptions,
  type ReincarnateOptions,
  type RestoreFilesOptions,
  type RestoreProgress,
  type RestoreProgressCallback,
  type RestoreResult,
  S3Prefix,
  type S3PrefixInit,
} from "./sandbox/persistence.js";
export {
  Pty,
  type PtyConnectOptions,
  type PtyCreateOptions,
  type PtyDataCallback,
  PtyHandle,
} from "./sandbox/pty.js";
export {
  type ControlPlaneOptions,
  type PauseOptions,
  Sandbox,
  type SandboxConnectOptions,
  type SandboxCreateOptions,
  type SandboxListOptions,
  type StaticPauseOptions,
} from "./sandbox/sandbox.js";
export type { TransportSettings } from "./transport/transport.js";
export { VERSION } from "./version.js";
