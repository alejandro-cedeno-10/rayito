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
// Pasos (arquitectura M15 §7.8, función pura `route()` más abajo): (1)
// borra cualquier cabecera `x-aws-proxy-*` que traiga el viewer, para que
// nadie pueda suplantar el origen o el JWE desde fuera; (2) rechaza sin
// tocar el KVS un puerto reservado (RESERVED_PORTS, defensa en
// profundidad — el SDK ya nunca registra una ruta ahí); (3) trata una ruta
// cuyo `m.x` (expires_at, epoch en segundos) ya pasó como si no existiera
// — un 404, igual que una ruta nunca registrada, no un 403 — porque
// `register(ttl_seconds=)`/`refresh()` sólo escriben ese campo, nadie lo
// borra solo (hallazgo del review de PR #74: antes `route()` nunca lo
// leía, así que una ruta huérfana sólo dejaba de servir cuando caducaba el
// JWE en sí, del lado del proxy de AWS, no por el TTL que el SDK dice
// ofrecer); (4) si la ruta exige `traffic_token` (`m.t` no vacío), lo
// comprueba en tiempo constante contra la cabecera
// `e2b-traffic-access-token` o la cookie `rayito_tt`; (5) llama a
// `cf.updateRequestOrigin` con el endpoint de la ruta y las cabeceras que
// el proxy de AWS Lambda MicroVMs espera.
//
// `import cf from "cloudfront"` es la forma documentada por AWS de acceder
// a los builtins del runtime `cloudfront-js-2.0` (no una importación de
// paquete), así que se mantiene en el código desplegado. `export`, en
// cambio, no está documentado como soportado por ese runtime para las
// propias declaraciones de la función — nunca se ha comprobado contra una
// distribución real — así que `scripts/tests/test_custom_domain_function_sync.py`
// exige que el `FunctionCode` embebido en `infra/custom-domain.yaml` sea
// este mismo fichero con cada `export ` de nivel superior quitado; las
// declaraciones `export` de aquí existen sólo para que
// `infra/functions/tests/*.test.mjs` las importe con Node directamente.
//
// `cloudfront-js-2.0` no es ES moderno completo: rechaza al compilar
// `for...of` y los parámetros por defecto (medido con `TestFunction`, Q121 de
// AWS_API_NOTES.md), y Node los acepta, así que los tests de Node no lo
// detectan. Aquí sólo bucles con índice y valores por defecto explícitos.

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

// `limits.json` `reservedPorts` (ADR-006: 8080 es `rayd`, 9000 los hooks);
// espejo literal de `RESERVED_PORTS` en `_limits.py`/`limits.ts`, verificado
// igual contra `limits.json` por
// `scripts/tests/test_custom_domain_function_sync.py::test_reserved_ports_match_limits_json`
// (este runtime no puede importar un módulo generado: CloudFront Functions
// sólo resuelve los builtins `cloudfront`/`crypto`, nunca un fichero propio).
export const RESERVED_PORTS = [8080, 9000];

/** "8000-ws-7.sbx.example.com" -> "8000-ws-7": la etiqueta es la primera
 * componente del host, siempre (`_domain.route_host`), en minúsculas (una
 * etiqueta DNS no distingue mayúsculas; el KVS sí). */
