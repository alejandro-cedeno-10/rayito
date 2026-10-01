// Rayito m15-custom-domain: enrutado dinámico de CloudFront por sandbox.
//
// Asociada como evento `viewer-request` (único evento donde CloudFront
// permite `cf.updateRequestOrigin`, ver AWS_API_NOTES.md §29). Runtime
// `cloudfront-js-2.0` (exigido por el helper de KVS y por
// `cf.updateRequestOrigin`). El KeyValueStore asociado guarda dos claves
// por ruta (`_custom_domain/_domain.py` del SDK):
//   j:<puerto>-<alias>  -> el JWE vigente de `create-microvm-auth-token`
//   m:<puerto>-<alias>  -> JSON compacto {"e": endpoint, "t": sha256(token) o "", "x": exp}
//
// Pasos (arquitectura M15 §7.8): (1) borra cualquier cabecera
// `x-aws-proxy-*` que traiga el viewer, para que nadie pueda suplantar el
// origen o el JWE desde fuera; (2) si la ruta exige `traffic_token`
// (`m.t` no vacío), lo comprueba en tiempo constante contra la cabecera
// `e2b-traffic-access-token` o la cookie `rayito_tt`; (3) llama a
// `cf.updateRequestOrigin` con el endpoint de la ruta y las cabeceras que
// el proxy de AWS Lambda MicroVMs espera.
//
// `export` sólo en las funciones puras de abajo (para que
// `infra/functions/tests/*.test.mjs` las importe con Node directamente);
// `handler` se queda sin `export`, tal cual el ejemplo oficial de AWS para
// `AWS::CloudFront::Function` (sin verificar aún contra una distribución
// real: D3/DOM-2 lo confirma en la etapa de aceptación AWS).

import cf from "cloudfront";
import crypto from "crypto";

const kvsHandle = cf.kvs();

export const JWE_KEY_PREFIX = "j:";
export const META_KEY_PREFIX = "m:";
export const TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token";
export const TRAFFIC_TOKEN_COOKIE = "rayito_tt";
export const PROXY_AUTH_HEADER = "x-aws-proxy-auth";
export const PROXY_PORT_HEADER = "x-aws-proxy-port";
export const AWS_PROXY_HEADER_PREFIX = "x-aws-proxy-";

/** "8000-ws-7.sbx.example.com" -> "8000-ws-7": la etiqueta es la primera
 * componente del host, siempre (`_domain.route_host`). */
export function routeLabel(host) {
  const dot = host.indexOf(".");
  return dot === -1 ? host : host.substring(0, dot);
}

/** El puerto es el prefijo numérico de la etiqueta, antes del primer guion. */
export function portFromLabel(label) {
  const dash = label.indexOf("-");
  return dash === -1 ? label : label.substring(0, dash);
}

/** Nunca reenvía una cabecera `x-aws-proxy-*` que haya puesto el viewer:
 * sólo las que pone esta Function, más abajo, cuentan. */
export function stripUpstreamProxyHeaders(headers) {
  for (const name in headers) {
    if (Object.prototype.hasOwnProperty.call(headers, name) && name.indexOf(AWS_PROXY_HEADER_PREFIX) === 0) {
      delete headers[name];
    }
  }
}

/** La cookie `name` del valor crudo de la cabecera `cookie`, o `null`. */
export function readCookie(headers, name) {
  const cookieHeader = headers.cookie;
  if (!cookieHeader) {
    return null;
  }
  const entries = Array.isArray(cookieHeader) ? cookieHeader : [cookieHeader];
  for (const entry of entries) {
    const parts = (entry.value || "").split(";");
    for (const part of parts) {
      const eq = part.indexOf("=");
      if (eq === -1) {
        continue;
      }
      if (part.substring(0, eq).trim() === name) {
        return part.substring(eq + 1).trim();
      }
    }
  }
  return null;
}

/** sha256 hexadecimal de `value` (mismo formato que `_domain.traffic_token_digest`). */
export function sha256Hex(value) {
  const hash = crypto.createHash("sha256");
  hash.update(value);
  return hash.digest("hex");
}

/** Comparación en tiempo constante de dos cadenas hex de igual longitud
 * esperada (sha256 siempre produce 64 caracteres); distinta longitud ya es
 * "no coincide" sin más coste. */
export function constantTimeEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/** `true` si el `traffic_token` que trae la petición coincide con el hash
 * guardado en la ruta (`metadata.t`); `metadata.t === ""` es una ruta
 * pública (sin token que comprobar). */
export function trafficTokenAccepted(headers, metadataTokenHash) {
  if (!metadataTokenHash) {
    return true;
  }
  const headerValue = headers[TRAFFIC_TOKEN_HEADER] && headers[TRAFFIC_TOKEN_HEADER].value;
  const provided = headerValue || readCookie(headers, TRAFFIC_TOKEN_COOKIE);
  if (!provided) {
    return false;
  }
  return constantTimeEqual(sha256Hex(provided), metadataTokenHash);
}

async function handler(event) {
  const request = event.request;
  stripUpstreamProxyHeaders(request.headers);
  const label = routeLabel(request.headers.host.value);
  let jwe;
  let metadata;
  try {
    jwe = await kvsHandle.get(JWE_KEY_PREFIX + label, { format: "string" });
    metadata = await kvsHandle.get(META_KEY_PREFIX + label, { format: "json" });
  } catch (err) {
    return { statusCode: 404, statusDescription: "Not Found", headers: {} };
  }
  if (!trafficTokenAccepted(request.headers, metadata.t)) {
    return { statusCode: 403, statusDescription: "Forbidden", headers: {} };
  }
  cf.updateRequestOrigin({
    domainName: metadata.e,
    customHeaders: {
      [PROXY_AUTH_HEADER]: jwe,
      [PROXY_PORT_HEADER]: portFromLabel(label),
    },
  });
  return request;
}
