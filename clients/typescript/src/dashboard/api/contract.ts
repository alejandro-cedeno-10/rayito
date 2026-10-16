/**
 * El contrato de `/api/v1`: versión, rutas, códigos de error cerrados y los
 * tipos de petición y respuesta del núcleo. El esquema JSON de las respuestas
 * vive en `testdata/dashboard/api-v1.schema.json`, y los tests de contrato lo
 * aplican a cada cáscara. Espejo de `rayito._dashboard._api._contract`.
 */

import {
  HTTP_BAD_GATEWAY,
  HTTP_BAD_REQUEST,
  HTTP_FORBIDDEN,
  HTTP_INTERNAL_ERROR,
  HTTP_METHOD_NOT_ALLOWED,
  HTTP_MISDIRECTED,
  HTTP_NOT_FOUND,
  HTTP_PAYLOAD_TOO_LARGE,
  HTTP_SERVICE_UNAVAILABLE,
  HTTP_TOO_MANY_REQUESTS,
  HTTP_UNAUTHORIZED,
  HTTP_UNSUPPORTED_MEDIA_TYPE,
} from "../../http-status.js";

export const API_VERSION = "v1" as const;
export const API_PREFIX = `/api/${API_VERSION}` as const;

/** `meta.deployment` del panel local (`rayito dashboard`): las opciones se dan por la CLI. */
export const DEPLOYMENT_LOCAL = "local" as const;
/** `meta.deployment` del panel en tu cuenta: las opciones salen de la pila y de Cognito. */
export const DEPLOYMENT_STACK = "stack" as const;

/** Los `error.code` posibles: una lista cerrada que el esquema enumera. */
export const ERROR_CODES = {
  invalid_argument: HTTP_BAD_REQUEST,
  invalid_json: HTTP_BAD_REQUEST,
  confirmation_required: HTTP_BAD_REQUEST,
  unauthorized: HTTP_UNAUTHORIZED,
  forbidden: HTTP_FORBIDDEN,
  csrf_failed: HTTP_FORBIDDEN,
  actions_disabled: HTTP_FORBIDDEN,
  cost_explorer_disabled: HTTP_FORBIDDEN,
  aws_access_denied: HTTP_FORBIDDEN,
  not_found: HTTP_NOT_FOUND,
  method_not_allowed: HTTP_METHOD_NOT_ALLOWED,
  payload_too_large: HTTP_PAYLOAD_TOO_LARGE,
  unsupported_media_type: HTTP_UNSUPPORTED_MEDIA_TYPE,
  misdirected_request: HTTP_MISDIRECTED,
  aws_throttled: HTTP_TOO_MANY_REQUESTS,
  jobs_busy: HTTP_TOO_MANY_REQUESTS,
  internal_error: HTTP_INTERNAL_ERROR,
  aws_error: HTTP_BAD_GATEWAY,
  auth_unavailable: HTTP_SERVICE_UNAVAILABLE,
  audit_unavailable: HTTP_SERVICE_UNAVAILABLE,
} as const;

export type ErrorCode = keyof typeof ERROR_CODES;

export const ERROR_MESSAGES: Readonly<Record<ErrorCode, string>> = {
  invalid_argument: "AWS rechazó un argumento de la petición",
  invalid_json: "el cuerpo no es un objeto JSON válido",
  confirmation_required: 'la acción exige "confirm": true',
  unauthorized: "falta la sesión: abre la URL que imprimió `rayito dashboard`",
  forbidden: "origen no admitido",
  csrf_failed: "falta o no coincide el token CSRF",
  actions_disabled: "las acciones están desactivadas: arranca con --allow-actions",
  cost_explorer_disabled:
    "el coste facturado está desactivado: arranca con --cost-explorer " +
    "(cada consulta a Cost Explorer se factura)",
  aws_access_denied: "AWS rechazó las credenciales o falta un permiso IAM",
  not_found: "no existe",
  method_not_allowed: "método no admitido",
  payload_too_large: "el cuerpo supera el tope",
  unsupported_media_type: "el cuerpo tiene que ser application/json",
  misdirected_request: "Host no admitido",
  aws_throttled: "AWS limitó la tasa: reintenta en unos segundos",
  internal_error: "error interno del panel",
  aws_error: "AWS devolvió un error",
  jobs_busy: "ya hay demasiadas acciones en cola: espera a que terminen",
  auth_unavailable: "no se pudo comprobar la identidad: reintenta en unos segundos",
  audit_unavailable: "no se pudo escribir el registro de auditoría: la acción no se ejecutó",
};

export const ACTION_NAMES = ["kill", "suspend", "resume"] as const;

/** Un documento JSON ya serializado desde el dominio. */
export type JsonObject = { readonly [key: string]: unknown };

/**
 * Una petición ya desligada del transporte: `path` sin la cadena de consulta
 * y `query` ya partida.
 */
export interface ApiRequest {
  readonly method: string;
  readonly path: string;
  readonly query: Readonly<Record<string, readonly string[]>>;
  readonly body: Uint8Array;
}

export interface ApiResponse {
  readonly status: number;
  readonly body: JsonObject;
}

/**
 * Lo que una cáscara (el servidor local) necesita de la API: atender una
 * petición, saber si exige CSRF y cerrarse. `DashboardApi` y `AccountsApi`
 * lo cumplen.
 */
export interface ApiBackend {
  handle(request: ApiRequest): Promise<ApiResponse>;
  requiresCsrf(request: ApiRequest): boolean;
  close(): Promise<void>;
}

export interface LogLine {
  readonly timestamp: string;
  readonly message: string;
  readonly stream: string;
}

/**
 * `fallback` avisa de que el stream esperado no existía y las líneas vienen
 * de streams encontrados por nombre, que otro sandbox con el mismo grupo de
 * logs pudo haber fabricado.
 */
export interface LogPage {
  readonly lines: readonly LogLine[];
  readonly truncated: boolean;
  readonly fallback: boolean;
}

/**
 * Los últimos `limit` eventos del stream de CloudWatch de un sandbox;
 * `truncated` avisa de que hay más. Un sandbox sin logs (sin
 * `executionRoleArn`) devuelve una página vacía.
 */
export interface LogsSource {
  read(sandboxId: string, options: { readonly limit: number }): Promise<LogPage>;
}

export function isoUtc(moment: Date): string {
  return moment.toISOString();
}
