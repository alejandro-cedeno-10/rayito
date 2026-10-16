/**
 * Los adaptadores de AWS de `rayito image publish` y `rayito image prune`
 * sobre el SDK de AWS v3: cada cliente se construye al usarlo, y CloudWatch
 * Logs y CloudFormation son peers opcionales que sólo se importan cuando la
 * orden los necesita (los logs de un build fallido, la salida `BuildRoleArn`).
 *
 * `PublishPorts` hereda el adaptador de construcción de imágenes de
 * `Template.build()` (`AwsBuildClients`): el mismo envío, el mismo gate y la
 * misma subida por contenido. Los errores de AWS salen saneados
 * (`sanitizeAwsError`); un rechazo de una operación de borrado sale como
 * `PruneRefusal` con su código.
 */

import {
  DeleteMicrovmImageVersionCommand,
  GetMicrovmImageBuildCommand,
  GetMicrovmImageCommand,
  GetMicrovmImageVersionCommand,
  LambdaMicrovmsClient,
  ListManagedMicrovmImageVersionsCommand,
  ListMicrovmImageBuildsCommand,
  UpdateMicrovmImageVersionCommand,
} from "@aws-sdk/client-lambda-microvms";
import { type AwsClientSettings, type ControlPlane, clientConfig } from "../aws/control-plane.js";
import {
  awsCode,
  CLOUDWATCH_LOGS_PEER,
  isAwsSdkError,
  loadOptionalSdkClient,
} from "../aws/optional-client.js";
import { redactAwsText, sanitizeAwsError } from "../aws/sanitize.js";
import { listImageVersionSummaries, type Sender } from "../dashboard/aws.js";
import { CloudFormationProvisioner } from "../stacks/cloudformation.js";
import { AwsBuildClients } from "../templates/build.js";
import { NOT_FOUND_CODE, type Version } from "./prune-domain.js";
import { type PrunePorts, PruneRefusal } from "./prune-service.js";
import { BUILD_ROLE_OUTPUT_KEY } from "./publish-domain.js";
import type { PublishPorts, RecentLogs } from "./publish-service.js";

type Credentials = AwsClientSettings["credentials"];

const INACTIVE_STATUS = "INACTIVE";
const UNKNOWN_CODE = "Unknown";

interface CloudWatchLogsModule {
  readonly CloudWatchLogsClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly DescribeLogStreamsCommand: new (input: object) => unknown;
  readonly GetLogEventsCommand: new (input: object) => unknown;
}

function microvmsClient(region: string, credentials: Credentials): LambdaMicrovmsClient {
  return new LambdaMicrovmsClient(
    clientConfig(region, credentials === undefined ? {} : { credentials }),
  );
}

/** `AwsBuildClients` más lo que sólo necesita publicar: el último build, la salida de la pila del rol y los logs recientes. */
export class SdkPublishPorts extends AwsBuildClients implements PublishPorts {
  readonly #credentials: Credentials;
  readonly #microvms: LambdaMicrovmsClient;

  constructor(region: string, credentials: Credentials) {
    super(region, credentials);
    this.#credentials = credentials;
    this.#microvms = microvmsClient(region, credentials);
  }

  async buildRoleArnOfStack(stackName: string): Promise<string | undefined> {
    const status = await new CloudFormationProvisioner({
      region: this.region,
      credentials: this.#credentials,
    }).describe(stackName);
    return status?.outputs[BUILD_ROLE_OUTPUT_KEY];
  }

