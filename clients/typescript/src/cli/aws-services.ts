/**
 * El adaptador de `CliServices` sobre el SDK de AWS v3. Resuelve el perfil y
 * la región como la CLI de Python (`--profile`/`--region`, luego
 * `AWS_REGION`, luego la cadena del SDK) y construye cada cliente al usarlo:
 * `rayito stack list` o `image zip` nunca tocan AWS.
 */

import {
  GetMicrovmImageVersionCommand,
  LambdaMicrovmsClient,
} from "@aws-sdk/client-lambda-microvms";
import { STSClient } from "@aws-sdk/client-sts";
import {
  awsClientSettingsOf,
  type ControlPlane,
  clientConfig,
  sharedControlPlane,
} from "../aws/control-plane.js";
import type { StsSender } from "../dashboard/assume.js";
import {
  artifactBasename,
  listImageSummaries,
  listImageVersionSummaries,
  type Sender,
} from "../dashboard/aws.js";
import type { ImageSummary, ImageVersionSummary } from "../dashboard/domain.js";
import type { ImageCatalog } from "../dashboard/ports.js";
import { SdkPrunePorts, SdkPublishPorts } from "../images/aws.js";
import { DynamoDbIndex } from "../index/dynamodb.js";
import type { Logger } from "../logger.js";
import type { SandboxInfo, SandboxListItem } from "../models.js";
import { probeSandboxInfo } from "../sandbox/info-probe.js";
import { Sandbox } from "../sandbox/sandbox.js";
import { CloudWatchLogsApi } from "../sandbox-logs/aws.js";
import { OptionalStacks } from "../stacks/service.js";
import type { TransportSettings } from "../transport/transport.js";
import { SdkDoctorAws } from "./doctor/aws.js";
import { NO_REGION_MESSAGE, UsageError } from "./failures.js";
import { SdkTemplateTools, sdkCustomDomain, sdkLifecycleEvents } from "./ops-adapters.js";
import type {
  CliServices,
  ImageArtifacts,
  RemoteSandbox,
  SandboxCreateRequest,
  SandboxGateway,
  SandboxListQuery,
  SessionSettings,
} from "./services.js";

const PROFILE_ENV_VAR = "AWS_PROFILE";
const REGION_ENV_VAR = "AWS_REGION";

/**
 * Los sandboxes sobre `Sandbox` del SDK y un plano de control dado. Con
 * `transport` cada llamada al sandbox usa ese transporte: sólo lo pasan los
 * e2e locales, que apuntan al `rayd` de Docker en vez de al proxy de AWS.
 */
export class SdkSandboxGateway implements SandboxGateway {
  readonly #plane: ControlPlane;
  readonly #region: string;
  readonly #logger: Logger | undefined;
  readonly #transport: { readonly transport?: Partial<TransportSettings> };

  constructor(
    plane: ControlPlane,
    region: string,
    logger: Logger | undefined,
    transport?: Partial<TransportSettings>,
  ) {
    this.#plane = plane;
    this.#region = region;
    this.#logger = logger;
    this.#transport = transport === undefined ? {} : { transport };
  }

