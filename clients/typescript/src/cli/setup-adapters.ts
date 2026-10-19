/**
 * Los adaptadores de `rayito setup` (ADR-030, D1): la pila de IAM sobre
 * `OptionalStacks`, el bucket sobre `HeadBucket` del doctor, la imagen base
 * sobre el núcleo de `images/gateway.ts` y la publicación sobre la misma
 * tubería de `rayito image publish`. Se componen con lo que la sesión ya
 * tiene, como `ops-adapters.ts`; construirlos no llama a AWS ni carga ningún
 * peer. Espejo de `rayito._account_setup._aws` y `cli/_setup_publisher.py`.
 */

import {
  AccountSetupError,
  ARTIFACT_BUCKET_PARAMETER,
  BASE_IMAGE_NAME,
  BASE_IMAGE_VARIANT,
  type BucketProbe,
  IAM_STACK_NAME,
  type ImageGateResult,
  type ImageObservation,
  type PublishedImage,
  type StackObservation,
} from "../account-setup/domain.js";
import { IAM_STACK_COMPONENT } from "../account-setup/iam-stack.js";
import type {
  AccountSetupGateways,
  ArtifactBucketGateway,
  BaseImageGateway,
  BaseImagePublisher,
  IamStackGateway,
  Progress,
} from "../account-setup/ports.js";
import { GithubReleaseSource } from "../account-setup/release.js";
import { BUCKET_MISSING_CODES } from "../agent/aws-resolve.js";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { fileArtifacts } from "../images/artifact-node.js";
import {
  accountImageArn,
  DEFAULT_BUILD_TIMEOUT_MS,
  DEFAULT_MEMORY_MIB,
  type Sleeper,
  waitForGate,
} from "../images/gateway.js";
import { DEFAULT_STACK_NAME, type PublishSettings } from "../images/publish-domain.js";
import {
  buildOrReuse,
  gateTickLine,
  LATEST_BASE_IMAGE_VERSION,
  type PublishPorts,
  resolveBaseImageVersion,
} from "../images/publish-service.js";
import { CloudFormationProvisioner } from "../stacks/cloudformation.js";
import type { StackStatus } from "../stacks/model.js";
import type { StackProvisioner } from "../stacks/port.js";
import { DEFAULT_WAIT_TIMEOUT_MS, OptionalStacks } from "../stacks/service.js";
import { AwsOperationError, type DoctorAws } from "./doctor/ports.js";

type Credentials = AwsClientSettings["credentials"];

/** Los relojes de las esperas; los tests los sustituyen para no dormir. */
export interface SetupClock {
  readonly sleep: Sleeper;
  /** Milisegundos monótonos. */
  readonly now: () => number;
  readonly utcNow: () => Date;
}

const REAL_CLOCK: SetupClock = {
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now: Date.now,
  utcNow: () => new Date(),
};

function observationOf(status: StackStatus | undefined): StackObservation {
  return {
    name: IAM_STACK_NAME,
    status: status?.state,
    outputs: status?.outputs ?? {},
    parameters: status?.parameters ?? {},
  };
}

/** La pila `rayito-m0-iam` a través de `OptionalStacks`, con la plantilla empaquetada del SDK. */
export class SdkIamStack implements IamStackGateway {
  readonly #provisioner: StackProvisioner;
  readonly #stacks: OptionalStacks;
  readonly #waitTimeoutMs: number;

  constructor(provisioner: StackProvisioner, options: { readonly waitTimeoutMs?: number } = {}) {
    this.#provisioner = provisioner;
    this.#stacks = new OptionalStacks({ provisioner });
    this.#waitTimeoutMs = options.waitTimeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
  }

  async observe(): Promise<StackObservation> {
    return observationOf(
      await this.#stacks.status(IAM_STACK_COMPONENT, { stackName: IAM_STACK_NAME }),
    );
  }

  create(bucket: string): Promise<StackObservation> {
    return this.#deployBucket(bucket);
  }

  updateBucket(bucket: string): Promise<StackObservation> {
    return this.#deployBucket(bucket);
  }

  async wait(): Promise<StackObservation> {
    await this.#provisioner.wait(IAM_STACK_NAME, "deployed", this.#waitTimeoutMs);
    return this.observe();
  }

  /** `deploy` crea o actualiza según exista la pila y sólo manda `ArtifactBucket`. */
  async #deployBucket(bucket: string): Promise<StackObservation> {
    const status = await this.#stacks.deploy(IAM_STACK_COMPONENT, {
      stackName: IAM_STACK_NAME,
      parameters: { [ARTIFACT_BUCKET_PARAMETER]: bucket },
      waitTimeoutMs: this.#waitTimeoutMs,
    });
    return observationOf(status);
  }
}

/** Un `HeadBucket`: un código de "no existe o no es accesible" es un bucket ausente, no un fallo. */
export class SdkArtifactBuckets implements ArtifactBucketGateway {
  readonly #doctor: Pick<DoctorAws, "headBucket">;
  readonly #accountId: () => Promise<string>;

  constructor(doctor: Pick<DoctorAws, "headBucket">, accountId: () => Promise<string>) {
    this.#doctor = doctor;
    this.#accountId = accountId;
  }

