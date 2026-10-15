// Cliente de /api/v1. En el servidor local la sesión viaja en una cookie
// HttpOnly y las peticiones POST y la consulta de coste facturado (que cuesta
// dinero) llevan además el token CSRF que entrega /api/v1/session. Servida por
// la pila `dashboard`, cada petición lleva el token de acceso de Cognito en
// `Authorization` (`auth.js`) y no hay cookie que proteger con CSRF. Con
// `AuthMode=aws-iam` la API vive en otra Function URL que exige firma SigV4:
// `Authorization` lo ocupa la firma, así que el token de Cognito va en
// `X-Rayito-Token`, y las credenciales salen del identity pool (`identity.js`).

import {
  accessToken,
  confirmSession,
  iamSettings,
  identityToken,
  isSignedInMode,
  renewAfterRejection,
} from "./auth.js";
import { dropCredentials, signedHeaders } from "./identity.js";

const API = "/api/v1";
const CSRF_HEADER = "X-Rayito-CSRF";
const TOKEN_HEADER = "X-Rayito-Token";
const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;

export class ApiFailure extends Error {
  constructor(status, code, message, awsCode) {
    super(message);
    this.status = status;
    this.code = code;
    this.awsCode = awsCode ?? null;
  }
}

let csrfToken = null;
// Vista multicuenta (opt-in): la cuenta adicional que se está mirando; vacío
// es la cuenta del panel. Sólo vive mientras la pestaña está abierta.
let currentAccount = "";

export function setAccount(name) {
  currentAccount = name ?? "";
}

async function parse(response) {
  let document = null;
  try {
    document = await response.json();
  } catch {
    document = null;
  }
  if (response.ok) return document;
  const error = document?.error ?? {};
  throw new ApiFailure(response.status, error.code ?? "unknown", error.message ?? "Error de red", error.awsCode);
}

function query(params) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") search.set(name, String(value));
  }
  const text = search.toString().replaceAll("+", "%20");
  return text ? `?${text}` : "";
}

/** La URL de la petición: relativa al origen, o la de la función de la API con `aws-iam`. */
function target(path) {
  const settings = iamSettings();
  return settings === null ? path : `${settings.apiUrl}${path}`;
}

/** Las cabeceras de identidad de la petición, según el modo. */
async function identity(request, { csrf }) {
  const settings = iamSettings();
  if (settings !== null) {
    const signed = await signedHeaders(settings, await identityToken(), request);
    return { [TOKEN_HEADER]: await accessToken(), ...signed };
  }
  if (isSignedInMode()) return { Authorization: `Bearer ${await accessToken()}` };
  return csrf ? { [CSRF_HEADER]: await token() } : {};
}

function settle(response) {
  if (response.ok && isSignedInMode()) confirmSession();
  return parse(response);
}

/**
 * Un 403 de la capa de IAM de la Function URL (credenciales caducadas o
 * rechazadas) no llega a la función, así que repetirlo es seguro. Se distingue
 * de un 403 de la propia API porque éste lleva el documento `{ error }`.
 */
async function rejectedBySignature(response) {
  if (response.status !== HTTP_FORBIDDEN || iamSettings() === null) return false;
  try {
    return (await response.clone().json()).error === undefined;
  } catch {
    return true;
  }
}

async function attempt(url, init, { csrf }) {
  const request = { method: init.method ?? "GET", url, body: init.body };
  const headers = { Accept: "application/json", ...init.headers, ...(await identity(request, { csrf })) };
  return fetch(url, { ...init, credentials: iamSettings() === null ? "same-origin" : "omit", headers });
}

async function send(path, init, { csrf }) {
  const url = target(path);
  const response = await attempt(url, init, { csrf });
  if (await rejectedBySignature(response)) {
    dropCredentials();
    return settle(await attempt(url, init, { csrf }));
  }
  if (response.status !== HTTP_UNAUTHORIZED || !isSignedInMode()) return settle(response);
  if (!(await renewAfterRejection())) return parse(response);
  return settle(await attempt(url, init, { csrf }));
}

/** `scoped: false` salta el parámetro de cuenta: la sesión y la lista de cuentas son del panel. */
export function get(path, params, { csrf = false, scoped = true } = {}) {
  const scope = scoped ? { account: currentAccount } : {};
  return send(`${API}${path}${query({ ...params, ...scope })}`, {}, { csrf });
}

async function token() {
  if (csrfToken === null) csrfToken = (await get("/session", undefined, { scoped: false })).csrfToken;
  return csrfToken;
}

export function post(path, body) {
  return send(
    `${API}${path}${query({ account: currentAccount })}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) },
    { csrf: true },
  );
}

export function describe(error) {
  if (error instanceof ApiFailure) {
    return error.awsCode ? `${error.message} (${error.awsCode})` : error.message;
  }
  return "No se pudo contactar con el panel.";
}
