/**
 * `NetworkService` como lo implementa `rayd`: exige `x-access-token`,
 * sustituye la política entera (lo omitido se borra), responde
 * `FailedPrecondition` a una política que restringe en una imagen sin
 * `CAP_NET_ADMIN` (`capable = false`) y publica la `enforcement` resultante
 * también en `Health`. `failNext` lanza, por orden, los `ConnectError`
 * encolados; `answerEnforcement` fuerza la `enforcement` de la respuesta.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import {
  EgressEnforcement,
  type NetworkPolicy,
  NetworkPolicySchema,
  type NetworkState,
  NetworkStateSchema,
  type UpdateNetworkRequest,
} from "../../../src/gen/rayito/v1/network_pb.js";
import { isNetworkEntry } from "../../../src/sandbox/network.js";
import { assertProxyHeaders, type HeaderMap, headerMap, requireAccessToken } from "./common.js";
import type { FakeHealth } from "./health.js";

export const LOCAL_PROXY_PORT = 41_234;
export const NO_NET_ADMIN_MESSAGE =
  "la imagen no tiene CAP_NET_ADMIN: la política de egress exige rayito-base-caps";

function restricts(policy: NetworkPolicy): boolean {
  return policy.denyOut.length > 0 || policy.egressProxy !== undefined;
}

function modeEnforcement(policy: NetworkPolicy): EgressEnforcement {
  if (!restricts(policy)) {
    return EgressEnforcement.NONE;
  }
  const hostnames = policy.allowOut.some((entry) => !isNetworkEntry(entry));
  return policy.egressProxy !== undefined || hostnames
    ? EgressEnforcement.GUEST_ROUTES_AND_PROXY
    : EgressEnforcement.GUEST_ROUTES;
}

export class FakeNetworkService {
  readonly tokenSha256: string;
  readonly health: FakeHealth;
  readonly updateRequests: UpdateNetworkRequest[] = [];
  readonly updateHeaders: HeaderMap[] = [];
  readonly getHeaders: HeaderMap[] = [];
  readonly failNext: ConnectError[] = [];
  capable = true;
  answerEnforcement: EgressEnforcement | undefined;
  #allowOut: string[] = [];
  #denyOut: string[] = [];
  #proxyConfigured = false;
  #enforcement: EgressEnforcement = EgressEnforcement.NONE;
  #localProxyPort = 0;

  constructor(tokenSha256: string, health: FakeHealth) {
    this.tokenSha256 = tokenSha256;
    this.health = health;
  }

  updateNetwork(request: UpdateNetworkRequest, context: HandlerContext): NetworkState {
    this.updateHeaders.push(this.#authorized(context));
    this.#throwScripted();
    const policy = request.policy ?? create(NetworkPolicySchema);
    if (restricts(policy) && !this.capable) {
      throw new ConnectError(NO_NET_ADMIN_MESSAGE, Code.FailedPrecondition);
    }
    this.updateRequests.push(request);
    this.#allowOut = [...policy.allowOut];
    this.#denyOut = [...policy.denyOut];
    this.#proxyConfigured = policy.egressProxy !== undefined;
    this.#enforcement = this.answerEnforcement ?? modeEnforcement(policy);
    if (restricts(policy)) {
      this.#localProxyPort = LOCAL_PROXY_PORT;
    }
    this.health.egressEnforcement = this.#enforcement;
    return this.#state();
  }

  getNetwork(_request: unknown, context: HandlerContext): NetworkState {
    this.getHeaders.push(this.#authorized(context));
    this.#throwScripted();
    return this.#state();
  }

  #authorized(context: HandlerContext): HeaderMap {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    requireAccessToken(headers, this.tokenSha256);
    return headers;
  }

  #throwScripted(): void {
    const scripted = this.failNext.shift();
    if (scripted !== undefined) {
      throw scripted;
    }
  }

  #state(): NetworkState {
    return create(NetworkStateSchema, {
      allowOut: this.#allowOut,
      denyOut: this.#denyOut,
      egressProxyConfigured: this.#proxyConfigured,
      enforcement: this.#enforcement,
      localProxyPort: this.#localProxyPort,
    });
  }
}
