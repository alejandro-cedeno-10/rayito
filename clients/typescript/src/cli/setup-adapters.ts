/**
 * Los adaptadores de `rayito setup` (ADR-030, D1): las pilas de IAM y del
 * bucket sobre `OptionalStacks`, el bucket sobre `HeadBucket` del doctor, la imagen base
 * sobre el núcleo de `images/gateway.ts` y la publicación sobre la misma
 * tubería de `rayito image publish`. Se componen con lo que la sesión ya
 * tiene, como `ops-adapters.ts`; construirlos no llama a AWS ni carga ningún
 * peer. Espejo de `rayito._account_setup._aws` y `cli/_setup_publisher.py`.
 */

import { ARTIFACT_STACK_COMPONENT } from "../account-setup/artifact-stack.js";
import {
  AccountSetupError,
  ARTIFACT_BUCKET_PARAMETER,
  ARTIFACT_STACK_NAME,
  BASE_IMAGE_VARIANT,
  BUCKET_NAME_PARAMETER,
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
  ArtifactStackGateway,
  BaseImageGateway,
  BaseImagePublisher,
  IamStackGateway,
  Progress,
  StackTags,
} from "../account-setup/ports.js";
import { GithubReleaseSource } from "../account-setup/release.js";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { StackError } from "../errors.js";
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
import type { StackComponent, StackStatus } from "../stacks/model.js";
import type { StackProvisioner } from "../stacks/port.js";
import { DEFAULT_WAIT_TIMEOUT_MS, OptionalStacks } from "../stacks/service.js";
import { pythonStringRepr } from "../strings.js";
import { AwsOperationError, type DoctorAws } from "./doctor/ports.js";

type Credentials = AwsClientSettings["credentials"];

/**
 * Códigos de `HeadBucket` (fila `HeadBucket` de `AWS_API_NOTES.md`): 404 es un
 * bucket que no existe.
 */
export const BUCKET_NOT_FOUND_CODES: ReadonlySet<string> = new Set([
  "404",
  "NoSuchBucket",
  "NotFound",
]);
/** 403: el bucket existe y estas credenciales no lo leen, normalmente porque es de otra cuenta. */
export const BUCKET_FORBIDDEN_CODES: ReadonlySet<string> = new Set(["403"]);

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

function observationOf(status: StackStatus | undefined, stackName: string): StackObservation {
  return {
    name: stackName,
    status: status?.state,
    outputs: status?.outputs ?? {},
    parameters: status?.parameters ?? {},
  };
}

/** Una pila de `rayito setup` a través de `OptionalStacks`, con la plantilla empaquetada del SDK. */
export class SdkSetupStack implements IamStackGateway, ArtifactStackGateway {
  readonly #provisioner: StackProvisioner;
  readonly #stacks: OptionalStacks;
  readonly #component: StackComponent;
  readonly #stackName: string;
  readonly #bucketParameter: string;
  readonly #waitTimeoutMs: number;

  constructor(
    provisioner: StackProvisioner,
    options: {
      readonly component: StackComponent;
      readonly stackName: string;
      /** El único parámetro que se manda: el nombre del bucket. */
      readonly bucketParameter: string;
      readonly waitTimeoutMs?: number;
    },
  ) {
    this.#provisioner = provisioner;
    this.#stacks = new OptionalStacks({ provisioner });
    this.#component = options.component;
    this.#stackName = options.stackName;
    this.#bucketParameter = options.bucketParameter;
    this.#waitTimeoutMs = options.waitTimeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
  }

  async observe(): Promise<StackObservation> {
    return observationOf(
      await this.#stacks.status(this.#component, { stackName: this.#stackName }),
      this.#stackName,
    );
  }

  create(bucket: string, tags: StackTags): Promise<StackObservation> {
    return this.#deployBucket(bucket, tags);
  }

  updateBucket(bucket: string, tags: StackTags): Promise<StackObservation> {
    return this.#deployBucket(bucket, tags);
  }

