// Credenciales temporales para la variante `AuthMode=aws-iam`: el token de
// identidad de Cognito (el del Hosted UI) se canjea en el identity pool por
// credenciales de AWS con `GetId` y `GetCredentialsForIdentity`. Ambas son
// llamadas públicas del servicio de identidades (no se firman). Las credenciales
// viven sólo en memoria de la pestaña y se renuevan un poco antes de caducar.

import { signRequest } from "./sigv4.js";

const IDENTITY_CONTENT_TYPE = "application/x-amz-json-1.1";
const TARGET_PREFIX = "AWSCognitoIdentityService";
/** El servicio al que firma una Function URL. */
const SIGNING_SERVICE = "lambda";
const MS_PER_SECOND = 1000;
/** Se renuevan las credenciales un minuto antes de que caduquen. */
const EXPIRY_MARGIN_MS = 60 * MS_PER_SECOND;

let cache = null;

async function identityCall(settings, target, body) {
  const response = await fetch(`https://cognito-identity.${settings.region}.amazonaws.com/`, {
    method: "POST",
    credentials: "omit",
    headers: { "Content-Type": IDENTITY_CONTENT_TYPE, "X-Amz-Target": `${TARGET_PREFIX}.${target}` },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("No se pudieron obtener las credenciales de acceso.");
  return response.json();
}

async function fetchCredentials(settings, idToken) {
  const logins = { [settings.providerName]: idToken };
  const { IdentityId: identityId } = await identityCall(settings, "GetId", {
    IdentityPoolId: settings.identityPoolId,
    Logins: logins,
  });
  const { Credentials: raw } = await identityCall(settings, "GetCredentialsForIdentity", {
    IdentityId: identityId,
    Logins: logins,
  });
  return {
    idToken,
    credentials: { accessKeyId: raw.AccessKeyId, secretKey: raw.SecretKey, sessionToken: raw.SessionToken },
    expiresAt: raw.Expiration * MS_PER_SECOND,
  };
}

/** Credenciales vigentes para ese token de identidad (las reutiliza mientras valgan). */
export async function credentialsFor(settings, idToken) {
  const fresh = cache !== null && cache.idToken === idToken && Date.now() < cache.expiresAt - EXPIRY_MARGIN_MS;
  if (!fresh) cache = await fetchCredentials(settings, idToken);
  return cache.credentials;
}

/** Olvida las credenciales: la siguiente petición pide otras. */
export function dropCredentials() {
  cache = null;
}

/** Las cabeceras SigV4 de una petición a la API, con credenciales de `idToken`. */
export async function signedHeaders(settings, idToken, request) {
  const credentials = await credentialsFor(settings, idToken);
  return signRequest(request, credentials, { region: settings.region, service: SIGNING_SERVICE });
}
