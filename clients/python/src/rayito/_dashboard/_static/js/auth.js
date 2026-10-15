// Identidad de la SPA. En el servidor local la sesión es una cookie HttpOnly y
// no hay nada que hacer aquí. Servida por la pila `dashboard`, la función
// publica `/config.json` (el dominio del Hosted UI de Cognito y el id del
// cliente: valores públicos) y la SPA inicia sesión con el flujo de código con
// PKCE (RFC 7636): sin secreto de cliente, sin implícito. El token de acceso y
// el de refresco viven sólo en memoria de la pestaña; nunca se escriben en
// `localStorage` ni en la URL, y el verificador y el `state` sólo duran el
// viaje de ida y vuelta al Hosted UI (`sessionStorage`).
//
// Con `AuthMode=aws-iam` la configuración trae además un bloque `iam` (región,
// proveedor, identity pool y URL de la API): la SPA canjea el token de
// identidad por credenciales de AWS (`identity.js`) y firma con SigV4 las
// llamadas a la API (`api.js`). El inicio de sesión es el mismo.

const CONFIG_URL = "/config.json";
const AUTHORIZE_PATH = "/oauth2/authorize";
const TOKEN_PATH = "/oauth2/token";
const LOGOUT_PATH = "/logout";
const PKCE_METHOD = "S256";
const VERIFIER_BYTES = 32;
const STATE_BYTES = 16;
const MS_PER_SECOND = 1000;
/** Se renueva el token un poco antes de que caduque. */
const EXPIRY_MARGIN_MS = 30 * MS_PER_SECOND;
const FLOW_KEY = "rayito-dashboard-pkce";
/** Marca de «el servidor rechazó el token y ya se intentó renovar»: sobrevive a la ida y vuelta al Hosted UI. */
const REJECTED_KEY = "rayito-dashboard-rejected";
const HTTP_NOT_FOUND = 404;

let config = null;
let tokens = null;
let iam = null;

function base64Url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function randomString(size) {
  return base64Url(crypto.getRandomValues(new Uint8Array(size)));
}

async function challengeOf(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function redirectUri() {
  return `${location.origin}/`;
}

/**
 * Descubre el modo: `true` si la pila pide iniciar sesión, `false` en local.
 * Sólo un 404 significa «servidor local»; cualquier otro fallo de la función
 * (por ejemplo un 503 mientras Cognito no responde) es un error que la SPA
 * muestra con «Reintentar», no un modo sin identidad.
 */
export async function detectMode() {
  let response;
  try {
    response = await fetch(CONFIG_URL, { headers: { Accept: "application/json" } });
  } catch {
    return false;
  }
  if (response.status === HTTP_NOT_FOUND) return false;
  if (!response.ok) throw new Error("No se pudo leer la configuración de inicio de sesión.");
  const document = await response.json();
  config = document.auth ?? null;
  iam = config?.iam ?? null;
  return config !== null;
}

/** El bloque `iam` de la configuración, o `null` si la pila usa la variante por defecto. */
export function iamSettings() {
  return iam;
}

export function isSignedInMode() {
  return config !== null;
}

function remember(response) {
  const expiresInMs = response.expires_in * MS_PER_SECOND;
  tokens = {
    access: response.access_token,
    id: response.id_token ?? tokens?.id ?? null,
    refresh: response.refresh_token ?? tokens?.refresh ?? null,
    expiresAt: Date.now() + expiresInMs,
  };
}

async function requestTokens(fields) {
  const response = await fetch(`${config.domain}${TOKEN_PATH}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: config.clientId, ...fields }),
  });
  if (!response.ok) return false;
  remember(await response.json());
  return true;
}

async function signIn() {
  const verifier = randomString(VERIFIER_BYTES);
  const state = randomString(STATE_BYTES);
  try {
    sessionStorage.setItem(FLOW_KEY, JSON.stringify({ verifier, state, hash: location.hash }));
  } catch {
    // sin sessionStorage el canje fallará y se volverá a empezar
  }
  const query = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: redirectUri(),
    scope: config.scope,
    code_challenge_method: PKCE_METHOD,
    code_challenge: await challengeOf(verifier),
    state,
  });
  location.assign(`${config.domain}${AUTHORIZE_PATH}?${query}`);
  return new Promise(() => {});
}

function takeFlow() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(FLOW_KEY) ?? "null");
    sessionStorage.removeItem(FLOW_KEY);
    return stored;
  } catch {
    return null;
  }
}

async function redeemCode(code, state) {
  const flow = takeFlow();
  history.replaceState(null, "", `${location.pathname}${flow?.hash ?? ""}`);
  if (flow === null || flow.state !== state) return false;
  return requestTokens({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(),
    code_verifier: flow.verifier,
  });
}

async function refresh() {
  if (tokens?.refresh == null) return false;
  const refreshToken = tokens.refresh;
  const renewed = await requestTokens({ grant_type: "refresh_token", refresh_token: refreshToken });
  if (!renewed) tokens = null;
  return renewed;
}

/** El token de acceso vigente, o lleva al Hosted UI si no hay (no vuelve). */
export async function accessToken() {
  if (tokens !== null && Date.now() < tokens.expiresAt - EXPIRY_MARGIN_MS) return tokens.access;
  if (await refresh()) return tokens.access;
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (code !== null && (await redeemCode(code, params.get("state")))) return tokens.access;
  return signIn();
}

/** El token de identidad vigente (el que el identity pool canjea), renovándolo si hace falta. */
export async function identityToken() {
  await accessToken();
  return tokens.id;
}

function setRejected(value) {
  try {
    if (value) sessionStorage.setItem(REJECTED_KEY, "1");
    else sessionStorage.removeItem(REJECTED_KEY);
  } catch {
    // sin sessionStorage no hay protección contra el bucle, sólo se pierde esa marca
  }
}

function wasRejected() {
  try {
    return sessionStorage.getItem(REJECTED_KEY) !== null;
  } catch {
    return false;
  }
}

/**
 * Tras un 401: caduca el token de acceso (conserva el de refresco, que lo
 * renueva sin salir de la página) y pide otro; sin refresco lleva al Hosted
 * UI. Devuelve `false`, sin redirigir otra vez, si ya se intentó y el servidor
 * lo sigue rechazando (una configuración rota no debe dar vueltas al Hosted UI).
 */
export async function renewAfterRejection() {
  if (wasRejected()) {
    setRejected(false);
    return false;
  }
  setRejected(true);
  if (tokens !== null) tokens = { ...tokens, expiresAt: 0 };
  await accessToken();
  return true;
}

/** El servidor aceptó el token: la próxima vez que lo rechace se vuelve a intentar renovarlo. */
export function confirmSession() {
  setRejected(false);
}

export function signOut() {
  tokens = null;
  const query = new URLSearchParams({ client_id: config.clientId, logout_uri: redirectUri() });
  location.assign(`${config.domain}${LOGOUT_PATH}?${query}`);
}
