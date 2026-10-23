/**
 * Puertos de `rayito setup` (ADR-030, D1): lo que el servicio necesita de la
 * pila de IAM, de la pila del bucket, del bucket, de la imagen base, de quien la publica y de la
 * release. Los cumplen `cli/setup-adapters.ts` y `release.ts` sobre el SDK de
 * AWS y la red, y los tests con dobles en memoria. Espejo de
 * `rayito._account_setup._ports`.
 */

import type {
  BucketProbe,
  DownloadedRelease,
  ImageGateResult,
  ImageObservation,
  PublishedImage,
  StackObservation,
} from "./domain.js";

/** Etiquetas de pila K=V que `rayito setup --tag` aplica a las pilas que crea o actualiza. */
export type StackTags = Readonly<Record<string, string>>;

/** Una línea de progreso. */
export type Progress = (message: string) => void;

/**
 * La pila `rayito-m0-iam`. `create` y `updateBucket` despliegan la plantilla
 * de este SDK con su `templateFingerprint()` (el sha256 de su
 * `infra/iam.yaml`), que `rayito setup` compara con el de la pila.
 */
export interface IamStackGateway {
  observe(): Promise<StackObservation>;
  templateFingerprint(): Promise<string>;
  /** Crea la pila con `ArtifactBucket` y espera a que quede desplegada. */
  create(bucket: string, tags: StackTags): Promise<StackObservation>;
  /** Cambia `ArtifactBucket` y la plantilla (el resto conserva su valor) y espera. */
  updateBucket(bucket: string, tags: StackTags): Promise<StackObservation>;
  /** Espera a que termine la operación en curso. */
  wait(): Promise<StackObservation>;
}

/**
 * La pila `rayito-m0-artifacts` del bucket. Sin `update`: su `BucketName` no
 * cambia nunca durante la vida de la pila.
 */
export interface ArtifactStackGateway {
  observe(): Promise<StackObservation>;
  /** Crea la pila con `BucketName` y espera a que quede desplegada. */
  create(bucket: string, tags: StackTags): Promise<StackObservation>;
  /** Espera a que termine la operación en curso. */
  wait(): Promise<StackObservation>;
}

export interface ArtifactBucketGateway {
  accountId(): Promise<string>;
  probe(bucket: string): Promise<BucketProbe>;
}

export interface BaseImageGateway {
  observe(name: string): Promise<ImageObservation>;
  wait(imageArn: string, imageVersion: string, onProgress: Progress): Promise<ImageGateResult>;
}

/** Publica la imagen `name` desde el zip `artifact`; `osCapabilities` es el
 * valor de `additionalOsCapabilities` (`"ALL"` para `rayito-base-caps`) o
 * `undefined`. */
export interface BaseImagePublisher {
  publish(
    artifact: string,
    options: {
      readonly name: string;
      readonly osCapabilities: string | undefined;
      readonly bucket: string;
      readonly buildRoleArn: string;
      readonly onProgress: Progress;
    },
  ): Promise<PublishedImage>;
}

export interface ReleaseArtifactSource {
  /** Descarga `SHA256SUMS` y `rayito-image.zip` de la release `tag` a `directory`. */
  fetch(tag: string, directory: string, onProgress: Progress): Promise<DownloadedRelease>;
}

export interface AccountSetupGateways {
  readonly iamStack: IamStackGateway;
  readonly artifactStack: ArtifactStackGateway;
  readonly buckets: ArtifactBucketGateway;
  readonly images: BaseImageGateway;
  readonly publisher: BaseImagePublisher;
  readonly releases: ReleaseArtifactSource;
}
