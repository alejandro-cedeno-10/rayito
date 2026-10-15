// Cliente de /api/v1. En el servidor local la sesión viaja en una cookie
// HttpOnly y las peticiones POST y la consulta de coste facturado (que cuesta
// dinero) llevan además el token CSRF que entrega /api/v1/session. Servida por
// la pila `dashboard`, cada petición lleva el token de acceso de Cognito en
// `Authorization` (`auth.js`) y no hay cookie que proteger con CSRF.

import { accessToken, confirmSession, isSignedInMode, renewAfterRejection } from "./auth.js";

const API = "/api/v1";
const CSRF_HEADER = "X-Rayito-CSRF";
const HTTP_UNAUTHORIZED = 401;

export class ApiFailure extends Error {
  constructor(status, code, message, awsCode) {
    super(message);
    this.status = status;
    this.code = code;
    this.awsCode = awsCode ?? null;
  }
}

let csrfToken = null;

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
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** Las cabeceras de identidad de la petición, según el modo. */
async function identity({ csrf }) {
  if (isSignedInMode()) return { Authorization: `Bearer ${await accessToken()}` };
  return csrf ? { [CSRF_HEADER]: await token() } : {};
}

function settle(response) {
  if (response.ok && isSignedInMode()) confirmSession();
  return parse(response);
}

async function send(url, init, { csrf }) {
  const headers = { Accept: "application/json", ...init.headers, ...(await identity({ csrf })) };
  const response = await fetch(url, { ...init, credentials: "same-origin", headers });
  if (response.status !== HTTP_UNAUTHORIZED || !isSignedInMode()) return settle(response);
  if (!(await renewAfterRejection())) return parse(response);
  const retried = { ...init, credentials: "same-origin", headers: { ...headers, ...(await identity({ csrf })) } };
  return settle(await fetch(url, retried));
}

export function get(path, params, { csrf = false } = {}) {
  return send(`${API}${path}${query(params)}`, {}, { csrf });
}

async function token() {
  if (csrfToken === null) csrfToken = (await get("/session")).csrfToken;
  return csrfToken;
}

export function post(path, body) {
  return send(
    `${API}${path}`,
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
