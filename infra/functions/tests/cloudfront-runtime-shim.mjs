// Gancho de carga de módulos de Node (`node --import` o
// `--experimental-loader`) que resuelve el especificador `"cloudfront"`
// (el módulo builtin del runtime de CloudFront Functions, que no existe
// fuera de CloudFront) a un doble mínimo, sólo para que
// `custom_domain_router.test.mjs` pueda importar el fichero de producción
// tal cual sin tocarlo. Las funciones puras que de verdad se prueban no
// llaman a `cf.kvs()`/`cf.updateRequestOrigin`; este doble nunca se invoca.

const VIRTUAL_SPECIFIER = "cloudfront";
const VIRTUAL_URL = "rayito-cloudfront-runtime-shim:cloudfront";

const VIRTUAL_SOURCE = `
export default {
  kvs() {
    return {
      async get() {
        throw new Error("doble de pruebas: cloudfront.kvs().get() no está implementado");
      },
    };
  },
  updateRequestOrigin() {
    throw new Error("doble de pruebas: cloudfront.updateRequestOrigin() no está implementado");
  },
};
`;

export function resolve(specifier, context, nextResolve) {
  if (specifier === VIRTUAL_SPECIFIER) {
    return { url: VIRTUAL_URL, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}

export function load(url, context, nextLoad) {
  if (url === VIRTUAL_URL) {
    return { format: "module", source: VIRTUAL_SOURCE, shortCircuit: true };
  }
  return nextLoad(url, context);
}
