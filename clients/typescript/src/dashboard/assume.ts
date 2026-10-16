/**
 * Credenciales que asumen un rol de otra cuenta y se renuevan solas
 * (ADR-026, fase 3). Construirlas no llama a STS: el primer `AssumeRole`
 * ocurre cuando un cliente firma su primera petición, y se repite antes de
 * que caduquen las credenciales. El panel local las usa para la vista
 * multicuenta (`rayito dashboard --account`). Espejo de
 * `rayito._dashboard._assume`.
 *
 * Un rechazo de STS sube como `AuthenticationError` con el código de AWS y
 * sin su mensaje (que nombra el rol y la cuenta): la API del panel lo
 * muestra como `aws_access_denied`.
 */

import { AssumeRoleCommand, type AssumeRoleCommandOutput } from "@aws-sdk/client-sts";
import { awsCode } from "../aws/optional-client.js";
import { AuthenticationError } from "../errors.js";
import {
  DASHBOARD_ASSUME_ROLE_SESSION_NAME,
  DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
} from "../limits.js";

/**
 * Cuánto antes de caducar se piden credenciales nuevas: los 10 minutos del
 * refresco obligatorio de botocore, que es lo que usa la versión de Python.
 */
export const CREDENTIALS_REFRESH_MARGIN_MS = 600_000;

/** Las credenciales temporales en la forma que pide el SDK de AWS v3. */
export interface AssumedCredentials {
  readonly accessKeyId: string;
  readonly secretAccessKey: string;
  readonly sessionToken: string;
  readonly expiration: Date;
}

/** Lo único que el panel necesita de un cliente de STS. */
export interface StsSender {
  send(command: AssumeRoleCommand): Promise<AssumeRoleCommandOutput>;
}

/** El `AssumeRole` de un rol, con el `ExternalId` si se pidió. */
export class AssumedRole {
  readonly #sts: StsSender;
  readonly #input: {
    readonly RoleArn: string;
    readonly RoleSessionName: string;
    readonly DurationSeconds: number;
    readonly ExternalId?: string;
  };

  constructor(
    sts: StsSender,
    options: {
      readonly roleArn: string;
      readonly externalId?: string | undefined;
      readonly sessionName?: string;
      readonly durationSeconds?: number;
    },
  ) {
    this.#sts = sts;
    this.#input = {
      RoleArn: options.roleArn,
      RoleSessionName: options.sessionName ?? DASHBOARD_ASSUME_ROLE_SESSION_NAME,
      DurationSeconds: options.durationSeconds ?? DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
      ...(options.externalId ? { ExternalId: options.externalId } : {}),
    };
  }

  async fetch(): Promise<AssumedCredentials> {
    let output: AssumeRoleCommandOutput;
    try {
      output = await this.#sts.send(new AssumeRoleCommand(this.#input));
    } catch (error) {
      throw new AuthenticationError("AWS no dejó asumir el rol de la cuenta", {
        awsCode: awsCode(error),
      });
    }
    const credentials = output.Credentials;
    if (!(credentials?.Expiration instanceof Date)) {
      throw new AuthenticationError(
        "AWS devolvió credenciales del rol sin fecha de caducidad válida",
      );
    }
    return {
      accessKeyId: credentials.AccessKeyId as string,
      secretAccessKey: credentials.SecretAccessKey as string,
      sessionToken: credentials.SessionToken as string,
      expiration: credentials.Expiration,
    };
  }
}

/**
 * Un proveedor de credenciales para los clientes del SDK: reutiliza las del
 * rol hasta `CREDENTIALS_REFRESH_MARGIN_MS` antes de caducar y comparte una
 * sola petición entre llamadas simultáneas.
 */
export function assumedCredentials(
  role: AssumedRole,
  now: () => number = Date.now,
): () => Promise<AssumedCredentials> {
  let current: AssumedCredentials | undefined;
  let pending: Promise<AssumedCredentials> | undefined;
  const stillValid = (): boolean =>
    current !== undefined && current.expiration.getTime() - now() > CREDENTIALS_REFRESH_MARGIN_MS;
  return async () => {
    if (current !== undefined && stillValid()) {
      return current;
    }
    pending ??= role
      .fetch()
      .then((fetched) => {
        current = fetched;
        return fetched;
      })
      .finally(() => {
        pending = undefined;
      });
    return pending;
  };
}
