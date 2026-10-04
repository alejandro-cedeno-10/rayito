// Prueba las funciones puras de `custom_domain_router.js` con `node:test`
// (sin `cf`/KVS real: eso sólo se puede validar contra una distribución
// real, en la etapa de aceptación AWS — DOM-2/DOM-3). `route()` sí se
// prueba entera, con una `kvsGet` falsa en vez del runtime de CloudFront;
// sólo `handler` (que llama a `cf.updateRequestOrigin`) no se exporta, como
// el ejemplo oficial de `AWS::CloudFront::Function`, y no se prueba aquí.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  constantTimeEqual,
  portFromLabel,
  readCookie,
  readEventCookie,
  RESERVED_PORTS,
  route,
  routeLabel,
  sha256Hex,
  stripUpstreamProxyHeaders,
  trafficTokenAccepted,
} from "../custom_domain_router.js";

const TESTDATA = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../../testdata/custom-domain/hostnames.json", import.meta.url)),
    "utf8",
  ),
);

function requestFor(host, headers = {}) {
  return { headers: { host: { value: host }, ...headers } };
}

test("routeLabel toma la primera etiqueta del host", () => {
  assert.equal(routeLabel("8000-ws-7.sbx.example.com"), "8000-ws-7");
  assert.equal(routeLabel("sin-puntos"), "sin-puntos");
});

test("routeLabel pasa el host a minúsculas", () => {
  assert.equal(routeLabel("8000-WS-7.SBX.Example.com"), "8000-ws-7");
});

test("portFromLabel toma el prefijo numérico", () => {
  assert.equal(portFromLabel("8000-ws-7"), "8000");
  assert.equal(portFromLabel("8000-11111111-1111-1111-1111-111111111111"), "8000");
});

test("routeLabel/portFromLabel sobre los vectores compartidos con el SDK", () => {
  for (const c of TESTDATA.valid) {
    const label = routeLabel(c.host);
    assert.equal(label, `${c.port}-${c.alias}`, c.host);
    assert.equal(portFromLabel(label), String(c.port), c.host);
  }
});

test("stripUpstreamProxyHeaders borra sólo las cabeceras x-aws-proxy-*", () => {
  const headers = {
    "x-aws-proxy-auth": { value: "forjado" },
    "x-aws-proxy-port": { value: "9999" },
    "user-agent": { value: "curl" },
  };
  stripUpstreamProxyHeaders(headers);
  assert.deepEqual(Object.keys(headers), ["user-agent"]);
});

test("readCookie encuentra la cookie pedida entre varias", () => {
  const headers = { cookie: { value: "a=1; rayito_tt=el-token; b=2" } };
  assert.equal(readCookie(headers, "rayito_tt"), "el-token");
  assert.equal(readCookie(headers, "no-existe"), null);
});

test("readCookie sin cabecera cookie devuelve null", () => {
  assert.equal(readCookie({}, "rayito_tt"), null);
});

test("constantTimeEqual compara cadenas de igual longitud", () => {
  assert.equal(constantTimeEqual("abc", "abc"), true);
  assert.equal(constantTimeEqual("abc", "abd"), false);
  assert.equal(constantTimeEqual("abc", "abcd"), false);
});

