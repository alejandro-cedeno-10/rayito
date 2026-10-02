/**
 * `ConfigureService` como lo implementa `rayd`: una respuesta guionada por
 * `nextResults` (por defecto, cada sección presente se aplica) y el estado
 * que `configureStatus` devuelve (`secretGatewayStatus`). Exige
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
    return create(ConfigureStatusResponseSchema, {
      ...(this.secretGatewayStatus === undefined
        ? {}
        : { secretGateway: this.secretGatewayStatus }),
    });
  }
}
