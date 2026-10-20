/**
 * Los puertos de `--target agentcore` sobre el SDK de AWS v3 (ADR-031, D1): un
 * conjunto de adaptadores para una región y unas credenciales. Construirlo no
 * llama a AWS ni carga ningún cliente: cada uno se importa en su primera
 * llamada.
 */

import { CloudFormationProvisioner } from "../stacks/cloudformation.js";
import { SdkRuntimeControl, SdkRuntimeInvoker } from "./aws.js";
import {
  SdkAccountIdentity,
  SdkBucketDiscovery,
  SdkImageBuilder,
  SdkImageRegistry,
  SdkRuntimeLogs,
  SdkSourceStore,
} from "./aws-build.js";
import type { AwsSettings } from "./aws-support.js";
import type { AgentCorePorts } from "./ports.js";

export function sdkAgentCorePorts(settings: AwsSettings): AgentCorePorts {
  return {
    control: new SdkRuntimeControl(settings),
    invoker: new SdkRuntimeInvoker(settings),
    builder: new SdkImageBuilder(settings),
    registry: new SdkImageRegistry(settings),
    source: new SdkSourceStore(settings),
    buckets: new SdkBucketDiscovery(settings),
    logs: new SdkRuntimeLogs(settings),
    identity: new SdkAccountIdentity(settings),
    stacks: new CloudFormationProvisioner({
      region: settings.region,
      credentials: settings.credentials,
    }),
  };
}
