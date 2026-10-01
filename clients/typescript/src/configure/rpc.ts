/**
 * `ConfigureService` RPC adapter (M15 foundations). Mirrors
 * `rayito.sandbox_sync.configure`: builds the call over the generated
 * client and translates gRPC errors with the same table as the rest of the
 * SDK (`translateRpcError`). The access token is already added by the
 * channel's auth interceptor, like any other authenticated RPC.
 */

import { create } from "@bufbuild/protobuf";
import type { CallOptions, Client } from "@connectrpc/connect";
import type {
  ConfigureRequest,
  ConfigureResponse,
  ConfigureService,
  ConfigureStatusResponse,
} from "../gen/rayito/v1/configure_pb.js";
import { ConfigureStatusRequestSchema } from "../gen/rayito/v1/configure_pb.js";
import { translateRpcError } from "../transport/errors.js";

export async function callConfigure(
  client: Client<typeof ConfigureService>,
  request: ConfigureRequest,
  options: CallOptions,
): Promise<ConfigureResponse> {
  try {
    return await client.configure(request, options);
  } catch (error) {
    throw translateRpcError(error);
  }
}

export async function callConfigureStatus(
  client: Client<typeof ConfigureService>,
  options: CallOptions,
): Promise<ConfigureStatusResponse> {
  try {
    return await client.configureStatus(create(ConfigureStatusRequestSchema, {}), options);
  } catch (error) {
    throw translateRpcError(error);
  }
}