  async latestBuild(arn: string, version: string): Promise<Record<string, unknown>> {
    try {
      const builds = await this.#microvms.send(
        new ListMicrovmImageBuildsCommand({ imageIdentifier: arn, imageVersion: version }),
      );
      const first = builds.items?.[0];
      if (first === undefined) {
        return {};
      }
      const build = await this.#microvms.send(
        new GetMicrovmImageBuildCommand({
          imageIdentifier: arn,
          imageVersion: version,
          buildId: first.buildId,
        }),
      );
      const { $metadata: _metadata, ...fields } = build;
      return fields as Record<string, unknown>;
    } catch (error) {
      throw sanitizeAwsError(error, { includeMessage: true });
    }
  }

  async newestManagedVersion(arn: string): Promise<string | undefined> {
    try {
      let newest: { version: string; createdAt: Date } | undefined;
      let token: string | undefined;
      do {
        const page = await this.#microvms.send(
          new ListManagedMicrovmImageVersionsCommand({
            imageIdentifier: arn,
            ...(token === undefined ? {} : { nextToken: token }),
          }),
        );
        for (const item of page.items ?? []) {
          const createdAt = item.createdAt as Date;
          if (newest === undefined || createdAt > newest.createdAt) {
            newest = { version: String(item.imageVersion), createdAt };
          }
        }
        token = page.nextToken;
      } while (token !== undefined);
      return newest?.version;
    } catch (error) {
      throw sanitizeAwsError(error, { includeMessage: true });
    }
  }

  async recentLogs(group: string, streams: number, events: number): Promise<RecentLogs> {
    const { sdk, send } = await loadOptionalSdkClient<CloudWatchLogsModule>(
      CLOUDWATCH_LOGS_PEER,
      "rayito image publish (logs de un build fallido)",
      (module) => module.CloudWatchLogsClient,
      this.region,
      this.#credentials,
    );
    let described: { logStreams?: Array<{ logStreamName?: string }> };
    try {
      described = await send(
        new sdk.DescribeLogStreamsCommand({
          logGroupName: group,
          orderBy: "LastEventTime",
          descending: true,
          limit: streams,
        }),
      );
    } catch (error) {
      return { kind: "unavailable", code: awsCode(error) ?? UNKNOWN_CODE };
    }
    const collected = [];
    for (const stream of described.logStreams ?? []) {
      const name = String(stream.logStreamName);
      const page = await send<{ events?: Array<{ timestamp?: number; message?: string }> }>(
        new sdk.GetLogEventsCommand({ logGroupName: group, logStreamName: name, limit: events }),
      );
      collected.push({
        name,
        events: (page.events ?? []).map((event) => ({
          timestamp: event.timestamp ?? 0,
          message: event.message ?? "",
        })),
      });
    }
    return { kind: "streams", streams: collected };
  }
}

function refusalOf(error: unknown): PruneRefusal | undefined {
  if (!isAwsSdkError(error)) {
    return undefined;
  }
  return new PruneRefusal(awsCode(error) ?? UNKNOWN_CODE, redactAwsText(error.message));
}

/** Los puertos de la poda: versiones de la imagen, MicroVMs vivos y los borrados de una en una. */
export class SdkPrunePorts implements PrunePorts {
  readonly region: string;
  readonly #plane: ControlPlane;
  readonly #microvms: LambdaMicrovmsClient;
  readonly #accountId: () => Promise<string>;

  constructor(options: {
    readonly region: string;
    readonly credentials: Credentials;
    readonly plane: ControlPlane;
    readonly accountId: () => Promise<string>;
  }) {
    this.region = options.region;
    this.#plane = options.plane;
    this.#microvms = microvmsClient(options.region, options.credentials);
    this.#accountId = options.accountId;
  }

  accountId(): Promise<string> {
    return this.#accountId();
  }

  async listVersions(arn: string): Promise<Version[]> {
    const summaries = await listImageVersionSummaries(this.#microvms as unknown as Sender, arn);
    return summaries.map((summary) => ({
      imageVersion: summary.imageVersion,
      state: summary.state,
      status: summary.status,
      createdAt: summary.createdAt,
    }));
  }

  async liveVersions(arn: string): Promise<Set<string>> {
    const live = new Set<string>();
    for await (const item of this.#plane.listMicrovms({ imageArn: arn })) {
      live.add(item.templateVersion);
    }
    return live;
  }

  async imageState(arn: string): Promise<string> {
    const image = await this.#microvms.send(new GetMicrovmImageCommand({ imageIdentifier: arn }));
    return String(image.state);
  }

  async versionState(arn: string, imageVersion: string): Promise<string | undefined> {
    try {
      const detail = await this.#microvms.send(
        new GetMicrovmImageVersionCommand({ imageIdentifier: arn, imageVersion }),
      );
      return String(detail.state);
    } catch (error) {
      if (awsCode(error) === NOT_FOUND_CODE) {
        return undefined;
      }
      throw error;
    }
  }

  async deleteVersion(arn: string, imageVersion: string): Promise<void> {
    try {
      await this.#microvms.send(
        new DeleteMicrovmImageVersionCommand({ imageIdentifier: arn, imageVersion }),
      );
    } catch (error) {
      if (awsCode(error) === NOT_FOUND_CODE) {
        return;
      }
      const refusal = refusalOf(error);
      throw refusal ?? error;
    }
  }

  async deactivateVersion(arn: string, imageVersion: string): Promise<void> {
    try {
      await this.#microvms.send(
        new UpdateMicrovmImageVersionCommand({
          imageIdentifier: arn,
          imageVersion,
          status: INACTIVE_STATUS,
        }),
      );
    } catch (error) {
      const refusal = refusalOf(error);
      throw refusal ?? error;
    }
  }
}
