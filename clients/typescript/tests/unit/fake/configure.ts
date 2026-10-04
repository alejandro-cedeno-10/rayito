/**
 * `ConfigureService` como lo implementa `rayd`: una respuesta guionada por
 * `nextResults` (por defecto, cada sección presente se aplica) y el estado
 * que `configureStatus` devuelve (`secretGatewayStatus`, `s3MountsStatuses`,
 * `efsVolumesStatus`). Exige
 * `x-access-token`, como el resto de los servicios autenticados.
 */

import { create } from "@bufbuild/protobuf";
import type { HandlerContext } from "@connectrpc/connect";
import {
  ConfigSection,
  type ConfigureRequest,
  type ConfigureResponse,
  ConfigureResponseSchema,
  type ConfigureStatusResponse,
  ConfigureStatusResponseSchema,
  SectionCode,
  type SectionResult,
  SectionResultSchema,
} from "../../../src/gen/rayito/v1/configure_pb.js";
import type { EfsVolumesStatus } from "../../../src/gen/rayito/v1/efs_volumes_pb.js";
import type { S3MountsStatus } from "../../../src/gen/rayito/v1/s3_mounts_pb.js";
import type { SecretGatewayStatus } from "../../../src/gen/rayito/v1/secret_gateway_pb.js";
import { assertProxyHeaders, type HeaderMap, headerMap, requireAccessToken } from "./common.js";

export class FakeConfigureService {
  readonly tokenSha256: string;
  readonly configureRequests: ConfigureRequest[] = [];
  readonly configureHeaders: HeaderMap[] = [];
  readonly configureStatusHeaders: HeaderMap[] = [];
  /** Sustituye la respuesta de la próxima `Configure`; `undefined` (por
   * defecto) aplica cada sección presente (`SECTION_CODE_APPLIED`). */
  nextResults: readonly SectionResult[] | undefined;
  secretGatewayStatus: SecretGatewayStatus | undefined;
  /** Lo que cada `configureStatus` informa de `mounts`, en orden; el último
   * se repite. Vacío (por defecto): sin `s3Mounts` en la respuesta. */
  s3MountsStatuses: S3MountsStatus[] = [];
  /** Lo que `configureStatus` informa de `volumes`; `undefined` (por
   * defecto): sin `efsVolumes` en la respuesta. */
  efsVolumesStatus: EfsVolumesStatus | undefined;

  constructor(tokenSha256: string) {
    this.tokenSha256 = tokenSha256;
  }

  configure(request: ConfigureRequest, context: HandlerContext): ConfigureResponse {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    requireAccessToken(headers, this.tokenSha256);
    this.configureHeaders.push(headers);
    this.configureRequests.push(request);
    const results =
      this.nextResults ??
      (request.secretGateway === undefined
        ? []
        : [
            create(SectionResultSchema, {
              section: ConfigSection.SECRET_GATEWAY,
              code: SectionCode.APPLIED,
            }),
          ]);
    return create(ConfigureResponseSchema, { results: [...results], configGeneration: 1n });
  }

  configureStatus(_request: unknown, context: HandlerContext): ConfigureStatusResponse {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    requireAccessToken(headers, this.tokenSha256);
    this.configureStatusHeaders.push(headers);
    const s3Mounts =
      this.s3MountsStatuses.length > 1 ? this.s3MountsStatuses.shift() : this.s3MountsStatuses[0];
    return create(ConfigureStatusResponseSchema, {
      ...(this.secretGatewayStatus === undefined
        ? {}
        : { secretGateway: this.secretGatewayStatus }),
      ...(s3Mounts === undefined ? {} : { s3Mounts }),
      ...(this.efsVolumesStatus === undefined ? {} : { efsVolumes: this.efsVolumesStatus }),
    });
  }
}
