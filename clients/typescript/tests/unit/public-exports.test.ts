/**
 * La superficie pública de runtime, completa y ordenada: `rayito` y
 * `rayito/e2b` exportan exactamente estos valores. Añadir o quitar un export
 * obliga a tocar esta lista (y, si Python tiene el nombre, `__all__`; lo
 * contrasta `scripts/check_sdk_parity.py`). Los tipos no existen en ejecución:
 * los cubre `pnpm typecheck`. Espejo de `test_e2b_v2_exports.py` y de la
 * comprobación de `__all__` de Python.
 */

import { describe, expect, test } from "vitest";
import * as e2b from "../../src/e2b/index.js";
import * as native from "../../src/index.js";

const NATIVE_EXPORTS = [
  "AGENT_TEMPLATE_RUNTIMES",
  "ALL_TRAFFIC",
  "Agent",
  "AgentDeployment",
  "AgentDeploymentStream",
  "AgentError",
  "AgentLimits",
  "AgentModel",
  "AgentPermissions",
  "AgentProject",
  "AgentSpec",
  "AgentStream",
  "AgentTemplate",
  "AuthenticationError",
  "BuildError",
  "BulkResult",
  "CapacityError",
  "ChartType",
  "CodeClient",
  "CommandExitError",
  "CommandHandle",
  "Commands",
  "CustomDomain",
  "CustomDomainError",
  "CustomRuntime",
  "DEFAULT_AGENT_TEMPLATE_BASE",
  "DEFAULT_AGENT_TEMPLATE_NAME",
  "DEFAULT_INTERVAL_S",
  "DEFAULT_PERSIST_TIMEOUT_MS",
  "DEFAULT_SERVICE_NAME",
  "DashboardError",
  "DeepAgents",
  "DiskFullError",
  "DownloadLink",
  "DynamoDbIndex",
  "EfsVolume",
  "EfsVolumes",
  "EgressEnforcement",
  "Execution",
  "FileNotFoundError",
  "FileType",
  "FileUploadError",
  "Filesystem",
  "FilesystemEventType",
  "Fleet",
  "GatewayHandle",
  "GatewayStatus",
  "Git",
  "GitAuthError",
  "GitUpstreamError",
  "HermesRuntime",
  "HostAccess",
  "HttpApp",
  "InMemoryPoolBackend",
  "IndexWriteError",
  "InvalidArgumentError",
  "JsonFilePoolBackend",
  "LambdaMicrovmsControlPlane",
  "LifecycleEvents",
  "LifecycleUnsupportedError",
  "MAX_INTERVAL_S",
  "MIN_INTERVAL_S",
  "McpLocal",
  "McpRemote",
  "MountError",
  "NotFoundError",
  "OptionalStacks",
  "OtlpAuth",
  "PersistenceError",
  "PoolClosedError",
  "PortSpec",
  "Pty",
  "PtyHandle",
  "QuotaEntry",
  "QuotaExceededError",
  "QuotaReport",
  "Quotas",
  "RateLimitError",
  "ReadyCommand",
  "Result",
  "S3Mount",
  "S3Prefix",
  "Sandbox",
  "SandboxError",
  "SandboxIndexError",
  "SandboxLifetimeError",
  "SandboxListPaginator",
  "SandboxNotFoundError",
  "SandboxNotReadyError",
  "SandboxPool",
  "SandboxStateError",
  "ScaleType",
  "SecretCache",
  "SecretError",
  "SecretGateway",
  "SecretNotFoundError",
  "SecretRef",
  "SecretStore",
  "ServeOptions",
  "StackError",
  "StrandsRuntime",
  "SubAgent",
  "TelemetryExport",
  "Template",
  "TemplateError",
  "TimeoutError",
  "TokenUsage",
  "TransferError",
  "UnimplementedError",
  "UploadTicket",
  "UsageCost",
  "VERSION",
  "VolumeError",
  "VolumeMountError",
  "VolumeNotFoundError",
  "VolumePathNotFoundError",
  "VolumeStore",
  "WatchHandle",
  "WebhookError",
  "agentFailed",
  "agentPoolWarmup",
  "anthropicGateway",
  "azureOpenaiGateway",
  "bedrockGateway",
  "deepseekGateway",
  "defaultIdlePolicy",
  "directWiring",
  "gatewayWiring",
  "geminiGateway",
  "groqGateway",
  "hostConfig",
  "installTarget",
  "isInstallable",
  "isServable",
  "litellmGateway",
  "mistralGateway",
  "openaiCompatibleGateway",
  "openaiGateway",
  "openrouterGateway",
  "parseChart",
  "validatePoolConfig",
  "waitForFile",
  "waitForPort",
  "waitForProcess",
  "waitForUrl",
  "xaiGateway",
] as const;

const E2B_EXPORTS = [
  "ALL_TRAFFIC",
  "AuthenticationError",
  "BuildError",
  "ChartType",
  "CommandExitError",
  "ConnectionConfig",
  "DEFAULT_WATCH_TIMEOUT_MS",
  "E2B",
  "Execution",
  "FileNotFoundError",
  "FileType",
  "FileUploadError",
  "Filesystem",
  "FilesystemEventType",
  "Git",
  "GitAuthError",
  "GitUpstreamError",
  "InvalidArgumentError",
  "NotEnoughSpaceError",
  "NotFoundError",
  "Pty",
  "RateLimitError",
  "Result",
  "Sandbox",
  "SandboxError",
  "SandboxNotFoundError",
  "SandboxPaginator",
  "ScaleType",
  "Secret",
  "SecretError",
  "SecretNotFoundError",
  "SecretPaginator",
  "ServiceBusyError",
  "Template",
  "TemplateError",
  "TimeoutError",
  "UnimplementedError",
  "Volume",
  "WatchHandle",
  "bindVolume",
  "default",
  "getSignature",
] as const;

function sortedKeys(module: object): string[] {
  return Object.keys(module).sort();
}

describe("exports públicos", () => {
  test("`rayito` exporta exactamente los valores esperados", () => {
    expect(sortedKeys(native)).toEqual([...NATIVE_EXPORTS]);
  });

  test("`rayito/e2b` exporta exactamente los valores esperados", () => {
    expect(sortedKeys(e2b)).toEqual([...E2B_EXPORTS]);
  });

  test("ningún export es `undefined`", () => {
    for (const module of [native, e2b]) {
      for (const [name, value] of Object.entries(module)) {
        expect(value, name).not.toBeUndefined();
      }
    }
  });

  test("el default de `rayito/e2b` es su Sandbox", () => {
    expect(e2b.default).toBe(e2b.Sandbox);
  });

  test("los tipos de gráficos, ProcessInfo y WriteEntry se importan desde `rayito/e2b`", () => {
    const chart: e2b.Chart | undefined = undefined;
    const process: e2b.ProcessInfo | undefined = undefined;
    const entry: e2b.WriteEntry | undefined = undefined;
    expect([chart, process, entry]).toEqual([undefined, undefined, undefined]);
    expect(e2b.ChartType).toBe(native.ChartType);
    expect(e2b.ScaleType).toBe(native.ScaleType);
    expect(e2b.WatchHandle).toBe(native.WatchHandle);
  });
});