  list(query: SandboxListQuery): AsyncIterable<SandboxListItem> {
    const hasMetadata = Object.keys(query.metadata).length > 0;
    return Sandbox.list({
      controlPlane: this.#plane,
      template: query.template,
      templateVersion: query.templateVersion,
      states: query.states,
      ...this.#transport,
      ...(hasMetadata ? { metadata: query.metadata } : {}),
      ...(query.indexTable === undefined
        ? {}
        : { index: new DynamoDbIndex({ tableName: query.indexTable, region: this.#region }) }),
    });
  }

  info(sandboxId: string, options: { readonly readMetadata: boolean }): Promise<SandboxInfo> {
    const settings = { controlPlane: this.#plane, ...this.#transport };
    return options.readMetadata
      ? probeSandboxInfo(sandboxId, settings)
      : Sandbox.getInfo(sandboxId, settings);
  }

  kill(sandboxId: string): Promise<boolean> {
    return Sandbox.kill(sandboxId, { controlPlane: this.#plane });
  }

  connect(sandboxId: string, accessToken: string): Promise<RemoteSandbox> {
    return Sandbox.connect(sandboxId, {
      controlPlane: this.#plane,
      accessToken,
      logger: this.#logger,
      ...this.#transport,
    });
  }

  create(request: SandboxCreateRequest): Promise<RemoteSandbox> {
    return Sandbox.create({
      controlPlane: this.#plane,
      template: request.template,
      timeoutMs: request.timeoutMs,
      accessToken: request.accessToken,
      logger: this.#logger,
      ...this.#transport,
      ...(Object.keys(request.metadata).length === 0 ? {} : { metadata: request.metadata }),
      ...(Object.keys(request.envs).length === 0 ? {} : { envs: request.envs }),
    });
  }
}

/**
 * Las imágenes y sus versiones sin traducir los errores de AWS: la CLI los
 * imprime como `AWS error <código>: <mensaje>`, igual que la de Python.
 */
class RawImageCatalog implements ImageCatalog, ImageArtifacts {
  readonly #client: () => Sender;

  constructor(client: () => Sender) {
    this.#client = client;
  }

  listImages(options: { readonly nameFilter?: string | undefined } = {}): Promise<ImageSummary[]> {
    return listImageSummaries(this.#client(), options.nameFilter);
  }

  listVersions(imageArn: string): Promise<ImageVersionSummary[]> {
    return listImageVersionSummaries(this.#client(), imageArn);
  }

  async activeArtifact(imageArn: string, imageVersion: string): Promise<string | undefined> {
    const response = (await this.#client().send(
      new GetMicrovmImageVersionCommand({ imageIdentifier: imageArn, imageVersion }),
    )) as { codeArtifact?: { uri?: string } };
    return artifactBasename(response);
  }
}

async function resolveRegion(
  region: string | undefined,
  env: Readonly<Record<string, string | undefined>>,
): Promise<string> {
  const explicit = region ?? env[REGION_ENV_VAR];
  if (explicit) {
    return explicit;
  }
  try {
    return await new STSClient({}).config.region();
  } catch {
    throw new UsageError(NO_REGION_MESSAGE);
  }
}

/** `process.env.AWS_PROFILE` es lo que lee la cadena de credenciales del SDK de AWS. */
function selectProfile(profile: string | undefined): void {
  if (profile !== undefined) {
    process.env[PROFILE_ENV_VAR] = profile;
  }
}

export async function createAwsServices(settings: SessionSettings): Promise<CliServices> {
  selectProfile(settings.profile);
  const region = await resolveRegion(settings.region, settings.env);
  const controlPlane = sharedControlPlane(region);
  const credentials = awsClientSettingsOf(controlPlane).credentials;
  let microvms: LambdaMicrovmsClient | undefined;
  const catalog = new RawImageCatalog(() => {
    microvms ??= new LambdaMicrovmsClient(
      clientConfig(region, credentials === undefined ? {} : { credentials }),
    );
    return microvms as unknown as Sender;
  });
  const publishPorts = new SdkPublishPorts(region, credentials);
  const stacks = new OptionalStacks({
    region,
    ...(credentials === undefined ? {} : { credentials }),
  });
  let sts: STSClient | undefined;
  const stsSender: StsSender = {
    send: (command) => {
      sts ??= new STSClient(clientConfig(region, credentials === undefined ? {} : { credentials }));
      return sts.send(command);
    },
  };
  return {
    region,
    controlPlane,
    sts: stsSender,
    sandboxes: new SdkSandboxGateway(controlPlane, region, settings.logger),
    stacks,
    images: catalog,
    imageArtifacts: catalog,
    logs: new CloudWatchLogsApi(region, credentials),
    doctor: new SdkDoctorAws({ region, credentials, plane: controlPlane }),
    publishPorts,
    prunePorts: new SdkPrunePorts({
      region,
      credentials,
      plane: controlPlane,
      accountId: () => publishPorts.accountId(),
    }),
    templates: new SdkTemplateTools(region, credentials),
    lifecycleEvents: sdkLifecycleEvents(region, credentials, stacks),
    customDomain: sdkCustomDomain(region, credentials),
  };
}
