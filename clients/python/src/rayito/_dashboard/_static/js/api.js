// Cliente de /api/v1. La sesión viaja en una cookie HttpOnly; las
// peticiones POST y la consulta de coste facturado (que cuesta dinero) llevan
// además el token CSRF que entrega /api/v1/session.

const API = "/api/v1";
const CSRF_HEADER = "X-Rayito-CSRF";

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

export async function get(path, params, { csrf = false } = {}) {
  const headers = { Accept: "application/json" };
  if (csrf) headers[CSRF_HEADER] = await token();
  const response = await fetch(`${API}${path}${query(params)}`, { credentials: "same-origin", headers });
  return parse(response);
}

async function token() {
  if (csrfToken === null) csrfToken = (await get("/session")).csrfToken;
  return csrfToken;
}

export async function post(path, body) {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json", [CSRF_HEADER]: await token() },
    body: JSON.stringify(body ?? {}),
  });
  return parse(response);
}

export function describe(error) {
  if (error instanceof ApiFailure) {
    return error.awsCode ? `${error.message} (${error.awsCode})` : error.message;
  }
  return "No se pudo contactar con el panel local.";
}
