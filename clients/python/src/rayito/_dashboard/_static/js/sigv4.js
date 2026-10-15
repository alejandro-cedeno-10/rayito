// Firma SigV4 (AWS Signature Version 4) para el navegador, con la Web Crypto
// API y sin dependencias: la variante `AuthMode=aws-iam` de la pila `dashboard`
// firma cada llamada a la Function URL de la API con las credenciales
// temporales de Cognito. Sólo firma lo que la SPA envía: un método, una URL y,
// como mucho, un cuerpo de texto. Las cabeceras firmadas son las cuatro
// obligatorias (`host`, `x-amz-content-sha256`, `x-amz-date`,
// `x-amz-security-token`); el resto de cabeceras viaja sin firmar.

const ALGORITHM = "AWS4-HMAC-SHA256";
const TERMINATOR = "aws4_request";
const SIGNED_HEADERS = "host;x-amz-content-sha256;x-amz-date;x-amz-security-token";
const HEX_RADIX = 16;
const HEX_WIDTH = 2;
/** Los caracteres que `encodeURIComponent` deja pasar y SigV4 exige codificar. */
const EXTRA_RESERVED = /[!'()*]/g;

const encoder = new TextEncoder();

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((byte) => byte.toString(HEX_RADIX).padStart(HEX_WIDTH, "0")).join("");
}

async function sha256Hex(text) {
  return hex(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
}

async function hmac(key, text) {
  const imported = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", imported, encoder.encode(text));
}

function encodeComponent(text) {
  return encodeURIComponent(text).replace(EXTRA_RESERVED, (char) => `%${char.charCodeAt(0).toString(HEX_RADIX).toUpperCase()}`);
}

function decodeComponent(text) {
  return decodeURIComponent(text.replaceAll("+", " "));
}

/** La consulta canónica: nombres y valores decodificados, recodificados, ordenados por nombre y valor. */
export function canonicalQuery(search) {
  const pairs = search
    .replace(/^\?/, "")
    .split("&")
    .filter((part) => part !== "")
    .map((part) => {
      const [name, ...rest] = part.split("=");
      return [encodeComponent(decodeComponent(name)), encodeComponent(decodeComponent(rest.join("=")))];
    });
  pairs.sort((a, b) => (a[0] === b[0] ? (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) : a[0] < b[0] ? -1 : 1));
  return pairs.map(([name, value]) => `${name}=${value}`).join("&");
}

function amzDate(now) {
  return now.toISOString().replace(/[:-]|\.\d{3}/g, "");
}

async function signingKey(secretKey, date, region, service) {
  let key = await hmac(encoder.encode(`AWS4${secretKey}`), date);
  for (const part of [region, service, TERMINATOR]) key = await hmac(key, part);
  return key;
}

/**
 * Las cabeceras que firman la petición: `Authorization`, `X-Amz-Date`,
 * `X-Amz-Security-Token` y `X-Amz-Content-Sha256`. El navegador pone solo
 * `Host`, que es lo que se firma. Las rutas de la API sólo llevan
 * `[A-Za-z0-9/_-]`, así que la ruta canónica es la propia.
 *
 * @param {{method: string, url: string, body?: string}} request
 * @param {{accessKeyId: string, secretKey: string, sessionToken: string}} credentials
 */
export async function signRequest(request, credentials, { region, service, now = new Date() }) {
  const url = new URL(request.url);
  const date = amzDate(now);
  const day = date.slice(0, 8);
  const payloadHash = await sha256Hex(request.body ?? "");
  const canonicalHeaders =
    `host:${url.host}\nx-amz-content-sha256:${payloadHash}\nx-amz-date:${date}\n` +
    `x-amz-security-token:${credentials.sessionToken}\n`;
  const canonicalRequest = [
    request.method,
    url.pathname,
    canonicalQuery(url.search),
    canonicalHeaders,
    SIGNED_HEADERS,
    payloadHash,
  ].join("\n");
  const scope = `${day}/${region}/${service}/${TERMINATOR}`;
  const stringToSign = [ALGORITHM, date, scope, await sha256Hex(canonicalRequest)].join("\n");
  const signature = hex(await hmac(await signingKey(credentials.secretKey, day, region, service), stringToSign));
  return {
    Authorization: `${ALGORITHM} Credential=${credentials.accessKeyId}/${scope}, SignedHeaders=${SIGNED_HEADERS}, Signature=${signature}`,
    "X-Amz-Date": date,
    "X-Amz-Security-Token": credentials.sessionToken,
    "X-Amz-Content-Sha256": payloadHash,
  };
}