  /** Una pila que desaparece durante la espera es un fallo, como en Python. */
  async wait(): Promise<StackObservation> {
    await this.#provisioner.wait(this.#stackName, "deployed", this.#waitTimeoutMs);
    const observed = await this.observe();
    if (observed.status === undefined) {
      throw new StackError(
        `la pila ${pythonStringRepr(this.#stackName)} desapareció mientras se esperaba`,
        { code: "failed" },
      );
    }
    return observed;
  }

  /** `deploy` crea o actualiza según exista la pila y sólo manda el nombre del bucket. */
  async #deployBucket(bucket: string, tags: StackTags): Promise<StackObservation> {
    const status = await this.#stacks.deploy(this.#component, {
      stackName: this.#stackName,
      parameters: { [this.#bucketParameter]: bucket },
      tags,
      waitTimeoutMs: this.#waitTimeoutMs,
    });
    return observationOf(status, this.#stackName);
  }
}

/** La pila de IAM `rayito-m0-iam`, que recibe `ArtifactBucket`. */
export function sdkIamStack(provisioner: StackProvisioner): SdkSetupStack {
  return new SdkSetupStack(provisioner, {
    component: IAM_STACK_COMPONENT,
    stackName: IAM_STACK_NAME,
    bucketParameter: ARTIFACT_BUCKET_PARAMETER,
  });
}

/** La pila del bucket `rayito-m0-artifacts`, que recibe `BucketName`. */
export function sdkArtifactStack(provisioner: StackProvisioner): SdkSetupStack {
  return new SdkSetupStack(provisioner, {
    component: ARTIFACT_STACK_COMPONENT,
    stackName: ARTIFACT_STACK_NAME,
    bucketParameter: BUCKET_NAME_PARAMETER,
  });
}

/** Un `HeadBucket` con `ExpectedBucketOwner` (un bucket de otra cuenta es `forbidden`): 200 es `found`, 404 `missing` y 403 `forbidden`; cualquier otro error se propaga. */
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
      const facts = await this.#doctor.headBucket(bucket, await this.#accountId());
      return { state: "found", region: facts.region || undefined };
    } catch (error) {
      if (error instanceof AwsOperationError) {
        if (BUCKET_NOT_FOUND_CODES.has(error.code)) {
          return { state: "missing", region: undefined };
        }
        if (BUCKET_FORBIDDEN_CODES.has(error.code)) {
          return { state: "forbidden", region: undefined };
        }
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

/** Publica `rayito-base` (sin capacidades extra) o `rayito-base-caps` (el mismo
 * zip con `additionalOsCapabilities: ["ALL"]`, como `rayito image publish
 * --os-capabilities ALL`) con la tubería de `rayito image publish` sobre la
 * última base gestionada. */
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
      readonly name: string;
      readonly osCapabilities: string | undefined;
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
      imageName: options.name,
      variant: BASE_IMAGE_VARIANT,
      bucket: options.bucket,
      stackName: DEFAULT_STACK_NAME,
      buildRoleArn: options.buildRoleArn,
      baseImageVersion,
      memoryMib: DEFAULT_MEMORY_MIB,
      force: false,
      timeoutMs: DEFAULT_BUILD_TIMEOUT_MS,
      osCapabilities: options.osCapabilities,
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
        name: options.name,
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

/** Los seis puertos de `rayito setup` sobre la sesión de AWS; no llama a AWS al construirlos. */
export function sdkAccountSetupGateways(input: {
  readonly region: string;
  readonly credentials: Credentials;
  readonly doctor: Pick<DoctorAws, "headBucket">;
  readonly publishPorts: PublishPorts;
}): AccountSetupGateways {
  const { publishPorts } = input;
  const provisioner = new CloudFormationProvisioner({
    region: input.region,
    credentials: input.credentials,
  });
  return {
    iamStack: sdkIamStack(provisioner),
    artifactStack: sdkArtifactStack(provisioner),
    buckets: new SdkArtifactBuckets(input.doctor, () => publishPorts.accountId()),
    images: new SdkBaseImages(publishPorts),
    publisher: new PipelineImagePublisher(publishPorts),
    releases: new GithubReleaseSource(),
  };
}
