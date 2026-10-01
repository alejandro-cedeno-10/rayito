/**
 * Puerto y adaptador de `SizeCatalog` (M15, sizes-catalog). Espejo de
 * `rayito._size_catalog`. Confirma, con una única llamada gratuita a
 * `GetMicrovmImageVersion` por versión de imagen, que
 * `resources[0].minimumMemoryInMiB` coincide con el tamaño que
 * `Sandbox.create({ size })` resolvió en cliente. Nunca se llama si `size`
 * no se usó.
 */

/** Lo único que `ConventionCatalog` necesita del plano de control. */
export interface ImageVersionReader {
  getMicrovmImageVersion(imageArn: string, imageVersion: string): Promise<number>;
}

/** Puerto que `getInfo()` consulta para `SandboxInfo.baselineMemoryMib`. */
export interface SizeCatalog {
  minimumMemoryMib(
    reader: ImageVersionReader,
    imageArn: string,
    imageVersion: string,
  ): Promise<number>;
}

function cacheKey(imageArn: string, imageVersion: string): string {
  return `${imageArn}@${imageVersion}`;
}

/**
 * Cachea `minimumMemoryInMiB` por `(imageArn, imageVersion)` y por proceso:
 * una versión de imagen publicada es inmutable, así que el resultado no
 * puede quedar obsoleto dentro del mismo proceso. Las llamadas
 * concurrentes para la misma clave comparten la misma promesa en vuelo.
 */
export class ConventionCatalog implements SizeCatalog {
  readonly #cache = new Map<string, Promise<number>>();

  async minimumMemoryMib(
    reader: ImageVersionReader,
    imageArn: string,
    imageVersion: string,
  ): Promise<number> {
    const key = cacheKey(imageArn, imageVersion);
    const cached = this.#cache.get(key);
    if (cached !== undefined) {
      return cached;
    }
    const pending = reader
      .getMicrovmImageVersion(imageArn, imageVersion)
      .catch((error: unknown) => {
        this.#cache.delete(key);
        throw error;
      });
    this.#cache.set(key, pending);
    return pending;
  }
}

/** Instancia compartida por proceso: toda llamada de `Sandbox.create`/
 * `getInfo` que pide `size` reutiliza la misma caché. */
export const defaultSizeCatalog: ConventionCatalog = new ConventionCatalog();
