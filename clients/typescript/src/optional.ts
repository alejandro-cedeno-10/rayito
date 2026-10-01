/**
 * Convención compartida de opt-in para funciones que consumen cuota o dinero
 * de AWS (ADR-014, `docs/site/docs/optional-features.md`).
 *
 * Una función opcional se activa con una propiedad nombrada (más, como
 * mucho, una propiedad con su objeto de configuración); `undefined` es
 * "apagada". Ninguna variable de entorno, fichero de configuración ni
 * setter global activa una función de coste. Un objeto de configuración
 * crea su cliente del SDK de AWS de forma perezosa, en su primer uso, nunca
 * en su constructor.
 *
 * Este módulo da el único helper de esa convención que necesita TypeScript:
 * `loadOptionalPeer`, para peerDependencies opcionales (los clientes de AWS
 * SDK v3 que 0.4.0 no usaba, como `@aws-sdk/client-secrets-manager` o
 * `@aws-sdk/client-dynamodb`, y `@opentelemetry/api`). No importa nada
 * opcional de forma estática: importar `rayito` nunca resuelve un paquete
 * que sólo hace falta cuando el llamante activa la función correspondiente.
 */

import { InvalidArgumentError } from "./errors.js";

/**
 * Importa dinámicamente `specifier` bajo demanda para una función opcional
 * ya activada.
 *
 * Se llama sólo desde dentro de la función que el llamante activó con su
 * propia opción (nunca en el nivel superior de un módulo): así, no tener el
 * peer instalado no afecta a nadie que no use esa función. Sólo se traduce a
 * `InvalidArgumentError` (nombrando el paquete npm a instalar) el fallo de
 * resolución del propio `specifier`; cualquier otro error de la importación
 * (por ejemplo una dependencia transitiva rota de un peer ya instalado, o un
 * fallo interno del paquete) se propaga tal cual, porque no es un problema
 * de "falta el peer".
 *
 * @param specifier - especificador del módulo a importar (`@opentelemetry/api`).
 * @param feature - nombre de la función que lo necesita, para el mensaje de error.
 * @returns el módulo importado.
 */
export async function loadOptionalPeer<T>(specifier: string, feature: string): Promise<T> {
  try {
    return (await import(specifier)) as T;
  } catch (cause) {
    if (!isMissingRequestedSpecifier(cause, specifier)) {
      throw cause;
    }
    throw new InvalidArgumentError(
      `${feature} necesita el paquete opcional '${specifier}': instala npm install ${specifier}`,
      { cause },
    );
  }
}

/**
 * `true` sólo cuando la resolución falló para `specifier` mismo (o para su
 * paquete, en un especificador con subruta), nunca para una dependencia
 * anidada de un paquete que sí está instalado. Se compara sólo el nombre
 * entrecomillado que Node no pudo resolver ("Cannot find package|module
 * '<x>'"), no el mensaje entero: el mensaje también incluye la ruta del
 * importador, que para una dependencia transitiva rota contiene
 * `node_modules/<specifier>/...` y casaría por error.
 */
function isMissingRequestedSpecifier(error: unknown, specifier: string): boolean {
  const code = (error as { code?: unknown } | null)?.code;
  if (code !== "ERR_MODULE_NOT_FOUND" && code !== "MODULE_NOT_FOUND") {
    return false;
  }
  const message = (error as { message?: unknown } | null)?.message;
  if (typeof message !== "string") {
    return false;
  }
  const missing = /Cannot find (?:package|module) '([^']+)'/.exec(message)?.[1];
  return missing !== undefined && (missing === specifier || missing === packageName(specifier));
}

/** Nombre del paquete npm de un especificador (`@scope/name` o `name`, sin subruta). */
function packageName(specifier: string): string {
  const parts = specifier.split("/");
  return specifier.startsWith("@") ? parts.slice(0, 2).join("/") : (parts[0] ?? specifier);
}
