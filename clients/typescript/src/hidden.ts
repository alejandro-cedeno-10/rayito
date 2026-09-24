/**
 * Secretos como propiedades no enumerables: `util.inspect`, `console.log`,
 * `JSON.stringify`, el spread y `Object.keys` no los ven; el acceso directo
 * (`plan.accessToken`) sí. Es la contraparte de `field(repr=False)` del SDK
 * Python.
 */

export function defineHidden<T extends object, K extends string, V>(
  target: T,
  key: K,
  value: V,
): T & { readonly [P in K]: V } {
  Object.defineProperty(target, key, {
    value,
    enumerable: false,
    writable: false,
    configurable: false,
  });
  return target as T & { readonly [P in K]: V };
}
