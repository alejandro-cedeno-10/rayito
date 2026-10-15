/**
 * El plano de control por defecto de los servicios del panel: el compartido
 * del proceso (`sharedControlPlane`, o uno propio si hay credenciales),
 * construido en el primer uso y no al construir el servicio.
 */

import {
  type AwsClientSettings,
  LambdaMicrovmsControlPlane,
  sharedControlPlane,
} from "../aws/control-plane.js";
import type { ManagementPlane } from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

export class LazyPlane {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  #plane: ManagementPlane | undefined;

  constructor(region: string | undefined, credentials: Credentials, plane?: ManagementPlane) {
    this.#region = region;
    this.#credentials = credentials;
    this.#plane = plane;
  }

  get(): ManagementPlane {
    this.#plane ??=
      this.#credentials === undefined
        ? sharedControlPlane(this.#region)
        : LambdaMicrovmsControlPlane.fromRegion(this.#region, { credentials: this.#credentials });
    return this.#plane;
  }
}
