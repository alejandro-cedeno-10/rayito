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
  CustomDomain,
  type CustomDomainOptions,
  type CustomDomainRoute,
  type DeployCustomDomainOptions,
  type RegisterRouteOptions,
} from "./custom-domain/service.js";
export {
  AuthenticationError,
  BuildError,
  type BuildErrorOptions,
  CapacityError,
  CommandExitError,
  CustomDomainError,
  DiskFullError,
  FileNotFoundError,
  FileUploadError,
  GatewayError,
  type GatewayErrorOptions,
  GitAuthError,
  GitUpstreamError,
  IndexWriteError,
  InvalidArgumentError,
  LifecycleUnsupportedError,
  MountError,
  type MountErrorOptions,
  NotFoundError,
  PersistenceError,
  type PersistenceErrorOptions,
  PoolClosedError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxIndexError,
  SandboxLifetimeError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  SandboxStateError,
  SecretError,
  SecretNotFoundError,
  StackError,
  type StackErrorOptions,
  TemplateError,
  TimeoutError,
  TransferError,
  type TransferErrorOptions,
  UnimplementedError,
  VolumeError,
  VolumeMountError,
  type VolumeMountErrorOptions,
  VolumeNotFoundError,
  VolumePathNotFoundError,
  WebhookError,
} from "./errors.js";
export {
  type DynamoDbApi,
  DynamoDbIndex,
  type DynamoDbIndexOptions,
  type WriteFailurePolicy,
} from "./index/dynamodb.js";
export type { IndexRecord } from "./index/record.js";
export type { EventKind, EventRecord, KillReason, WebhookInfo } from "./lifecycle-events/domain.js";
export {
  type DeployWebhooksOptions,
  LifecycleEvents,
  type LifecycleEventsOptions,
} from "./lifecycle-events/service.js";
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
export { type MountStatus, S3Mount, type S3MountOptions } from "./s3-mounts/domain.js";
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
export {
  type AllowRule,
  GatewayStatus,
  SecretGateway,
  type SecretGatewayOptions,
} from "./secret-gateway/domain.js";
export { GatewayHandle } from "./secret-gateway/section.js";
export { SecretCache, type SecretCacheOptions } from "./secrets/cache.js";
export type { SecretOptions, SecretsInput } from "./secrets/inject.js";
export { type SecretLike, SecretRef, type SecretRefOptions } from "./secrets/names.js";
export {
  type SecretInfo,
  type SecretPage,
  SecretStore,
  type SecretStoreOptions,
  type SecretsManagerApi,
} from "./secrets/store.js";
export type {
  ResolvedSize,
  SizeInput,
  SizeName,
  SizeRequest,
} from "./sizing/sizing.js";
export type {
  CostStatement,
  DeployAction,
  DeployPlan,
  ParameterChange,
  ParameterPlan,
  StackArtifact,
  StackComponent,
  StackParameter,
  StackStatus,
} from "./stacks/model.js";
export type { DeployTarget, StackProvisioner, UpdateOutcome } from "./stacks/port.js";
export {
  type DeployOptions,
  type DestroyOptions,
  OptionalStacks,
  type OptionalStacksOptions,
  type ParameterChangesOptions,
} from "./stacks/service.js";
export {
  DEFAULT_INTERVAL_S,
  DEFAULT_SERVICE_NAME,
  MAX_INTERVAL_S,
  MIN_INTERVAL_S,
  type NameStyleOption,
  OtlpAuth,
  TelemetryExport,
  type TelemetryExportOptions,
  type TelemetryHealth,
} from "./telemetry-export/domain.js";
export type {
  BuildClients,
  BuildHandle,
  BuildInfo,
  BuildOptions,
  BuildState,
  BuildStatus,
} from "./templates/build.js";
export { Template } from "./templates/dsl.js";
export type {
  BaseImageRef,
  CopyStep,
  EnvStep,
  ReadyPoll,
  RunStep,
  StartSpec,
  TemplateSpec,
  UserStep,
  WireStep,
  WorkdirStep,
} from "./templates/instructions.js";
export {
  ReadyCommand,
  waitForFile,
  waitForPort,
  waitForProcess,
  waitForUrl,
} from "./templates/ready-cmds.js";
export type { TransportSettings } from "./transport/transport.js";
export { VERSION } from "./version.js";
export {
  EfsVolume,
  type EfsVolumeOptions,
  type MountState,
  type VolumeStatus,
} from "./volumes/domain.js";
export {
  EfsVolumes,
  type EfsVolumesDeployOptions,
  type EfsVolumesOptions,
} from "./volumes/efs-volumes.js";
export type {
  EfsNetworkReport,
  FindingLevel,
  NetworkFinding,
  NetworkInspector,
} from "./volumes/network.js";
export { VolumeStore, type VolumeStoreOptions } from "./volumes/store.js";