export function routeLabel(host) {
  const lower = host.toLowerCase();
  const dot = lower.indexOf(".");
  return dot === -1 ? lower : lower.substring(0, dot);
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

/** La cookie `name` del objeto `event.request.cookies` de CloudFront
 * Functions (`{nombre: {value}}`), o `null`. CloudFront entrega ahí las
 * cookies ya parseadas; con un evento de `TestFunction` que sólo trae
 * `cookies`, leer la cabecera `cookie` daba 403 (Q121). */
export function readEventCookie(cookies, name) {
  const entry = cookies && cookies[name];
  return entry && entry.value ? entry.value : null;
}

/** La cookie `name` del valor crudo de la cabecera `cookie`, o `null`
 * (respaldo de `readEventCookie` por si la cabecera llega sin parsear). */
export function readCookie(headers, name) {
  const cookieHeader = headers.cookie;
  if (!cookieHeader) {
    return null;
  }
  const entries = Array.isArray(cookieHeader) ? cookieHeader : [cookieHeader];
  for (let i = 0; i < entries.length; i += 1) {
    const parts = (entries[i].value || "").split(";");
    for (let j = 0; j < parts.length; j += 1) {
      const part = parts[j];
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
 * pública (sin token que comprobar). `cookies` es `event.request.cookies`. */
export function trafficTokenAccepted(headers, metadataTokenHash, cookies) {
  if (!metadataTokenHash) {
    return true;
  }
  const headerValue = headers[TRAFFIC_TOKEN_HEADER] && headers[TRAFFIC_TOKEN_HEADER].value;
  const provided =
    headerValue ||
    readEventCookie(cookies, TRAFFIC_TOKEN_COOKIE) ||
    readCookie(headers, TRAFFIC_TOKEN_COOKIE);
  if (!provided) {
    return false;
  }
  return constantTimeEqual(sha256Hex(provided), metadataTokenHash);
}

/**
 * La decisión de enrutado, pura: nunca toca `cf.updateRequestOrigin` ni la
 * respuesta HTTP final, así que se prueba con una `kvsGet` falsa
 * (`infra/functions/tests/custom_domain_router.test.mjs`) sin runtime de
 * CloudFront. `kvsGet(key, format)` es `(key, format) =>
 * kvsHandle.get(key, {format})` en producción (ver `handler` más abajo).
 *
 * @param now Reloj inyectable para los tests (epoch en milisegundos, como
 *   `Date.now()`, su valor por defecto); `metadata.x` llega en segundos
 *   (`_domain.RouteMetadata.encode`).
 * @returns `{kind: "not-found"}` (404: puerto reservado, sin ruta en el
 *   KVS, o `m.x` ya pasado), `{kind: "forbidden"}` (403: `traffic_token`
 *   ausente o incorrecto), o `{kind: "origin", domainName, customHeaders}`
 *   (pasa a `cf.updateRequestOrigin` tal cual).
 */
export async function route(request, kvsGet, now) {
  // Sin parámetro por defecto (`now = Date.now`): `cloudfront-js-2.0` lo
  // rechaza al compilar (`SyntaxError: Unexpected token "="`, Q121).
  const clock = now || Date.now;
  stripUpstreamProxyHeaders(request.headers);
  const label = routeLabel(request.headers.host.value);
  const port = portFromLabel(label);
  // Defensa en profundidad: el SDK nunca registra una ruta en un puerto
  // reservado (`_domain.validate_route_port`), pero si alguna vez hubiera
  // una entrada residual, no responder huecos a un host fabricado a mano
  // ahorra además la consulta al KVS.
  if (RESERVED_PORTS.includes(Number(port))) {
    return { kind: "not-found" };
  }
  let jwe;
  let metadata;
  try {
    jwe = await kvsGet(JWE_KEY_PREFIX + label, "string");
    metadata = await kvsGet(META_KEY_PREFIX + label, "json");
  } catch (err) {
    return { kind: "not-found" };
  }
  // `m.x` es el único campo que `register()`/`refresh()` escriben para
  // acotar una ruta huérfana (T25); tratarla como "no encontrada", no
  // "prohibida", porque desde fuera una ruta caducada no debe distinguirse
  // de una que nunca existió.
  if (typeof metadata.x === "number" && clock() >= metadata.x * 1000) {
    return { kind: "not-found" };
  }
  if (!trafficTokenAccepted(request.headers, metadata.t, request.cookies)) {
    return { kind: "forbidden" };
  }
  return {
    kind: "origin",
    domainName: metadata.e,
    customHeaders: {
      [PROXY_AUTH_HEADER]: jwe,
      [PROXY_PORT_HEADER]: port,
    },
  };
}

async function handler(event) {
  const decision = await route(event.request, (key, format) => kvsHandle.get(key, { format }));
  if (decision.kind === "not-found") {
    return { statusCode: 404, statusDescription: "Not Found", headers: {} };
  }
  if (decision.kind === "forbidden") {
    return { statusCode: 403, statusDescription: "Forbidden", headers: {} };
  }
  cf.updateRequestOrigin({ domainName: decision.domainName, customHeaders: decision.customHeaders });
  return event.request;
}