test("sha256Hex coincide con el digest esperado de una cadena vacía", () => {
  assert.equal(
    sha256Hex(""),
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
});

test("trafficTokenAccepted: hash vacío es una ruta pública", () => {
  assert.equal(trafficTokenAccepted({}, ""), true);
});

test("trafficTokenAccepted: cabecera con el token correcto", () => {
  const token = "un-token-secreto";
  const headers = { "e2b-traffic-access-token": { value: token } };
  assert.equal(trafficTokenAccepted(headers, sha256Hex(token)), true);
});

test("trafficTokenAccepted: cookie con el token correcto", () => {
  const token = "otro-token";
  const headers = { cookie: { value: `rayito_tt=${token}` } };
  assert.equal(trafficTokenAccepted(headers, sha256Hex(token)), true);
});

test("readEventCookie lee el objeto `cookies` ya parseado de CloudFront", () => {
  const cookies = { a: { value: "1" }, rayito_tt: { value: "el-token" } };
  assert.equal(readEventCookie(cookies, "rayito_tt"), "el-token");
  assert.equal(readEventCookie(cookies, "no-existe"), null);
  assert.equal(readEventCookie(undefined, "rayito_tt"), null);
});

test("trafficTokenAccepted: cookie del objeto `cookies` del evento (Q121)", () => {
  const token = "token-en-cookies";
  const cookies = { rayito_tt: { value: token } };
  assert.equal(trafficTokenAccepted({}, sha256Hex(token), cookies), true);
  assert.equal(trafficTokenAccepted({}, sha256Hex("otro"), cookies), false);
});

test("trafficTokenAccepted: token ausente o incorrecto se rechaza", () => {
  assert.equal(trafficTokenAccepted({}, sha256Hex("lo-que-sea")), false);
  const headers = { "e2b-traffic-access-token": { value: "equivocado" } };
  assert.equal(trafficTokenAccepted(headers, sha256Hex("correcto")), false);
});

test("RESERVED_PORTS trae los dos puertos reservados de limits.json", () => {
  assert.deepEqual(RESERVED_PORTS, [8080, 9000]);
});

// -------------------------------------------------------------- route()

// Año 2286 en epoch-segundos: "no caducada" para cualquier fixture que no
// esté probando expiración a propósito.
const FAR_FUTURE_EXPIRY = 9999999999;

function fakeKvs(entries) {
  return async (key, format) => {
    if (!(key in entries)) {
      throw new Error(`clave no encontrada: ${key}`);
    }
    const value = entries[key];
    return format === "json" ? JSON.parse(value) : value;
  };
}

test("route: 404 cuando la ruta no está en el KVS", async () => {
  const request = requestFor("8000-no-existe.sbx.example.com");
  const decision = await route(request, fakeKvs({}));
  assert.deepEqual(decision, { kind: "not-found" });
});

test("route: 404 directo para un puerto reservado, sin consultar el KVS", async () => {
  let calls = 0;
  const kvsGet = async () => {
    calls += 1;
    throw new Error("no debería consultarse el KVS para un puerto reservado");
  };
  const decision = await route(requestFor("9000-ws-7.sbx.example.com"), kvsGet);
  assert.deepEqual(decision, { kind: "not-found" });
  assert.equal(calls, 0);
});

test("route: 403 cuando la ruta exige traffic_token y no llega (o es incorrecto)", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: sha256Hex("correcto"), x: FAR_FUTURE_EXPIRY }),
  };
  const decision = await route(requestFor("8000-ws-7.sbx.example.com"), fakeKvs(entries));
  assert.deepEqual(decision, { kind: "forbidden" });
});

test("route: entrega el origen y las cabeceras del proxy en el camino feliz", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: "", x: FAR_FUTURE_EXPIRY }),
  };
  const decision = await route(requestFor("8000-ws-7.sbx.example.com"), fakeKvs(entries));
  assert.deepEqual(decision, {
    kind: "origin",
    domainName: "endpoint.example",
    customHeaders: { "x-aws-proxy-auth": "a-jwe", "x-aws-proxy-port": "8000" },
  });
});

test("route: borra una cabecera x-aws-proxy-* forjada por el viewer antes de decidir", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: "", x: FAR_FUTURE_EXPIRY }),
  };
  const request = requestFor("8000-ws-7.sbx.example.com", {
    "x-aws-proxy-auth": { value: "jwe-forjado" },
  });
  const decision = await route(request, fakeKvs(entries));
  assert.equal(decision.customHeaders["x-aws-proxy-auth"], "a-jwe");
  assert.equal("x-aws-proxy-auth" in request.headers, false);
});

test("route: acepta un host en mayúsculas igual que en minúsculas", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: "", x: FAR_FUTURE_EXPIRY }),
  };
  const decision = await route(requestFor("8000-WS-7.SBX.Example.com"), fakeKvs(entries));
  assert.equal(decision.kind, "origin");
});

// Hallazgo del review de PR #74 (T25): `m.x` es el único TTL que
// `register()`/`refresh()` escriben; `route()` debe leerlo, o una ruta
// huérfana sólo deja de servir cuando caduca el JWE en sí, del lado del
// proxy de AWS, nunca por lo que el SDK documenta como su TTL.

test("route: 404 cuando m.x ya pasó, con un reloj inyectado", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: "", x: 1_000 }),
  };
  // now() en milisegundos; m.x (1 000 s) ya pasó a los 1 000 001 s.
  const decision = await route(requestFor("8000-ws-7.sbx.example.com"), fakeKvs(entries), () => 1_000_001_000);
  assert.deepEqual(decision, { kind: "not-found" });
});

test("route: 404, no 403, para una ruta caducada que también exige traffic_token", async () => {
  // Una ruta caducada no debe distinguirse desde fuera de una que nunca
  // existió: ni siquiera revela que haría falta un traffic_token.
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: sha256Hex("correcto"), x: 1_000 }),
  };
  const decision = await route(requestFor("8000-ws-7.sbx.example.com"), fakeKvs(entries), () => 1_000_001_000);
  assert.deepEqual(decision, { kind: "not-found" });
});

test("route: sirve la ruta justo antes de que m.x expire, con un reloj inyectado", async () => {
  const entries = {
    "j:8000-ws-7": "a-jwe",
    "m:8000-ws-7": JSON.stringify({ e: "endpoint.example", t: "", x: 1_000 }),
  };
  const decision = await route(requestFor("8000-ws-7.sbx.example.com"), fakeKvs(entries), () => 999_000);
  assert.equal(decision.kind, "origin");
});
