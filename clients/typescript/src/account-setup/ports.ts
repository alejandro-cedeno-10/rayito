/**
 * Puertos de `rayito setup` (ADR-030, D1): lo que el servicio necesita de la
 * pila de IAM, del bucket, de la imagen base, de quien la publica y de la
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

/** Una línea de progreso. */
export type Progress = (message: string) => void;

export interface IamStackGateway {
  observe(): Promise<StackObservation>;
  /** Crea la pila con `ArtifactBucket` y espera a que quede desplegada. */
  create(bucket: string): Promise<StackObservation>;
  /** Cambia sólo `ArtifactBucket` (el resto conserva su valor) y espera. */
  updateBucket(bucket: string): Promise<StackObservation>;
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

export interface BaseImagePublisher {
  publish(
    artifact: string,
    options: {
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
  readonly buckets: ArtifactBucketGateway;
  readonly images: BaseImageGateway;
  readonly publisher: BaseImagePublisher;
  readonly releases: ReleaseArtifactSource;
}