  accountId(): Promise<string> {
    return this.#accountId();
  }

  async probe(bucket: string): Promise<BucketProbe> {
    try {
      const facts = await this.#doctor.headBucket(bucket);
      return { exists: true, region: facts.region || undefined };
    } catch (error) {
      if (error instanceof AwsOperationError && BUCKET_MISSING_CODES.has(error.code)) {
        return { exists: false, region: undefined };
      }
      throw error;
    }
  }
}

/** `GetMicrovmImage` y `ListMicrovmImageVersions` de `rayito-base`, y la espera del gate de tres estados. */
export class SdkBaseImages implements BaseImageGateway {
  readonly #ports: PublishPorts;
  readonly #clock: Pick<SetupClock, "sleep" | "now">;

  constructor(ports: PublishPorts, clock: Pick<SetupClock, "sleep" | "now"> = REAL_CLOCK) {
    this.#ports = ports;
    this.#clock = clock;
  }

  async observe(name: string): Promise<ImageObservation> {
    const imageArn = accountImageArn(this.#ports.region, await this.#ports.accountId(), name);
    const image = await this.#ports.getMicrovmImage(imageArn);
    if (image === undefined) {
      return { name, imageArn, state: undefined, versions: [] };
    }
    const items = await this.#ports.listMicrovmImageVersions(imageArn);
    return {
      name,
      imageArn,
      state: image.state,
      versions: items.map((item) => ({
        imageVersion: String(item.imageVersion),
        state: String(item.state),
        status: String(item.status),
        createdAt: new Date(item.createdAt as string | number | Date),
      })),
    };
  }

  async wait(
    imageArn: string,
    imageVersion: string,
    onProgress: Progress,
  ): Promise<ImageGateResult> {
    const { gate } = await waitForGate(
      this.#ports,
      imageArn,
      imageVersion,
      DEFAULT_BUILD_TIMEOUT_MS,
      this.#clock.sleep,
      {
        now: this.#clock.now,
        onTick: (read, elapsedMs) => onProgress(gateTickLine(read, imageVersion, elapsedMs)),
      },
    );
    return { launchable: gate.launchable, versionState: gate.versionState };
  }
}

/** Publica `rayito-base` con la tubería de `rayito image publish` sobre la última base gestionada. */
export class PipelineImagePublisher implements BaseImagePublisher {
  readonly #ports: PublishPorts;
  readonly #clock: SetupClock;

  constructor(ports: PublishPorts, clock: SetupClock = REAL_CLOCK) {
    this.#ports = ports;
    this.#clock = clock;
  }

  async publish(
    artifact: string,
    options: {
      readonly bucket: string;
      readonly buildRoleArn: string;
      readonly onProgress: Progress;
    },
  ): Promise<PublishedImage> {
    const { onProgress } = options;
    const baseImageVersion = await resolveBaseImageVersion(this.#ports, LATEST_BASE_IMAGE_VERSION);
    onProgress(`base_image_version=${baseImageVersion}`);
    const settings: PublishSettings = {
      artifact,
      imageName: BASE_IMAGE_NAME,
      variant: BASE_IMAGE_VARIANT,
      bucket: options.bucket,
      stackName: DEFAULT_STACK_NAME,
      buildRoleArn: options.buildRoleArn,
      baseImageVersion,
      memoryMib: DEFAULT_MEMORY_MIB,
      force: false,
      timeoutMs: DEFAULT_BUILD_TIMEOUT_MS,
      osCapabilities: undefined,
      environmentVariables: {},
      withEfs: false,
      artifactRunId: undefined,
    };
    const summary = await buildOrReuse(
      {
        ports: this.#ports,
        artifacts: fileArtifacts,
        sleep: this.#clock.sleep,
        now: this.#clock.now,
        emit: onProgress,
        utcNow: this.#clock.utcNow,
      },
      settings,
    );
    const gate = summary.gate as { readonly versionState?: string } | undefined;
    if (!summary.launchable) {
      throw new AccountSetupError("image_build_failed", {
        name: BASE_IMAGE_NAME,
        imageVersion: String(summary.imageVersion),
        state: gate?.versionState ?? "",
      });
    }
    return {
      imageArn: String(summary.imageArn),
      imageVersion: String(summary.imageVersion),
      artifactUploaded: summary.artifactUploaded === true,
    };
  }
}

/** Los cinco puertos de `rayito setup` sobre la sesión de AWS; no llama a AWS al construirlos. */
export function sdkAccountSetupGateways(input: {
  readonly region: string;
  readonly credentials: Credentials;
  readonly doctor: Pick<DoctorAws, "headBucket">;
  readonly publishPorts: PublishPorts;
}): AccountSetupGateways {
  const { publishPorts } = input;
  return {
    iamStack: new SdkIamStack(
      new CloudFormationProvisioner({ region: input.region, credentials: input.credentials }),
    ),
    buckets: new SdkArtifactBuckets(input.doctor, () => publishPorts.accountId()),
    images: new SdkBaseImages(publishPorts),
    publisher: new PipelineImagePublisher(publishPorts),
    releases: new GithubReleaseSource(),
  };
}
