/**
 * Adaptador de `LogsApi` sobre `@aws-sdk/client-cloudwatch-logs`, un peer
 * opcional que se importa en la primera llamada (ADR-014): sin leer logs no
 * se carga ni se llama a AWS. Un error del SDK sale como el código de AWS
 * (`ResourceNotFoundException` es `LogGroupNotFoundError`).
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import {
  awsCode,
  CLOUDWATCH_LOGS_PEER,
  loadOptionalSdkClient,
  type OptionalSdkClient,
} from "../aws/optional-client.js";
import {
  type DescribeStreamsRequest,
  type EventsPage,
  type GetEventsRequest,
  LogGroupNotFoundError,
  type LogsApi,
  STREAM_PAGE_SIZE,
  type StreamsPage,
} from "./service.js";

const NOT_FOUND_CODE = "ResourceNotFoundException";
const FEATURE = "rayito sandbox logs";

interface CloudWatchLogsModule {
  readonly CloudWatchLogsClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly DescribeLogStreamsCommand: new (input: object) => unknown;
  readonly GetLogEventsCommand: new (input: object) => unknown;
}

interface StreamsResponse {
  readonly logStreams?: ReadonlyArray<{ readonly logStreamName?: string }>;
  readonly nextToken?: string;
}

interface EventsResponse {
  readonly events?: ReadonlyArray<{ readonly timestamp?: number; readonly message?: string }>;
  readonly nextForwardToken?: string;
  readonly nextBackwardToken?: string;
}

function describeInput(request: DescribeStreamsRequest): object {
  return {
    logGroupName: request.group,
    ...(request.prefix === undefined ? {} : { logStreamNamePrefix: request.prefix }),
    ...(request.orderByLastEvent === true
      ? { orderBy: "LastEventTime", descending: true, limit: STREAM_PAGE_SIZE }
      : {}),
    ...(request.nextToken === undefined ? {} : { nextToken: request.nextToken }),
  };
}

export class CloudWatchLogsApi implements LogsApi {
  readonly #region: string;
  readonly #credentials: AwsClientSettings["credentials"];
  #client: Promise<OptionalSdkClient<CloudWatchLogsModule>> | undefined;

  constructor(region: string, credentials: AwsClientSettings["credentials"]) {
    this.#region = region;
    this.#credentials = credentials;
  }

  #sdk(): Promise<OptionalSdkClient<CloudWatchLogsModule>> {
    this.#client ??= loadOptionalSdkClient<CloudWatchLogsModule>(
      CLOUDWATCH_LOGS_PEER,
      FEATURE,
      (sdk) => sdk.CloudWatchLogsClient,
      this.#region,
      this.#credentials,
    ).catch((error: unknown) => {
      this.#client = undefined;
      throw error;
    });
    return this.#client;
  }

  async #send<T>(build: (sdk: CloudWatchLogsModule) => unknown): Promise<T> {
    const client = await this.#sdk();
    try {
      return await client.send<T>(build(client.sdk));
    } catch (error) {
      if (awsCode(error) === NOT_FOUND_CODE) {
        throw new LogGroupNotFoundError();
      }
      throw error;
    }
  }

  async describeStreams(request: DescribeStreamsRequest): Promise<StreamsPage> {
    const response = await this.#send<StreamsResponse>(
      (sdk) => new sdk.DescribeLogStreamsCommand(describeInput(request)),
    );
    return {
      names: (response.logStreams ?? []).map((stream) => String(stream.logStreamName)),
      nextToken: response.nextToken,
    };
  }

  async getEvents(request: GetEventsRequest): Promise<EventsPage> {
    const response = await this.#send<EventsResponse>(
      (sdk) =>
        new sdk.GetLogEventsCommand({
          logGroupName: request.group,
          logStreamName: request.stream,
          startFromHead: request.fromEnd !== true,
          ...(request.limit === undefined ? {} : { limit: request.limit }),
          ...(request.startTimeMs === undefined ? {} : { startTime: request.startTimeMs }),
          ...(request.nextToken === undefined ? {} : { nextToken: request.nextToken }),
        }),
    );
    return {
      events: (response.events ?? []).map((event) => ({
        timestamp: Number(event.timestamp),
        message: String(event.message),
      })),
      nextForwardToken: response.nextForwardToken,
      nextBackwardToken: response.nextBackwardToken,
    };
  }
}
