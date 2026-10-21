/**
 * Los ficheros que `rayito/cdk` deja en disco para la síntesis y el
 * `Provider` de los recursos personalizados de `RayitoAgent` (ADR-032 D3 y
 * D13). El zip del manejador (`infra/agentcore-cdk-handler.py` con su modelo
 * de botocore) y la plantilla `infra/agentcore-runtime.yaml` viajan dentro
 * del paquete; se escriben una vez por proceso en un directorio temporal
 * para que `lambda.Code.fromAsset` y `CfnInclude` los lean. Son los mismos
 * bytes que usa Python, así que el asset tiene el mismo hash.
 */

import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { type PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Code, Function as LambdaFunction, Runtime, RuntimeFamily } from "aws-cdk-lib/aws-lambda";
import { LogGroup, type RetentionDays } from "aws-cdk-lib/aws-logs";
import { Provider } from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import {
  AGENTCORE_BUILD_LOG_RETENTION_DAYS,
  CDK_BUILD_QUERY_INTERVAL_SECONDS,
  CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES,
  CDK_HANDLER_RUNTIME,
  CDK_HANDLER_TIMEOUT_SECONDS,
} from "../limits.js";
import { ARTIFACT_BASE64 } from "../stacks/templates/agentcore-cdk-handler.gen.js";
import { TEMPLATE_BODY } from "../stacks/templates/agentcore-runtime.gen.js";

/** `contract.constants.handler.assetFileName`. */
export const HANDLER_ASSET_FILE_NAME = "agentcore-cdk-handler.zip";
export const RUNTIME_TEMPLATE_FILE_NAME = "agentcore-runtime.yaml";
export const ON_EVENT_ENTRY = "index.on_event";
export const IS_COMPLETE_ENTRY = "index.is_complete";
const STAGING_PREFIX = "rayito-cdk-";

let stagingDirectory: string | undefined;
const staged = new Map<string, string>();

function stage(name: string, content: Uint8Array | string): string {
  const existing = staged.get(name);
  if (existing !== undefined) {
    return existing;
  }
  const file = stagingPath(name, content);
  staged.set(name, file);
  return file;
}

/**
 * Una ruta del directorio temporal de este proceso: con `content`, un
 * fichero con esos bytes; sin él, un directorio vacío. La síntesis lee de
 * aquí los assets que el constructo genera (el zip del código, el contexto
 * de Docker).
 */
export function stagingPath(name: string, content?: Uint8Array | string): string {
  stagingDirectory ??= mkdtempSync(join(tmpdir(), STAGING_PREFIX));
  const path = join(stagingDirectory, name);
  if (content === undefined) {
    mkdirSync(path, { recursive: true });
  } else {
    writeFileSync(path, content);
  }
  return path;
}

/** El zip del manejador en disco (una vez por proceso). */
export function handlerZipPath(): string {
  return stage(HANDLER_ASSET_FILE_NAME, Buffer.from(ARTIFACT_BASE64, "base64"));
}

/** `infra/agentcore-runtime.yaml` en disco (una vez por proceso), para `CfnInclude`. */
export function runtimeTemplatePath(): string {
  return stage(RUNTIME_TEMPLATE_FILE_NAME, TEMPLATE_BODY);
}

const LOG_RETENTION = AGENTCORE_BUILD_LOG_RETENTION_DAYS as RetentionDays;
const LAMBDA_SERVICE = "lambda.amazonaws.com";

function logGroup(scope: Construct, id: string): LogGroup {
  return new LogGroup(scope, id, {
    retention: LOG_RETENTION,
    removalPolicy: RemovalPolicy.DESTROY,
  });
}

/**
 * `Handler`: las dos funciones del `Provider` (`OnEvent` e `IsComplete`)
 * con un rol compartido (`Role`, que sólo escribe en sus grupos de logs de
 * 14 días); el resto de permisos los añade `RayitoAgent` con `addToPolicy`,
 * acotados a su proyecto de CodeBuild, su repositorio y su runtime. Mismos
 * ids que `AgentCoreHandler` de Python.
 */
export class AgentCoreHandler extends Construct {
  readonly role: Role;
  readonly onEvent: LambdaFunction;
  readonly isComplete: LambdaFunction;
  readonly #code: Code;

  constructor(scope: Construct, id: string) {
    super(scope, id);
    this.role = new Role(this, "Role", { assumedBy: new ServicePrincipal(LAMBDA_SERVICE) });
    this.#code = Code.fromAsset(handlerZipPath());
    this.onEvent = this.#function("OnEvent", ON_EVENT_ENTRY);
    this.isComplete = this.#function("IsComplete", IS_COMPLETE_ENTRY);
  }

  #function(id: string, entry: string): LambdaFunction {
    const group = logGroup(this, `${id}Logs`);
    group.grantWrite(this.role);
    return new LambdaFunction(this, id, {
      runtime: new Runtime(CDK_HANDLER_RUNTIME, RuntimeFamily.PYTHON),
      handler: entry,
      code: this.#code,
      role: this.role,
      timeout: Duration.seconds(CDK_HANDLER_TIMEOUT_SECONDS),
      logGroup: group,
    });
  }

  /** Un permiso más para las dos funciones. */
  addToPolicy(statement: PolicyStatement): void {
    this.role.addToPrincipalPolicy(statement);
  }
}

/**
 * El `Provider` del constructo: consulta cada `cdkBuildQueryIntervalSeconds`
 * y se rinde tras el plazo del build más `cdkBuildTotalTimeoutMarginMinutes`.
 * Sus funciones de marco escriben en `ProviderLogs` (14 días).
 */
export function buildProvider(
  scope: Construct,
  handler: AgentCoreHandler,
  buildTimeoutMinutes: number,
): Provider {
  return new Provider(scope, "Provider", {
    onEventHandler: handler.onEvent,
    isCompleteHandler: handler.isComplete,
    queryInterval: Duration.seconds(CDK_BUILD_QUERY_INTERVAL_SECONDS),
    totalTimeout: Duration.minutes(buildTimeoutMinutes + CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES),
    logGroup: logGroup(scope, "ProviderLogs"),
  });
}
