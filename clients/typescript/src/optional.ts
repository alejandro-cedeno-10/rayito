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
 * peer instalado no afecta a nadie que no use esa función. Un fallo de
 * resolución se traduce a `InvalidArgumentError` nombrando el paquete npm a
 * instalar; cualquier otro error de la importación (por ejemplo un fallo
 * interno del paquete) se propaga tal cual, porque no es un problema de
 * "falta el peer".
 *
 * @param specifier - especificador del módulo a importar (`@opentelemetry/api`).
 * @param feature - nombre de la función que lo necesita, para el mensaje de error.
 * @returns el módulo importado.
 */
export async function loadOptionalPeer<T>(specifier: string, feature: string): Promise<T> {
  try {
    return (await import(specifier)) as T;
  } catch (cause) {
    if (!isModuleResolutionFailure(cause)) {
      throw cause;
    }
    throw new InvalidArgumentError(
      `${feature} necesita el paquete opcional '${specifier}': instala npm install ${specifier}`,
      { cause },
    );
  }
}

function isModuleResolutionFailure(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code;
  return code === "ERR_MODULE_NOT_FOUND" || code === "MODULE_NOT_FOUND";
}
