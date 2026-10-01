// Prueba las funciones puras de `custom_domain_router.js` con `node:test`
// (sin `cf`/KVS: eso sólo se puede validar contra una distribución real,
// en la etapa de aceptación AWS — DOM-2/DOM-3). `handler` no se exporta
// (igual que el ejemplo oficial de `AWS::CloudFront::Function`), así que
// no se prueba aquí.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  constantTimeEqual,
  portFromLabel,
  readCookie,
  routeLabel,
  sha256Hex,
  stripUpstreamProxyHeaders,
  trafficTokenAccepted,
} from "../custom_domain_router.js";

test("routeLabel toma la primera etiqueta del host", () => {
  assert.equal(routeLabel("8000-ws-7.sbx.example.com"), "8000-ws-7");
  assert.equal(routeLabel("sin-puntos"), "sin-puntos");
});

test("portFromLabel toma el prefijo numérico", () => {
  assert.equal(portFromLabel("8000-ws-7"), "8000");
  assert.equal(portFromLabel("8000-11111111-1111-1111-1111-111111111111"), "8000");
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

test("trafficTokenAccepted: token ausente o incorrecto se rechaza", () => {
  assert.equal(trafficTokenAccepted({}, sha256Hex("lo-que-sea")), false);
  const headers = { "e2b-traffic-access-token": { value: "equivocado" } };
  assert.equal(trafficTokenAccepted(headers, sha256Hex("correcto")), false);
});
