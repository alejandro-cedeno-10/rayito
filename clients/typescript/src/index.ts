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
  type ListMicrovmsPageOptions,
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
  DiskFullError,
  FileNotFoundError,
  FileUploadError,
  GitAuthError,
  GitUpstreamError,
  InvalidArgumentError,
  LifecycleUnsupportedError,
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
  TransferError,
  type TransferErrorOptions,
  UnimplementedError,
} from "./errors.js";
export type { Logger } from "./logger.js";
export {
  ALL_TRAFFIC,
  type CodeContext,
  type CommandResult,
  defaultIdlePolicy,
  EgressEnforcement,
  type EgressProxyInput,
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
  type MicrovmListPage,
  type NetworkPolicyInput,
  type NetworkSelector,
  type NetworkSelectorContext,
  type NetworkState,
  type OutputChunk,
  type OutputMessage,
  type ProcessInfo,
  type PtySize,
  type ResolvedS3Staging,
  Result,
  type S3Staging,
  type SandboxHealth,
  type SandboxInfo,
  type SandboxLifecycle,
  type SandboxListItem,
  type SandboxMetrics,
  type TransferDirectionName,
  type TransferPhaseName,
  type TransferStatus,
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
  type ReadResult,
  type RemoveOptions,
  type UserOptions,
  WatchHandle,
  type WatchOptions,
  type WriteFilesOptions,
  type WriteOptions,
} from "./sandbox/filesystem.js";
export {
  Git,
  type GitAddOpts,
  type GitCloneOpts,
  type GitCommitOpts,
  type GitConfigOpts,
  type GitDangerouslyAuthenticateOpts,
  type GitDeleteBranchOpts,
  type GitInitOpts,
  type GitPullOpts,
  type GitPushOpts,
  type GitRemoteAddOpts,
  type GitRequestOpts,
  type GitResetOpts,
  type GitRestoreOpts,
} from "./sandbox/git.js";
export type {
  GitBranches,
  GitConfigScope,
  GitFileStatus,
  GitResetMode,
  GitStatus,
  GitStatusLabel,
} from "./sandbox/git-args.js";
export type { LoggingOption, PortLike } from "./sandbox/launch.js";
export type { OnTimeout } from "./sandbox/lifecycle.js";
export type { ListOrder } from "./sandbox/listing.js";
export type { MetricsHistoryOptions } from "./sandbox/metrics.js";
export { SandboxListPaginator } from "./sandbox/paginator.js";
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
  type InstanceConnectOptions,
  type PauseOptions,
  Sandbox,
  type SandboxConnectOptions,
  type SandboxCreateOptions,
  type SandboxListOptions,
  type SandboxPaginateOptions,
  type SandboxSetTimeoutOptions,
  type SignedUrlOptions,
  type StaticMetricsHistoryOptions,
  type StaticPauseOptions,
  type StaticUpdateNetworkOptions,
  type UpdateNetworkOptions,
} from "./sandbox/sandbox.js";
export {
  DownloadLink,
  type DownloadUrlOptions,
  UploadTicket,
  type UploadUrlOptions,
  type WaitOptions,
} from "./sandbox/transfer.js";
export type { TransportSettings } from "./transport/transport.js";
export { VERSION } from "./version.js";
