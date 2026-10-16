/**
 * La vista multicuenta del panel local (ADR-026, fase 3), opt-in: cada
 * `--account NOMBRE=<arn de rol>` añade una cuenta que se ve con un rol de
 * sólo lectura (`infra/dashboard-read-role.yaml`). La cuenta del panel sigue
 * siendo la principal; las demás son siempre de sólo lectura, sin coste
 * facturado ni logs.
 *
 * `AccountsApi` envuelve las `DashboardApi` y reparte cada petición por
 * `?account=NOMBRE` (sin él, la principal). La SPA sólo ve etiquetas: el ARN
 * del rol y el id de la cuenta no salen por la API. Espejo de
 * `rayito._dashboard._accounts`.
 */

import { InvalidArgumentError } from "../errors.js";
import { HTTP_OK } from "../http-status.js";
import { DASHBOARD_ACCOUNT_LABEL_MAX_CHARS, DASHBOARD_ACCOUNTS_MAX } from "../limits.js";
import { API_PREFIX, type ApiBackend, type ApiRequest, type ApiResponse } from "./api/contract.js";
import { ApiError, errorResponse, isActionRoute } from "./api/router.js";

export const ACCOUNT_PARAM = "account";
const ACCOUNTS_PATH = `${API_PREFIX}/accounts`;
const GET = "GET";
const LABEL_PATTERN = new RegExp(
  `^[A-Za-z0-9][A-Za-z0-9 _.-]{0,${DASHBOARD_ACCOUNT_LABEL_MAX_CHARS - 1}}$`,
);
const ROLE_ARN_PATTERN = /^arn:aws[a-z-]*:iam::[0-9]{12}:role\/[\w+=,.@/-]{1,512}$/;
const SPEC_SEPARATOR = "=";
export const READ_ONLY_ACCOUNT_MESSAGE =
  "las cuentas adicionales son de sólo lectura: las acciones se piden en la cuenta principal";

/**
 * Una cuenta adicional: la etiqueta que ve el usuario y el rol que se asume.
 * El rol lleva el id de la cuenta, así que no se enumera: ni `console.log` ni
 * `JSON.stringify` lo muestran.
 */
export class AccountSpec {
  readonly label: string;
  readonly #roleArn: string;

  constructor(label: string, roleArn: string) {
    this.label = label;
    this.#roleArn = roleArn;
  }

  get roleArn(): string {
    return this.#roleArn;
  }
}

/**
 * `NOMBRE=ARN` (repetible) a especificaciones validadas. Los mensajes
 * nombran la opción, nunca el valor recibido.
 */
export function parseAccountSpecs(values: readonly string[]): AccountSpec[] {
  if (values.length > DASHBOARD_ACCOUNTS_MAX) {
    throw new InvalidArgumentError(
      `--account: como mucho ${DASHBOARD_ACCOUNTS_MAX} cuentas adicionales`,
    );
  }
  const specs = values.map(parseAccountSpec);
  const labels = specs.map((spec) => spec.label.toLowerCase());
  if (new Set(labels).size !== labels.length) {
    throw new InvalidArgumentError("--account: los nombres de cuenta no se pueden repetir");
  }
  return specs;
}

function parseAccountSpec(value: string): AccountSpec {
  const separator = value.indexOf(SPEC_SEPARATOR);
  const label = separator < 0 ? value : value.slice(0, separator);
  if (separator < 0 || !LABEL_PATTERN.test(label)) {
    throw new InvalidArgumentError(
      "--account: formato NOMBRE=ARN_DEL_ROL (el nombre, de hasta " +
        `${DASHBOARD_ACCOUNT_LABEL_MAX_CHARS} letras, números, espacios, «_», «.» o «-»)`,
    );
  }
  const roleArn = value.slice(separator + SPEC_SEPARATOR.length);
  if (!ROLE_ARN_PATTERN.test(roleArn)) {
    throw new InvalidArgumentError("--account: el valor tras «=» debe ser el ARN de un rol");
  }
  return new AccountSpec(label, roleArn);
}

/**
 * El contrato `/api/v1` sobre varias cuentas: la principal y las adicionales,
 * todas de sólo lectura salvo, si lo tiene, la principal.
 */
export class AccountsApi implements ApiBackend {
  readonly #primary: ApiBackend;
  readonly #others: ReadonlyMap<string, ApiBackend>;

  constructor(primary: ApiBackend, others: ReadonlyMap<string, ApiBackend>) {
    this.#primary = primary;
    this.#others = others;
  }

  requiresCsrf(request: ApiRequest): boolean {
    return this.#primary.requiresCsrf(request);
  }

  async close(): Promise<void> {
    await this.#primary.close();
    await Promise.all([...this.#others.values()].map((api) => api.close()));
  }

  async handle(request: ApiRequest): Promise<ApiResponse> {
    if (request.method === GET && request.path === ACCOUNTS_PATH) {
      return {
        status: HTTP_OK,
        body: { accounts: [...this.#others.keys()].map((name) => ({ name })) },
      };
    }
    const names = request.query[ACCOUNT_PARAM] ?? [];
    if (names.length === 0) {
      return this.#primary.handle(request);
    }
    const api = this.#others.get(names[0] as string);
    if (api === undefined) {
      return errorResponse(new ApiError("invalid_argument", "account: cuenta desconocida"));
    }
    if (isActionRoute(request.method, request.path)) {
      return errorResponse(new ApiError("actions_disabled", READ_ONLY_ACCOUNT_MESSAGE));
    }
    const remaining = Object.fromEntries(
      Object.entries(request.query).filter(([name]) => name !== ACCOUNT_PARAM),
    );
    return api.handle({ ...request, query: remaining });
  }
}
